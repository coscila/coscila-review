# SPDX-License-Identifier: Apache-2.0
"""A judge reached through the run principal's broker socket, for the verbatim follow-up instruments.

``BrokerJudge`` follows ``claim_check.Judge`` (``complete(messages, settings=...) -> followup_judge.JudgeReply``) over
``census_broker.BrokerTransport``. Each call is one one-item bulk job that names the instrument's broker digest (the
bundle ``ocsl-review instruments export`` prints and ``pnpm ops broker instruments install`` installs) and carries the
request the instrument built: its messages unchanged and its settings, which the broker checks against the installed
bundle and the worker's model before any provider call. The job's idempotency key comes from the request digest, so a
repeated call meets the job the broker already holds. As in ``census_runner.execute_broker``:

- the served model and the fingerprint are what the judge's own response named (``servedModel``,
  ``systemFingerprint``); another served model is model drift, which stops the run;
- a completed item counts only when its reported request digest equals the canonical digest of the request built;
  otherwise, and when the broker refuses the item before any provider call, the transport halts (a fidelity stop);
- every other failure (a pause, an outage, a timeout, an upstream error) is a call without a response: its items stay
  open for the runner's further passes.

Only the verbatim follow-up instruments can run here (citation-support@2 declares no verbatim transport). The judge
is on premises only when ``--on-premises`` is given and the operator's policy lists the broker provider (and model) in
``onPremBrokerProviders``.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Mapping

from . import instruments
from .census_broker import BrokerTransport, idempotency_key, request_sha256
from .errors import ConfidentialityError, ReviewError, ValidationError
from .followup_judge import JudgeReply
from .followup_model import FOLLOWUP_INSTRUMENTS

MISSING_MODEL = "(no model named in the response)"
REFUSED_STATUS = frozenset({400, 413, 422})
BROKER_REFUSALS = frozenset({"INSTRUMENT_MESSAGE_MISMATCH", "PRINCIPAL_REQUIRED", "INVALID_REQUEST"})


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class BrokerJudge:
    """One bulk job per call over the run principal's broker socket (see the module docstring)."""

    def __init__(self, transport: Any, *, model: str, location: str = "external",
                 wait_seconds: float | None = None):
        if location not in ("external", "on-prem"):
            raise ValidationError("a broker judge is external or on-prem")
        self.transport, self.model, self.location = transport, model, location
        self.wait_seconds = wait_seconds
        self.endpoint = None
        self.host = "broker"
        self.name = f"broker-judge:{getattr(transport, 'worker_id', None) or 'worker'}"
        self.stopped: str | None = None
        self._bundles: dict[str, Mapping[str, Any]] = {}
        self._required: set[str] = set()
        self._lock = threading.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ bundles
    def _remember(self, exported: Mapping[str, Any]) -> None:
        prompt = exported["bundle"]["systemPrompt"]
        self._bundles[hashlib.sha256(prompt.encode("utf-8")).hexdigest()] = exported

    def _load(self) -> None:
        if self._loaded:
            return
        for instrument_id in FOLLOWUP_INSTRUMENTS:
            try:
                self._remember(instruments.broker_bundle(instrument_id))
            except ValidationError:
                continue
        self._loaded = True

    def _require(self, exported: Mapping[str, Any]) -> None:
        with self._lock:
            if exported["digest"] in self._required:
                return
            self.transport.require({f"{exported['instrumentId']} {exported['tier']}": exported["digest"]},
                                   model=self.model)
            self._required.add(exported["digest"])

    def prepare(self, instrument: Any, tier: str) -> None:
        """Before any call: the instrument must export a verbatim bundle, installed and granted on the broker."""
        exported = instruments.broker_bundle(instrument.id, tier=tier)
        with self._lock:
            self._remember(exported)
        self._require(exported)

    def bundle_for(self, messages: list[dict[str, str]]) -> Mapping[str, Any]:
        system = messages[0].get("content") if messages and messages[0].get("role") == "system" else None
        if not isinstance(system, str):
            raise ValidationError("a verbatim instrument call starts with its system prompt")
        key = hashlib.sha256(system.encode("utf-8")).hexdigest()
        with self._lock:
            if key not in self._bundles:
                self._load()
            exported = self._bundles.get(key)
        if exported is None:
            raise ValidationError("the broker judge runs only the verbatim follow-up instruments (citation-function@1, "
                                  "numeric-check@1, claim-match@1); this call's system prompt is none of them")
        return exported

    # ------------------------------------------------------------------ calls
    def _halt(self, reason: str, code: str) -> None:
        self.stopped = self.stopped or code
        halt = getattr(self.transport, "halt", None)
        if callable(halt):
            halt(reason, code=code)

    def complete(self, messages: list[dict[str, str]], *, settings: Mapping[str, Any]) -> JudgeReply:
        exported = self.bundle_for(messages)
        self._require(exported)
        payload = {"model": self.model, "messages": messages, **dict(settings)}
        expected = request_sha256(payload)
        item_id = "followup-" + expected[:40]
        try:
            count = len(json.loads(messages[-1]["content"]).get("items") or [])
        except (ValueError, AttributeError, KeyError, TypeError):
            count = 1
        outcome = self.transport.call(item_id=item_id, messages=messages, item_count=max(1, count),
                                      digest=exported["digest"], key=idempotency_key(item_id, expected),
                                      settings={key: value for key, value in payload.items() if key != "messages"},
                                      wait_seconds=self.wait_seconds)
        item = outcome.item if isinstance(outcome.item, dict) else None
        usage, served, fingerprint, text = {}, None, None, ""
        if item is not None:
            tokens = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            usage = {"prompt_tokens": _count(tokens.get("input")), "completion_tokens": _count(tokens.get("output"))}
            served = item.get("servedModel") if isinstance(item.get("servedModel"), str) else None
            fingerprint = item.get("systemFingerprint") if isinstance(item.get("systemFingerprint"), str) else None
            output = item.get("output")
            text = output if isinstance(output, str) else ("" if output is None else json.dumps(output,
                                                                                                ensure_ascii=False))
        if outcome.kind == "drift" and item is not None:
            return JudgeReply(text, served or MISSING_MODEL, usage, item.get("finishReason"), fingerprint)
        if outcome.kind != "settled" or item is None:
            raise ReviewError(f"broker {outcome.kind}: {outcome.error or outcome.code or 'no settlement'}")
        status = item.get("httpStatus") if isinstance(item.get("httpStatus"), int) else None
        if item.get("state") == "completed":
            if served != self.model:
                return JudgeReply(text, served or MISSING_MODEL, usage, item.get("finishReason"), fingerprint)
            if item.get("requestSha256") != expected:
                self._halt("the broker's request digest differs from the request the instrument built",
                           "request_mismatch")
                raise ReviewError("broker request_mismatch: the request sent upstream is not the one the instrument built")
            return JudgeReply(text, served, usage, item.get("finishReason"), fingerprint)
        code = str(item.get("errorCode") or "INTERNAL")
        attempts = _count(item.get("attempts")) or 0
        if code == "INVALID_REQUEST" and attempts and status in REFUSED_STATUS:
            raise ReviewError(f"judge refused the request (broker INVALID_REQUEST, HTTP {status})")
        if code in BROKER_REFUSALS and not attempts:
            self._halt(f"the broker refused the instrument call: {code}", "fidelity")
            raise ReviewError(f"broker refused the instrument call: {code}")
        raise ReviewError(f"broker {item.get('state')}: {code}" + (f" (HTTP {status})" if status else ""))

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()


def judge_from_args(args: Any, policy: Any, socket_path: str) -> BrokerJudge:
    """The broker judge of a judging command (``followup_judge.judge_from_args`` calls this for --broker-socket)."""
    if getattr(args, "api_key_env", None):
        raise ValidationError("the broker transport holds no provider credential: the broker injects it; drop "
                              "--api-key-env")
    transport = BrokerTransport(socket_path, worker=getattr(args, "broker_worker", None))
    transport.open()
    location = "external"
    if getattr(args, "on_premises", False):
        if not getattr(args, "policy", None):
            raise ConfidentialityError("--on-premises needs --policy with the broker provider in onPremBrokerProviders")
        if not policy.broker_provider_on_prem(transport.provider_id, transport.worker_model):
            raise ConfidentialityError(f"broker provider {transport.provider_id} is not an on-prem broker provider of "
                                       "the policy")
        transport.on_premises = True
        location = "on-prem"
    if transport.worker_model != args.model:
        raise ValidationError(f"the broker worker {transport.worker_id!r} requests model {transport.worker_model!r}; "
                              f"the protocol pins {args.model!r} (the request digests could never agree)")
    transport.begin()
    return BrokerJudge(transport, model=args.model, location=location)
