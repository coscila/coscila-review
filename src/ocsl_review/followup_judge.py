# SPDX-License-Identifier: Apache-2.0
"""Runner of measurement instruments on a follow-up branch: routing, batches, receipts, validation and repair.

``run_items`` runs one registered instrument over a list of items: the follow-up instruments (citation-function@1,
numeric-check@1, claim-match@1), whose validator modules expose ``build_messages(items, tier)`` and
``validate_response(items, text)``, and citation-support@2 unchanged through ``corpus_assessment``. It

- refuses before any call when an item with entitled evidence would reach a judge the operator's policy does not list
  as on premises (``provenance`` travels with every item and is never sent);
- skips items that already hold an accepted judgment of the same instrument digest, tier and variant (resumable);
- batches items with the instrument's geometry and request settings, stores every request and response by sha256,
  writes one receipt per call (served model, fingerprint, tokens, finish reason) and one judgment per item;
- sends items without a valid output once more in a repair call when the instrument declares a repair pass;
- stops at the first response from a model other than the pinned one (``model-drift``), leaving open items open.

Judges follow ``claim_check.Judge`` (``complete(messages, settings=...) -> JudgeResponse``); ``FollowupHttpJudge`` adds
the response fingerprint and the exact request bytes to it. A judge reached through the run principal's broker socket
lives in ``followup_judge_broker``.
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .claim_check import ConfidentialPolicy, endpoint_text, normalize_endpoint
from .errors import ConfidentialityError, ReviewError, ValidationError
from .followup_branch import now, store_artifact
from .followup_model import canonical, digest

REPAIR_NOTE_KEY = "repair"
PROVENANCE_KEY = "provenance"


@dataclass(frozen=True)
class JudgeReply:
    """A judge's answer with what the receipt needs (``claim_check.JudgeResponse`` plus the fingerprint)."""
    text: str
    model: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    fingerprint: str | None = None


