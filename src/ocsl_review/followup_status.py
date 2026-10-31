# SPDX-License-Identifier: Apache-2.0
"""Status observations of every cited work of the parent census, and the status-resolution rule.

Sources (each a ``followup_model.StatusSource``; a provider failure is an ``unknown`` observation, never an exception):

- ``CrossrefStatus``: ``/works/{doi}`` (``updated-by`` notices on the work, with their origin: the publisher or
  Retraction Watch, whose data Crossref's REST API carries) and ``/works?filter=updates:{doi}`` (notices whose
  ``update-to`` names the work); every notice kind.
- ``PubMedStatus``: efetch ``CommentsCorrectionsList`` (``scholarly.parse_pubmed_record``); the notice date comes only
  from the notice's own record (esummary), otherwise it stays empty; retraction, expression of concern, correction.
- ``EuropePmcStatus``: the ``isRetracted`` flag of the Europe PMC core record; retraction only.
- ``import_retraction_watch``: the Retraction Watch CSV snapshot the operator stored as a dataset input, read locally
  (``integrity.RetractionWatchDataset``); every notice kind.

A source that answers without a notice records ``none`` for the kinds it covers. ``resolve_status`` gives every cited
work of the parent one ``fu_work_status`` row under ``status-resolution@1``. Requests go through
``followup_net.client()`` with identifiers only, and every provider response is recorded with
``followup_net.record_request``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import followup_net
from .errors import ProviderError, ReviewError, ValidationError
from .followup_branch import FollowupBranch, now, register_dataset
from .followup_model import STATUS_RULE, StatusObservation, WorkRef, WorkStatus, canonical
from .followup_schema import NOTICE_KINDS

CROSSREF = "https://api.crossref.org"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPERATION = "followup_status"
KIND_STATUS = {"retraction": "retracted", "expression-of-concern": "expression-of-concern", "correction": "corrected",
               "reinstatement": "reinstated"}
STATUS_KIND = {status: kind for kind, status in KIND_STATUS.items()}
# The notice kinds a status of none must have covered (a reinstatement only ever reverses a retraction).
DECIDING_KINDS = ("retraction", "expression-of-concern", "correction")
LIVE_SOURCES = ("crossref", "pubmed", "europe-pmc")
RW_SOURCE = "retraction-watch-csv"
RW_LABEL = {"retraction": "Retraction", "expression-of-concern": "Expression of concern", "correction": "Correction",
            "reinstatement": "Reinstatement"}
PRECISION = {"year": 1, "month": 2, "day": 3}
SOURCE_RANK = {name: rank for rank, name in enumerate(
    ("crossref:retraction-watch", "crossref:publisher", "crossref", RW_SOURCE, "pubmed", "europe-pmc"))}
CROSSREF_ROWS = 100
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def make_client(workspace: Path | None = None) -> Any:
    """The scholarly client for status requests (no contact identity beyond OCSL_REVIEW_CONTACT_EMAIL)."""
    return followup_net.client(workspace=workspace)


def protocol_source(source: str) -> str:
    """The protocol's name of an observation source (Crossref's origins are one source)."""
    return "crossref" if source.startswith("crossref") else source


# ----------------------------------------------------------------------------- dates
def date_parts(value: Any) -> tuple[str | None, str | None]:
    """ISO date and precision (day, month or year) of Crossref ``date-parts``; (None, None) when absent or malformed."""
    parts = value.get("date-parts") if isinstance(value, Mapping) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None, None
    numbers: list[int] = []
    for part in parts[0][:3]:
        try:
            numbers.append(int(part))
        except (TypeError, ValueError):
            break
    return _iso(numbers)


def _iso(numbers: Sequence[int]) -> tuple[str | None, str | None]:
    if not numbers or not 1000 <= numbers[0] <= 9999:
        return None, None
    numbers = list(numbers[:3])
    if len(numbers) > 1 and not 1 <= numbers[1] <= 12:
        numbers = numbers[:1]
    if len(numbers) > 2:
        try:
            datetime(numbers[0], numbers[1], numbers[2])
        except ValueError:
            numbers = numbers[:2]
    text = f"{numbers[0]:04d}" + "".join(f"-{number:02d}" for number in numbers[1:])
    return text, ("year", "month", "day")[len(numbers) - 1]


def pubmed_date(value: Any) -> tuple[str | None, str | None]:
    """ISO date and precision of a PubMed ``pubdate`` such as ``2022 Mar 4``, ``2022 Mar`` or ``2022``."""
    match = re.match(r"^\s*(\d{4})(?:\s+([A-Za-z]{3})[A-Za-z]*(?:\s+(\d{1,2}))?)?\b", str(value or ""))
    if not match:
        return None, None
    numbers = [int(match.group(1))]
    if match.group(2) and match.group(2).lower() in MONTHS:
        numbers.append(MONTHS[match.group(2).lower()])
        if match.group(3):
            numbers.append(int(match.group(3)))
    return _iso(numbers)


def _key(date: str | None, precision: str | None) -> tuple[int, ...]:
    parts = tuple(int(part) for part in (date or "").split("-") if part)
    return parts[:PRECISION.get(precision or "", len(parts))]


def compare_dates(a: str | None, a_precision: str | None, b: str | None, b_precision: str | None) -> int | None:
    """-1, 0 or 1 comparing two dates at the coarser of their precisions; None when either is missing."""
    if not a or not b:
        return None
    left, right = _key(a, a_precision), _key(b, b_precision)
    depth = min(len(left), len(right))
    left, right = left[:depth], right[:depth]
    return (left > right) - (left < right)


def _notice_order(a: StatusObservation, b: StatusObservation) -> int:
    """Earliest-dated first (at the coarser precision), the finer date first on a tie, undated notices last."""
    order = compare_dates(a.notice_date, a.date_precision, b.notice_date, b.date_precision)
    if order is None:
        order = (not a.notice_date) - (not b.notice_date)
    if order:
        return order
    finer = PRECISION.get(b.date_precision or "", 0) - PRECISION.get(a.date_precision or "", 0)
    if finer:
        return finer
    left = (a.notice_date or "", SOURCE_RANK.get(a.source, 99), a.notice_doi or "", a.notice_pmid or "",
            a.observation_id)
    right = (b.notice_date or "", SOURCE_RANK.get(b.source, 99), b.notice_doi or "", b.notice_pmid or "",
             b.observation_id)
    return (left > right) - (left < right)


# ----------------------------------------------------------------------------- provider requests
def failure_class(error: BaseException) -> tuple[str, int | None, str]:
    """(receipt outcome, HTTP status, failure class) of a provider failure; never the response text."""
    message = str(error)
    match = re.search(r"provider HTTP (\d{3})", message)
    status = int(match.group(1)) if match else None
    if status == 404:
        return "not-found", status, "http-404"
    if status is not None:
        return ("unavailable" if status in (408, 425, 429) or status >= 500 else "error"), status, f"http-{status}"
    lowered = message.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "unavailable", None, "timeout"
    if "malformed" in lowered:
        return "error", None, "malformed-response"
    if "size bound" in lowered or "truncated" in lowered:
        return "error", None, "response-bound"
    return "unavailable", None, "request-failed"


@dataclass(frozen=True)
class Answer:
    """One provider response as an adapter sees it (the receipt is already written)."""
    ok: bool
    body: bytes | None
    http_status: int | None
    request_id: str
    sha256: str | None
    outcome: str
    error: str | None = None

    def json(self) -> Any:
        try:
            return json.loads((self.body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ProviderError("provider returned malformed JSON") from error


class Recorder:
    """Thread-safe receipts: every provider response goes through ``followup_net.record_request`` under one lock."""

    def __init__(self, connection: Any, lock: Any = None):
        self.connection = connection
        self.lock = lock or threading.RLock()
        self.counts: Counter = Counter()

    def __call__(self, **fields: Any) -> str:
        with self.lock:
            request_id = followup_net.record_request(self.connection, **fields)
            self.counts[(fields["provider"], fields["outcome"])] += 1
        return request_id

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = defaultdict(dict)
        for (provider, outcome), count in sorted(self.counts.items()):
            out[provider][outcome] = count
        return dict(out)


class ProviderSession:
    """Requests through the follow-up scholarly client: egress authorization, the scholarly transport (its User-Agent,
    bounds, retries and per-host spacing) and one receipt per response."""

    def __init__(self, scholarly: Any, recorder: Callable[..., str], *, operation: str = OPERATION):
        self.scholarly, self.recorder, self.operation = scholarly, recorder, operation

    @property
    def contact(self) -> str | None:
        return getattr(self.scholarly, "contact_email", None)

    def get(self, provider: str, url: str, *, params: Mapping[str, Any] | None = None,
            egress: Mapping[str, Any], headers: Mapping[str, str] | None = None, document: bool = False) -> Answer:
        self.scholarly._authorize(provider, self.operation, dict(egress))
        sent = {key: value for key, value in (params or {}).items() if value is not None}
        try:
            if document:
                target = url + ("?" + urllib.parse.urlencode(sent, doseq=True) if sent else "")
                response = self.scholarly.transport.get_document(target)
            else:
                response = self.scholarly.transport.get(url, params=sent or None, headers=dict(headers or {}) or None)
        except ProviderError as error:
            outcome, status, kind = failure_class(error)
            request_id = self.recorder(provider=provider, operation=self.operation, url=url, params=sent,
                                       outcome=outcome, http_status=status, error=kind)
            return Answer(False, None, status, request_id, None, outcome, kind)
        body = bytes(response.body or b"")
        request_id = self.recorder(provider=provider, operation=self.operation, url=url, params=sent, outcome="ok",
                                   http_status=int(response.status), body=body or None)
        return Answer(True, body, int(response.status), request_id, hashlib.sha256(body).hexdigest(), "ok")


def ncbi_params(session: ProviderSession, **params: Any) -> dict[str, Any]:
    """NCBI E-utilities parameters as the scholarly client sends them (tool name, the operator's contact and key)."""
    value = {**params, "tool": "ocsl_review"}
    if session.contact:
        value["email"] = session.contact
    if os.environ.get("NCBI_API_KEY"):
        value["api_key"] = os.environ["NCBI_API_KEY"]
    return value


def normal_doi(value: Any) -> str | None:
    from .scholarly import normalize_doi
    try:
        return normalize_doi(str(value)) if value else None
    except ReviewError:
        return None


def normal_pmid(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{1,10}", text) and text.strip("0") else None


# ----------------------------------------------------------------------------- sources
def _unknown(work: WorkRef, source: str, covers: Sequence[str], snapshot_id: str, reason: str,
             requests: Sequence[str] = (), retrieved_at: str | None = None) -> StatusObservation:
    return StatusObservation(work.work_key, source, "unknown", retrieved_at or now(), snapshot_id, tuple(covers),
                             detail={"reason": reason, **({"requests": list(requests)} if requests else {})})


def _none(work: WorkRef, source: str, covers: Sequence[str], snapshot_id: str, retrieved_at: str,
          requests: Sequence[str], receipt: str | None) -> StatusObservation:
    return StatusObservation(work.work_key, source, "none", retrieved_at, snapshot_id, tuple(covers),
                             receipt_sha256=receipt, detail={"requests": list(requests)})


class CrossrefStatus:
    """Crossref notices of a work: ``updated-by`` on the work and ``update-to`` of notices naming it."""

    name = "crossref"
    covers = NOTICE_KINDS

    def __init__(self, session: ProviderSession):
        self.session = session

    @staticmethod
    def _label(origin: Any) -> str:
        return f"crossref:{origin}" if origin in ("publisher", "retraction-watch") else "crossref"

    def observe(self, work: WorkRef, *, snapshot_id: str) -> list[StatusObservation]:
        from .integrity import NOTICE_KINDS as CROSSREF_TYPES
        doi = normal_doi(work.doi)
        if doi is None:
            return [_unknown(work, "crossref", self.covers, snapshot_id, "no-doi")]
        mailto = {"mailto": self.session.contact} if self.session.contact else {}
        record = self.session.get("crossref", f"{CROSSREF}/works/{urllib.parse.quote(doi, safe='')}",
                                  params=mailto or None, egress={"doi": doi})
        updates = self.session.get("crossref", f"{CROSSREF}/works",
                                   params={"filter": f"updates:{doi}", "rows": CROSSREF_ROWS, **mailto},
                                   egress={"doi": doi, "rows": CROSSREF_ROWS})
        retrieved = now()
        requests = [record.request_id, updates.request_id]
        found: dict[tuple, tuple[Mapping[str, Any], str, str | None, str]] = {}
        problems: list[str] = []

        def add(update: Mapping[str, Any], notice_doi: Any, receipt: str | None, via: str) -> None:
            kind = CROSSREF_TYPES.get(str(update.get("type", "")).strip().lower().replace(" ", "_"))
            if kind is None:
                return
            label = self._label(update.get("source"))
            date, precision = date_parts(update.get("updated"))
            notice = normal_doi(notice_doi) or (str(notice_doi).lower() if notice_doi else None)
            key = (label, kind, notice, date)
            if key not in found:
                found[key] = (update, kind, receipt, via)

        if record.ok:
            try:
                message = record.json().get("message")
                if not isinstance(message, dict) or normal_doi(message.get("DOI")) != doi:
                    raise ProviderError("Crossref returned an absent or mismatched DOI")
                for update in message.get("updated-by") or []:
                    if isinstance(update, Mapping):
                        add(update, update.get("DOI"), record.sha256, "updated-by")
            except (ProviderError, AttributeError):
                problems.append("work-record-malformed")
        else:
            problems.append(f"work-record-{record.error}")
        if updates.ok:
            try:
                items = (updates.json().get("message") or {}).get("items")
                if not isinstance(items, list):
                    raise ProviderError("Crossref returned no item list")
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    for update in item.get("update-to") or []:
                        if isinstance(update, Mapping) and normal_doi(update.get("DOI")) == doi:
                            add(update, item.get("DOI"), updates.sha256, "update-to")
            except (ProviderError, AttributeError):
                problems.append("updates-query-malformed")
        else:
            problems.append(f"updates-query-{updates.error}")
        if record.outcome == "not-found":
            return [_unknown(work, "crossref", self.covers, snapshot_id, "not-found", requests, retrieved)]
        if not found:
            if problems:
                return [_unknown(work, "crossref", self.covers, snapshot_id, ";".join(problems), requests, retrieved)]
            return [_none(work, "crossref", self.covers, snapshot_id, retrieved, requests, record.sha256)]
        observations = []
        ordered = sorted(found.items(), key=lambda item: tuple("" if part is None else part for part in item[0]))
        for (label, kind, notice, date), (update, _, receipt, via) in ordered:
            precision = date_parts(update.get("updated"))[1]
            detail: dict[str, Any] = {"requests": requests, "via": via, "label": update.get("label")}
            if update.get("record-id") is not None:
                detail["recordId"] = update.get("record-id")
            if problems:
                detail["partial"] = problems
            observations.append(StatusObservation(
                work.work_key, label, KIND_STATUS[kind], retrieved, snapshot_id, self.covers,
                notice_type=str(update.get("type") or ""), notice_doi=notice, notice_date=date,
                date_precision=precision, receipt_sha256=receipt, detail=detail))
        return observations


class PubMedStatus:
    """PubMed CommentsCorrections links of a work; notice dates only from the notice's own record."""

    name = "pubmed"
    covers = DECIDING_KINDS
    REFTYPES = {"RetractionIn": "retraction", "RetractedandRepublishedIn": "retraction",
                "ExpressionOfConcernIn": "expression-of-concern", "ErratumIn": "correction",
                "CorrectedandRepublishedIn": "correction"}

    def __init__(self, session: ProviderSession, *, notice_dates: bool = True):
        self.session, self.notice_dates = session, notice_dates

    def _notice_date(self, pmid: str) -> tuple[str | None, str | None, str | None]:
        answer = self.session.get("pubmed", f"{PUBMED}/esummary.fcgi",
                                  params=ncbi_params(self.session, db="pubmed", id=pmid, retmode="json"),
                                  egress={"pmid": pmid})
        if not answer.ok:
            return None, None, answer.request_id
        try:
            item = (answer.json().get("result") or {}).get(pmid) or {}
        except (ProviderError, AttributeError):
            return None, None, answer.request_id
        date, precision = pubmed_date(item.get("pubdate") or item.get("epubdate"))
        return date, precision, answer.request_id

    def observe(self, work: WorkRef, *, snapshot_id: str) -> list[StatusObservation]:
        from .scholarly import parse_pubmed_record
        pmid = normal_pmid(work.pmid)
        if pmid is None:
            return [_unknown(work, "pubmed", self.covers, snapshot_id, "no-pmid")]
        answer = self.session.get("pubmed", f"{PUBMED}/efetch.fcgi",
                                  params=ncbi_params(self.session, db="pubmed", id=pmid, retmode="xml"),
                                  egress={"pmid": pmid}, headers={"Accept": "application/xml"})
        retrieved = now()
        if not answer.ok:
            return [_unknown(work, "pubmed", self.covers, snapshot_id, answer.error or answer.outcome,
                             [answer.request_id], retrieved)]
        try:
            record = parse_pubmed_record(answer.body or b"", pmid)
        except ProviderError:
            return [_unknown(work, "pubmed", self.covers, snapshot_id, "malformed-response", [answer.request_id],
                             retrieved)]
        notices = [notice for notice in record.get("notices") or [] if notice.get("refType") in self.REFTYPES]
        if not notices:
            return [_none(work, "pubmed", self.covers, snapshot_id, retrieved, [answer.request_id], answer.sha256)]
        observations, seen = [], set()
        for notice in notices:
            kind = self.REFTYPES[notice["refType"]]
            notice_pmid = normal_pmid(notice.get("pmid"))
            if (kind, notice_pmid) in seen:
                continue
            seen.add((kind, notice_pmid))
            date = precision = None
            requests = [answer.request_id]
            if self.notice_dates and notice_pmid:
                date, precision, request = self._notice_date(notice_pmid)
                requests.append(request)
            observations.append(StatusObservation(
                work.work_key, "pubmed", KIND_STATUS[kind], retrieved, snapshot_id, self.covers,
                notice_type=notice["refType"], notice_pmid=notice_pmid, notice_date=date, date_precision=precision,
                receipt_sha256=answer.sha256,
                detail={"requests": requests, "noticeDate": "notice record" if date else "unavailable"}))
        return observations


class EuropePmcStatus:
    """The retraction flag of a work's Europe PMC core record (no notice, no date)."""

    name = "europe-pmc"
    covers = ("retraction",)

    def __init__(self, session: ProviderSession):
        self.session = session

    def observe(self, work: WorkRef, *, snapshot_id: str) -> list[StatusObservation]:
        doi, pmid = normal_doi(work.doi), normal_pmid(work.pmid)
        if doi:
            query, egress = f'DOI:"{doi}"', {"doi": doi}
        elif pmid:
            query, egress = f"EXT_ID:{pmid} AND SRC:MED", {"pmid": pmid}
        else:
            return [_unknown(work, "europe-pmc", self.covers, snapshot_id, "no-identifier")]
        answer = self.session.get("europe-pmc", f"{EUROPE_PMC}/search",
                                  params={"query": query, "pageSize": 5, "resultType": "core", "format": "json"},
                                  egress=egress)
        retrieved = now()
        if not answer.ok:
            return [_unknown(work, "europe-pmc", self.covers, snapshot_id, answer.error or answer.outcome,
                             [answer.request_id], retrieved)]
        try:
            results = (answer.json().get("resultList") or {}).get("result") or []
        except (ProviderError, AttributeError):
            return [_unknown(work, "europe-pmc", self.covers, snapshot_id, "malformed-response", [answer.request_id],
                             retrieved)]

        def matches(item: Any) -> bool:
            if not isinstance(item, Mapping):
                return False
            if doi:
                return normal_doi(item.get("doi")) == doi
            return normal_pmid(item.get("pmid") or (item.get("id") if item.get("source") == "MED" else None)) == pmid
        record = next((item for item in results if matches(item)), None)
        if record is None:
            return [_unknown(work, "europe-pmc", self.covers, snapshot_id, "no-record", [answer.request_id],
                             retrieved)]
        flag = record.get("isRetracted")
        if flag in ("Y", True):
            return [StatusObservation(work.work_key, "europe-pmc", "retracted", retrieved, snapshot_id, self.covers,
                                      notice_type="isRetracted", receipt_sha256=answer.sha256,
                                      detail={"requests": [answer.request_id], "record": record.get("id")})]
        if flag in ("N", False):
            return [_none(work, "europe-pmc", self.covers, snapshot_id, retrieved, [answer.request_id],
                          answer.sha256)]
        return [_unknown(work, "europe-pmc", self.covers, snapshot_id, "no-flag", [answer.request_id], retrieved)]


SOURCES: dict[str, Callable[[ProviderSession], Any]] = {
    "crossref": CrossrefStatus, "pubmed": PubMedStatus, "europe-pmc": EuropePmcStatus}


# ----------------------------------------------------------------------------- storage
def cited_works(connection: Any) -> list[WorkRef]:
    """Every distinct cited work of the parent census with the parent's identifiers, in work-key order."""
    rows = connection.execute(
        "SELECT k.work_key, w.doi, w.pmid, w.pmcid, w.title, w.year FROM "
        "(SELECT DISTINCT cited_work_key AS work_key FROM census.citations WHERE cited_work_key IS NOT NULL) k "
        "LEFT JOIN census.works w ON w.work_key=k.work_key ORDER BY k.work_key").fetchall()
    return [WorkRef(row[0], row[1], row[2], row[3], row[4], None if row[5] is None else str(row[5])) for row in rows]


def write_observations(connection: Any, observations: Iterable[StatusObservation]) -> int:
    rows = [observation.row() for observation in observations]
    before = connection.total_changes
    connection.executemany("INSERT OR IGNORE INTO fu_status_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return connection.total_changes - before


def snapshot_digest(connection: Any, snapshot_id: str) -> tuple[str, int]:
    """sha256 of the canonical observation rows of one snapshot (ordered by observation id) and their count."""
    value = hashlib.sha256()
    count = 0
    for row in connection.execute("SELECT * FROM fu_status_observations WHERE snapshot_id=? ORDER BY observation_id",
                                  (snapshot_id,)):
        value.update(canonical(list(row)).encode("utf-8") + b"\n")
        count += 1
    return value.hexdigest(), count


def _require_unfrozen(connection: Any, what: str) -> None:
    if connection.execute("SELECT COUNT(*) FROM fu_protocol_freeze").fetchone()[0]:
        raise ValidationError(f"the follow-up strata are frozen: {what} would change the status rows they were built "
                              "on; status rows are fixed once the protocol is frozen")


# ----------------------------------------------------------------------------- Retraction Watch snapshot
def import_retraction_watch(connection: Any, path: Path, declared_version: str, *,
                            protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Status observations from a local Retraction Watch CSV snapshot (no network), registered as a dataset version."""
    from .integrity import RetractionWatchDataset
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"the Retraction Watch snapshot is missing: {path}")
    if not declared_version or not str(declared_version).strip():
        raise ValidationError("give the snapshot's declared version")
    pinned = ((protocol or {}).get("status") or {})
    if protocol is not None and RW_SOURCE not in (pinned.get("sources") or []):
        raise ValidationError("the protocol lists no retraction-watch-csv status source")
    declared = (pinned.get("retractionWatchCsv") or {})
    if declared.get("declaredVersion") and declared["declaredVersion"] != declared_version:
        raise ValidationError("the snapshot's declared version differs from the protocol's "
                              f"({declared['declaredVersion']!r})")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if declared.get("sha256") and declared["sha256"] != sha:
        raise ValidationError("the snapshot's sha256 differs from the one the protocol pins")
    try:
        dataset = RetractionWatchDataset(path, declared_version=declared_version)
    except ValueError as error:
        raise ValidationError(f"not a Retraction Watch snapshot: {error}") from error
    if dataset.version.sha256 != sha:
        raise ValidationError("the snapshot changed while it was read")
    snapshot_id = register_dataset(connection, kind=RW_SOURCE, name="retraction-watch",
                                   declared_version=declared_version, sha256=sha, rows=dataset.version.rows,
                                   file_name=path.name,
                                   detail={"matchedBy": "OriginalPaperDOI (lower case) or OriginalPaperPubMedID"})
    retrieved = connection.execute("SELECT registered_at FROM fu_datasets WHERE dataset_id=?",
                                   (snapshot_id,)).fetchone()[0]
    observations: list[StatusObservation] = []
    counts: Counter = Counter()
    for work in cited_works(connection):
        doi, pmid = normal_doi(work.doi), normal_pmid(work.pmid)
        if not doi and not pmid:
            observations.append(_unknown(work, RW_SOURCE, NOTICE_KINDS, snapshot_id, "no-identifier",
                                         retrieved_at=retrieved))
            counts["unknown"] += 1
            continue
        notices = dataset.notices(doi=doi, pmid=pmid)
        if not notices:
            observations.append(StatusObservation(work.work_key, RW_SOURCE, "none", retrieved, snapshot_id,
                                                  NOTICE_KINDS, receipt_sha256=sha))
            counts["none"] += 1
            continue
        for notice in notices:
            status = KIND_STATUS[notice["kind"]]
            date = notice.get("date")
            observations.append(StatusObservation(
                work.work_key, RW_SOURCE, status, retrieved, snapshot_id, NOTICE_KINDS,
                notice_type=RW_LABEL[notice["kind"]], notice_doi=normal_doi(notice.get("noticeDoi")),
                notice_pmid=normal_pmid(notice.get("noticePmid")), notice_date=date,
                date_precision="day" if date else None, receipt_sha256=sha,
                detail={"recordId": notice.get("recordId"), "reasons": notice.get("reasons") or []}))
            counts[status] += 1
    written = write_observations(connection, observations)
    return {"snapshot": snapshot_id, "sha256": sha, "rows": dataset.version.rows, "declaredVersion": declared_version,
            "works": len({observation.work_key for observation in observations}), "observations": len(observations),
            "written": written, "statuses": dict(sorted(counts.items()))}


# ----------------------------------------------------------------------------- live status snapshot
def new_snapshot_id() -> str:
    return "status-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def protocol_live_sources(protocol: Mapping[str, Any] | None) -> list[str]:
    listed = ((protocol or {}).get("status") or {}).get("sources")
    if not listed:
        return list(LIVE_SOURCES)
    return [source for source in LIVE_SOURCES if source in listed]


def fetch_status(connection: Any, scholarly: Any, *, sources: Sequence[str], limit: int | None = None,
                 snapshot: str | None = None, lock: Any = None, commit_every: int = 200,
                 notice_dates: bool = True) -> dict[str, Any]:
    """One status snapshot: every cited work asked at each live source (one thread per source, so each host keeps
    its own interval). ``snapshot`` continues an earlier snapshot: pairs of work and source that already answered
    in it are skipped, failed ones are asked again."""
    unknown = sorted(set(sources) - set(SOURCES))
    if unknown or not sources:
        raise ValidationError("status sources are crossref, pubmed and europe-pmc" + (
            f" (unknown: {', '.join(unknown)})" if unknown else ""))
    if limit is not None and limit < 1:
        raise ValidationError("--limit must be positive")
    if snapshot is not None:
        if not re.fullmatch(r"status-\d{8}T\d{12}Z", snapshot):
            raise ValidationError("a status snapshot id looks like status-YYYYmmddTHHMMSSffffffZ")
        if not connection.execute("SELECT 1 FROM fu_status_observations WHERE snapshot_id=? LIMIT 1",
                                  (snapshot,)).fetchone():
            raise ValidationError(f"no observations belong to snapshot {snapshot}")
    snapshot_id = snapshot or new_snapshot_id()
    lock = lock or threading.RLock()
    recorder = Recorder(connection, lock)
    session = ProviderSession(scholarly, recorder)
    works = cited_works(connection)
    answered: set[tuple[str, str]] = set()
    if snapshot is not None:
        answered = {(row[0], protocol_source(row[1])) for row in connection.execute(
            "SELECT DISTINCT work_key, source FROM fu_status_observations WHERE snapshot_id=? AND status!='unknown'",
            (snapshot_id,))}
    queues: dict[str, list[WorkRef]] = {}
    for source in sources:
        pending = [work for work in works if (work.work_key, source) not in answered]
        queues[source] = pending[:limit] if limit else pending
    counts: dict[str, Counter] = {source: Counter() for source in sources}
    written = [0]
    errors: list[BaseException] = []
    stop = threading.Event()

    def run(source: str) -> None:
        adapter = (SOURCES[source](session, notice_dates=notice_dates) if source == "pubmed"
                   else SOURCES[source](session))
        since = 0
        for work in queues[source]:
            if stop.is_set():
                return
            observations = adapter.observe(work, snapshot_id=snapshot_id)
            with lock:
                written[0] += write_observations(connection, observations)
                counts[source].update(observation.status for observation in observations)
                since += 1
                if since >= commit_every:
                    connection.commit()
                    since = 0

    def guarded(source: str) -> None:
        try:
            run(source)
        except BaseException as error:  # noqa: BLE001 - raised again once every source thread stopped
            errors.append(error)
            stop.set()

    threads = [threading.Thread(target=guarded, args=(source,), name=f"followup-status-{source}", daemon=True)
               for source in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with lock:
        connection.commit()
    if errors:
        if isinstance(errors[0], ReviewError):
            raise errors[0]
        raise ReviewError(f"status fetch stopped: {type(errors[0]).__name__}: {errors[0]}") from errors[0]
    sha, rows = snapshot_digest(connection, snapshot_id)
    dataset = register_dataset(connection, kind="status-snapshot", name="status-observations",
                               declared_version=snapshot_id, sha256=sha, rows=rows,
                               detail={"sources": list(sources), "limit": limit, "continued": snapshot is not None})
    return {"snapshot": snapshot_id, "dataset": dataset, "continued": snapshot is not None, "sources": list(sources),
            "works": {source: len(queue) for source, queue in queues.items()}, "observationsWritten": written[0],
            "observations": {source: dict(sorted(count.items())) for source, count in counts.items()},
            "requests": recorder.summary(), "snapshotRows": rows}


# ----------------------------------------------------------------------------- status-resolution@1
def default_snapshots(connection: Any) -> list[str]:
    """The latest live status snapshot and the latest Retraction Watch snapshot import."""
    chosen = []
    live = connection.execute("SELECT MAX(snapshot_id) FROM fu_status_observations WHERE source!=? AND snapshot_id "
                              "LIKE 'status-%'", (RW_SOURCE,)).fetchone()[0]
    if live:
        chosen.append(live)
    imported = connection.execute("SELECT dataset_id FROM fu_datasets WHERE kind=? ORDER BY registered_at DESC, "
                                  "dataset_id DESC LIMIT 1", (RW_SOURCE,)).fetchone()
    if imported:
        chosen.append(imported[0])
    return chosen


def _observation(row: Mapping[str, Any]) -> StatusObservation:
    return StatusObservation(row["work_key"], row["source"], row["status"], row["retrieved_at"], row["snapshot_id"],
                             tuple(json.loads(row["covers_json"])), notice_type=row["notice_type"],
                             notice_doi=row["notice_doi"], notice_pmid=row["notice_pmid"],
                             notice_date=row["notice_date"], date_precision=row["date_precision"],
                             receipt_sha256=row["receipt_sha256"], detail=json.loads(row["detail_json"] or "{}"))


def resolve_work(work_key: str, observations: Sequence[StatusObservation], *, snapshot_id: str,
                 resolved_at: str) -> WorkStatus:
    """status-resolution@1 for one work (see the module docstring and docs/followup-evidence.md)."""
    by_kind: dict[str, list[StatusObservation]] = defaultdict(list)
    for observation in observations:
        if observation.status in STATUS_KIND:
            by_kind[STATUS_KIND[observation.status]].append(observation)
    reinstated = False
    deciding = None
    retractions = by_kind["retraction"]
    if retractions:
        dated = [item for item in retractions if item.notice_date]
        later = any(all(compare_dates(item.notice_date, item.date_precision, retraction.notice_date,
                                      retraction.date_precision) == 1 for retraction in dated)
                    for item in by_kind["reinstatement"] if item.notice_date) if dated else False
        if later:
            reinstated = True
        else:
            deciding = "retraction"
    if deciding is None:
        deciding = next((kind for kind in ("expression-of-concern", "correction") if by_kind[kind]), None)
    sources: dict[str, dict[str, Any]] = {}
    covered: set[str] = set()
    for observation in observations:
        entry = sources.setdefault(observation.source, {"source": observation.source, "answered": False,
                                                        "statuses": set(), "covers": set(), "retrievedAt": "",
                                                        "notices": []})
        entry["statuses"].add(observation.status)
        entry["retrievedAt"] = max(entry["retrievedAt"], observation.retrieved_at)
        if observation.status != "unknown":
            entry["answered"] = True
            entry["covers"].update(observation.covers)
            covered.update(observation.covers)
        if observation.status in STATUS_KIND:
            entry["notices"].append({"kind": STATUS_KIND[observation.status], "doi": observation.notice_doi,
                                     "pmid": observation.notice_pmid, "date": observation.notice_date,
                                     "precision": observation.date_precision})
    uncovered = sorted(set(DECIDING_KINDS) - covered)
    notice = None
    if deciding is not None:
        status = KIND_STATUS[deciding]
        notice = sorted(by_kind[deciding], key=cmp_to_key(_notice_order))[0]
    elif not uncovered:
        status = "none"
    else:
        status = "unknown"
    answering = [entry["retrievedAt"] for entry in sources.values() if entry["answered"]]
    checked = max(answering) if answering else max((entry["retrievedAt"] for entry in sources.values()),
                                                   default=resolved_at)
    listed = [{**entry, "statuses": sorted(entry["statuses"]), "covers": sorted(entry["covers"]),
               "notices": sorted(entry["notices"], key=lambda item: canonical(item))}
              for _, entry in sorted(sources.items(), key=lambda item: (SOURCE_RANK.get(item[0], 99), item[0]))]
    return WorkStatus(work_key, status, listed, checked, snapshot_id, STATUS_RULE, reinstated,
                      notice.notice_doi if notice else None, notice.notice_date if notice else None,
                      notice.date_precision if notice else None, notice.source if notice else None, uncovered)


def resolve_status(connection: Any, *, snapshots: Sequence[str] | None = None,
                   protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One fu_work_status row for every cited work of the parent (status-resolution@1); refused after a freeze."""
    _require_unfrozen(connection, "resolving the status again")
    chosen = sorted(set(snapshots or default_snapshots(connection)))
    for snapshot_id in chosen:
        if not connection.execute("SELECT 1 FROM fu_status_observations WHERE snapshot_id=? LIMIT 1",
                                  (snapshot_id,)).fetchone():
            raise ValidationError(f"no observations belong to snapshot {snapshot_id}")
    allowed = set(((protocol or {}).get("status") or {}).get("sources") or (*LIVE_SOURCES, RW_SOURCE))
    resolution = "+".join(chosen) if chosen else "no-snapshot"
    by_work: dict[str, list[StatusObservation]] = defaultdict(list)
    if chosen:
        marks = ",".join("?" for _ in chosen)
        for row in connection.execute(f"SELECT * FROM fu_status_observations WHERE snapshot_id IN ({marks}) "
                                      "ORDER BY work_key, observation_id", chosen):
            if protocol_source(row["source"]) in allowed:
                by_work[row["work_key"]].append(_observation(row))
    resolved_at = now()
    rows, counts, decided = [], Counter(), Counter()
    works = cited_works(connection)
    for work in works:
        result = resolve_work(work.work_key, by_work.get(work.work_key, []), snapshot_id=resolution,
                              resolved_at=resolved_at)
        counts[result.status] += 1
        if result.decided_by:
            decided[result.decided_by] += 1
        rows.append((result.work_key, result.status, int(result.reinstated), result.notice_doi, result.notice_date,
                     result.date_precision, result.decided_by, canonical(list(result.sources)),
                     canonical(list(result.unknown_kinds)), result.checked_at, result.snapshot_id,
                     result.rule_version, resolved_at))
    connection.executemany("INSERT OR REPLACE INTO fu_work_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    agreement = census_flag_agreement(connection)
    return {"rule": STATUS_RULE, "snapshots": chosen, "works": len(works), "statuses": dict(sorted(counts.items())),
            "reinstated": sum(row[2] for row in rows), "decidedBy": dict(sorted(decided.items())),
            "censusRetractedFlag": agreement}


def census_flag_agreement(connection: Any) -> dict[str, Any]:
    """Diagnostic: the parent's retraction flag (what the census judge was shown) against the resolved status."""
    pairs = connection.execute(
        "SELECT COALESCE(w.retracted, 0) AS flag, COALESCE(s.status, 'no-status-row') AS status, COUNT(*) FROM "
        "(SELECT DISTINCT cited_work_key AS work_key FROM census.citations WHERE cited_work_key IS NOT NULL) k "
        "LEFT JOIN census.works w ON w.work_key=k.work_key LEFT JOIN fu_work_status s ON s.work_key=k.work_key "
        "GROUP BY flag, status ORDER BY flag, status").fetchall()
    table = {f"{'flagged' if row[0] else 'unflagged'}:{row[1]}": row[2] for row in pairs}
    flagged = sum(count for key, count in table.items() if key.startswith("flagged:"))
    retracted = sum(count for key, count in table.items() if key.endswith(":retracted"))
    both = table.get("flagged:retracted", 0)
    return {"pairs": table, "flagged": flagged, "resolvedRetracted": retracted, "both": both,
            "flaggedNotRetracted": flagged - both, "retractedNotFlagged": retracted - both,
            "note": "a diagnostic only: the census flag is never a status source"}


# ----------------------------------------------------------------------------- commands
def cmd_import(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = branch.protocol()
    with branch.writing() as connection:
        return import_retraction_watch(connection, Path(args.csv), args.declared_version, protocol=protocol)


def cmd_fetch(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = branch.protocol()
    sources = list(dict.fromkeys(args.source)) if getattr(args, "source", None) else protocol_live_sources(protocol)
    if not sources:
        raise ValidationError("the protocol lists no live status source (crossref, pubmed, europe-pmc)")
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else None
    scholarly = make_client(workspace)
    with branch.writing() as connection:
        return fetch_status(connection, scholarly, sources=sources, limit=getattr(args, "limit", None),
                            snapshot=getattr(args, "snapshot", None))


def cmd_resolve(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = branch.protocol()
    with branch.writing() as connection:
        result = resolve_status(connection, snapshots=getattr(args, "snapshot", None) or None, protocol=protocol)
    report = branch.verify(deep=False)
    result["citedWorksWithoutStatusRow"] = report["citedWorksWithoutStatusRow"]
    return result
