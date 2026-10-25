# SPDX-License-Identifier: Apache-2.0
"""Execution of citation-census@1: preparation, calls, repair, full-text tier and receipts.

Nothing here changes what the judge sees; prompts, geometry, settings and the
label parser come from ``citation_census``. This module owns everything
around the instrument: review preparation from structured documents, the
chat-completions transport with bounded retries, the repair pass, the served
model check, per-call receipts with prompt and body digests, identity-bound
evidence, the append-only verdict history and the predeclared no-truncation
sensitivity. The working database is ``scores_db``.
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

from . import citation_census as C
from . import scores_db
from .census_extraction import extract_review, resolve_identity
from .errors import ConfidentialityError, ReviewError, ValidationError
from .sanitize import SANITIZER_VERSION, sanitize_text

IDENTITY_LADDER = "identity-ladder@1"
RESOLVED = ("resolved", "resolved_unverified")


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

    def _release(self) -> None:
        if self._writer is not None:
            fcntl.flock(self._writer, fcntl.LOCK_UN)
            os.close(self._writer)
            self._writer = None

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
        self._release()

    # ------------------------------------------------------------------ preparation
    def prepare_review(self, record: Mapping[str, Any], document: bytes | None,
                       table_pages: Sequence[tuple[int, bytes]] = ()) -> dict[str, Any]:
        """Extract one review, resolve identities, record loci, works, abstracts and deterministic rows.

        ``table_pages`` are the separately rendered table pages of a publisher HTML review (``document.tables``).
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
        order = self.connection.execute("SELECT COALESCE(MAX(review_order), -1) + 1 FROM reviews").fetchone()[0]
        base = (journal, doi, record.get("paperKey"), record.get("title"), record.get("year"), record.get("firstOnline"),
                record.get("stratum"), kind if document is not None else None,
                "unavailable" if document is None else (record.get("provenanceClass") or "public"))
        if document is None:
            with self.lock, self.connection:
                self.connection.execute(
                    "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, route, provenance_class,"
                    " review_order, abstract_state, full_text_state) VALUES(?,?,?,?,?,?,?,?,?,?,'completed','completed')",
                    (*base, order))
            return {"doi": doi, "state": "unavailable"}
        if kind not in ("jats", "html"):
            raise ValidationError(f"{doi}: document kind must be jats or html")
        try:
            review = extract_review(document, html=kind == "html", table_pages=table_pages)
        except (ValidationError, ET.ParseError, UnicodeError) as error:
            return self._record_unreadable(record, base, order, document_sha, kind, f"{type(error).__name__}: {error}")
        works = {key: dict(value) for key, value in (record.get("works") or {}).items()}
        supplied = record.get("identities") or {}
        title = record.get("title") or review["title"] or ""
        rows, loci, identities, evidence, history, findings = [], [], [], {}, [], []
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
                " country, country_source, topic, topic_map, document_sha256, extraction_json, uncovered, review_order)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (journal, doi, record.get("paperKey"), title, record.get("year"), record.get("firstOnline"),
                 record.get("stratum"), kind, base[8], record.get("country") or review["country"]["country"],
                 "supplied" if record.get("country") else review["country"]["source"],
                 record.get("topic") or review["topic"], review["topicMap"], document_sha,
                 canonical(review["diagnostics"]), uncovered, order))
            for key, work in works.items():
                version = work.get("identityVersion") or IDENTITY_LADDER
                existing_work = self.connection.execute("SELECT identity_version FROM works WHERE work_key=?", (key,)).fetchone()
                if existing_work and existing_work[0] != version:
                    raise ValidationError(f"work {key} changed identity version; re-prepare downstream evidence")
                self.connection.execute("INSERT OR IGNORE INTO works VALUES(?,?,?,?,?,?,?,?,?)", (
                    key, work.get("doi"), work.get("pmid"), work.get("pmcid"), work.get("title"), work.get("venue"),
                    None if work.get("year") is None else str(work.get("year")), int(bool(work.get("retracted"))), version))
            self.connection.executemany("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", list(evidence.values()))
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
                           kind: str | None, reason: str) -> dict[str, Any]:
        """A review whose document cannot be extracted stays in coverage with its reason; nothing is judged."""
        journal, doi = base[0], base[1]
        diagnostics = {"extractionFailed": reason[:500]}
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, route, provenance_class,"
                " document_sha256, extraction_json, review_order, abstract_state, full_text_state)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'completed','completed')",
                (journal, doi, record.get("paperKey"), record.get("title"), record.get("year"), record.get("firstOnline"),
                 record.get("stratum"), kind, base[8], document_sha, canonical(diagnostics), order))
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
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO reviews(journal, doi, paper_key, title, year, first_online, stratum, provenance_class,"
                " extraction_json, review_order, abstract_state, full_text_state)"
                " VALUES(?,?,?,?,?,?,?,'unavailable',?,?,'completed','completed')",
                (journal, doi, record.get("paperKey"), record.get("title"), record.get("year"), record.get("firstOnline"),
                 record.get("stratum"), canonical({"documentUnreadable": reason[:500]}), order))
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

    def _receipt(self, result: CallResult, *, journal: str, doi: str, tier: str, variant: str, batch: int,
                 kind: str, labels: Sequence[str], parsed: int, missing: int, problems: int) -> str:
        base = canonical([self.run_id, journal, doi, tier, variant, batch, kind])
        with self.lock, self.connection:
            nonce = self.connection.execute(
                "SELECT COUNT(*) FROM calls WHERE run_id=? AND journal=? AND doi=? AND tier=? AND variant=? AND batch=? AND kind=?",
                (self.run_id, journal, doi, tier, variant, batch, kind)).fetchone()[0]
            call_id = "call-" + sha256_text(base + ":" + str(nonce))[:24]
            r = result.receipt
            self.connection.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, self.run_id, journal, doi, tier, variant, batch, kind, len(labels), canonical(list(labels)),
                 self.instrument.id, self.instrument.digest, self.revision, r.get("request_model"), r.get("served_model"),
                 r.get("fingerprint"), r["settings_sha256"], r["systemSha256"], r["userSha256"], r["messagesSha256"],
                 r["bodySha256"], r["max_tokens"], r.get("prompt_tokens"), r.get("completion_tokens"),
                 r.get("total_tokens"), r.get("cached_tokens"), r.get("finish_reason"), r.get("truncated"),
                 r["attempts"], r.get("seconds"), r.get("error"), parsed, missing, problems, utc_now()))
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
        failures, drift, responded, answered = 0, None, False, True
        for kind in ("first", "repair"):
            if kind == "first" and start == "repair":
                continue
            sub = batch if kind == "first" else [it for it in batch if it["label"] in open_labels and it["label"] not in got]
            if not sub:
                break
            messages = (C.build_abstract_messages(sub, title, journal, cut=cut) if kind == "first"
                        else C.build_repair_messages(sub, title, journal, cut=cut))
            payload = C.request_payload(messages, model=self.request_model, items=len(sub), tier="abstract")
            result = execute(self.transport, payload, tier="abstract", required_model=self.required_model, sleep=self.sleep)
            labels = [it["label"] for it in sub]
            if result.text is None:
                failures += result.receipt.get("error") != "model_drift"
                self._receipt(result, journal=journal, doi=doi, tier="abstract", variant=variant, batch=batch_index,
                              kind=kind, labels=labels, parsed=0, missing=len(sub), problems=0)
                if result.receipt.get("error") == "model_drift":
                    drift = result.served_model
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
                "drift": drift, "incomplete": incomplete, "responded": responded}

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

    def _pause(self, reason: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO meta VALUES('runState', ?)", ("paused:" + reason,))

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
                if drift or tripped:
                    break
                key = (journal, doi)
                if not batches:
                    if variant == "primary":
                        write(key, [])
                    continue
                pending[key] = {"left": len(batches), "outcomes": []}
                for index, batch, todo, start in batches:
                    while len(inflight) >= limit and not (drift or tripped):
                        collect()
                    if drift or tripped:
                        break
                    future = pool.submit(self._judge_batch, journal, doi, title, index, batch, cut=cut, variant=variant,
                                         todo=todo, start=start)
                    inflight[future] = key
            while inflight:
                collect()
            # Reviews cut short by a drift or by the breaker keep what their answered batches decided; the
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
        """A new invocation resumes a run paused by model drift or by a transport outage."""
        if scores_db.meta(self.connection).get("runState", "").startswith("paused:"):
            with self.lock, self.connection:
                self.connection.execute("DELETE FROM meta WHERE key='runState'")

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
            if not reviews or retries >= transport_retries:
                break
            retries += 1
            self.sleep(cooldown * 2 ** (retries - 1))
        with self.lock:
            totals["calls"] = self.connection.execute("SELECT COUNT(*) FROM calls WHERE run_id=? AND tier='abstract'",
                                                      (self.run_id,)).fetchone()[0]
        if drift is None and reviews:
            totals["openReviews"] = len(reviews)
            self._pause("transport")
            return {**totals, "state": "paused",
                    "reason": f"transport: {len(reviews)} reviews still open after {totals['passes']} passes; "
                              "run the command again to resume"}
        return self._paused(totals, drift)

    # ------------------------------------------------------------------ full-text tier
    def _retrieve(self, work: Mapping[str, Any], fetch_xml, entitled) -> tuple[str | None, str, str | None, str]:
        """Full text of a cited work: (text, outcome, source, provenance class); cached per identity version."""
        with self.lock:
            cached = self.connection.execute(
                "SELECT text, provider, provenance_class FROM evidence WHERE work_key=? AND identity_version=? "
                "AND unit='full-text' AND text IS NOT NULL AND route='source' ORDER BY evidence_id LIMIT 1",
                (work["work_key"], work["identity_version"])).fetchone()
        if cached:
            return cached["text"], "cache", cached["provider"], cached["provenance_class"]
        text = raw = source = None
        provenance = "public"
        if work.get("pmcid") and fetch_xml is not None:
            pmcid = work["pmcid"] if str(work["pmcid"]).upper().startswith("PMC") else "PMC%s" % work["pmcid"]
            try:
                fetched = fetch_xml(pmcid)
            except RetryableFetch:
                # Throttled, a server error or a dropped connection: keep the entry open, never a failed retrieval.
                return None, "retry", None, provenance
            if fetched:
                try:
                    text, raw, source = C.jats_to_text(fetched), fetched, "europe-pmc-fulltextxml"
                except ValidationError:
                    text = None
        if text is None and entitled is not None:
            supplied = entitled(dict(work))
            if supplied:
                raw, media = supplied
                text = C.jats_to_text(raw) if "xml" in media else raw.decode("utf-8")
                source, provenance = "entitled", "entitled"
        if text is None:
            return None, "no-pmcid" if not work.get("pmcid") else "fetch-failed", None, provenance
        with self.lock, self.connection:
            self.connection.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "src-" + sha256_text(canonical([work["work_key"], work["identity_version"], sha256_text(text)]))[:32],
                work["work_key"], work["identity_version"], "full-text", sha256_text(text), source, "source", provenance,
                utc_now(), len(text), hashlib.sha256(raw).hexdigest() if raw else None, text))
        return text, source, source, provenance

    def _prepare_group(self, doi: str, group: list[Any], works: Mapping[str, Mapping[str, Any]], fetch_xml, entitled,
                       allow_entitled: bool) -> tuple[str, str, str, list[dict[str, Any]], int]:
        """Retrieve every entry of one review's group, record definitive failures, build the windows."""
        journal, title = group[0]["journal"], group[0]["title"] or ""
        retrieved, retry = [], 0
        for row in group:
            work = works[row["cited_work_key"]]
            text, outcome, source, provenance = self._retrieve(work, fetch_xml, entitled)
            if outcome == "retry":
                retry += 1
                continue
            if text is not None and provenance == "entitled" and not allow_entitled:
                text, outcome = None, "entitled-blocked"
            if text is None:
                with self.lock, self.connection:
                    self.connection.execute("UPDATE citations SET ft_retrieval=? WHERE journal=? AND doi=? AND label=?",
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
                              "_entry": {"row": row, "outcome": outcome, "source": source, "provenance": provenance}})
        return doi, journal, title, retrieved, retry

    def _judge_full_text(self, journal: str, doi: str, title: str, index: int, items: list[dict[str, Any]]):
        messages = C.build_full_text_messages(items, title, journal)
        payload = C.request_payload(messages, model=self.request_model, items=len(items), tier="full-text")
        result = execute(self.transport, payload, tier="full-text", required_model=self.required_model, sleep=self.sleep)
        labels = [it["label"] for it in items]
        if result.text is None:
            call_id = self._receipt(result, journal=journal, doi=doi, tier="full-text", variant="primary", batch=index,
                                    kind="full-text", labels=labels, parsed=0, missing=len(items), problems=0)
            if result.receipt.get("error") == "model_drift":
                return {"drift": result.served_model, "failed": False, "judged": 0, "missing": 0}
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
                        touched: set[tuple[str, str]]) -> tuple[str | None, bool]:
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
            retrievals = [fetch_pool.submit(self._prepare_group, doi, group, works, fetch_xml, entitled, allow_entitled)
                          for doi, group in groups]
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
                if drift or tripped[0]:
                    continue
                for index, chunk in enumerate(C.full_text_chunks(retrieved)):
                    while len(calls) >= limit:
                        collect(True)
                    if drift or tripped[0]:
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
                      cooldown: float = 60.0) -> dict[str, Any]:
        """Escalated entries: seeded order by review, retrieval, windows, five per call, reasoning low, no repair.

        Entries whose fetch failed transiently or whose call got no response stay open and are retried in
        further passes; ``retry_failed`` also reopens entries an earlier run recorded as ``fetch-failed``.
        """
        if self.transport is None:
            raise ValidationError("a transport is required to run the census")
        if retry_failed:
            with self.lock, self.connection:
                self.connection.execute(
                    "UPDATE citations SET ft_retrieval=NULL WHERE ft_retrieval='fetch-failed' AND model_judged=1 AND "
                    "abs_escalated=1 AND cited_inbody=1 AND identity IN ('resolved','resolved_unverified')")
                self.connection.execute(
                    "UPDATE reviews SET full_text_state='pending' WHERE EXISTS (SELECT 1 FROM citations c WHERE "
                    f"c.journal=reviews.journal AND c.doi=reviews.doi AND {self._ESCALATED} AND c.ft_retrieval IS NULL)")
        self._egress_gate("full_text_state='pending'")
        self._resume()
        totals = {"escalated": 0, "retrieved": 0, "calls": 0, "judged": 0, "missing": 0, "transportFailures": 0,
                  "fetchRetries": 0, "passes": 0}
        drift, retries, still_open = None, 0, 0
        touched: set[tuple[str, str]] = set()
        while True:
            totals["passes"] += 1
            drift, _ = self._full_text_pass(fetch_xml, entitled, fetch_workers, totals, touched)
            with self.lock:
                still_open = self.connection.execute(
                    f"SELECT COUNT(*) FROM citations c WHERE {self._ESCALATED} AND c.ft_retrieval IS NULL").fetchone()[0]
            if drift is not None or not still_open or retries >= transport_retries:
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
            self._pause("transport")
            return {**totals, "state": "paused",
                    "reason": f"transport: {still_open} escalated entries still open after {totals['passes']} passes "
                              "(throttled or failed fetches, or calls without a response); run the command again"}
        return self._paused(totals, drift)

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
                    "UPDATE citations SET ft_retrieval=?, ft_source=?, ft_evidence_id=?, ft_window_characters=?, ft_call_id=?,"
                    " ft_position=?, ft_category=?, ft_support_verdict=?, ft_class=?, ft_reason=?, ft_quote=?,"
                    " ft_quote_present=?, ft_quote_in_evidence=? WHERE journal=? AND doi=? AND label=?",
                    (entry["outcome"], entry["source"], evidence_id, len(window), call_id, position,
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
            if drift is not None or not by_review or retries >= transport_retries:
                break
            retries += 1
            self.sleep(cooldown * 2 ** (retries - 1))
        if drift is None and by_review:
            totals["openBatches"] = sum(len(v) for v in by_review.values())
            self._pause("transport")
            return {**totals, "state": "paused",
                    "reason": f"transport: {totals['openBatches']} {variant} batches still open; run the command again"}
        return self._paused(totals, drift)

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
    return {"runId": meta.get("runId"), "instrumentDigest": pinned, "codeInstrumentDigest": code_instrument_digest,
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
