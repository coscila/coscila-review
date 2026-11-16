# SPDX-License-Identifier: Apache-2.0
"""A judge reached through the run principal's broker socket, for the verbatim follow-up instruments.

``BrokerJudge`` follows ``claim_check.Judge`` (``complete(messages, settings=...) -> followup_judge.JudgeReply``) over
``census_broker.BrokerTransport``. Each call is one one-item bulk job that names the instrument's broker digest (the
bundle ``ocsl-review instruments export`` prints and ``pnpm ops broker instruments install`` installs) and carries the
request the instrument built: its messages unchanged and its settings, which the broker checks against the installed
bundle and the worker's model before any provider call. The job's idempotency key comes from the call's identity
(``followup_judge.call_identity``: instrument digest, tier, variant, call kind, item keys, branch and attempt) and the
request digest. A call repeated after a crash, or after a pass in which it never settled, meets the job the broker
already holds; a replicate, a repair call and a call after a settled one carry another identity, so each starts a job of
its own and reaches the model, as the census's call ids do. As in ``census_runner.execute_broker``:

- the served model and the fingerprint are what the judge's own response named (``servedModel``,
  ``systemFingerprint``); another served model is model drift, which stops the run;
- a completed item counts only when its reported request digest equals the canonical digest of the request built;
  otherwise, and when the broker refuses the item before any provider call, the transport halts (a fidelity stop);
- a call that did not settle (a pause, an outage, a timeout, a halted transport) raises ``followup_judge.CallOpen``
  and keeps its identity; a settled failure (an upstream error after the broker's attempts) is a call without a
  response that advances it. Either way its items stay open for the runner's further passes.

Only the verbatim follow-up instruments can run here (``followup_model.BROKER_INSTRUMENTS``: citation-function@1,
numeric-check@1, claim-match@1 and citation-support-verbatim@1, the verbatim version of citation-support@2, which
itself declares no verbatim transport). A call names the broker bundle of its instrument and tier: the bundle whose
system prompt is the call's system message and whose settings are the call's (temperature, reasoning effort, seed and
the output-token rule for the call's items), so the two tiers of citation-support-verbatim@1, which share their
prompt, are told apart by their reasoning effort. The judge is on premises only when ``--on-premises`` is given and the
operator's policy lists the broker provider (and model) in ``onPremBrokerProviders``.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Mapping

from . import instruments
from .census_broker import BrokerTransport, canonical_json, idempotency_key, request_sha256
from .errors import ConfidentialityError, ReviewError, ValidationError
from .followup_judge import CallOpen, JudgeReply
from .followup_model import BROKER_INSTRUMENTS

MISSING_MODEL = "(no model named in the response)"
REFUSED_STATUS = frozenset({400, 413, 422})
BROKER_REFUSALS = frozenset({"INSTRUMENT_MESSAGE_MISMATCH", "PRINCIPAL_REQUIRED", "INVALID_REQUEST"})


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _items_of(messages: list[dict[str, str]]) -> int:
    """The number of instrument items a call carries (its user message's ``items``), at least one."""
    try:
        return max(1, len(json.loads(messages[-1]["content"]).get("items") or []))
    except (ValueError, AttributeError, KeyError, TypeError, IndexError):
        return 1


def settings_match(bundle: Mapping[str, Any], settings: Mapping[str, Any], items: int) -> bool:
    """Whether a call's request settings are the ones the broker sends for ``bundle`` (the execution daemon's verbatim
    check): temperature and reasoning effort as the bundle's, the seed only when the bundle has one, and max_tokens
    max(minimum, perItem x items)."""
    rule = bundle.get("maxOutputTokens") or {}
    expected = {"temperature": bundle.get("temperature"), "reasoning_effort": bundle.get("reasoning"),
                "max_tokens": max(int(rule.get("minimum") or 0), int(rule.get("perItem") or 0) * items)}
    if bundle.get("seed") is not None:
        expected["seed"] = bundle["seed"]
    elif settings.get("seed") is not None:
        return False
    return all(settings.get(name) == value for name, value in expected.items())


class BrokerJudge:
    """One bulk job per call over the run principal's broker socket (see the module docstring)."""

    # A broker run records the pinned study bundle in every receipt (followup_judge.run_items refuses without one).
    requires_study_bundle = True
    # followup_judge.run_items passes every call's identity (complete(..., call=...)), which keys its broker job.
    keys_calls = True

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
        # The broker bundles the judge knows, by the sha256 of their system prompt and then by broker digest (the two
        # tiers of an instrument may share one prompt), and the bundles the command prepared and required.
        self._bundles: dict[str, dict[str, Mapping[str, Any]]] = {}
        self._prepared: set[str] = set()
        self._required: set[str] = set()
        self.bundles: dict[str, str] = {}
        self._lock = threading.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ bundles
    def _remember(self, exported: Mapping[str, Any]) -> None:
        prompt = exported["bundle"]["systemPrompt"]
        self._bundles.setdefault(hashlib.sha256(prompt.encode("utf-8")).hexdigest(), {})[exported["digest"]] = exported

    def _load(self) -> None:
        """Every tier of every verbatim follow-up instrument (an instrument that does not verify is left out)."""
        if self._loaded:
            return
        for instrument_id in BROKER_INSTRUMENTS:
            try:
                tiers = sorted(instruments.load(instrument_id).manifest["tiers"])
            except ValidationError:
                continue
            for tier in tiers:
                try:
                    self._remember(instruments.broker_bundle(instrument_id, tier=tier))
                except ValidationError:
                    continue
        self._loaded = True

    def _require(self, exported: Mapping[str, Any]) -> None:
        with self._lock:
            if exported["digest"] in self._required:
                return
            label = f"{exported['instrumentId']} {exported['tier']}"
            self.transport.require({label: exported["digest"]}, model=self.model)
            self._required.add(exported["digest"])
            self.bundles[label] = exported["digest"]

    def prepare(self, instrument: Any, tier: str) -> None:
        """Before any call: the instrument must export a verbatim bundle for the tier, installed and granted on the
        broker."""
        exported = instruments.broker_bundle(instrument.id, tier=tier)
        with self._lock:
            self._remember(exported)
            self._prepared.add(exported["digest"])
        self._require(exported)

    def bundle_for(self, messages: list[dict[str, str]],
                   settings: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """The broker bundle of a call: its system prompt names the instrument, and where tiers share the prompt the
        call's settings (``settings_match``), then the tier the command prepared, name the tier. A call whose settings
        fit no tier still goes out under its instrument's bundle, and the broker refuses it (a fidelity stop)."""
        system = messages[0].get("content") if messages and messages[0].get("role") == "system" else None
        if not isinstance(system, str):
            raise ValidationError("a verbatim instrument call starts with its system prompt")
        key = hashlib.sha256(system.encode("utf-8")).hexdigest()
        with self._lock:
            self._load()
            candidates = list((self._bundles.get(key) or {}).values())
            prepared = set(self._prepared)
        if not candidates:
            raise ValidationError("the broker judge runs only the verbatim follow-up instruments ("
                                  + ", ".join(BROKER_INSTRUMENTS) + "); this call's system prompt is none of them")
        if len(candidates) > 1 and settings is not None:
            items = _items_of(messages)
            candidates = [exported for exported in candidates
                          if settings_match(exported["bundle"], settings, items)] or candidates
        if len(candidates) > 1:
            candidates = [exported for exported in candidates if exported["digest"] in prepared] or candidates
        if len(candidates) != 1:
            raise ValidationError("this call's system prompt and settings fit several broker bundles ("
                                  + ", ".join(sorted(f"{exported['instrumentId']} {exported['tier']}"
                                                     for exported in candidates))
                                  + "): prepare the instrument tier before the first call")
        return candidates[0]

    # ------------------------------------------------------------------ calls
    def _halt(self, reason: str, stop: str) -> None:
        """A fidelity stop: the judge records which (``request_mismatch`` or ``fidelity``) and the transport halts with
        the census's FIDELITY code, so the result pauses as the census does."""
        self.stopped = self.stopped or stop
        halt = getattr(self.transport, "halt", None)
        if callable(halt):
            halt(reason, code="FIDELITY")

    @staticmethod
    def item_id(expected: str, call: Mapping[str, Any] | None) -> str:
        """The broker item id of a call: its identity and request digest (the request digest alone without one)."""
        if not call:
            return "followup-" + expected[:40]
        return "followup-" + hashlib.sha256(f"{canonical_json(dict(call))}\n{expected}".encode("utf-8")).hexdigest()[:40]

    def complete(self, messages: list[dict[str, str]], *, settings: Mapping[str, Any],
                 call: Mapping[str, Any] | None = None) -> JudgeReply:
        exported = self.bundle_for(messages, settings)
        self._require(exported)
        payload = {"model": self.model, "messages": messages, **dict(settings)}
        expected = request_sha256(payload)
        item_id = self.item_id(expected, call)
        outcome = self.transport.call(item_id=item_id, messages=messages, item_count=_items_of(messages),
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
            raise CallOpen(f"broker {outcome.kind}: {outcome.error or outcome.code or 'no settlement'}")
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