class FollowupHttpJudge:
    """An OpenAI-compatible chat-completions endpoint; records the exact request bytes and the response fingerprint.

    ``location`` is ``on-prem`` only when the caller checked the endpoint against the operator's policy.
    """

    def __init__(self, base_url: str, model: str, *, location: str = "external", api_key_env: str | None = None,
                 timeout: float = 600.0, opener: Callable[..., Any] | None = None):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValidationError("judge base URL must be http(s) with a host")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.endpoint = endpoint_text(normalize_endpoint(self.url, strict=False))
        self.model, self.location, self.host = model, location, parsed.hostname
        self.name = f"http-judge:{parsed.hostname}"
        self.api_key_env, self.timeout = api_key_env, timeout
        self.opener = opener or urllib.request.urlopen

    def body(self, messages: list[dict[str, str]], settings: Mapping[str, Any]) -> bytes:
        return json.dumps({"model": self.model, "messages": messages, **settings}, ensure_ascii=False).encode("utf-8")

    def complete(self, messages: list[dict[str, str]], *, settings: Mapping[str, Any]) -> JudgeReply:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(self.url, data=self.body(messages, settings), headers=headers, method="POST")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(32 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ReviewError(f"judge HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as error:
            raise ReviewError(f"judge request failed: {type(error).__name__}") from error
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ReviewError("judge response is not a chat completion") from error
        if not isinstance(text, str) or not text:
            raise ReviewError("judge response carries no content")
        return JudgeReply(text, str(payload.get("model") or ""), payload.get("usage") or {}, choice.get("finish_reason"),
                          payload.get("system_fingerprint"))


class SupportModule:
    """citation-support@2 unchanged: the corpus_assessment planner and validator."""

    def build_messages(self, items: Sequence[Mapping[str, Any]], tier: str) -> list[dict[str, str]]:
        from .corpus_assessment import build_messages
        return build_messages([dict(item) for item in items], tier)

    def validate_response(self, items: Sequence[Mapping[str, Any]], text: Any) -> dict[str, Any]:
        from .corpus_assessment import AssessmentValidator
        return AssessmentValidator().validate_response([dict(item) for item in items], text)


def module_for(instrument: Any) -> Any:
    name = instrument.manifest["validator"]["module"]
    if name == "ocsl_review.corpus_assessment":
        return SupportModule()
    module = importlib.import_module(name)
    if not callable(getattr(module, "build_messages", None)) or not callable(getattr(module, "validate_response", None)):
        raise ValidationError(f"{instrument.id}: its validator module offers no build_messages and validate_response")
    return module


@dataclass(frozen=True)
class Plan:
    """Geometry and request settings of one instrument tier, as the bundle declares them."""
    instrument_id: str
    tier: str
    items_per_call: int
    floor: int
    per_item: int
    temperature: float
    reasoning_effort: str | None
    repair_passes: int

    def settings(self, items: int) -> dict[str, Any]:
        value: dict[str, Any] = {"temperature": self.temperature, "max_tokens": max(self.floor, self.per_item * items),
                                 "stream": False}
        if self.reasoning_effort:
            value["reasoning_effort"] = self.reasoning_effort
        return value


def plan_for(instrument: Any, tier: str) -> Plan:
    spec = instrument.tier(tier)
    geometry = spec["geometry"]["itemsPerCall"]
    if isinstance(geometry, int):
        per_call = geometry
    else:
        # A bundle that needs calibration above a batch size runs at that size (no calibration set exists).
        per_call = int(geometry.get("calibrationRequiredAbove") or geometry.get("min") or 1)
    request = spec.get("request") or {}
    rule = request.get("maxOutputTokens") if isinstance(request.get("maxOutputTokens"), dict) else None
    floor, per_item = (int(rule["floor"]), int(rule["perItem"])) if rule else (3000, 700)
    return Plan(instrument.id, tier, per_call, floor, per_item, float(request.get("temperature", 0) or 0),
                request.get("reasoningEffort"), int((spec.get("repair") or {}).get("passes", 1 if not rule else 0)))


def entitled_allowed(policy: ConfidentialPolicy, judge: Any) -> bool:
    """Entitled evidence reaches only a judge the policy lists as on premises (or an in-process judge)."""
    return policy.with_mode("confidential-manuscript").judge_allowed(judge)


def accepted(connection: sqlite3.Connection, instrument_digest: str, tier: str,
             variant: str = "primary") -> dict[str, dict[str, Any]]:
    """Accepted verdicts by item key for one instrument digest, tier and variant."""
    return {row[0]: json.loads(row[1]) for row in connection.execute(
        "SELECT item_key, verdict_json FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant=? "
        "AND outcome='accepted'", (instrument_digest, tier, variant))}


def _sendable(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != PROVENANCE_KEY}


def _request_bytes(judge: Any, messages: list[dict[str, str]], settings: Mapping[str, Any]) -> bytes:
    body = getattr(judge, "body", None)
    if callable(body):
        return body(messages, settings)
    return canonical({"model": getattr(judge, "model", ""), "messages": messages, **settings}).encode("utf-8")


def run_items(connection: sqlite3.Connection, instrument: Any, items: Sequence[Mapping[str, Any]], judge: Any, *,
              tier: str, model: str, policy: ConfidentialPolicy | None = None, module: Any = None,
              variant: str = "primary", concurrency: int = 1, code_revision: str | None = None,
              study_bundle: str | None = None) -> dict[str, Any]:
    """Judge ``items`` (dicts with ``id`` and ``provenance``) with one instrument tier; see the module docstring."""
    policy = policy or ConfidentialPolicy("public-literature")
    module = module or module_for(instrument)
    plan = plan_for(instrument, tier)
    if not model:
        raise ValidationError("give the pinned served model (the protocol's model.served)")
    keys = [item.get("id") for item in items]
    if len(set(keys)) != len(keys) or not all(isinstance(key, str) and key for key in keys):
        raise ValidationError("item ids must be unique non-empty strings")
    classes = {item.get(PROVENANCE_KEY) for item in items}
    if not classes <= {"public", "entitled"}:
        raise ValidationError("every item needs its provenance class (public or entitled)")
    if "entitled" in classes and not entitled_allowed(policy, judge):
        raise ConfidentialityError("entitled evidence reaches only judges the operator's policy lists as on premises")
    instrument_digest = instrument.digest
    done = set(accepted(connection, instrument_digest, tier, variant))
    pending = [dict(item) for item in items if item["id"] not in done]
    summary: dict[str, Any] = {"instrument": instrument.id, "digest": instrument_digest, "tier": tier,
                               "variant": variant, "items": len(items), "skipped": len(items) - len(pending),
                               "calls": 0, "repairCalls": 0, "accepted": 0, "rejected": 0, "failedCalls": 0,
                               "stopped": None}
    queue: deque[tuple[list[dict[str, Any]], str]] = deque(
        (pending[start:start + plan.items_per_call], "first") for start in range(0, len(pending), plan.items_per_call))
    in_flight: dict[Future, tuple[list[dict[str, Any]], str, list[dict[str, str]], dict[str, Any], str]] = {}

    def call(messages: list[dict[str, str]], settings: dict[str, Any]) -> Any:
        return judge.complete(messages, settings=settings)

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        while queue or in_flight:
            while queue and len(in_flight) < max(1, int(concurrency)) and summary["stopped"] is None:
                batch, kind = queue.popleft()
                messages = module.build_messages([_sendable(item) for item in batch], tier)
                settings = plan.settings(len(batch))
                in_flight[pool.submit(call, messages, settings)] = (batch, kind, messages, settings, now())
            if not in_flight:
                break
            finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in finished:
                batch, kind, messages, settings, started = in_flight.pop(future)
                retry = _record(connection, instrument, plan, judge, policy, model, batch, kind, messages, settings,
                                started, future, summary, module=module, variant=variant,
                                code_revision=code_revision, study_bundle=study_bundle)
                if retry and kind == "first" and plan.repair_passes > 0 and summary["stopped"] is None:
                    queue.append((retry, "repair"))
            connection.commit()
    return summary


def _record(connection, instrument, plan, judge, policy, model, batch, kind, messages, settings, started, future,
            summary, *, module, variant, code_revision, study_bundle) -> list[dict[str, Any]]:
    """Write the receipt and judgments of one finished call; returns the items to repair."""
    request = _request_bytes(judge, messages, settings)
    request_sha = store_artifact(connection, request, "judge-request", "application/json")
    item_keys = [item["id"] for item in batch]
    call_id = "call-" + digest(instrument.digest, plan.tier, variant, kind, item_keys, request_sha, started)[:32]
    reply, error = None, None
    try:
        reply = future.result()
    except Exception as failure:  # noqa: BLE001 - every failure is a receipt, never a verdict
        error = f"{type(failure).__name__}: {str(failure)[:200]}"
    response_sha = (store_artifact(connection, reply.text.encode("utf-8"), "judge-response", "text/plain")
                    if reply is not None else None)
    served = getattr(reply, "model", None) if reply is not None else None
    outcome = "completed"
    if reply is None:
        outcome = "failed"
    elif served != model or not policy.served_model_allowed(judge, served):
        outcome, error = "model-drift", f"served model {served!r} differs from the pinned model"
        summary["stopped"] = "model-drift"
    usage = getattr(reply, "usage", None) or {}
    classes = sorted({item.get(PROVENANCE_KEY) for item in batch})
    connection.execute(
        "INSERT INTO fu_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (call_id, instrument.id, instrument.digest, plan.tier, variant, kind, len(batch), canonical(item_keys),
         getattr(judge, "name", "judge"), getattr(judge, "location", ""), getattr(judge, "endpoint", None),
         getattr(judge, "model", None), served, getattr(reply, "fingerprint", None), request_sha, response_sha,
         digest(settings), usage.get("prompt_tokens") if isinstance(usage, Mapping) else None,
         usage.get("completion_tokens") if isinstance(usage, Mapping) else None,
         getattr(reply, "finish_reason", None), outcome, error, canonical(classes), code_revision, study_bundle,
         started, now()))
    summary["calls" if kind == "first" else "repairCalls"] += 1
    if outcome != "completed":
        summary["failedCalls"] += 1
        return []
    result = module.validate_response([_sendable(item) for item in batch], reply.text)
    created = now()
    retry = []
    for item in batch:
        key = item["id"]
        if key in result["accepted"]:
            connection.execute("INSERT INTO fu_judgments(instrument_id, instrument_digest, tier, variant, item_key, "
                               "call_id, outcome, verdict_json, error_code, error, created_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (instrument.id, instrument.digest, plan.tier, variant, key, call_id, "accepted",
                                canonical(result["accepted"][key]), None, None, created))
            summary["accepted"] += 1
            continue
        failure = result["rejected"].get(key) or {"code": "missing_item", "message": "no output", "retryable": True}
        connection.execute("INSERT INTO fu_judgments(instrument_id, instrument_digest, tier, variant, item_key, call_id, "
                           "outcome, verdict_json, error_code, error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (instrument.id, instrument.digest, plan.tier, variant, key, call_id, "rejected", None,
                            failure.get("code"), str(failure.get("message"))[:300], created))
        summary["rejected"] += 1
        if failure.get("retryable"):
            retry.append(item)
    return retry


