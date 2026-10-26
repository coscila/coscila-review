# SPDX-License-Identifier: Apache-2.0
"""Execution of citation-census@1: preparation, calls, repair, full-text tier and receipts.

Nothing here changes what the judge sees; prompts, geometry, settings and the
label parser come from ``citation_census``. This module owns everything
around the instrument: review preparation from structured documents, the
chat-completions transport with bounded retries, the repair pass, the served
model check, per-call receipts with prompt and body digests, identity-bound
evidence, the append-only verdict history and the predeclared no-truncation
sensitivity. Calls go to a judge directly (chat completions) or through the
run principal's broker socket (``census_broker``), where the broker's request
digest must equal the canonical digest of the body built here. The working
database is ``scores_db``.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import http.client
import json
import math
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from xml.etree import ElementTree as ET

from . import census_broker
from . import citation_census as C
from . import scores_db
from .census_extraction import extract_review, resolve_identity
from .census_inputs import (CLASS_OF, FULL_TEXT, QUERY_RULE, REVIEW_IDENTITY, check_identity, document_identifiers,
                            identifiers_of, normal_doi, review_provenance, verify_abstract_claim, verify_item_claim)
from .census_works import metadata_label
from .errors import ConfidentialityError, ReviewError, ValidationError
from .sanitize import SANITIZER_VERSION, sanitize_text

IDENTITY_LADDER = "identity-ladder@1"
RESOLVED = ("resolved", "resolved_unverified")
PROVENANCE_MARKS = ",".join("?" * 17)


def detected_rows(field: str, text: str | None, *, journal: str | None = None, doi: str | None = None,
                  label: str | None = None, seq: int | None = None, evidence_id: str | None = None) -> list[tuple]:
    """Hidden characters and injection sentences in text the judge will see, recorded without changing it.

    citation-census@1 sends its inputs byte for byte, so the sanitizer only detects here
    (claim-check@1 strips the same findings before its prompts). Only digests are kept.
    """
    scope = "evidence" if evidence_id else "citation"
    rows = []
    for removal in sanitize_text(text or "", mode="detect", scope=field).removals:
        key = canonical([scope, journal, doi, label, seq, evidence_id, field, removal.start, removal.kind, removal.rule])
        rows.append(("finding-" + sha256_text(key)[:32], scope, journal, doi, label, seq, evidence_id, field,
                     removal.kind, removal.rule, removal.start, removal.length, removal.sha256, SANITIZER_VERSION,
                     utc_now()))
    return rows


def review_item_id(journal: str, doi: str) -> str:
    return "review-" + sha256_text(canonical([journal, doi]))[:32]


def provenance_row(item_id: str, kind: str, label: str, detail: Mapping[str, Any] | None, *, journal: str | None = None,
                   doi: str | None = None, work_key: str | None = None, evidence_id: str | None = None,
                   source_sha256: str | None = None, verified: Any = None) -> tuple:
    """One input_provenance row: where an item's bytes came from, with its acquisition record."""
    if label not in scores_db.PROVENANCE:
        raise ValidationError(f"unknown provenance: {label}")
    detail = dict(detail or {})
    return (item_id, kind, journal, doi, work_key, evidence_id, label, detail.get("cache"), detail.get("cacheVersion"),
            source_sha256 or detail.get("sourceSha256"), detail.get("identity"), detail.get("provider"),
            detail.get("sourceUrl"), detail.get("retrievedAt") or detail.get("capturedAt"),
            None if verified is None else int(bool(verified)), canonical(detail) if detail else None, utc_now())


def abstract_provenance(work: Mapping[str, Any], abstract: str) -> tuple[str, dict[str, Any]]:
    """(label, detail) of the abstract a cited work carries: the lookup's record, unless the text changed since."""
    recorded = work.get("abstractProvenance")
    if isinstance(recorded, dict) and recorded.get("provenance") in scores_db.PROVENANCE:
        if recorded.get("textSha256") != sha256_text(abstract or ""):
            # A provenance record binds the text it vouches for; text changed (or never bound) since is supplied.
            return "supplied", {"changedAfterLookup": dict(recorded)}
        return str(recorded["provenance"]), dict(recorded)
    return ("supplied" if abstract else "unavailable"), {}


class TransportError(ReviewError):
    """A call failed after its bounded transport attempts.

    ``refused``: the server answered and rejected this request itself (400, 413, 422); repeating it changes
    nothing, so it counts as an answered call, unlike a connection failure, a timeout or a server error.
    """

    def __init__(self, message: str = "", *, refused: bool = False):
        super().__init__(message)
        self.refused = refused


REFUSED_STATUS = frozenset({400, 413, 422})


class ModelDrift(ReviewError):
    """The server reported a model other than the instrument's required model; the run pauses."""


