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
- stops at the first response from a model other than the pinned one (``model-drift``), and sends nothing more once a
  broker judge's transport has halted (a fidelity stop, a budget, provider-limit, revocation or cancellation halt),
  leaving open items open for the next invocation.

``step_result`` gives every judging command the result contract of the census steps at the top level: ``state``
(completed, or paused when the run stopped or items are still open), a ``reason`` with the census prefixes
(``model_drift:``, ``fidelity:``, ``transport: broker halted:``, ``transport: N items still open``), ``resumeAfter``
and ``transport`` (the broker transport's description, with its halt record) when there are any, and the totals
``accepted``, ``invalid``, ``calls`` and ``reviews`` of the invocation (``reviews`` counts the units decided, which
are loci or candidate sentences here, under the census field name).

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
from .followup_branch import check_instrument, now, store_artifact
from .followup_model import canonical, digest

REPAIR_NOTE_KEY = "repair"
PROVENANCE_KEY = "provenance"
# How a broker judge names its own fidelity stops, and the halt codes the census treats as fidelity stops.
FIDELITY_STOPS = frozenset({"fidelity", "request_mismatch"})
FIDELITY_CODES = frozenset({"FIDELITY", "INSTRUMENT_MISMATCH", "INSTRUMENT_MESSAGE_MISMATCH", "WORKER_KEY_CONFLICT"})


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
    """citation-support@2 unchanged: the corpus_assessment planner and validator. A response the validator cannot
    read at all (not JSON, no items array) rejects every item of its call, retryable as the validator says, instead
    of ending the run."""

    def build_messages(self, items: Sequence[Mapping[str, Any]], tier: str) -> list[dict[str, str]]:
        from .corpus_assessment import build_messages
        return build_messages([dict(item) for item in items], tier)

    def validate_response(self, items: Sequence[Mapping[str, Any]], text: Any) -> dict[str, Any]:
        from .corpus_assessment import AssessmentError, AssessmentValidator
        try:
            return AssessmentValidator().validate_response([dict(item) for item in items], text)
        except AssessmentError as error:
            return {"accepted": {}, "unexpected": [], "unidentifiedItems": 0,
                    "rejected": {item["id"]: {"code": error.code, "message": str(error), "retryable": error.retryable}
                                 for item in items}}


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
              study_bundle: str | None = None, transport_retries: int = 0, cooldown: float = 0.0,
              sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Judge ``items`` (dicts with ``id`` and ``provenance``) with one instrument tier; see the module docstring.

    Items of calls that got no response stay open; ``transport_retries`` further passes send them again, the first
    after ``cooldown`` seconds and each next one after twice as long. ``summary["open"]`` counts what is left.
    """
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
    # Before any call: the registry digest equals every pin (study bundle, protocol), and the pinned bundle's sha256
    # goes into every receipt, replicate calls included. A broker run needs a pinned bundle, as the census's does.
    pinned_bundle = check_instrument(connection, instrument)
    study_bundle = study_bundle or pinned_bundle
    if items and getattr(judge, "requires_study_bundle", False) and not study_bundle:
        raise ValidationError("a broker run records the study bundle in every receipt: pin it first with followup init "
                              "--study-bundle or followup pin-bundle")
    instrument_digest = instrument.digest
    done = set(accepted(connection, instrument_digest, tier, variant))
    pending = [dict(item) for item in items if item["id"] not in done]
    summary: dict[str, Any] = {"instrument": instrument.id, "digest": instrument_digest, "tier": tier,
                               "variant": variant, "items": len(items), "skipped": len(items) - len(pending),
                               "calls": 0, "repairCalls": 0, "accepted": 0, "rejected": 0, "failedCalls": 0,
                               "passes": 0, "open": 0, "stopped": None}
    queue: deque[tuple[list[dict[str, Any]], str]] = deque(
        (pending[start:start + plan.items_per_call], "first") for start in range(0, len(pending), plan.items_per_call))
    in_flight: dict[Future, tuple[list[dict[str, Any]], str, list[dict[str, str]], dict[str, Any], str]] = {}

    def call(messages: list[dict[str, str]], settings: dict[str, Any]) -> Any:
        return judge.complete(messages, settings=settings)

    left_open: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    if summary["stopped"] is None and pending:
        summary["stopped"] = judge_stop(judge)
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        while True:
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
                    retry, failed = _record(connection, instrument, plan, judge, policy, model, batch, kind, messages,
                                            settings, started, future, summary, module=module, variant=variant,
                                            code_revision=code_revision, study_bundle=study_bundle, outcomes=outcomes)
                    left_open.extend(failed)
                    if summary["stopped"] is None:
                        # A halted broker transport sends nothing more: no further batch, repair or pass.
                        summary["stopped"] = judge_stop(judge)
                    if retry and kind == "first" and plan.repair_passes > 0 and summary["stopped"] is None:
                        queue.append((retry, "repair"))
                connection.commit()
            if not left_open or summary["stopped"] is not None or summary["passes"] >= max(0, int(transport_retries)):
                break
            sleep(float(cooldown) * (2 ** summary["passes"]))
            summary["passes"] += 1
            if summary["stopped"] is None:
                summary["stopped"] = judge_stop(judge)
                if summary["stopped"] is not None:
                    break
            again, left_open = left_open, []
            queue.extend((again[start:start + plan.items_per_call], "first")
                         for start in range(0, len(again), plan.items_per_call))
    decided = {row[0] for row in connection.execute(
        "SELECT DISTINCT item_key FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant=? AND outcome IN "
        "('accepted','rejected')", (instrument_digest, tier, variant))}
    summary["open"] = sum(1 for item in pending if item["id"] not in decided)
    summary["invalid"] = sum(1 for outcome in outcomes.values() if outcome == "rejected")
    summary["halted"] = transport_halted(judge)
    return summary


def transport_halted(judge: Any) -> dict[str, Any] | None:
    """The halt record (reason, code, resumeAfter) of a broker judge's transport, or None."""
    halted = getattr(getattr(judge, "transport", None), "halted", None)
    return dict(halted) if isinstance(halted, Mapping) and halted else None


def judge_stop(judge: Any) -> str | None:
    """Why a judge sends nothing more: ``fidelity`` (a broker judge's own stop or a fidelity halt code),
    ``model-drift`` (the broker paused on another served model) or ``transport`` (any other halt), else None."""
    stopped = getattr(judge, "stopped", None)
    if stopped:
        return "fidelity" if stopped in FIDELITY_STOPS else str(stopped)
    halted = transport_halted(judge)
    if not halted:
        return None
    code = halted.get("code")
    if code == "MODEL_DRIFT":
        return "model-drift"
    return "fidelity" if code in FIDELITY_CODES else "transport"


def step_result(summary: Mapping[str, Any] | None, judge: Any = None) -> dict[str, Any]:
    """The census step contract of one judging invocation (see the module docstring)."""
    summary = summary or {}
    accepted = int(summary.get("accepted") or 0)
    invalid = int(summary.get("invalid") or 0)
    result: dict[str, Any] = {"state": "completed", "accepted": accepted, "invalid": invalid,
                              "calls": int(summary.get("calls") or 0) + int(summary.get("repairCalls") or 0),
                              "reviews": accepted + invalid}
    halted = summary.get("halted") or (transport_halted(judge) if judge is not None else None)
    stopped = summary.get("stopped")
    left = int(summary.get("open") or 0)
    reason = None
    if stopped == "model-drift":
        drift = summary.get("drift") or {}
        reason = (f"model_drift: served {drift.get('served')!r}, required {drift.get('required')!r}" if drift else
                  "model_drift: " + str((halted or {}).get("reason") or "the broker paused on another served model"))
    elif stopped == "fidelity":
        reason = "fidelity: " + str((halted or {}).get("reason") or "the broker refused the instrument call or "
                                    "reported another request than the instrument built")
    elif stopped == "transport" or halted:
        reason = "transport: broker halted: " + str((halted or {}).get("reason") or "the transport halted")
    elif left:
        reason = f"transport: {left} items still open after {int(summary.get('passes') or 0)} further passes"
    if reason:
        result.update(state="paused", reason=reason + "; run the command again to resume")
        if halted and halted.get("resumeAfter"):
            result["resumeAfter"] = halted["resumeAfter"]
    describe = getattr(getattr(judge, "transport", None), "describe", None)
    if callable(describe):
        result["transport"] = describe()
    return result


def _record(connection, instrument, plan, judge, policy, model, batch, kind, messages, settings, started, future,
            summary, *, module, variant, code_revision, study_bundle, outcomes=None) -> tuple[list, list]:
    """Write the receipt and judgments of one finished call; returns the items to repair and the items left open."""
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
        summary["drift"] = {"served": served, "required": model}
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
        return [], (list(batch) if outcome == "failed" else [])
    result = module.validate_response([_sendable(item) for item in batch], reply.text)
    created = now()
    retry = []
    outcomes = outcomes if outcomes is not None else {}
    for item in batch:
        key = item["id"]
        if key in result["accepted"]:
            outcomes[key] = "accepted"
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
        if outcomes.get(key) != "accepted":
            outcomes[key] = "rejected"
        if failure.get("retryable"):
            retry.append(item)
    return retry, []


def prepare_judge(judge: Any, instrument: Any, tier: str) -> None:
    """Let a judge that needs it check the instrument before any call (the broker judge checks that its verbatim
    bundle is installed and granted); every judging command calls this before its first item is sent."""
    prepare = getattr(judge, "prepare", None)
    if callable(prepare):
        prepare(instrument, tier)


def judge_summary(judge: Any) -> dict[str, Any]:
    """Name and location of the judge, and the reason a broker judge halted its transport (a fidelity stop)."""
    return {"name": getattr(judge, "name", "judge"), "location": getattr(judge, "location", ""),
            "stopped": getattr(judge, "stopped", None)}


def close_judge(judge: Any) -> None:
    """Close a judge that holds a connection (the broker judge's transport) when its command ends."""
    close = getattr(judge, "close", None)
    if callable(close):
        close()


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