def block_items(connection: sqlite3.Connection, instrument: Any, tier: str, items: Sequence[tuple[str, str, str]], *,
                variant: str = "primary") -> int:
    """Record items that are never sent (key, code, message), for example a redacted citing sentence."""
    created = now()
    connection.executemany("INSERT INTO fu_judgments(instrument_id, instrument_digest, tier, variant, item_key, call_id, "
                           "outcome, verdict_json, error_code, error, created_at) VALUES(?,?,?,?,?,NULL,'blocked',NULL,"
                           "?,?,?)", [(instrument.id, instrument.digest, tier, variant, key, code, message, created)
                                      for key, code, message in items])
    return len(items)


def judge_from_args(args: Any) -> tuple[Any, ConfidentialPolicy]:
    """The judge and policy of a judging command: --base-url (direct) or --broker-socket (the run principal's broker).

    A direct judge is on premises only when --on-premises is given and the --policy file lists its endpoint.
    """
    policy = ConfidentialPolicy.load(args.policy) if getattr(args, "policy", None) else ConfidentialPolicy(
        "public-literature")
    base_url, socket_path = getattr(args, "base_url", None), getattr(args, "broker_socket", None)
    if base_url and socket_path:
        raise ValidationError("give either --base-url (a judge called directly) or --broker-socket, not both")
    if not getattr(args, "model", None):
        raise ValidationError("give --model, the served model the protocol pins")
    if base_url:
        judge = FollowupHttpJudge(base_url, args.model, api_key_env=getattr(args, "api_key_env", None))
        if getattr(args, "on_premises", False):
            if not getattr(args, "policy", None):
                raise ConfidentialityError("--on-premises needs --policy with the judge endpoint in onPremEndpoints")
            if not policy.endpoint_on_prem(judge.url):
                raise ConfidentialityError(f"{judge.endpoint} is not an on-prem endpoint of the policy")
            judge.location = "on-prem"
        return judge, policy
    socket_path = socket_path or os.environ.get("OCSL_BROKER_SOCKET")
    if not socket_path:
        raise ValidationError("give --base-url for a judge called directly, or --broker-socket (or OCSL_BROKER_SOCKET)")
    try:
        broker = importlib.import_module("ocsl_review.followup_judge_broker")
    except ModuleNotFoundError as error:
        raise ReviewError("this build has no broker judge for follow-up instruments (followup_judge_broker)") from error
    return broker.judge_from_args(args, policy, socket_path), policy