class RetryableFetch(ReviewError):
    """A full-text fetch failed transiently (throttling, server error, timeout); the entry stays open."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def code_revision() -> str:
    from . import __version__
    home = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(["git", "-C", str(home), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return f"ocsl-review {__version__}" + (f"+{commit}" if commit else "")


STUDY_BUNDLE_FORMAT = "ocsl-study-bundle-v1"


def load_study_bundle(value: str | Path, *, instrument: Any) -> tuple[dict[str, Any] | None, str]:
    """(bundle, sha256) of ``study prepare --study-bundle``: a bundle file whose digest and instrument pin are
    checked, or a bare sha256 (bundle None) when the bundle itself lives with the Study Run."""
    from .instruments import canonical as bundle_canonical, sha256_bytes
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", text):
        return None, text
    path = Path(text)
    if not text or not path.is_file():
        raise ValidationError("--study-bundle takes a study bundle file (JSON) or the bundle's sha256")
    data = path.read_bytes()
    if len(data) > 1024 * 1024:
        raise ValidationError("the study bundle file exceeds 1 MiB")
    try:
        bundle = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValidationError("the study bundle file is not UTF-8 JSON") from error
    if not isinstance(bundle, dict) or bundle.get("format") != STUDY_BUNDLE_FORMAT:
        raise ValidationError(f"the study bundle file is no {STUDY_BUNDLE_FORMAT} record")
    bundle.pop("artifactSha256", None)
    record = {key: item for key, item in bundle.items() if key != "sha256"}
    digest = sha256_bytes(bundle_canonical(record).encode("utf-8"))
    if bundle.get("sha256") != digest:
        raise ValidationError("the study bundle's sha256 differs from its content")
    pinned = (bundle.get("instruments") or {}).get(instrument.id)
    if pinned != instrument.digest:
        raise ValidationError(f"the study bundle pins {instrument.id} as {pinned or 'nothing'}; the installed instrument "
                              f"is {instrument.digest}")
    return bundle, digest


class HttpChatTransport:
    """POST {base}/chat/completions with the exact body bytes; the key comes from an environment variable."""

    def __init__(self, base_url: str, *, api_key_env: str | None = None, on_premises: bool = False,
                 opener: Callable[..., Any] | None = None):
        if not base_url.startswith(("http://", "https://")):
            raise ValidationError("transport base URL must be http(s)")
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.on_premises = on_premises
        self.opener = opener or urllib.request.urlopen

    def send(self, body: bytes, timeout: float) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "")
            if key:
                headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise TransportError(f"HTTP {error.code}", refused=error.code in REFUSED_STATUS) from error
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
            # HTTPException covers a body cut off mid-response (IncompleteRead) and malformed status lines.
            raise TransportError(f"{type(error).__name__}: {str(error)[:120]}") from error
        if not isinstance(payload, dict):
            raise TransportError("response body is not a JSON object")
        return payload


@dataclass
class CallResult:
    receipt: dict[str, Any]
    text: str | None
    served_model: str | None
    responded: bool = True      # False when no attempt received a response (connection, HTTP error, timeout)
    record: bool = True         # False: no receipt row (a broker call that never settled keeps its call id for a replay)
    stop: str | None = None     # "fidelity": the broker sent another request than the instrument built, or refused it
    call_id: str | None = None


class EmptyCompletion(TransportError):
    """A chat completion arrived with no content: a response, but nothing the parser can read."""


def _completion(response: Any) -> tuple[dict[str, Any], str]:
    """The first choice and its text, or a TransportError for any response that is not a chat completion."""
    if not isinstance(response, dict):
        raise TransportError("response is not a JSON object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise TransportError("response carries no choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise TransportError("response choice carries no message")
    content = message.get("content")
    if content is None or content == "":
        raise EmptyCompletion("empty content; finish=%s" % choice.get("finish_reason"))
    if not isinstance(content, str):
        raise TransportError("message content is not text")
    return choice, content


def _response_fields(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    return {"served_model": response.get("model"), "fingerprint": response.get("system_fingerprint"),
            "finish_reason": choice.get("finish_reason"), "truncated": int(choice.get("finish_reason") == "length"),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"), "cached_tokens": details.get("cached_tokens")}


def execute(transport: Any, payload: dict[str, Any], *, tier: str, required_model: str,
            sleep: Callable[[float], None] = time.sleep) -> CallResult:
    """One logical call with the instrument's transport attempts; returns the receipt fields.

    ``responded`` tells a call that never got a response (retryable later) from one whose last attempt was
    answered with an empty completion or a refusal of the request (an answer without any parseable line).
    """
    spec = C.settings(tier)
    attempts = int(spec["transport"]["attempts"])
    backoff = list(spec["transport"]["backoffSeconds"])
    timeout = float(spec["request"]["timeoutSeconds"])
    body = C.request_body(payload)
    settings = {k: v for k, v in payload.items() if k != "messages"}
    receipt = {**C.digests(payload["messages"], body), "settings_sha256": sha256_text(canonical(settings)),
               "max_tokens": payload["max_tokens"], "request_model": payload["model"], "attempts": 0, "error": None}
    error, empty, refused = None, None, False
    for attempt in range(attempts):
        receipt["attempts"] = attempt + 1
        started = time.monotonic()
        response = None
        try:
            response = transport.send(body, timeout)
            choice, content = _completion(response)
        except TransportError as failure:
            error = str(failure)
            empty = response if isinstance(failure, EmptyCompletion) else None
            refused = getattr(failure, "refused", False)
            receipt["seconds"] = round(time.monotonic() - started, 3)
            if refused:
                break       # the server rejected this very request; sending it again cannot help
            if attempt + 1 < attempts:
                sleep(backoff[min(attempt, len(backoff) - 1)])
            continue
        served = response.get("model")
        receipt.update({"seconds": round(time.monotonic() - started, 3), **_response_fields(response), "error": None})
        if served != required_model:
            receipt["error"] = "model_drift"
            return CallResult(receipt, None, served)
        return CallResult(receipt, content, served)
    if empty is not None:
        receipt.update(_response_fields(empty))
        receipt["error"] = "model_drift" if receipt["served_model"] != required_model else error
        return CallResult(receipt, None, receipt["served_model"])
    receipt["error"] = error or "transport failure"
    return CallResult(receipt, None, None, responded=refused)


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# Closed codes of an item the broker refused before any provider call (no attempt): it will not send the request the
# instrument built. INSTRUMENT_MESSAGE_MISMATCH: the messages or settings differ from the bundle installed under the
# digest (or the worker's model); PRINCIPAL_REQUIRED: the socket carries no run principal authority for a verbatim
# instrument; INVALID_REQUEST: an item the broker cannot run verbatim at all.
BROKER_REFUSALS = frozenset({"INSTRUMENT_MESSAGE_MISMATCH", "PRINCIPAL_REQUIRED", "INVALID_REQUEST"})


def execute_broker(transport: Any, payload: dict[str, Any], *, tier: str, required_model: str, call_id: str,
                   digest: str, items: int) -> CallResult:
    """One logical call as a one-item broker job (``census_broker.BrokerTransport``); returns the receipt fields.

    The item carries ``payload`` whole: its messages unchanged and its settings, which the broker checks against the
    installed bundle and the worker's model before any provider call; a refusal stops the run (``BROKER_REFUSALS``).
    The broker retries transport failures itself. A completed item counts only when it reports the digest of
    the request it sent upstream and that digest equals the canonical digest of ``payload``; otherwise the call
    is invalid, its output is never parsed and the run stops (``stop="fidelity"``). A call that never settled
    (paused, halted, unavailable, timed out) records no receipt row, so the next invocation repeats the same call
    id and meets the same broker job. Never-dispatched items and calls without an answer carry no served model.
    """
    expected = census_broker.request_sha256(payload)
    body = C.request_body(payload)
    settings = {k: v for k, v in payload.items() if k != "messages"}
    receipt = {**C.digests(payload["messages"], body), "settings_sha256": sha256_text(canonical(settings)),
               "max_tokens": payload["max_tokens"], "request_model": payload["model"], "attempts": 0, "error": None,
               "transport": "broker", "request_sha256": expected,
               "broker_principal_id": getattr(transport, "principal_id", None), "broker_item_id": call_id,
               "broker_instrument_digest": digest}
    outcome = transport.call(item_id=call_id, messages=payload["messages"], item_count=items, digest=digest,
                             key=census_broker.idempotency_key(call_id, expected), settings=settings)
    receipt["seconds"] = outcome.seconds
    if outcome.job:
        receipt["broker_job_id"] = outcome.job.get("jobId")
    item = outcome.item if isinstance(outcome.item, dict) else None
    if item is not None:
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        receipt.update({"attempts": _token_count(item.get("attempts")) or 0,
                        "prompt_tokens": _token_count(usage.get("input")),
                        "completion_tokens": _token_count(usage.get("output")),
                        "total_tokens": _token_count(usage.get("total")),
                        "cached_tokens": _token_count(usage.get("cacheRead")),
                        "finish_reason": item.get("finishReason"),
                        "truncated": int(bool(item.get("truncated")) or item.get("finishReason") == "length"),
                        "broker_settings_sha256": item.get("settingsSha256"),
                        "broker_request_sha256": item.get("requestSha256"),
                        "broker_error_code": item.get("errorCode")})
    if outcome.kind == "drift" and item is not None:
        receipt.update({"served_model": item.get("model"), "fingerprint": item.get("serverFingerprint"),
                        "error": "model_drift"})
        return CallResult(receipt, None, item.get("model"))
    if outcome.kind != "settled" or item is None:
        receipt["error"] = outcome.error or outcome.kind
        return CallResult(receipt, None, None, responded=False, record=False)
    if item.get("state") == "completed":
        served = item.get("model")
        receipt.update({"served_model": served, "fingerprint": item.get("serverFingerprint")})
        if served != required_model:
            receipt["error"] = "model_drift"
            return CallResult(receipt, None, served)
        if item.get("requestSha256") != expected:
            receipt["error"] = "request_mismatch"
            return CallResult(receipt, None, served, responded=False, stop="fidelity")
        output = item.get("output")
        text = output if isinstance(output, str) else ("" if output is None else json.dumps(output, ensure_ascii=False))
        if not text.strip():
            receipt["error"] = "empty content; finish=%s" % item.get("finishReason")
            return CallResult(receipt, None, served)
        return CallResult(receipt, text, served)
    code = str(item.get("errorCode") or "INTERNAL")
    if code == "INVALID_REQUEST" and receipt["attempts"]:
        # The judge server refused this very request (HTTP 400, 413 or 422): an answer without labels.
        receipt["error"] = "refused: broker INVALID_REQUEST"
        return CallResult(receipt, None, None, responded=True)
    if code in BROKER_REFUSALS and not receipt["attempts"]:
        # The broker refused the item before any provider call: it would not send the request the instrument built.
        # Nothing reached a judge, and the same request would be refused again: the run stops as a fidelity failure.
        detail = " ".join(str(item.get("error") or "").split())[:300]
        receipt["error"] = f"broker refused the instrument call: {code}" + (f" ({detail})" if detail else "")
        return CallResult(receipt, None, None, responded=False, stop="fidelity")
    receipt["error"] = "broker %s: %s" % (item.get("state"), code)
    return CallResult(receipt, None, None, responded=False)


def writer_lock(database: Path) -> int:
    """Exclusive, non-blocking lock on ``<database>.lock``: one census process writes a database at a time."""
    descriptor = os.open(str(database) + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise ValidationError("another census process is writing this database; run one study command at a time "
                              "(status and gates read it without the lock)") from error
    return descriptor


class CensusRun:
    """A census run over one working scores database (one writing process at a time)."""

    def __init__(self, database: Path, *, seed: int, transport: Any = None, request_model: str | None = None,
                 concurrency: int = 60, run_id: str | None = None, sleep: Callable[[float], None] = time.sleep,
                 revision: str | None = None, breaker: int | None = None):
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValidationError("the census protocol seed must be an integer")
        if not 1 <= concurrency <= 1024:
            raise ValidationError("concurrency must be from 1 to 1024")
        self.instrument = C.instrument()
        self.required_model = self.instrument.manifest["requiredModel"]["served"]
        self.request_model = request_model or self.required_model
        self.transport, self.concurrency, self.sleep = transport, concurrency, sleep
        # Consecutive batches or calls without any response before a pass stops dispatching (an outage).
        self.breaker = breaker or max(4, concurrency)
        self.revision = revision or code_revision()
        self.lock = threading.Lock()
        self.input_caches: Any = None
        self._fidelity: list[str] = []
        self._digests: dict[tuple[str, str], str] = {}
        self._writer = writer_lock(Path(database))
        try:
            self.connection = scores_db.connect(Path(database), create=True)
        except Exception:
            self._release()
            raise
        self.run_id = run_id or "census-" + hashlib.sha256(
            canonical([self.instrument.digest, seed]).encode()).hexdigest()[:16]
        try:
            with self.lock, self.connection:
                scores_db.set_meta(self.connection, {"instrumentId": self.instrument.id,
                                                     "instrumentDigest": self.instrument.digest,
                                                     "requiredModel": self.required_model, "seed": str(seed),
                                                     "runId": self.run_id, "identityLadder": IDENTITY_LADDER})
        except Exception:
            self.close()
            raise
        self.seed = seed
        self.study_bundle = scores_db.meta(self.connection).get("studyBundleSha256")

    def _release(self) -> None:
        if self._writer is not None:
            fcntl.flock(self._writer, fcntl.LOCK_UN)
            os.close(self._writer)
            self._writer = None

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
        closer = getattr(getattr(self, "transport", None), "close", None)
        if callable(closer):
            closer()
        self._release()

    # ------------------------------------------------------------------ study bundle
    def pin_study_bundle(self, value: str | Path) -> dict[str, Any]:
        """Pin the study bundle before the first call; its digest is recorded in every call receipt.

        ``value`` is a study bundle file (``ocsl-study-bundle-v1``, the record of
        ``instruments.study_bundle_digest``; its sha256 and its citation-census@1 digest are checked) or the
        bundle's sha256 alone. A database pins one bundle.
        """
        bundle, digest = load_study_bundle(value, instrument=self.instrument)
        with self.lock, self.connection:
            pinned = scores_db.meta(self.connection).get("studyBundleSha256")
            if pinned is None and self.connection.execute("SELECT 1 FROM calls LIMIT 1").fetchone():
                raise ValidationError("the study bundle is pinned before the first call: this database already holds "
                                      "call receipts without it")
            values: dict[str, Any] = {"studyBundleSha256": digest}
            if bundle is not None:
                values["studyBundle"] = canonical(bundle)
            scores_db.set_meta(self.connection, values)
        self.study_bundle = digest
        return {"studyBundleSha256": digest, "contentVerified": bundle is not None}

    # ------------------------------------------------------------------ input pins
    def pin_inputs(self, caches: Any) -> dict[str, Any]:
        """Pin the byte caches a study reads (store Merkle root, provider cache digest); a later change is refused.

        Cited-work cache hits recorded while preparing are then checked against the pinned versions.
        """
        self.input_caches = caches
        values = {}
        if caches is not None and caches.store is not None:
            values["inputStore"] = caches.store.version
        if caches is not None and caches.provider_cache is not None:
            values["inputProviderCache"] = caches.provider_cache.version
        if values:
            with self.lock, self.connection:
                scores_db.set_meta(self.connection, values)
        return values

    def pin_enumeration(self, enumeration: Any) -> dict[str, Any]:
        """Pin a verified fresh enumeration and record its population; one enumeration per study database."""
        with self.lock, self.connection:
            scores_db.set_meta(self.connection, {"enumerationSha256": enumeration.sha256,
                                                 "enumerationReviews": str(len(enumeration.reviews)),
                                                 "enumerationQueryRule": QUERY_RULE})
            self.connection.executemany(
                "INSERT OR IGNORE INTO enumeration VALUES(?,?,?,?,?,?,?,?)",
                [(journal, doi, item.get("stratum"), item.get("firstOnline"), item.get("year"), item.get("pmid"),
                  item.get("pmcid"), enumeration.sha256) for (journal, doi), item in enumeration.reviews.items()])
        return {"enumerationSha256": enumeration.sha256, "reviews": len(enumeration.reviews)}

    # ------------------------------------------------------------------ preparation
    def _review_provenance(self, record: Mapping[str, Any], document_sha: str | None,
                           provenance: tuple[str, Mapping[str, Any], Any] | None) -> tuple[str, dict[str, Any], Any]:
        if provenance is not None:
            label, detail, verified = provenance
            return label, dict(detail or {}), verified
        label, detail = review_provenance(record, document_sha)
        return label, detail, None

    def _provenance_class(self, record: Mapping[str, Any], label: str, document: bytes | None) -> str:
        """The class of the acquisition, else the record's; a supplied document without one is entitled (as the
        acquirer treats a journal without a route), so it reaches only on-premises judges."""
        if label in CLASS_OF:
            return CLASS_OF[label]
        return "unavailable" if document is None else (record.get("provenanceClass") or "entitled")

    def prepare_review(self, record: Mapping[str, Any], document: bytes | None,
                       table_pages: Sequence[tuple[int, bytes]] = (), *,
                       provenance: tuple[str, Mapping[str, Any], Any] | None = None) -> dict[str, Any]:
        """Extract one review, resolve identities, record loci, works, abstracts and deterministic rows.

        ``table_pages`` are the separately rendered table pages of a publisher HTML review (``document.tables``).
        ``provenance`` is (label, acquisition record, verified); by default it is read from the record's
        ``acquisition`` block (``supplied`` for a document without one). A review acquired from a cache or a
        live route must carry its own DOI.
        """
        journal, doi = record.get("journal"), (record.get("doi") or "").lower()
        if not journal or not doi:
            raise ValidationError("a review record needs journal and doi")
        C.system_prompt(journal)
        kind = (record.get("document") or {}).get("kind") or record.get("route")
        existing = self.connection.execute("SELECT document_sha256 FROM reviews WHERE journal=? AND doi=?",
                                           (journal, doi)).fetchone()
        document_sha = hashlib.sha256(document).hexdigest() if document is not None else None
        if existing is not None:
            if existing[0] != document_sha:
                raise ValidationError(f"{doi}: review already prepared from a different document")
            return {"doi": doi, "state": "already-prepared"}
        label, acquisition, verified = self._review_provenance(record, document_sha, provenance)
        order = self.connection.execute("SELECT COALESCE(MAX(review_order), -1) + 1 FROM reviews").fetchone()[0]
        base = (journal, doi, record.get("paperKey"), record.get("title"), record.get("year"), record.get("firstOnline"),
                record.get("stratum"), kind if document is not None else None,
                self._provenance_class(record, label, document))
        review_item = provenance_row(review_item_id(journal, doi), "review", label, acquisition, journal=journal, doi=doi,
                                     source_sha256=document_sha, verified=verified)
        if document is None:
            with self.lock, self.connection:
                self.connection.execute(
                    "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, route, provenance_class,"
                    " review_order, abstract_state, full_text_state, provenance)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,'completed','completed',?)", (*base, order, label))
                self.connection.execute(f"INSERT OR IGNORE INTO input_provenance VALUES({PROVENANCE_MARKS})", review_item)
            return {"doi": doi, "state": "unavailable"}
        if kind not in ("jats", "html"):
            raise ValidationError(f"{doi}: document kind must be jats or html")
        try:
            review = extract_review(document, html=kind == "html", table_pages=table_pages)
        except (ValidationError, ET.ParseError, UnicodeError) as error:
            return self._record_unreadable(record, base, order, document_sha, kind, f"{type(error).__name__}: {error}",
                                           review_item=review_item, label=label)
        if acquisition and label not in ("supplied", "unavailable"):
            # The acquirer's identity check again: every identifier the document declares about itself agrees with
            # the record, and one of them is shared (the DOI, or for JATS without one its PMCID or PMID).
            expected = {"doi": normal_doi(doi), **{k: v for k, v in identifiers_of(record).items() if k != "doi"}}
            try:
                observed = document_identifiers(document, kind)
            except ValidationError:
                observed = {"doi": normal_doi(review["doi"])} if review["doi"] else {}
            accepted, method = check_identity(observed, expected, require=REVIEW_IDENTITY)
            if not accepted:
                raise ValidationError(f"{doi}: the acquired document's identifiers do not confirm this review ({method})")
        works = {key: dict(value) for key, value in (record.get("works") or {}).items()}
        supplied = record.get("identities") or {}
        title = record.get("title") or review["title"] or ""
        rows, loci, identities, evidence, history, findings = [], [], [], {}, [], []
        abstract_items: dict[str, tuple] = {}
        now = utc_now()
        for entry in review["entries"]:
            if entry.label in supplied:
                ident = dict(supplied[entry.label])
                ident.setdefault("method", "supplied")
                if ident.get("workKey"):
                    ident.setdefault("identity", "resolved_unverified")
            else:
                ident = resolve_identity(entry, works)
            key = ident.get("workKey")
            work = works.get(key) if key else None
            if key and work is None:
                raise ValidationError(f"{doi}: identity for label {entry.label} names an unknown work")
            identity_class = ident.get("identity") if key else "unresolved"
            version = (work or {}).get("identityVersion") or IDENTITY_LADDER
            deterministic = C.deterministic_record(cited=entry.cited_in_body, identity_resolved=identity_class in RESOLVED)
            for seq, locus in enumerate(sorted(entry.loci, key=lambda item: (item.unit, item.offset))):
                loci.append((journal, doi, entry.label, seq, locus.sentence, locus.section, locus.unit, locus.offset,
                             locus.unit_tag, int("table" in locus.flags), int("fig" in locus.flags),
                             int("box" in locus.flags), int("list" in locus.flags),
                             int(seq < C.max_citing_sentences())))
                if deterministic is None and seq < C.max_citing_sentences():
                    findings += detected_rows("citing_sentence", locus.sentence, journal=journal, doi=doi,
                                              label=entry.label, seq=seq)
            abstract_id, cut, sent_chars = None, None, None
            if work and deterministic is None:
                abstract = work.get("abstract") or ""
                info = C.abstract_evidence({"abstract": abstract})
                abstract_id = "abs-" + sha256_text(canonical([key, version, sha256_text(abstract)]))[:32]
                evidence[abstract_id] = (abstract_id, key, version, "abstract", sha256_text(abstract),
                                         work.get("abstractSource"), work.get("abstractRoute"), "public",
                                         work.get("abstractRetrievedAt"), len(abstract), None, abstract)
                abstract_label, abstract_detail = abstract_provenance(work, abstract)
                abstract_items[abstract_id] = provenance_row(
                    abstract_id, "abstract", abstract_label, abstract_detail, work_key=key, evidence_id=abstract_id,
                    verified=verify_abstract_claim(work, {"provenance": abstract_label, **abstract_detail}, abstract,
                                                   self.input_caches))
                cut, sent_chars = int(info["cut"]), info["sentCharacters"]
            ident_proof = {k: v for k, v in ident.items() if k not in ("workKey", "identity", "method", "titleAgrees")}
            identities.append((journal, doi, entry.label, key, identity_class, ident.get("method"),
                               None if ident.get("titleAgrees") is None else int(bool(ident.get("titleAgrees"))),
                               IDENTITY_LADDER, canonical({"identifiers": entry.identifiers, **ident_proof})))
            row = {"journal": journal, "doi": doi, "label": entry.label, "ordinal": entry.ordinal,
                   "target_id": entry.target, "printed": entry.printed, "cited_inbody": int(entry.cited_in_body),
                   "n_loci": len(entry.loci), "n_outside_body_loci": entry.outside_body_loci, "identity": identity_class,
                   "identity_version": version if key else None, "map_method": ident.get("method"),
                   "cited_work_key": key, "cited_doi": (work or {}).get("doi"), "cited_pmid": (work or {}).get("pmid"),
                   "cited_pmcid": (work or {}).get("pmcid"), "cited_title": ((work or {}).get("title") or "")[:500] or None,
                   "abs_evidence_id": abstract_id, "abs_cut": cut, "abs_sent_characters": sent_chars,
                   "abs_source": (work or {}).get("abstractSource")}
            if deterministic is not None:
                row.update({"abs_category": deterministic["category"], "abs_support_verdict": deterministic["support_verdict"],
                            "abs_escalated": 0, "abs_class": deterministic["class"], "abs_basis": deterministic["basis"],
                            "abs_confidence": deterministic["confidence"], "abs_reason": deterministic["reason"]})
                history.append((journal, doi, entry.label, "abstract", "primary", None, None, "deterministic",
                                deterministic["category"], deterministic["support_verdict"], 0, "", deterministic["reason"],
                                canonical(deterministic), now))
            rows.append(row)
        uncovered = review["diagnostics"]["uncovered"]
        with self.lock:
            known = {row[0] for row in self.connection.execute(
                "SELECT evidence_id FROM evidence WHERE evidence_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(evidence)),))}
        for evidence_id in sorted(set(evidence) - known):  # each abstract is checked once per database
            findings += detected_rows("abstract", evidence[evidence_id][11], evidence_id=evidence_id)
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, route, provenance_class,"
                " country, country_source, topic, topic_map, document_sha256, extraction_json, uncovered, review_order,"
                " provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (journal, doi, record.get("paperKey"), title, record.get("year"), record.get("firstOnline"),
                 record.get("stratum"), kind, base[8], record.get("country") or review["country"]["country"],
                 "supplied" if record.get("country") else review["country"]["source"],
                 record.get("topic") or review["topic"], review["topicMap"], document_sha,
                 canonical(review["diagnostics"]), uncovered, order, label))
            self.connection.execute(f"INSERT OR IGNORE INTO input_provenance VALUES({PROVENANCE_MARKS})", review_item)
            for key, work in works.items():
                version = work.get("identityVersion") or IDENTITY_LADDER
                existing_work = self.connection.execute("SELECT identity_version FROM works WHERE work_key=?", (key,)).fetchone()
                if existing_work and existing_work[0] != version:
                    raise ValidationError(f"work {key} changed identity version; re-prepare downstream evidence")
                derivation = (work.get("workProvenance") or {}).get("derivation") if isinstance(
                    work.get("workProvenance"), dict) else None
                self.connection.execute(
                    "INSERT OR IGNORE INTO works(work_key, doi, pmid, pmcid, title, venue, year, retracted, identity_version,"
                    " derivation, metadata_provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        key, work.get("doi"), work.get("pmid"), work.get("pmcid"), work.get("title"), work.get("venue"),
                        None if work.get("year") is None else str(work.get("year")), int(bool(work.get("retracted"))),
                        version, derivation, metadata_label(work)))
            self.connection.executemany("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", list(evidence.values()))
            self.connection.executemany(f"INSERT OR IGNORE INTO input_provenance VALUES({PROVENANCE_MARKS})",
                                        [abstract_items[evidence_id] for evidence_id in sorted(set(evidence) - known)])
            for row in rows:
                self.connection.execute(
                    f"INSERT INTO citations({', '.join(row)}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))
            self.connection.executemany("INSERT INTO loci VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", loci)
            self.connection.executemany("INSERT INTO identity VALUES(?,?,?,?,?,?,?,?,?)", identities)
            self.connection.executemany("INSERT OR IGNORE INTO sanitizer_findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        findings)
            self.connection.executemany(
                "INSERT INTO history(journal, doi, label, tier, variant, call_id, position, outcome, category,"
                " support_verdict, escalate, quote, reason, record_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                history)
            scores_db.aggregate_reviews(self.connection, journal, doi)
        return {"doi": doi, "state": "prepared", "entries": len(rows), "uncovered": uncovered,
                "judgeable": sum(1 for r in rows if "abs_class" not in r), "sanitizerFindings": len(findings)}

    def _record_unreadable(self, record: Mapping[str, Any], base: tuple, order: int, document_sha: str | None,
                           kind: str | None, reason: str, *, review_item: tuple, label: str) -> dict[str, Any]:
        """A review whose document cannot be extracted stays in coverage with its reason; nothing is judged."""
        journal, doi = base[0], base[1]
        diagnostics = {"extractionFailed": reason[:500]}
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, route, provenance_class,"
                " document_sha256, extraction_json, review_order, abstract_state, full_text_state, provenance)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'completed','completed',?)",
                (journal, doi, record.get("paperKey"), record.get("title"), record.get("year"), record.get("firstOnline"),
                 record.get("stratum"), kind, base[8], document_sha, canonical(diagnostics), order, label))
            self.connection.execute(f"INSERT OR IGNORE INTO input_provenance VALUES({PROVENANCE_MARKS})", review_item)
        return {"doi": doi, "state": "extraction-failed", "reason": diagnostics["extractionFailed"]}

    def record_unavailable(self, record: Mapping[str, Any], reason: str) -> dict[str, Any]:
        """A review whose document could not be read (missing file, digest mismatch) stays in coverage."""
        journal, doi = record.get("journal"), (record.get("doi") or "").lower()
        if not journal or not doi:
            raise ValidationError("a review record needs journal and doi")
        with self.lock:
            if self.connection.execute("SELECT 1 FROM reviews WHERE journal=? AND doi=?", (journal, doi)).fetchone():
                return {"doi": doi, "state": "already-prepared"}
            order = self.connection.execute("SELECT COALESCE(MAX(review_order), -1) + 1 FROM reviews").fetchone()[0]
        claimed = record.get("acquisition") if isinstance(record.get("acquisition"), dict) else {}
        item = provenance_row(review_item_id(journal, doi), "review", "unavailable",
                              {"reason": reason[:500], **({"claimed": claimed} if claimed else {})}, journal=journal, doi=doi)
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, provenance_class,"
                " extraction_json, review_order, abstract_state, full_text_state, provenance)"
                " VALUES(?,?,?,?,?,?,?,'unavailable',?,?,'completed','completed','unavailable')",
                (journal, doi, record.get("paperKey"), record.get("title"), record.get("year"), record.get("firstOnline"),
                 record.get("stratum"), canonical({"documentUnreadable": reason[:500]}), order))
            self.connection.execute(f"INSERT OR IGNORE INTO input_provenance VALUES({PROVENANCE_MARKS})", item)
        return {"doi": doi, "state": "unavailable", "reason": reason[:500]}

    # ------------------------------------------------------------------ abstract tier
    def _items(self, journal: str, doi: str) -> list[dict[str, Any]]:
        items = []
        rows = self.connection.execute(
            "SELECT c.label, c.cited_work_key, w.title, w.venue, w.year, w.retracted, e.text AS abstract "
            "FROM citations c JOIN works w ON w.work_key=c.cited_work_key LEFT JOIN evidence e ON e.evidence_id=c.abs_evidence_id "
            "WHERE c.journal=? AND c.doi=? AND c.cited_inbody=1 AND c.identity IN ('resolved','resolved_unverified') "
            "ORDER BY c.ordinal", (journal, doi)).fetchall()
        for row in rows:
            passages = [{"sentence": r["sentence"], "section": r["section"]} for r in self.connection.execute(
                "SELECT sentence, section FROM loci WHERE journal=? AND doi=? AND label=? AND sent=1 ORDER BY seq",
                (journal, doi, row["label"]))]
            year = row["year"]
            if isinstance(year, str) and year.isdigit():
                year = int(year)
            items.append({"label": row["label"], "citing_passages": passages,
                          "cited_work": {"title": row["title"], "venue": row["venue"], "year": year,
                                         "retracted": bool(row["retracted"])},
                          "abstract": row["abstract"] or ""})
        return items

    def _call_id(self, journal: str, doi: str, tier: str, variant: str, batch: int, kind: str) -> str:
        """The receipt id of the next call of this batch and kind (one thread handles a batch at a time).

        A call recorded with an error advances the id; a broker call that never settled keeps it, so a later
        invocation repeats the same id and meets the broker job that already holds it.
        """
        base = canonical([self.run_id, journal, doi, tier, variant, batch, kind])
        with self.lock:
            nonce = self.connection.execute(
                "SELECT COUNT(*) FROM calls WHERE run_id=? AND journal=? AND doi=? AND tier=? AND variant=? AND batch=? AND kind=?",
                (self.run_id, journal, doi, tier, variant, batch, kind)).fetchone()[0]
        return "call-" + sha256_text(base + ":" + str(nonce))[:24]

    def _broker(self) -> bool:
        return getattr(self.transport, "kind", None) == "broker"

    def _broker_digest(self, tier: str, journal: str) -> str:
        """The broker allow-list digest of this tier's bundle for the journal (``instruments export``)."""
        key = (tier, journal)
        digest = self._digests.get(key)
        if digest is None:
            from .instruments import broker_bundle
            digest = broker_bundle(self.instrument.id, tier=tier, parameters={"journal": journal})["digest"]
            self._digests[key] = digest
        return digest

    def _broker_preflight(self, tier: str, condition: str, params: Sequence[Any] = ()) -> None:
        """Before any broker call: a pinned study bundle, the worker's model and every needed bundle granted."""
        if not self._broker():
            return
        if not self.study_bundle:
            raise ValidationError("a broker run records the study bundle in every receipt: pin it first with "
                                  "study prepare --study-bundle")
        with self.lock:
            journals = [row[0] for row in self.connection.execute(
                f"SELECT DISTINCT journal FROM reviews WHERE {condition} ORDER BY journal", params)]
        self.transport.require({f"{tier} tier, {journal}": self._broker_digest(tier, journal) for journal in journals},
                               model=self.request_model)

    def _call(self, payload: dict[str, Any], *, tier: str, journal: str, doi: str, variant: str, batch: int, kind: str,
              items: int) -> CallResult:
        call_id = self._call_id(journal, doi, tier, variant, batch, kind)
        if self._broker():
            result = execute_broker(self.transport, payload, tier=tier, required_model=self.required_model,
                                    call_id=call_id, digest=self._broker_digest(tier, journal), items=items)
        else:
            result = execute(self.transport, payload, tier=tier, required_model=self.required_model, sleep=self.sleep)
            result.receipt.setdefault("transport", "direct")
            result.receipt.setdefault("request_sha256", census_broker.request_sha256(payload))
        result.call_id = call_id
        return result

    def _stop(self, result: CallResult) -> None:
        """A fidelity failure: the call is invalid, its labels stay open and no further call is dispatched."""
        r = result.receipt
        if r.get("error") == "request_mismatch":
            reported = r.get("broker_request_sha256")
            text = (f"call {result.call_id}: the broker reported request {reported or 'no request digest'}, the "
                    f"instrument built {r.get('request_sha256')}; the broker must send verbatim instrument calls "
                    "unchanged (no worker policy, no assignment text)")
        elif r.get("broker_error_code") == "INSTRUMENT_MESSAGE_MISMATCH":
            text = (f"call {result.call_id}: {r.get('error')}; the bundle installed under "
                    f"{r.get('broker_instrument_digest')} or the broker worker's model differs from the request the "
                    "instrument built (install exactly the bundle 'ocsl-review instruments export' prints for this tier "
                    "and journal)")
        else:
            text = (f"call {result.call_id}: {r.get('error')}; the broker must run this instrument in verbatim mode over "
                    "the run principal's socket")
        with self.lock:
            self._fidelity.append(text)

    def _stop_reason(self) -> tuple[str, str, dict[str, Any]] | None:
        with self.lock:
            if self._fidelity:
                return "fidelity", self._fidelity[0], {"reason": self._fidelity[0]}
        halted = getattr(self.transport, "halted", None)
        if halted:
            return "transport", "broker halted: " + str(halted.get("reason")), halted
        return None

    def _stopped(self, totals: dict[str, Any]) -> dict[str, Any] | None:
        """The paused result of a fidelity failure or a halted broker (provider limit, budget, refusal), or None."""
        stop = self._stop_reason()
        if stop is None:
            return None
        kind, text, detail = stop
        self._pause(kind, detail)
        result = {**totals, "state": "paused", "reason": f"{kind}: {text}; run the command again to resume"}
        if detail.get("resumeAfter"):
            result["resumeAfter"] = detail["resumeAfter"]
        return result

    def _with_transport(self, result: dict[str, Any]) -> dict[str, Any]:
        describe = getattr(self.transport, "describe", None)
        return {**result, "transport": describe()} if self._broker() and callable(describe) else result

    def _receipt(self, result: CallResult, *, journal: str, doi: str, tier: str, variant: str, batch: int,
                 kind: str, labels: Sequence[str], parsed: int, missing: int, problems: int) -> str:
        call_id = result.call_id or self._call_id(journal, doi, tier, variant, batch, kind)
        r = result.receipt
        row = {"call_id": call_id, "run_id": self.run_id, "journal": journal, "doi": doi, "tier": tier, "variant": variant,
               "batch": batch, "kind": kind, "n_items": len(labels), "labels_json": canonical(list(labels)),
               "instrument_id": self.instrument.id, "instrument_digest": self.instrument.digest,
               "code_revision": self.revision, "request_model": r.get("request_model"),
               "served_model": r.get("served_model"), "fingerprint": r.get("fingerprint"),
               "settings_sha256": r["settings_sha256"], "system_sha256": r["systemSha256"], "user_sha256": r["userSha256"],
               "messages_sha256": r["messagesSha256"], "body_sha256": r["bodySha256"], "max_tokens": r["max_tokens"],
               "prompt_tokens": r.get("prompt_tokens"), "completion_tokens": r.get("completion_tokens"),
               "total_tokens": r.get("total_tokens"), "cached_tokens": r.get("cached_tokens"),
               "finish_reason": r.get("finish_reason"), "truncated": r.get("truncated"), "attempts": r["attempts"],
               "seconds": r.get("seconds"), "error": r.get("error"), "n_parsed": parsed, "n_missing": missing,
               "n_problems": problems, "created_at": utc_now(), "transport": r.get("transport", "direct"),
               "study_bundle_sha256": self.study_bundle, "request_sha256": r.get("request_sha256")}
        row.update({column: r.get(column) for column in scores_db.BROKER_CALL_COLUMNS})
        with self.lock, self.connection:
            self.connection.execute(f"INSERT INTO calls({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                                    tuple(row.values()))
        return call_id

    def _judge_batch(self, journal: str, doi: str, title: str, batch_index: int, batch: list[dict[str, Any]], *,
                     cut: Any = True, variant: str = "primary", todo: Sequence[str] | None = None,
                     start: str = "first") -> dict[str, Any]:
        """First call, then one repair pass for labels without a parseable line (including failed calls).

        ``batch`` is the whole paper batch (its order gives the positions). ``todo`` names the labels still
        open; ``start="repair"`` resumes a batch whose first call was answered but whose repair call never
        was. When the call that would decide the open labels got no response after its transport attempts,
        the outcome is ``incomplete``: those labels stay open for the next pass instead of becoming invalid.
        """
        open_labels = set(it["label"] for it in batch) if todo is None else set(todo)
        got: dict[str, tuple[dict[str, Any], str, int]] = {}
        failures, drift, responded, answered, stopped = 0, None, False, True, False
        for kind in ("first", "repair"):
            if kind == "first" and start == "repair":
                continue
            sub = batch if kind == "first" else [it for it in batch if it["label"] in open_labels and it["label"] not in got]
            if not sub:
                break
            messages = (C.build_abstract_messages(sub, title, journal, cut=cut) if kind == "first"
                        else C.build_repair_messages(sub, title, journal, cut=cut))
            payload = C.request_payload(messages, model=self.request_model, items=len(sub), tier="abstract")
            result = self._call(payload, tier="abstract", journal=journal, doi=doi, variant=variant, batch=batch_index,
                                kind=kind, items=len(sub))
            labels = [it["label"] for it in sub]
            if result.text is None:
                failures += result.receipt.get("error") != "model_drift"
                if result.record:
                    self._receipt(result, journal=journal, doi=doi, tier="abstract", variant=variant, batch=batch_index,
                                  kind=kind, labels=labels, parsed=0, missing=len(sub), problems=0)
                if result.receipt.get("error") == "model_drift":
                    drift = result.served_model
                    break
                if result.stop:
                    # Another request than the instrument built went upstream (or the broker refused it): nothing
                    # is parsed, the labels stay open and the run stops dispatching.
                    self._stop(result)
                    answered, stopped = False, True
                    break
                if not result.record:
                    # A broker call that never settled may still run there: no repair call duplicates it, and the
                    # next pass or invocation repeats the same call id and meets the same broker job.
                    answered = False
                    break
                # An empty completion is an answer without labels; only a call nobody answered stays open.
                answered = result.responded
                responded = responded or result.responded
                continue
            responded, answered = True, True
            records, problems, missing = C.parse_labels(result.text, labels, stage="abstract")
            call_id = self._receipt(result, journal=journal, doi=doi, tier="abstract", variant=variant, batch=batch_index,
                                    kind=kind, labels=labels, parsed=len(records), missing=len(missing),
                                    problems=len(problems))
            positions = {label: i + 1 for i, label in enumerate(labels)}
            for label, record in records.items():
                if label in open_labels:
                    got[label] = (record, call_id, positions[label])
        incomplete = drift is None and not answered and any(label not in got for label in open_labels)
        return {"batch": batch_index, "items": batch, "got": got, "todo": open_labels, "failures": failures,
                "drift": drift, "incomplete": incomplete, "responded": responded, "stopped": stopped}

    def _write_abstract(self, journal: str, doi: str, outcomes: list[dict[str, Any]], *, cut: Any, variant: str) -> dict[str, int]:
        """Write decided labels; an incomplete batch leaves its unanswered labels open (never invalid)."""
        now = utc_now()
        counts = {"accepted": 0, "invalid": 0, "open": 0}
        with self.lock, self.connection:
            for outcome in outcomes:
                if variant != "primary" and outcome["incomplete"]:
                    counts["open"] += len(outcome["todo"])
                    continue                    # a sensitivity batch is re-run whole on the next pass
                for position, item in enumerate(outcome["items"], 1):
                    label = item["label"]
                    if label not in outcome["todo"]:
                        continue
                    found = outcome["got"].get(label)
                    if variant != "primary":
                        record = found[0] if found else None
                        self.connection.execute(
                            "INSERT INTO history(journal, doi, label, tier, variant, call_id, position, outcome, category,"
                            " support_verdict, escalate, quote, reason, record_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (journal, doi, label, "abstract", variant, found[1] if found else None, found[2] if found else None,
                             "accepted" if found else "invalid", record and record["category"],
                             record and record["support_verdict"], record and record["escalate"], record and record["quote"],
                             record and record["reason"], canonical(record) if record else None, now))
                        counts["accepted" if found else "invalid"] += 1
                        continue
                    if found:
                        record, call_id, call_position = found
                        sent = C.abstract_evidence(item, cut=cut)
                        abstract_sent = (item.get("abstract") or "").strip().replace("\n", " ")
                        if sent["cutCharacters"] is not None:
                            abstract_sent = abstract_sent[:sent["cutCharacters"]]
                        quote = C.quote_check(record, abstract_sent)
                        self.connection.execute(
                            "UPDATE citations SET model_judged=1, abs_category=?, abs_support_verdict=?, abs_escalated=?,"
                            " abs_class=?, abs_basis='model', abs_confidence=?, abs_reason=?, abs_quote=?,"
                            " abs_quote_present=?, abs_quote_in_evidence=?, abs_call_id=?, abs_batch=?, abs_position=?"
                            " WHERE journal=? AND doi=? AND label=?",
                            (record["category"], record["support_verdict"], record["escalate"],
                             C.abstract_class(record["category"], record["escalate"]), record["confidence"],
                             record["reason"], record["quote"],
                             None if quote["quotePresent"] is None else int(quote["quotePresent"]),
                             None if quote["quoteInEvidence"] is None else int(quote["quoteInEvidence"]),
                             call_id, outcome["batch"], position, journal, doi, label))
                        self.connection.execute(
                            "INSERT INTO history(journal, doi, label, tier, variant, call_id, position, outcome, category,"
                            " support_verdict, escalate, quote, reason, record_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (journal, doi, label, "abstract", variant, call_id, call_position, "accepted", record["category"],
                             record["support_verdict"], record["escalate"], record["quote"], record["reason"],
                             canonical(record), now))
                        counts["accepted"] += 1
                    elif outcome["incomplete"]:
                        counts["open"] += 1     # no response decided this label; the next pass asks again
                    else:
                        self.connection.execute(
                            "UPDATE citations SET model_judged=0, abs_class='unsure', abs_basis='invalid:no-parseable-output',"
                            " abs_batch=?, abs_position=? WHERE journal=? AND doi=? AND label=?",
                            (outcome["batch"], position, journal, doi, label))
                        self.connection.execute(
                            "INSERT INTO history(journal, doi, label, tier, variant, call_id, position, outcome, created_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?)", (journal, doi, label, "abstract", variant, None, position, "invalid", now))
                        counts["invalid"] += 1
            if variant == "primary":
                remaining = self.connection.execute(
                    "SELECT COUNT(*) FROM citations WHERE journal=? AND doi=? AND cited_inbody=1 AND identity IN "
                    "('resolved','resolved_unverified') AND abs_basis IS NULL", (journal, doi)).fetchone()[0]
                if not remaining:
                    self.connection.execute("UPDATE reviews SET abstract_state='completed' WHERE journal=? AND doi=?",
                                            (journal, doi))
                scores_db.aggregate_reviews(self.connection, journal, doi)
        return counts

    def _pause(self, reason: str, detail: Mapping[str, Any] | None = None) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO meta VALUES('runState', ?)", ("paused:" + reason,))
            if detail:
                self.connection.execute("INSERT OR REPLACE INTO meta VALUES('runPause', ?)",
                                        (json.dumps(dict(detail), sort_keys=True, ensure_ascii=False),))

    def _schedule(self, work: Iterable[tuple[str, str, str, list[tuple[int, list[dict[str, Any]], Any, str]]]], *,
                  cut: Any, variant: str) -> tuple[dict[str, int], str | None, bool]:
        """Run review batches with at most two batches per stream in flight; write each review when complete.

        Returns the totals, the served model of a drift (or None) and whether the breaker stopped the pass
        after ``self.breaker`` consecutive batches that got no response at all.
        """
        totals = {"reviews": 0, "accepted": 0, "invalid": 0, "open": 0, "transportFailures": 0}
        pending: dict[tuple[str, str], dict[str, Any]] = {}
        drift: list[str] = []
        tripped: list[bool] = []
        silent = [0]
        limit = 2 * self.concurrency
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            inflight: dict[concurrent.futures.Future, tuple[str, str]] = {}

            def write(key, outcomes):
                counts = self._write_abstract(key[0], key[1], sorted(outcomes, key=lambda o: o["batch"]),
                                              cut=cut, variant=variant)
                for name in ("accepted", "invalid", "open"):
                    totals[name] += counts[name]
                totals["reviews"] += 1

            def collect() -> None:
                done, _ = concurrent.futures.wait(list(inflight), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    key = inflight.pop(future)
                    outcome = future.result()
                    totals["transportFailures"] += outcome["failures"]
                    if outcome["drift"] is not None:
                        drift.append(outcome["drift"])
                    silent[0] = 0 if outcome["responded"] else silent[0] + int(outcome["incomplete"])
                    if silent[0] >= self.breaker and not tripped:
                        tripped.append(True)
                    slot = pending[key]
                    slot["left"] -= 1
                    if outcome["drift"] is None:
                        slot["outcomes"].append(outcome)
                    if slot["left"] == 0 and not drift:
                        write(key, slot["outcomes"])
                        del pending[key]

            for journal, doi, title, batches in work:
                if drift or tripped or self._stop_reason():
                    break
                key = (journal, doi)
                if not batches:
                    if variant == "primary":
                        write(key, [])
                    continue
                pending[key] = {"left": len(batches), "outcomes": []}
                for index, batch, todo, start in batches:
                    while len(inflight) >= limit and not (drift or tripped or self._stop_reason()):
                        collect()
                    if drift or tripped or self._stop_reason():
                        break
                    future = pool.submit(self._judge_batch, journal, doi, title, index, batch, cut=cut, variant=variant,
                                         todo=todo, start=start)
                    inflight[future] = key
            while inflight:
                collect()
            # Reviews cut short by a drift, the breaker or a stop keep what their answered batches decided; the
            # batches that drifted or were never sent stay open and resume with the call they still need.
            for key, slot in pending.items():
                if slot["outcomes"]:
                    write(key, slot["outcomes"])
        return totals, (drift[0] if drift else None), bool(tripped)

    def _egress_gate(self, condition: str, params: Sequence[Any] = ()) -> None:
        """Entitled review text reaches only a judge the operator declared on premises; checked before any call."""
        if getattr(self.transport, "on_premises", False):
            return
        with self.lock:
            entitled = self.connection.execute(
                f"SELECT COUNT(*) FROM reviews WHERE provenance_class='entitled' AND ({condition})", params).fetchone()[0]
        if entitled:
            raise ConfidentialityError(f"{entitled} entitled reviews may only be judged by an on-premises judge")

    def _resume(self) -> None:
        """A new invocation resumes a run paused by model drift, a transport outage, a halted broker or a fidelity
        failure; the broker transport starts the invocation afresh (no halt, no resumed job yet)."""
        with self.lock:
            self._fidelity = []
        begin = getattr(self.transport, "begin", None)
        if callable(begin):
            begin()
        if scores_db.meta(self.connection).get("runState", "").startswith("paused:"):
            with self.lock, self.connection:
                self.connection.execute("DELETE FROM meta WHERE key IN ('runState', 'runPause')")

    def _paused(self, totals: dict[str, Any], drift: str | None) -> dict[str, Any]:
        if drift is None:
            return {**totals, "state": "completed"}
        self._pause("model_drift")
        return {**totals, "state": "paused", "reason": f"model_drift: served {drift!r}, required {self.required_model!r}"}

    def _open_batches(self, journal: str, doi: str) -> list[tuple[int, list[dict[str, Any]], list[str], str]]:
        """Paper batches with labels still open; a batch whose first call was answered resumes with its repair call."""
        items = self._items(journal, doi)
        decided = {row[0] for row in self.connection.execute(
            "SELECT label FROM citations WHERE journal=? AND doi=? AND abs_basis IS NOT NULL", (journal, doi))}
        answered = {row[0] for row in self.connection.execute(
            "SELECT DISTINCT batch FROM calls WHERE run_id=? AND journal=? AND doi=? AND tier='abstract' AND "
            "variant='primary' AND kind='first' AND error IS NULL", (self.run_id, journal, doi))}
        batches = []
        for index, batch in enumerate(C.paper_batches(items)):
            todo = [it["label"] for it in batch if it["label"] not in decided]
            if todo:
                start = "repair" if (len(todo) < len(batch) or index in answered) else "first"
                batches.append((index, batch, todo, start))
        return batches

    def run_abstract(self, *, limit: int | None = None, transport_retries: int = 3,
                     cooldown: float = 60.0) -> dict[str, Any]:
        """Abstract tier for every pending review, in enumeration order; resumable per batch.

        A pass stops early when the breaker trips (consecutive batches without any response). Reviews
        left open are retried in up to ``transport_retries`` further passes after a growing cooldown; if
        some are still open, the run pauses with ``paused:transport`` and the next invocation resumes it.
        """
        if self.transport is None:
            raise ValidationError("a transport is required to run the census")
        self._egress_gate("abstract_state='pending'")
        self._broker_preflight("abstract", "abstract_state='pending'")
        self._resume()
        with self.lock:
            reviews = [tuple(r) for r in self.connection.execute(
                "SELECT journal, doi, title FROM reviews WHERE abstract_state='pending' ORDER BY review_order").fetchall()]
        if limit is not None:
            reviews = reviews[:limit]

        def work(selection):
            for journal, doi, title in selection:
                with self.lock:
                    batches = self._open_batches(journal, doi)
                yield journal, doi, title or "", batches
        totals = {"reviews": 0, "accepted": 0, "invalid": 0, "transportFailures": 0, "passes": 0}
        drift, retries = None, 0
        while True:
            totals["passes"] += 1
            outcome, drift, _ = self._schedule(work(reviews), cut=True, variant="primary")
            for name in ("accepted", "invalid", "transportFailures"):
                totals[name] += outcome[name]
            if drift is not None:
                break
            with self.lock:
                open_reviews = {tuple(r) for r in self.connection.execute(
                    "SELECT journal, doi FROM reviews WHERE abstract_state='pending'")}
            totals["reviews"] += sum(1 for journal, doi, _ in reviews if (journal, doi) not in open_reviews)
            reviews = [review for review in reviews if review[:2] in open_reviews]
            if not reviews or retries >= transport_retries or self._stop_reason():
                break
            retries += 1
            self.sleep(cooldown * 2 ** (retries - 1))
        with self.lock:
            totals["calls"] = self.connection.execute("SELECT COUNT(*) FROM calls WHERE run_id=? AND tier='abstract'",
                                                      (self.run_id,)).fetchone()[0]
        if drift is None and reviews:
            totals["openReviews"] = len(reviews)
            stopped = self._stopped(totals)
            if stopped is not None:
                return self._with_transport(stopped)
            self._pause("transport")
            return self._with_transport({**totals, "state": "paused",
                                         "reason": f"transport: {len(reviews)} reviews still open after {totals['passes']} "
                                                   "passes; run the command again to resume"})
        return self._with_transport(self._paused(totals, drift))

    # ------------------------------------------------------------------ full-text tier
    def _retrieve(self, work: Mapping[str, Any], fetch_xml, entitled,
                  cache=None) -> tuple[str | None, str, str | None, str, str]:
        """Full text of a cited work: (text, outcome, source, provenance class, provenance label), cached per identity
        version.

        A study's byte caches (``cache``) are asked first; their Europe PMC fullTextXML is accepted only with its
        digest verified and its PMCID or DOI confirmed. Where the source came from is recorded once per source
        (``input_provenance``, kind full-text) and returned as the label (cache-hit-public, live-public,
        live-entitled; supplied for a source recorded before provenance existed).
        """
        with self.lock:
            cached = self.connection.execute(
                "SELECT e.text, e.provider, e.provenance_class, p.provenance FROM evidence e LEFT JOIN input_provenance p "
                "ON p.item_id=e.evidence_id WHERE e.work_key=? AND e.identity_version=? AND e.unit='full-text' "
                "AND e.text IS NOT NULL AND e.route='source' ORDER BY e.evidence_id LIMIT 1",
                (work["work_key"], work["identity_version"])).fetchone()
        if cached:
            return cached[0], "cache", cached[1], cached[2], cached[3] or "supplied"
        text = raw = source = None
        provenance, label, detail, rejected = "public", "unavailable", {}, []
        pmcid = None
        if work.get("pmcid"):
            pmcid = work["pmcid"] if str(work["pmcid"]).upper().startswith("PMC") else "PMC%s" % work["pmcid"]
        if pmcid and cache is not None:
            hit, rejected = cache.full_text(work)
            if hit is not None:
                try:
                    text, raw, source = C.jats_to_text(hit["body"]), hit["body"], "europe-pmc-fulltextxml"
                    label, detail = "cache-hit-public", hit["provenance"]
                except ValidationError:
                    rejected.append({**hit["provenance"], "reason": "unreadable"})
                    text = None
        if text is None and pmcid and fetch_xml is not None:
            try:
                fetched = fetch_xml(pmcid)
            except RetryableFetch:
                # Throttled, a server error or a dropped connection: keep the entry open, never a failed retrieval.
                return None, "retry", None, provenance, "unavailable"
            if fetched:
                try:
                    text, raw, source = C.jats_to_text(fetched), fetched, "europe-pmc-fulltextxml"
                    label, detail = "live-public", {"provider": source, "sourceUrl": FULL_TEXT % pmcid,
                                                    "retrievedAt": utc_now()}
                except ValidationError:
                    text = None
        if text is None and entitled is not None:
            supplied = entitled(dict(work))
            if supplied:
                raw, media = supplied
                text = C.jats_to_text(raw) if "xml" in media else raw.decode("utf-8")
                source, provenance = "entitled", "entitled"
                label, detail = "live-entitled", {"provider": "entitled", "retrievedAt": utc_now()}
        if text is None:
            return None, "no-pmcid" if not work.get("pmcid") else "fetch-failed", None, provenance, "unavailable"
        evidence_id = "src-" + sha256_text(canonical([work["work_key"], work["identity_version"], sha256_text(text)]))[:32]
        raw_sha = hashlib.sha256(raw).hexdigest() if raw else None
        # Whether the run retrieved escalated works live as well: a cache hit then stands in for its own download.
        item = provenance_row(evidence_id, "full-text", label, {**detail, "liveRetrieval": fetch_xml is not None,
                                                                **({"rejected": rejected} if rejected else {})},
                              work_key=work["work_key"], evidence_id=evidence_id, source_sha256=raw_sha,
                              verified=verify_item_claim(detail, cache))
        with self.lock, self.connection:
            self.connection.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                evidence_id, work["work_key"], work["identity_version"], "full-text", sha256_text(text), source, "source",
                provenance, utc_now(), len(text), raw_sha, text))
            self.connection.execute(f"INSERT OR IGNORE INTO input_provenance VALUES({PROVENANCE_MARKS})", item)
        return text, source, source, provenance, label

    def _prepare_group(self, doi: str, group: list[Any], works: Mapping[str, Mapping[str, Any]], fetch_xml, entitled,
                       allow_entitled: bool, cache=None) -> tuple[str, str, str, list[dict[str, Any]], int]:
        """Retrieve every entry of one review's group, record definitive failures, build the windows."""
        journal, title = group[0]["journal"], group[0]["title"] or ""
        retrieved, retry = [], 0
        for row in group:
            work = works[row["cited_work_key"]]
            text, outcome, source, provenance, label = self._retrieve(work, fetch_xml, entitled, cache)
            if outcome == "retry":
                retry += 1
                continue
            if text is not None and provenance == "entitled" and not allow_entitled:
                text, outcome = None, "entitled-blocked"
            if text is None:
                # No window reaches the judge: the entry's full-text provenance is unavailable.
                with self.lock, self.connection:
                    self.connection.execute(
                        "UPDATE citations SET ft_retrieval=?, ft_provenance='unavailable' WHERE journal=? AND doi=? AND label=?",
                        (outcome, journal, doi, row["label"]))
                continue
            with self.lock:
                passages = [{"sentence": r["sentence"], "section": r["section"]}
                            for r in self._loci(journal, doi, row["label"])]
            year = int(work["year"]) if isinstance(work["year"], str) and work["year"].isdigit() else work["year"]
            window = C.full_text_window(text, C.claim_text(passages[:3]))
            findings = detected_rows("fulltext_window", window, journal=journal, doi=doi, label=row["label"])
            if findings:
                with self.lock, self.connection:
                    self.connection.executemany(
                        "INSERT OR IGNORE INTO sanitizer_findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", findings)
            retrieved.append({"label": row["label"], "citing_passages": passages,
                              "cited_work": {"title": work["title"], "venue": work["venue"], "year": year},
                              "fulltext_window": window,
                              "_entry": {"row": row, "outcome": outcome, "source": source, "provenance": provenance,
                                         "label": label}})
        return doi, journal, title, retrieved, retry

    def _judge_full_text(self, journal: str, doi: str, title: str, index: int, items: list[dict[str, Any]]):
        messages = C.build_full_text_messages(items, title, journal)
        payload = C.request_payload(messages, model=self.request_model, items=len(items), tier="full-text")
        result = self._call(payload, tier="full-text", journal=journal, doi=doi, variant="primary", batch=index,
                            kind="full-text", items=len(items))
        labels = [it["label"] for it in items]
        if result.text is None:
            call_id = (self._receipt(result, journal=journal, doi=doi, tier="full-text", variant="primary", batch=index,
                                     kind="full-text", labels=labels, parsed=0, missing=len(items), problems=0)
                       if result.record else None)
            if result.receipt.get("error") == "model_drift":
                return {"drift": result.served_model, "failed": False, "judged": 0, "missing": 0}
            if result.stop:
                self._stop(result)
                return {"drift": None, "failed": True, "judged": 0, "missing": 0}
            if result.responded:
                # An empty completion answers the call: its entries are recorded as missing (no repair pass).
                self._record_full_text(journal, doi, items, {}, call_id)
                return {"drift": None, "failed": False, "judged": 0, "missing": len(items)}
            return {"drift": None, "failed": True, "judged": 0, "missing": 0}
        records, problems, missing = C.parse_labels(result.text, labels, stage="fulltext")
        call_id = self._receipt(result, journal=journal, doi=doi, tier="full-text", variant="primary", batch=index,
                                kind="full-text", labels=labels, parsed=len(records), missing=len(missing),
                                problems=len(problems))
        self._record_full_text(journal, doi, items, records, call_id)
        return {"drift": None, "failed": False, "judged": len(records), "missing": len(missing)}

    _ESCALATED = ("c.model_judged=1 AND c.abs_escalated=1 AND c.cited_inbody=1 "
                  "AND c.identity IN ('resolved','resolved_unverified')")

    def _full_text_pass(self, fetch_xml, entitled, fetch_workers: int, totals: dict[str, int],
                        touched: set[tuple[str, str]], cache=None) -> tuple[str | None, bool]:
        """One pass over the escalated entries still open (ft_retrieval NULL); returns (drift, breaker tripped)."""
        with self.lock:
            rows = [dict(r) for r in self.connection.execute(
                "SELECT c.journal, c.doi, c.label, c.cited_work_key, r.title FROM citations c JOIN reviews r "
                f"ON r.journal=c.journal AND r.doi=c.doi WHERE {self._ESCALATED} AND c.ft_retrieval IS NULL "
                "ORDER BY r.review_order, c.ordinal")]
            touched.update((row["journal"], row["doi"]) for row in rows)
            needed = sorted({row["cited_work_key"] for row in rows})
            works = {}
            for start in range(0, len(needed), 500):
                chunk = needed[start:start + 500]
                works.update({r["work_key"]: dict(r) for r in self.connection.execute(
                    f"SELECT * FROM works WHERE work_key IN ({','.join('?' * len(chunk))})", chunk)})
        totals["escalated"] = max(totals.get("escalated", 0), len(rows))
        groups = C.full_text_groups(rows, self.seed, review=lambda row: row["doi"])
        allow_entitled = bool(getattr(self.transport, "on_premises", False))
        drift: list[str] = []
        silent, tripped = [0], [False]
        limit = 4 * self.concurrency
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, fetch_workers)) as fetch_pool, \
                concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as call_pool:
            retrievals = [fetch_pool.submit(self._prepare_group, doi, group, works, fetch_xml, entitled, allow_entitled,
                                            cache) for doi, group in groups]
            calls: set[concurrent.futures.Future] = set()

            def collect(block: bool) -> None:
                done, _ = concurrent.futures.wait(list(calls), timeout=None if block else 0,
                                                  return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    calls.discard(future)
                    outcome = future.result()
                    if outcome["drift"] is not None:
                        drift.append(outcome["drift"])
                    silent[0] = silent[0] + 1 if outcome["failed"] else 0
                    if silent[0] >= self.breaker:
                        tripped[0] = True
                    totals["transportFailures"] += int(outcome["failed"])
                    totals["calls"] += int(not outcome["failed"] and outcome["drift"] is None)
                    totals["judged"] += outcome["judged"]
                    totals["missing"] += outcome["missing"]

            for future in concurrent.futures.as_completed(retrievals):
                doi, journal, title, retrieved, retry = future.result()
                totals["retrieved"] += len(retrieved)
                totals["fetchRetries"] += retry
                if drift or tripped[0] or self._stop_reason():
                    continue
                for index, chunk in enumerate(C.full_text_chunks(retrieved)):
                    while len(calls) >= limit:
                        collect(True)
                    if drift or tripped[0] or self._stop_reason():
                        break
                    calls.add(call_pool.submit(self._judge_full_text, journal, doi, title, index, chunk))
                if calls:
                    collect(False)
            while calls:
                collect(True)
        return (drift[0] if drift else None), tripped[0]

    def run_full_text(self, *, fetch_xml: Callable[[str], bytes | None] | None = None,
                      entitled: Callable[[dict[str, Any]], tuple[bytes, str] | None] | None = None,
                      fetch_workers: int = 8, retry_failed: bool = False, transport_retries: int = 3,
                      cooldown: float = 60.0, cache: Any = None) -> dict[str, Any]:
        """Escalated entries: seeded order by review, retrieval, windows, five per call, reasoning low, no repair.

        Entries whose fetch failed transiently or whose call got no response stay open and are retried in
        further passes; ``retry_failed`` also reopens entries an earlier run recorded as ``fetch-failed``.
        ``cache`` (census_inputs.InputCaches) is asked before Europe PMC for each work's fullTextXML, and only with
        ``fetch_xml``: a cached full text stands in for a download the run makes for every escalated work, so the
        works earlier runs happened to fetch never decide which entries get a window.
        """
        if self.transport is None:
            raise ValidationError("a transport is required to run the census")
        if cache is not None and fetch_xml is None:
            raise ValidationError("full-text byte caches stand in only for downloads the run makes for every escalated "
                                  "work: retrieve live as well (study full-text --europe-pmc) or give no caches")
        if retry_failed:
            with self.lock, self.connection:
                self.connection.execute(
                    "UPDATE citations SET ft_retrieval=NULL WHERE ft_retrieval='fetch-failed' AND model_judged=1 AND "
                    "abs_escalated=1 AND cited_inbody=1 AND identity IN ('resolved','resolved_unverified')")
                self.connection.execute(
                    "UPDATE reviews SET full_text_state='pending' WHERE EXISTS (SELECT 1 FROM citations c WHERE "
                    f"c.journal=reviews.journal AND c.doi=reviews.doi AND {self._ESCALATED} AND c.ft_retrieval IS NULL)")
        self._egress_gate("full_text_state='pending'")
        self._broker_preflight("full-text", "full_text_state='pending'")
        self._resume()
        totals = {"escalated": 0, "retrieved": 0, "calls": 0, "judged": 0, "missing": 0, "transportFailures": 0,
                  "fetchRetries": 0, "passes": 0}
        drift, retries, still_open = None, 0, 0
        touched: set[tuple[str, str]] = set()
        while True:
            totals["passes"] += 1
            drift, _ = self._full_text_pass(fetch_xml, entitled, fetch_workers, totals, touched, cache)
            with self.lock:
                still_open = self.connection.execute(
                    f"SELECT COUNT(*) FROM citations c WHERE {self._ESCALATED} AND c.ft_retrieval IS NULL").fetchone()[0]
            if drift is not None or not still_open or retries >= transport_retries or self._stop_reason():
                break
            retries += 1
            self.sleep(cooldown * 2 ** (retries - 1))
        with self.lock, self.connection:
            for journal, doi in sorted(touched):
                scores_db.aggregate_reviews(self.connection, journal, doi)
            self.connection.execute(
                "UPDATE reviews SET full_text_state='completed' WHERE abstract_state='completed' AND NOT EXISTS ("
                "SELECT 1 FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND "
                f"{self._ESCALATED} AND c.ft_retrieval IS NULL)")
        if drift is None and still_open:
            totals["openEntries"] = still_open
            stopped = self._stopped(totals)
            if stopped is not None:
                return self._with_transport(stopped)
            self._pause("transport")
            return self._with_transport({**totals, "state": "paused",
                                         "reason": f"transport: {still_open} escalated entries still open after "
                                                   f"{totals['passes']} passes (throttled or failed fetches, or calls "
                                                   "without a response); run the command again"})
        return self._with_transport(self._paused(totals, drift))

    def _loci(self, journal: str, doi: str, label: str):
        return self.connection.execute(
            "SELECT sentence, section FROM loci WHERE journal=? AND doi=? AND label=? AND sent=1 ORDER BY seq",
            (journal, doi, label)).fetchall()

    def _record_full_text(self, journal: str, doi: str, items: list[dict[str, Any]], records: Mapping[str, Any],
                          call_id: str | None) -> None:
        now = utc_now()
        with self.lock, self.connection:
            for position, item in enumerate(items, 1):
                entry = item["_entry"]
                row = entry["row"]
                current = self.connection.execute(
                    "SELECT c.cited_work_key, w.identity_version FROM citations c JOIN works w ON w.work_key=c.cited_work_key "
                    "WHERE c.journal=? AND c.doi=? AND c.label=?", (journal, doi, row["label"])).fetchone()
                if current is None or current[0] != row["cited_work_key"]:
                    raise ValidationError("full-text evidence identity differs from the citation's current identity")
                window = item["fulltext_window"]
                evidence_id = "ftw-" + sha256_text(canonical([current[0], current[1], sha256_text(window)]))[:32]
                self.connection.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    evidence_id, current[0], current[1], "full-text", sha256_text(window), entry["source"], "window",
                    entry["provenance"], now, len(window), None, window))
                record = records.get(row["label"])
                quote = C.quote_check(record, window) if record else {"quotePresent": None, "quoteInEvidence": None}
                self.connection.execute(
                    "UPDATE citations SET ft_retrieval=?, ft_source=?, ft_provenance=?, ft_evidence_id=?, ft_window_characters=?,"
                    " ft_call_id=?, ft_position=?, ft_category=?, ft_support_verdict=?, ft_class=?, ft_reason=?, ft_quote=?,"
                    " ft_quote_present=?, ft_quote_in_evidence=? WHERE journal=? AND doi=? AND label=?",
                    (entry["outcome"], entry["source"], entry.get("label") or "supplied", evidence_id, len(window), call_id,
                     position,
                     record["category"] if record else None, record["support_verdict"] if record else None,
                     C.full_text_class(record["category"]) if record else None,
                     record["reason"] if record else None, record["quote"] if record else None,
                     None if quote["quotePresent"] is None else int(quote["quotePresent"]),
                     None if quote["quoteInEvidence"] is None else int(quote["quoteInEvidence"]),
                     journal, doi, row["label"]))
                self.connection.execute(
                    "INSERT INTO history(journal, doi, label, tier, variant, call_id, position, outcome, category,"
                    " support_verdict, escalate, quote, reason, record_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (journal, doi, row["label"], "full-text", "primary", call_id, position,
                     "accepted" if record else "missing", record and record["category"],
                     record and record["support_verdict"], record and record["escalate"], record and record["quote"],
                     record and record["reason"], canonical(record) if record else None, now))

    # ------------------------------------------------------------------ predeclared sensitivity
    def no_truncation_sample(self, entries: int | None = None) -> list[tuple[str, str, int]]:
        """Seeded whole review batches with at least one cut abstract, until ``entries`` entries are covered."""
        target = entries or int(self.instrument.manifest["sensitivities"]["no-truncation"]["sampleEntries"])
        with self.lock:
            batches = self.connection.execute(
                "SELECT journal, doi, abs_batch AS batch, COUNT(*) AS n, SUM(abs_cut=1) AS cut FROM citations "
                "WHERE abs_batch IS NOT NULL AND cited_inbody=1 AND identity IN ('resolved','resolved_unverified') "
                "GROUP BY journal, doi, abs_batch HAVING cut > 0 ORDER BY journal, doi, abs_batch").fetchall()
        order = [(b["journal"], b["doi"], b["batch"], b["n"]) for b in batches]
        random.Random(f"{self.seed}:no-truncation").shuffle(order)
        selected, covered = [], 0
        for journal, doi, batch, n in order:
            if covered >= target:
                break
            selected.append((journal, doi, batch))
            covered += n
        return selected

    def run_no_truncation(self, *, entries: int | None = None, transport_retries: int = 3,
                          cooldown: float = 60.0) -> dict[str, Any]:
        """Re-run the sampled batches unchanged except for the abstract cut; history only, variant no-truncation."""
        if self.transport is None:
            raise ValidationError("a transport is required to run the census")
        self._egress_gate("abstract_state='completed'")
        self._broker_preflight("abstract", "abstract_state='completed'")
        self._resume()
        selected = self.no_truncation_sample(entries)
        return self._run_variant(selected, variant="no-truncation", cut=None, transport_retries=transport_retries,
                                 cooldown=cooldown)

    def _run_variant(self, selected: Sequence[tuple[str, str, int]], *, variant: str, cut: Any, transport_retries: int,
                     cooldown: float) -> dict[str, Any]:
        """Whole sampled primary batches re-run unchanged (history only), with the same passes as the primary tier."""
        def open_batches() -> dict[tuple[str, str], list[int]]:
            with self.lock:
                done = {(r[0], r[1], r[2]) for r in self.connection.execute(
                    "SELECT DISTINCT h.journal, h.doi, c.abs_batch FROM history h JOIN citations c ON c.journal=h.journal "
                    "AND c.doi=h.doi AND c.label=h.label WHERE h.variant=?", (variant,))}
            by_review: dict[tuple[str, str], list[int]] = {}
            for journal, doi, batch in selected:
                if (journal, doi, batch) not in done:
                    by_review.setdefault((journal, doi), []).append(batch)
            return by_review

        def work(by_review):
            for (journal, doi), indices in by_review.items():
                with self.lock:
                    title = self.connection.execute("SELECT title FROM reviews WHERE journal=? AND doi=?",
                                                    (journal, doi)).fetchone()[0]
                    batches = C.paper_batches(self._items(journal, doi))
                yield journal, doi, title or "", [(index, batches[index], None, "first") for index in sorted(indices)]
        by_review = open_batches()
        totals = {"reviews": 0, "accepted": 0, "invalid": 0, "transportFailures": 0, "passes": 0,
                  "batches": sum(len(v) for v in by_review.values()), "selectedBatches": len(selected)}
        drift, retries = None, 0
        while by_review:
            totals["passes"] += 1
            outcome, drift, _ = self._schedule(work(by_review), cut=cut, variant=variant)
            for name in ("reviews", "accepted", "invalid", "transportFailures"):
                totals[name] += outcome[name]
            by_review = open_batches()
            if drift is not None or not by_review or retries >= transport_retries or self._stop_reason():
                break
            retries += 1
            self.sleep(cooldown * 2 ** (retries - 1))
        if drift is None and by_review:
            totals["openBatches"] = sum(len(v) for v in by_review.values())
            stopped = self._stopped(totals)
            if stopped is not None:
                return self._with_transport(stopped)
            self._pause("transport")
            return self._with_transport({**totals, "state": "paused",
                                         "reason": f"transport: {totals['openBatches']} {variant} batches still open; "
                                                   "run the command again"})
        return self._with_transport(self._paused(totals, drift))

    def replicate_sample(self, entries: int) -> list[tuple[str, str, int]]:
        """Whole primary batches stratified by journal and year, allocated in proportion to judged entries."""
        if not isinstance(entries, int) or entries < 1:
            raise ValidationError("the replicate needs a positive number of entries")
        with self.lock:
            batches = self.connection.execute(
                "SELECT c.journal, c.doi, c.abs_batch AS batch, r.year, COUNT(*) AS n FROM citations c JOIN reviews r "
                "ON r.journal=c.journal AND r.doi=c.doi WHERE c.abs_batch IS NOT NULL AND c.cited_inbody=1 "
                "AND c.identity IN ('resolved','resolved_unverified') GROUP BY c.journal, c.doi, c.abs_batch "
                "ORDER BY c.journal, c.doi, c.abs_batch").fetchall()
        strata: dict[tuple[str, Any], list[tuple[str, str, int, int]]] = {}
        for b in batches:
            strata.setdefault((b["journal"], b["year"]), []).append((b["journal"], b["doi"], b["batch"], b["n"]))
        total = sum(b["n"] for b in batches)
        selected = []
        for key in sorted(strata, key=lambda k: (k[0], str(k[1]))):
            members = list(strata[key])
            target = math.ceil(entries * sum(m[3] for m in members) / total) if total else 0
            random.Random(f"{self.seed}:replicate:{key[0]}:{key[1]}").shuffle(members)
            covered = 0
            for journal, doi, batch, n in members:
                if covered >= target:
                    break
                selected.append((journal, doi, batch))
                covered += n
        return selected

    def run_replicate(self, *, entries: int, transport_retries: int = 3, cooldown: float = 60.0) -> dict[str, Any]:
        """Re-run a stratified sample of primary batches unchanged; history only, variant replicate."""
        if self.transport is None:
            raise ValidationError("a transport is required to run the census")
        self._egress_gate("abstract_state='completed'")
        self._broker_preflight("abstract", "abstract_state='completed'")
        self._resume()
        selected = self.replicate_sample(entries)
        return self._run_variant(selected, variant="replicate", cut=True, transport_retries=transport_retries,
                                 cooldown=cooldown)

    # ------------------------------------------------------------------ status and gates
    def status(self) -> dict[str, Any]:
        return {**census_status(self.connection, code_instrument_digest=self.instrument.digest), "runId": self.run_id}


ESCALATED_OPEN = ("cited_inbody=1 AND identity IN ('resolved','resolved_unverified') AND model_judged=1 "
                  "AND abs_escalated=1 AND ft_retrieval IS NULL")


def census_status(connection: Any, *, code_instrument_digest: str | None = None) -> dict[str, Any]:
    """Run status from any connection, including a read-only one: nothing is written or pinned."""
    c = connection
    meta = {row[0]: row[1] for row in c.execute("SELECT key, value FROM meta")}
    abstract_calls = c.execute("SELECT COUNT(*), SUM(error IS NOT NULL AND error!='model_drift'), SUM(truncated=1), "
                               "SUM(prompt_tokens), SUM(completion_tokens) FROM calls "
                               "WHERE tier='abstract' AND variant='primary'").fetchone()
    # Fingerprints can only come with a response: receipts of calls that never got one are counted apart.
    responded = c.execute("SELECT COUNT(*), SUM(fingerprint IS NOT NULL) FROM calls WHERE served_model IS NOT NULL").fetchone()
    judgeable = c.execute("SELECT COUNT(*) FROM citations WHERE cited_inbody=1 AND identity IN ('resolved','resolved_unverified')"
                          " AND (abs_basis IS NULL OR abs_basis='model' OR abs_basis LIKE 'invalid%')").fetchone()[0]
    invalid = c.execute("SELECT COUNT(*) FROM citations WHERE abs_basis LIKE 'invalid%'").fetchone()[0]
    open_entries = c.execute("SELECT COUNT(*) FROM citations WHERE cited_inbody=1 AND identity IN "
                             "('resolved','resolved_unverified') AND abs_basis IS NULL").fetchone()[0]
    open_full_text = c.execute(f"SELECT COUNT(*) FROM citations WHERE {ESCALATED_OPEN}").fetchone()[0]
    duplicates = c.execute("SELECT COUNT(*) FROM (SELECT journal, doi, label FROM history WHERE tier='abstract' AND "
                           "variant='primary' AND outcome='accepted' GROUP BY journal, doi, label HAVING COUNT(*) > 1)").fetchone()[0]
    reviews = {r[0]: r[1] for r in c.execute("SELECT abstract_state, COUNT(*) FROM reviews GROUP BY abstract_state")}
    full_text = {r[0]: r[1] for r in c.execute("SELECT full_text_state, COUNT(*) FROM reviews GROUP BY full_text_state")}
    calls = abstract_calls[0] or 0
    pinned = meta.get("instrumentDigest")
    result = {"runId": meta.get("runId"), "instrumentDigest": pinned, "codeInstrumentDigest": code_instrument_digest,
              "instrumentDigestMatches": None if code_instrument_digest is None or pinned is None
              else pinned == code_instrument_digest,
              "runState": meta.get("runState", "active"), "reviews": reviews, "fullTextReviews": full_text,
              "openEntries": open_entries, "openFullTextEntries": open_full_text,
              "judgeableEntries": judgeable, "invalidOutputs": invalid,
              "invalidShare": invalid / judgeable if judgeable else None,
              "abstractCalls": calls, "transportFailures": abstract_calls[1] or 0,
              "transportFailureShare": (abstract_calls[1] or 0) / calls if calls else None,
              "truncatedCalls": abstract_calls[2] or 0,
              "receiptsWithResponse": responded[0] or 0, "receiptsWithFingerprint": responded[1] or 0,
              "fingerprintOffered": bool(responded[1]),
              "promptTokens": abstract_calls[3], "completionTokens": abstract_calls[4],
              "duplicateJudgments": duplicates,
              "uncoveredReviews": c.execute("SELECT COUNT(*) FROM reviews WHERE uncovered>0").fetchone()[0],
              "extractionFailedReviews": c.execute("SELECT COUNT(*) FROM reviews WHERE "
                                                   "json_extract(extraction_json, '$.extractionFailed') IS NOT NULL").fetchone()[0]}
    result.update(transport_status(c, meta))
    result.update(input_status(c, meta))
    return result


def transport_status(c: Any, meta: Mapping[str, str]) -> dict[str, Any]:
    """Study bundle and broker receipts: calls per transport, request-digest mismatches, calls the broker refused before
    any provider call, and accepted verdicts (primary and sensitivity variants, in the append-only history) whose broker
    call did not attest the request the instrument built (0 by construction)."""
    pause = meta.get("runPause")
    out: dict[str, Any] = {"studyBundleSha256": meta.get("studyBundleSha256"),
                           "runPause": json.loads(pause) if pause else None}
    columns = scores_db.table_columns(c, "calls")
    if "transport" not in columns:
        return {**out, "callsByTransport": {}, "brokerCalls": 0, "brokerRequestMismatches": 0, "brokerRefusals": 0,
                "brokerVerdictsFromUnverifiedRequests": 0, "receiptsWithoutStudyBundle": None}
    by_transport = {(row[0] or "direct"): row[1] for row in c.execute(
        "SELECT transport, COUNT(*) FROM calls GROUP BY transport ORDER BY transport")}
    mismatches = c.execute("SELECT COUNT(*) FROM calls WHERE transport='broker' AND error='request_mismatch'").fetchone()[0]
    refusals = c.execute("SELECT COUNT(*) FROM calls WHERE transport='broker' AND "
                         "error LIKE 'broker refused the instrument call:%'").fetchone()[0]
    verdicts = c.execute("SELECT COUNT(*) FROM history h JOIN calls k ON k.call_id=h.call_id WHERE h.outcome='accepted' "
                         "AND k.transport='broker' AND (k.broker_request_sha256 IS NULL OR "
                         "k.broker_request_sha256 != k.request_sha256)").fetchone()[0]
    pinned = meta.get("studyBundleSha256")
    without = (c.execute("SELECT COUNT(*) FROM calls WHERE study_bundle_sha256 IS NULL OR study_bundle_sha256 != ?",
                         (pinned,)).fetchone()[0] if pinned else None)
    return {**out, "callsByTransport": by_transport, "brokerCalls": by_transport.get("broker", 0),
            "brokerRequestMismatches": mismatches, "brokerRefusals": refusals, "brokerVerdictsFromUnverifiedRequests": verdicts,
            "receiptsWithoutStudyBundle": without}


def input_status(c: Any, meta: Mapping[str, str]) -> dict[str, Any]:
    """Provenance of the inputs: reviews per provenance class, items per kind and class, pinned caches, enumeration."""
    out: dict[str, Any] = {"inputs": {key: json.loads(meta[key]) for key in ("inputStore", "inputProviderCache") if key in meta}}
    if "provenance" in scores_db.table_columns(c, "reviews"):
        out["reviewProvenance"] = {(row[0] or "unrecorded"): row[1] for row in c.execute(
            "SELECT provenance, COUNT(*) FROM reviews GROUP BY provenance ORDER BY provenance")}
        by_journal: dict[str, dict[str, int]] = {}
        for journal, label, count in c.execute("SELECT journal, provenance, COUNT(*) FROM reviews "
                                               "GROUP BY journal, provenance ORDER BY journal, provenance"):
            by_journal.setdefault(journal, {})[label or "unrecorded"] = count
        out["reviewProvenanceByJournal"] = by_journal
    if scores_db.has_table(c, "input_provenance"):
        items: dict[str, dict[str, int]] = {}
        for kind, label, count in c.execute("SELECT kind, provenance, COUNT(*) FROM input_provenance "
                                            "GROUP BY kind, provenance ORDER BY kind, provenance"):
            items.setdefault(kind, {})[label] = count
        out["itemProvenance"] = items
        unverified = {kind: count for kind, count in c.execute(
            "SELECT kind, COUNT(*) FROM input_provenance WHERE provenance LIKE 'cache-hit-%' AND COALESCE(verified, 0)!=1 "
            "GROUP BY kind ORDER BY kind")}
        # Abstract evidence no recorded lookup vouches for: the work was never looked up (no record; text carried
        # over from an earlier manifest, or none), or its text changed after the lookup.
        unlooked = c.execute("SELECT COUNT(*) FROM input_provenance WHERE kind='abstract' AND (detail_json IS NULL OR "
                             "json_extract(detail_json, '$.changedAfterLookup') IS NOT NULL)").fetchone()[0]
        # Cache hits count as stand-ins only where the live route was used as well: full texts retrieved live for
        # every escalated work, review documents acquired with the journal's live route on.
        live_route = "COALESCE(json_extract(detail_json, '$.liveRoute'), json_extract(detail_json, '$.claimed.liveRoute'), 0)"
        works_columns = scores_db.table_columns(c, "works")
        derived = "derivation IS NOT NULL" if "derivation" in works_columns else "0"
        recorded = ("metadata_provenance IN ('cache-hit-public', 'cache-hit-entitled', 'live-public', 'live-entitled', "
                    "'unavailable')" if "metadata_provenance" in works_columns else "0")
        out["inputChecks"] = {
            "unverifiedReviewCacheHits": unverified.get("review", 0),
            "unverifiedAbstractCacheHits": unverified.get("abstract", 0),
            "unverifiedFullTextCacheHits": unverified.get("full-text", 0),
            "abstractsWithoutLookup": unlooked,
            "fullTextCacheHitsWithoutLiveRetrieval": c.execute(
                "SELECT COUNT(*) FROM input_provenance WHERE kind='full-text' AND provenance LIKE 'cache-hit-%' AND "
                "COALESCE(json_extract(detail_json, '$.liveRetrieval'), 0) != 1").fetchone()[0],
            "reviewsAcquiredWithoutLiveRoute": c.execute(
                f"SELECT COUNT(*) FROM input_provenance WHERE kind='review' AND provenance != 'supplied' AND {live_route} != 1"
            ).fetchone()[0],
            "suppliedIdentityMaps": c.execute("SELECT COUNT(*) FROM citations WHERE map_method='supplied'").fetchone()[0],
            "suppliedCountries": c.execute("SELECT COUNT(*) FROM reviews WHERE country_source='supplied'").fetchone()[0],
            "worksWithoutDerivation": c.execute(f"SELECT COUNT(*) FROM works WHERE NOT ({derived})").fetchone()[0],
            "worksWithoutRecordedMetadata": c.execute(f"SELECT COUNT(*) FROM works WHERE NOT ({recorded})").fetchone()[0]}
    if meta.get("enumerationSha256") and scores_db.has_table(c, "enumeration"):
        population = scores_db.population(c)
        outside = c.execute("SELECT COUNT(*) FROM reviews r WHERE NOT EXISTS (SELECT 1 FROM enumeration e "
                            "WHERE e.journal=r.journal AND e.doi=r.doi)").fetchone()[0]
        out["enumeration"] = {"sha256": meta["enumerationSha256"], "reviews": population["enumerated"],
                              "inStudy": population["inStudy"], "notInStudy": population["enumerated"] - population["inStudy"],
                              "excluded": population["excluded"], "unexplained": population["unexplained"],
                              "reviewsOutsideEnumeration": outside, "populationSha256": population["sha256"],
                              "queryRule": meta.get("enumerationQueryRule")}
        if scores_db.has_table(c, "manifest_comparisons"):
            columns = scores_db.table_columns(c, "manifest_comparisons")
            digest = "MAX(population_sha256)" if "population_sha256" in columns else "NULL"
            out["comparisons"] = [
                {"label": row[0], "runComplete": bool(row[1]), "populationSha256": row[2],
                 "populationUnchanged": row[2] == population["sha256"]}
                for row in c.execute(f"SELECT label, MIN(run_complete), {digest} FROM manifest_comparisons "
                                     "GROUP BY label ORDER BY label")]
    return out


def record_exclusions(database: Path, records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Record protocol exclusions of enumerated reviews that are not in the study, each with its reason.

    Only in a study pinned to a fresh enumeration and before any manifest comparison: the population is frozen once a
    stored manifest has been compared. Exclusions count towards a complete population (every enumerated review in
    the study or excluded) and are kept in the facts.
    """
    database = Path(database)
    if not database.is_file():
        raise ValidationError(f"scores database is missing: {database}")
    writer = writer_lock(database)
    try:
        connection = scores_db.connect(database, create=True)
        try:
            pinned = scores_db.meta(connection).get("enumerationSha256")
            if not pinned:
                raise ValidationError("exclusions belong to a study pinned to a fresh enumeration")
            if connection.execute("SELECT 1 FROM manifest_comparisons LIMIT 1").fetchone():
                raise ValidationError("a manifest comparison is recorded: the study population is frozen")
            counts = {"excluded": 0, "already": 0}
            rows: dict[tuple[str, str], tuple] = {}
            for record in records:
                journal, doi = record.get("journal"), normal_doi(record.get("doi"))
                reason = " ".join(str(record.get("reason") or "").split())
                if not journal or not doi or not reason or len(reason) > 500:
                    raise ValidationError("an exclusion needs journal, doi and a reason of at most 500 characters")
                if not connection.execute("SELECT 1 FROM enumeration WHERE journal=? AND doi=?", (journal, doi)).fetchone():
                    raise ValidationError(f"{doi} is not a review of the pinned enumeration")
                if connection.execute("SELECT 1 FROM reviews WHERE journal=? AND doi=?", (journal, doi)).fetchone():
                    raise ValidationError(f"{doi} is already in the study; only reviews outside it can be excluded")
                existing = connection.execute("SELECT reason FROM exclusions WHERE journal=? AND doi=?",
                                              (journal, doi)).fetchone()
                earlier = rows.get((journal, doi))
                if (existing and existing[0] != reason) or (earlier and earlier[2] != reason):
                    raise ValidationError(f"{doi} is already excluded for another reason")
                if existing or earlier:
                    counts["already"] += 1
                    continue
                rows[(journal, doi)] = (journal, doi, reason, pinned, utc_now())
            with connection:
                connection.executemany("INSERT INTO exclusions VALUES(?,?,?,?,?)", list(rows.values()))
            counts["excluded"] = len(rows)
            return {**counts, "population": scores_db.population(connection)}
        finally:
            connection.close()
    finally:
        fcntl.flock(writer, fcntl.LOCK_UN)
        os.close(writer)


COMPARISON_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


def record_comparison(database: Path, stored: Path, output: Path, *, label: str = "stored",
                      allow_incomplete: bool = False) -> dict[str, Any]:
    """Compare the study's pinned fresh enumeration with a stored manifest; record counts, never import anything.

    The comparison is a diagnostic, reported and never tuned: it runs only on a study pinned to a fresh
    enumeration (``study prepare --require-fresh-enumeration``) whose census run is complete, unless
    ``allow_incomplete``, which the comparison records. Only DOI, journal and stratum of the stored manifest are
    read; the per-review lists go to the report file, the database keeps counts per journal and stratum.
    """
    from .census_inputs import COMPARISON_FORMAT, compare_manifests, load_stored_manifest
    if not COMPARISON_LABEL.fullmatch(label or ""):
        raise ValidationError("a comparison label is lower-case letters, digits and hyphens")
    database, output = Path(database), Path(output)
    if not database.is_file():
        raise ValidationError(f"scores database is missing: {database}")
    if output.exists():
        raise ValidationError("comparison report already exists; write it to a new path")
    writer = writer_lock(database)
    temporary = None
    try:
        connection = scores_db.connect(database, create=True)
        try:
            meta = scores_db.meta(connection)
            pinned = meta.get("enumerationSha256")
            if not pinned:
                raise ValidationError("compare a stored manifest only after study prepare --require-fresh-enumeration "
                                      "has pinned the fresh enumeration")
            # Complete means every enumerated review in the study or excluded with a reason, and nothing pending: a
            # study with no reviews, or only a pilot, is no completed run.
            state = scores_db.completeness(connection)
            if not state["complete"] and not allow_incomplete:
                raise ValidationError("compare a stored manifest after the census run is complete (a diagnostic, never "
                                      "tuned): " + "; ".join(state["problems"]))
            if connection.execute("SELECT 1 FROM manifest_comparisons WHERE label=?", (label,)).fetchone():
                raise ValidationError(f"comparison {label!r} is already recorded; choose another label")
            population = state["population"]
            studied = {(row[0], row[1]) for row in connection.execute("SELECT journal, doi FROM reviews")}
            excluded = {(row[0], row[1]) for row in connection.execute("SELECT journal, doi FROM exclusions")}
            fresh = [{"journal": row[0], "doi": row[1], "stratum": row[2], "inStudy": (row[0], row[1]) in studied,
                      "excluded": (row[0], row[1]) in excluded} for row in connection.execute(
                "SELECT journal, doi, stratum FROM enumeration WHERE receipt_sha256=? ORDER BY journal, doi", (pinned,))]
            records, info = load_stored_manifest(Path(stored))
            result = compare_manifests(fresh, records)
            compared_at = utc_now()
            report = {"format": COMPARISON_FORMAT, "label": label, "comparedAt": compared_at,
                      "fresh": {"enumerationSha256": pinned, "reviews": len(fresh)}, "stored": info, **result,
                      "population": population, "runComplete": state["complete"], "runProblems": state["problems"],
                      "imported": 0,
                      "note": "comparison only: the stored manifest was read after the fresh enumeration was pinned and "
                              "never entered the study; the population is frozen from here on (no prepare, no "
                              "exclusion)"}
            body = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            report_sha = hashlib.sha256(body).hexdigest()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name("." + output.name + ".incoming")
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            with connection:
                connection.executemany(
                    "INSERT INTO manifest_comparisons(label, journal, stratum, fresh, stored, in_both, fresh_only, stored_only,"
                    " stored_sha256, enumeration_sha256, report_sha256, run_complete, compared_at, in_study, excluded,"
                    " population_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                        (label, cell["journal"], cell["stratum"], cell["fresh"], cell["stored"], cell["inBoth"],
                         cell["freshOnly"], cell["storedOnly"], info["sha256"], pinned, report_sha, int(state["complete"]),
                         compared_at, cell["inStudy"], cell["excluded"], population["sha256"]) for cell in result["cells"]])
            os.replace(temporary, output)
            temporary = None
        finally:
            connection.close()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        fcntl.flock(writer, fcntl.LOCK_UN)
        os.close(writer)
    return {"label": label, "report": str(output), "reportSha256": report_sha, "runComplete": state["complete"],
            "runProblems": state["problems"], "population": population, "totals": result["totals"], "stored": info,
            "byJournal": {journal: {k: v for k, v in cell.items() if k != "strata"}
                          for journal, cell in result["byJournal"].items()}}


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Review records one line at a time (a census manifest can hold gigabytes of abstracts)."""
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError as error:
                raise ValidationError(f"census input line {number} is not JSON") from error
            if not isinstance(value, dict):
                raise ValidationError("census input lines must be JSON objects")
            yield value


def load_records(path: Path) -> list[dict[str, Any]]:
    return list(iter_records(path))


def abstract_lookup_failed(record: Mapping[str, Any]) -> list[str]:
    """Works whose Europe PMC lookup failed in transport; ``study abstracts`` must run again before preparing."""
    return sorted(key for key, work in (record.get("works") or {}).items()
                  if "europe-pmc-failed" in (work.get("europePmcOutcome"), work.get("abstractSource")))


def _read_input(base: Path, entry: Mapping[str, Any]) -> bytes:
    relative = Path(entry["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("census document paths must stay inside the input directory")
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValidationError("census document path escapes the input directory")
    data = path.read_bytes()
    if entry.get("sha256") and hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValidationError(f"{relative}: document digest differs from the manifest")
    return data


def read_document(base: Path, record: Mapping[str, Any]) -> bytes | None:
    document = record.get("document")
    if not document:
        return None
    return _read_input(base, document)


def read_table_pages(base: Path, record: Mapping[str, Any]) -> list[tuple[int, bytes]]:
    """``document.tables``: [{"number": 1, "path": ..., "sha256": ...}] for tables rendered on pages of their own."""
    pages = []
    for entry in (record.get("document") or {}).get("tables") or []:
        number = entry.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValidationError("a table page needs a positive integer number")
        pages.append((number, _read_input(base, entry)))
    if len({number for number, _ in pages}) != len(pages):
        raise ValidationError("table page numbers repeat")
    return sorted(pages)
