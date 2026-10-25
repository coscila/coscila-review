# SPDX-License-Identifier: Apache-2.0
"""Reference integrity classes, kept separate from support verdicts.

Research-integrity tooling for authors, reviewers and study teams. Findings are mechanical
statements about the reference list and the registered record; they never speak about intent.
Adverse findings read "check suggested": there is no calibration set that would let any of
them read as established.

Classes
  listed-not-cited        a reference-list entry that no citation in the text points to
  cited-not-listed        a citation marker that matches no reference-list entry
  ambiguous-citation      a marker that matches several entries equally well
  identity-unresolved     the cited work cannot be identified; support stays "unsure", never "unsupported"
  doi-not-registered      the handle service, the doi.org resolver and Crossref all answered 404
  doi-check-inconclusive  access denied (401/403), rate limited (429), server or network failure
  doi-endpoint-artefact   one endpoint refused a DOI that another resolves (informational)
  metadata-disagreement   the printed entry differs from the registered record (different work,
                          first author absent or spelled differently, year, volume, first page)
  impossible-year         the printed year is later than the manuscript's own date
  duplicate-identity      three or more entries point to one work
  repeated-entry          two entries point to one work
  retracted / expression-of-concern / corrected / reinstated
                          a dated notice with its source; an unknown status stays unknown
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

from .author_year import name_key, parse_reference

INTEGRITY_VERSION = "integrity-1"
ADVERSE = "check suggested"
INFO = "info"
HANDLE_API = "https://doi.org/api/handles/"
DOI_PROXY = "https://doi.org/"
CROSSREF_WORKS = "https://api.crossref.org/works/"
REGISTRY_HOSTS = frozenset({"doi.org", "api.crossref.org"})
MAX_REGISTRY_BYTES = 256 * 1024
NOTICE_KINDS = {
    "retraction": "retraction", "withdrawal": "retraction", "removal": "retraction",
    "partial_retraction": "retraction", "partial-retraction": "retraction",
    "expression_of_concern": "expression-of-concern", "expression-of-concern": "expression-of-concern",
    "correction": "correction", "erratum": "correction", "corrigendum": "correction",
    "reinstatement": "reinstatement",
}
_RW_NATURE = {"retraction": "retraction", "expression of concern": "expression-of-concern",
              "correction": "correction", "reinstatement": "reinstatement"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def finding(finding_class: str, subject_kind: str, subject_id: str, *, severity: str = ADVERSE,
            detail: Mapping[str, Any] | None = None, key: Any = None,
            dataset_version: str | None = None) -> dict[str, Any]:
    """One integrity finding with an id that is stable across runs (for screen --diff)."""

    stable = {"class": finding_class, "subjectKind": subject_kind, "subjectId": subject_id, "key": key}
    return {"findingId": "finding-" + _digest(stable)[:24], "class": finding_class, "subjectKind": subject_kind,
            "subjectId": subject_id, "severity": severity, "detail": dict(detail or {}),
            "datasetVersion": dataset_version, "version": INTEGRITY_VERSION}


# Local classes ------------------------------------------------------------------------------

def local_findings(manuscript: Mapping[str, Any], identities: Mapping[str, Mapping[str, Any]] | None = None
                   ) -> list[dict[str, Any]]:
    """Classes that need no network: citation-list reconciliation and identity collisions."""

    identities = identities or {}
    entries = {entry["id"]: entry for entry in manuscript.get("bibliographyEntries", [])}
    cited: dict[str, list[str]] = defaultdict(list)
    findings: list[dict[str, Any]] = []
    for cluster in manuscript.get("citationClusters", []):
        for reference_id in cluster.get("referenceIds", []):
            cited[reference_id].append(cluster["id"])
        marker = cluster.get("authorYear")
        if marker:
            for position, item in enumerate(marker["items"]):
                if item["status"] == "unresolved":
                    findings.append(finding("cited-not-listed", "citation", f"{cluster['id']}#{position}", detail={
                        "marker": cluster["raw"], "item": item["text"], "authors": item["authors"],
                        "year": item["year"] or item["special"], "nearMatches": item.get("nearMatches", [])},
                        key=[item["authors"], item["year"], item["suffix"]]))
                elif item["status"] == "ambiguous":
                    findings.append(finding("ambiguous-citation", "citation", f"{cluster['id']}#{position}", detail={
                        "marker": cluster["raw"], "item": item["text"], "candidates": item["candidates"]},
                        key=[item["authors"], item["year"], item["suffix"]]))
        elif cluster.get("unresolvedMarkers") and cluster.get("style") in {"numeric", "structured"}:
            for marker_text in cluster["unresolvedMarkers"]:
                findings.append(finding("cited-not-listed", "citation", f"{cluster['id']}:{marker_text}", detail={
                    "marker": cluster["raw"], "label": marker_text}, key=[marker_text]))
    for entry_id, entry in entries.items():
        if entry_id not in cited:
            findings.append(finding("listed-not-cited", "entry", entry_id, detail={
                "label": entry.get("label"), "reference": entry.get("raw", "")[:300]}, key=[entry.get("raw", "")]))
    by_work: dict[str, list[str]] = defaultdict(list)
    for entry_id, identity in identities.items():
        if identity.get("workId"):
            by_work[identity["workId"]].append(entry_id)
        elif entry_id in entries:
            findings.append(finding("identity-unresolved", "entry", entry_id, detail={
                "state": identity.get("state", "unresolved"), "method": identity.get("method", ""),
                "candidates": identity.get("candidates", []),
                "consequence": "support verdicts for this entry stay unsure"}, key=[entries[entry_id].get("raw", "")]))
    for work_id, members in sorted(by_work.items()):
        if len(members) >= 3:
            findings.append(finding("duplicate-identity", "work", work_id, detail={"entries": sorted(members)},
                                    key=sorted(entries[m].get("raw", "") for m in members if m in entries)))
        elif len(members) == 2:
            findings.append(finding("repeated-entry", "work", work_id, severity=INFO, detail={"entries": sorted(members)},
                                    key=sorted(entries[m].get("raw", "") for m in members if m in entries)))
    return findings


# DOI registration ---------------------------------------------------------------------------

class RegistryTransport(Protocol):
    def __call__(self, method: str, url: str) -> tuple[int, Mapping[str, str], bytes]:
        """Return (status, headers, body); status -1 means a network failure. Never follow redirects."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_registry_transport(*, timeout: float = 20.0, user_agent: str = "ocsl-review/0.1 (reference integrity check)"
                              ) -> RegistryTransport:
    """Bounded public HTTPS requests to the registry hosts only; redirects are reported, not followed."""

    opener = urllib.request.build_opener(_NoRedirect())

    def fetch(method: str, url: str) -> tuple[int, Mapping[str, str], bytes]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or (parsed.hostname or "") not in REGISTRY_HOSTS:
            raise ValueError(f"registry transport refuses {parsed.hostname!r}")
        request = urllib.request.Request(url, method=method, headers={"User-Agent": user_agent,
                                                                      "Accept": "application/json"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return int(response.status), dict(response.headers.items()), response.read(MAX_REGISTRY_BYTES)
        except urllib.error.HTTPError as error:
            try:
                body = error.read(MAX_REGISTRY_BYTES)
            finally:
                error.close()
            return int(error.code), dict(error.headers.items()) if error.headers else {}, body
        except (urllib.error.URLError, TimeoutError, OSError):
            return -1, {}, b""

    return fetch


def _outcome(status: int) -> str:
    if status in (200, 301, 302, 303, 307, 308):
        return "found"
    if status in (404, 410):
        return "not-found"
    if status in (401, 403):
        return "denied"
    if status == 429:
        return "rate-limited"
    if status >= 500:
        return "server-error"
    if status < 0:
        return "network-error"
    return "other"


class RegistryCheck:
    """Three independent opinions before a DOI can be called unregistered.

    A refusal from one endpoint is not evidence that a citation is invented: the handle service
    (percent-encoded, because older DOIs contain characters that break a raw request), the
    doi.org resolver without following redirects, and Crossref must all answer 404. Access
    denied, rate limits and failures make the check inconclusive and never "missing".
    """

    def __init__(self, transport: RegistryTransport | None = None, *,
                 authorize: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
                 min_interval: float = 0.15, sleep: Callable[[float], None] = time.sleep):
        self.transport = transport or urllib_registry_transport()
        self.authorize = authorize
        self.min_interval = max(0.0, float(min_interval))
        self.sleep = sleep
        self._last = 0.0

    def _get(self, method: str, url: str) -> tuple[int, Mapping[str, str], bytes]:
        if self.min_interval:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
        return self.transport(method, url)

    def check(self, doi: str) -> dict[str, Any]:
        doi = doi.strip()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi, re.I):
            return {"doi": doi, "verdict": "malformed", "endpoints": {}, "checkedAt": datetime.now(UTC).isoformat()}
        if self.authorize is not None:
            self.authorize("doi.org", "doi_registry_check", {"doi": doi})
        encoded = urllib.parse.quote(doi, safe="/:;=@+,()")
        endpoints: dict[str, dict[str, Any]] = {}
        status, _, body = self._get("GET", HANDLE_API + encoded)
        code = None
        try:
            code = json.loads(body.decode("utf-8")).get("responseCode") if body else None
        except (UnicodeDecodeError, ValueError, AttributeError):
            code = None
        handle = _outcome(status)
        if status == 200 and code not in (1, None):
            handle = "not-found"
        endpoints["handle"] = {"status": status, "responseCode": code, "outcome": handle}
        status, headers, _ = self._get("HEAD", DOI_PROXY + encoded)
        endpoints["resolver"] = {"status": status, "outcome": _outcome(status),
                                 "location": headers.get("Location") or headers.get("location")}
        status, _, _ = self._get("HEAD", CROSSREF_WORKS + encoded)
        endpoints["crossref"] = {"status": status, "outcome": _outcome(status)}
        outcomes = {name: value["outcome"] for name, value in endpoints.items()}
        if any(value == "found" for value in outcomes.values()):
            verdict = "registered-endpoint-artefact" if any(v == "not-found" for v in outcomes.values()) else "registered"
        elif all(value == "not-found" for value in outcomes.values()):
            verdict = "not-registered"
        else:
            verdict = "inconclusive"
        return {"doi": doi, "verdict": verdict, "endpoints": endpoints, "checkedAt": datetime.now(UTC).isoformat()}


def registry_findings(entry_id: str, result: Mapping[str, Any]) -> list[dict[str, Any]]:
    verdict = result.get("verdict")
    detail = {"doi": result.get("doi"), "endpoints": result.get("endpoints", {})}
    if verdict == "not-registered":
        return [finding("doi-not-registered", "entry", entry_id, detail=detail, key=[result.get("doi")])]
    if verdict == "malformed":
        return [finding("doi-not-registered", "entry", entry_id, detail={**detail, "reason": "fails DOI syntax"},
                        key=[result.get("doi")])]
    if verdict == "inconclusive":
        reasons = sorted({value["outcome"] for value in result.get("endpoints", {}).values() if value["outcome"] != "not-found"})
        return [finding("doi-check-inconclusive", "entry", entry_id, severity=INFO,
                        detail={**detail, "reasons": reasons, "note": "never read as missing"}, key=[result.get("doi")])]
    if verdict == "registered-endpoint-artefact":
        return [finding("doi-endpoint-artefact", "entry", entry_id, severity=INFO, detail=detail, key=[result.get("doi")])]
    return []


# Publication status -------------------------------------------------------------------------

def _date_parts(value: Any) -> str | None:
    if isinstance(value, Mapping):
        parts = value.get("date-parts") or []
        if parts and parts[0]:
            numbers = [int(part) for part in parts[0][:3] if str(part).isdigit()]
            if numbers:
                return "-".join(f"{number:04d}" if index == 0 else f"{number:02d}" for index, number in enumerate(numbers))
    return None


def _date_text(value: str) -> str | None:
    value = (value or "").strip()
    for pattern in ("%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    return "-".join(match.groups()) if match else None


def crossref_notices(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Dated notices that update this work, from Crossref "updated-by" relations."""

    if not record:
        return []
    raw = record.get("rawMetadata") if isinstance(record.get("rawMetadata"), Mapping) else record
    notices = []
    for update in raw.get("updated-by") or []:
        if not isinstance(update, Mapping):
            continue
        kind = NOTICE_KINDS.get(str(update.get("type", "")).lower())
        if kind is None:
            continue
        notices.append({"kind": kind, "date": _date_parts(update.get("updated")), "noticeDoi": update.get("DOI"),
                        "source": "crossref" + (f":{update['source']}" if update.get("source") else ""),
                        "label": update.get("label")})
    return notices


@dataclass(frozen=True)
class DatasetVersion:
    name: str
    declared_version: str
    sha256: str
    rows: int
    path_name: str

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.declared_version}#{self.sha256[:12]}"

    def record(self) -> dict[str, Any]:
        return {"id": self.identifier, "name": self.name, "declaredVersion": self.declared_version,
                "sha256": self.sha256, "rows": self.rows, "file": self.path_name}


class RetractionWatchDataset:
    """An offline copy of the Retraction Watch data (as distributed through Crossref), pinned by hash."""

    def __init__(self, path: Path, *, declared_version: str | None = None):
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        required = {"OriginalPaperDOI", "RetractionNature", "RetractionDate"}
        if not reader.fieldnames or not required <= set(reader.fieldnames):
            raise ValueError("Retraction Watch file lacks OriginalPaperDOI, RetractionNature or RetractionDate")
        self.by_doi: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.by_pmid: dict[str, list[dict[str, Any]]] = defaultdict(list)
        rows = 0
        for row in reader:
            rows += 1
            kind = _RW_NATURE.get((row.get("RetractionNature") or "").strip().lower())
            if kind is None:
                continue
            notice = {"kind": kind, "date": _date_text(row.get("RetractionDate", "")),
                      "noticeDoi": (row.get("RetractionDOI") or "").strip() or None,
                      "noticePmid": (row.get("RetractionPubMedID") or "").strip() or None,
                      "recordId": (row.get("Record ID") or "").strip() or None,
                      "reasons": [part.strip(" +") for part in (row.get("Reason") or "").split(";") if part.strip(" +")]}
            doi = (row.get("OriginalPaperDOI") or "").strip().lower()
            if doi and doi not in {"unavailable", "na", "n/a"}:
                self.by_doi[doi].append(notice)
            pmid = (row.get("OriginalPaperPubMedID") or "").strip()
            if pmid.isdigit() and pmid != "0":
                self.by_pmid[pmid].append(notice)
        self.version = DatasetVersion("retraction-watch", declared_version or Path(path).stem, digest, rows, Path(path).name)

    def notices(self, *, doi: str | None = None, pmid: str | None = None) -> list[dict[str, Any]]:
        found = list(self.by_doi.get((doi or "").strip().lower(), [])) + list(self.by_pmid.get((pmid or "").strip(), []))
        unique, seen = [], set()
        for notice in found:
            key = (notice["kind"], notice["date"], notice["noticeDoi"], notice["recordId"])
            if key not in seen:
                seen.add(key)
                unique.append({**notice, "source": "retraction-watch", "sourceVersion": self.version.identifier})
        return unique


def combine_status(*, scholarly: Mapping[str, Any] | None = None, notices: Iterable[Mapping[str, Any]] = (),
                   datasets: Sequence[DatasetVersion] = (), dataset_checked: bool = False,
                   checked_sources: Sequence[str] = ()) -> dict[str, Any]:
    """Dated status from every source; with no successful source the state stays unknown."""

    collected = [dict(notice) for notice in notices]
    sources = ([version.identifier for version in datasets] if dataset_checked else []) + list(checked_sources)
    limitations: list[str] = []
    scholarly_state = None
    if scholarly is not None:
        scholarly_state = scholarly.get("state")
        limitations.extend(str(item) for item in scholarly.get("limitations", []))
        if scholarly_state not in (None, "unknown"):
            sources.append("scholarly:" + ",".join(scholarly.get("providersChecked", [])))
        for signal in scholarly.get("signals", []):
            kind = {"retraction": "retraction", "expression-of-concern": "expression-of-concern",
                    "correction": "correction"}.get(signal.get("kind"))
            if kind and not any(notice["kind"] == kind for notice in collected):
                detail = signal.get("detail") if isinstance(signal.get("detail"), Mapping) else {}
                collected.append({"kind": kind, "date": None, "noticePmid": detail.get("pmid"),
                                  "source": f"scholarly:{signal.get('provider')}", "relationship": signal.get("relationship")})
    collected.sort(key=lambda notice: (notice.get("date") or "9999", notice["kind"]))
    retractions = [n for n in collected if n["kind"] == "retraction"]
    reinstated = [n for n in collected if n["kind"] == "reinstatement"]
    latest_retraction = max((n.get("date") or "" for n in retractions), default=None)
    latest_reinstatement = max((n.get("date") or "" for n in reinstated), default=None)
    if retractions and not (latest_reinstatement and latest_retraction and latest_reinstatement > latest_retraction):
        state = "retracted"
    elif retractions:
        state = "reinstated"
    elif any(n["kind"] == "expression-of-concern" for n in collected):
        state = "expression-of-concern"
    elif any(n["kind"] == "correction" for n in collected):
        state = "corrected"
    elif sources:
        # Only the sources listed were checked; this is not a guarantee of currency.
        state = "no-notice-found"
    else:
        state = "unknown"
    return {"state": state, "notices": collected, "sources": sources, "scholarlyState": scholarly_state,
            "limitations": limitations, "checkedAt": datetime.now(UTC).isoformat()}


def status_findings(entry_id: str, status: Mapping[str, Any], *, dataset_version: str | None = None) -> list[dict[str, Any]]:
    state = status.get("state")
    if state not in {"retracted", "expression-of-concern", "corrected", "reinstated"}:
        return []
    kind = {"retracted": "retraction", "expression-of-concern": "expression-of-concern",
            "corrected": "correction", "reinstated": "reinstatement"}[state]
    dated = [notice for notice in status.get("notices", []) if notice["kind"] == kind]
    return [finding(state, "entry", entry_id, severity=INFO if state == "reinstated" else ADVERSE, detail={
        "notices": dated, "sources": status.get("sources", [])},
        key=[state, sorted((n.get("date") or "", n.get("noticeDoi") or "") for n in dated)],
        dataset_version=dataset_version)]


# Metadata agreement -------------------------------------------------------------------------

def _words(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", name_key_text(value))


def name_key_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def title_coverage(title: str, printed: str) -> float | None:
    """Share of the registered title's words that occur, in order, in the printed entry."""

    title_words = [word for word in _words(title) if len(word) > 2]
    printed_words = _words(printed)
    if len(title_words) < 3 or not printed_words:
        return None
    matcher = SequenceMatcher(None, title_words, printed_words, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(title_words)


_VOLUME_PAGES = re.compile(
    r"(?<![\w.])(?P<volume>\d{1,4})\s*(?:\((?P<issue>[^()]{1,12})\))?\s*[,:]\s*(?:pp?\.\s*)?(?P<first>[A-Za-z]?\d{1,6})"
    r"(?:\s*[-–]\s*[A-Za-z]?\d{1,6})?(?![\w/])")


def printed_volume_page(printed: str) -> tuple[str | None, str | None]:
    """Volume and first page when the entry prints them unambiguously, else (None, None)."""

    text = re.split(r"\b(?:doi\s*:|https?://|PMID)", printed, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\((?:19|20)\d{2}[a-z]?\)|(?<!\d)(?:19|20)\d{2}[a-z]?\s*;", " ", text)
    matches = [match for match in _VOLUME_PAGES.finditer(text) if not re.fullmatch(r"(?:19|20)\d{2}", match.group("volume"))]
    if len(matches) != 1:
        return None, None
    return matches[0].group("volume"), matches[0].group("first")


def metadata_findings(entry_id: str, printed: str, record: Mapping[str, Any], *,
                      reference_year: int | None = None) -> list[dict[str, Any]]:
    """Compare the printed entry with the registered record reached through its identifier."""

    findings: list[dict[str, Any]] = []
    parsed = parse_reference(printed)
    differences: list[dict[str, Any]] = []
    coverage = title_coverage(str(record.get("title") or ""), printed)
    if coverage is not None and coverage < 0.5:
        differences.append({"field": "work", "printed": printed[:200], "registered": record.get("title"),
                            "titleCoverage": round(coverage, 3), "note": "the identifier resolves to a different work"})
    authors = [str(author) for author in record.get("authors") or [] if author]
    if parsed.names and authors and not parsed.corporate:
        printed_key = name_key(parsed.names[0])
        registered_keys = [name_key(token) for author in authors for token in re.split(r"[\s,]+", author) if token]
        if printed_key and printed_key not in registered_keys:
            best = max((SequenceMatcher(None, printed_key, key).ratio() for key in registered_keys if len(key) > 2), default=0.0)
            differences.append({"field": "first-author", "printed": parsed.names[0], "registered": authors[:6],
                                "note": "near spelling variant" if best >= 0.86 else "absent from the registered author list",
                                "similarity": round(best, 3)})
    registered_year = record.get("year")
    if parsed.year and registered_year and abs(int(parsed.year) - int(registered_year)) > 1:
        differences.append({"field": "year", "printed": parsed.year, "registered": registered_year})
    volume, first_page = printed_volume_page(printed)
    registered_volume = str(record.get("volume") or "").strip()
    registered_page = re.split(r"[-–]", str(record.get("pages") or "").strip())[0].strip()
    if volume and registered_volume and volume != registered_volume:
        differences.append({"field": "volume", "printed": volume, "registered": registered_volume})
    if first_page and registered_page and first_page.lstrip("0") != registered_page.lstrip("0"):
        differences.append({"field": "first-page", "printed": first_page, "registered": registered_page})
    if differences:
        findings.append(finding("metadata-disagreement", "entry", entry_id, detail={
            "differences": differences, "registeredDoi": record.get("doi"), "provider": record.get("provider")},
            key=sorted(item["field"] for item in differences)))
    if parsed.year and reference_year and int(parsed.year) > int(reference_year):
        findings.append(finding("impossible-year", "entry", entry_id, detail={
            "printedYear": parsed.year, "referenceDate": reference_year}, key=[parsed.year]))
    return findings


def crossref_record(scholarly_result: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    for record in (scholarly_result or {}).get("records", []):
        if record.get("provider") == "crossref":
            return record
    return None


class IntegrityChecker:
    """Run the networked classes for one reference list through injected, policy-gated clients."""

    def __init__(self, *, registry: RegistryCheck | None = None, scholarly: Any = None,
                 datasets: Sequence[RetractionWatchDataset] = (), reference_year: int | None = None):
        self.registry = registry
        self.scholarly = scholarly
        self.datasets = list(datasets)
        self.reference_year = reference_year or date.today().year

    def check_entry(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        identifiers = entry.get("identifiers") or {}
        dois = [value for value in identifiers.get("doi", []) if value]
        pmids = [value for value in identifiers.get("pmid", []) if value]
        doi = dois[0].rstrip(".,;") if dois else None
        pmid = pmids[0] if pmids else None
        result: dict[str, Any] = {"entryId": entry["id"], "doi": doi, "pmid": pmid, "findings": []}
        parsed_year = parse_reference(entry.get("raw", "")).year
        if parsed_year and int(parsed_year) > self.reference_year:
            result["findings"].append(finding("impossible-year", "entry", entry["id"], detail={
                "printedYear": parsed_year, "referenceDate": self.reference_year}, key=[parsed_year]))
        if doi and self.registry is not None:
            registry = self.registry.check(doi)
            result["registry"] = registry
            result["findings"].extend(registry_findings(entry["id"], registry))
            if registry["verdict"] in {"not-registered", "malformed"}:
                return result
        scholarly_result = None
        if self.scholarly is not None and (doi or pmid):
            try:
                scholarly_result = self.scholarly.get_publication_status(doi=doi) if doi else \
                    self.scholarly.get_publication_status(pmid=pmid)
            except Exception as error:  # noqa: BLE001 - a provider failure leaves the status unknown
                scholarly_result = None
                result["statusError"] = f"{type(error).__name__}: {error}"
            result["scholarly"] = scholarly_result
        record = crossref_record(scholarly_result)
        notices = crossref_notices(record)
        dataset_checked = bool(self.datasets) and bool(doi or pmid)
        for dataset in self.datasets:
            notices.extend(dataset.notices(doi=doi, pmid=pmid))
        if scholarly_result is not None or dataset_checked:
            # A retrieved Crossref record is itself a checked source: its update relations were read.
            status = combine_status(scholarly=scholarly_result, notices=notices,
                                    datasets=[dataset.version for dataset in self.datasets], dataset_checked=dataset_checked,
                                    checked_sources=["crossref-relations"] if record is not None else [])
        else:
            status = {"state": "unknown", "notices": [], "sources": [], "scholarlyState": None,
                      "limitations": ["no status source was checked"], "checkedAt": datetime.now(UTC).isoformat()}
        result["status"] = status
        version = ",".join(dataset.version.identifier for dataset in self.datasets) or None
        result["findings"].extend(status_findings(entry["id"], status, dataset_version=version))
        if record is not None:
            result["findings"].extend(metadata_findings(entry["id"], entry.get("raw", ""), record))
        return result


__all__ = [
    "ADVERSE", "DatasetVersion", "INFO", "INTEGRITY_VERSION", "IntegrityChecker", "RegistryCheck",
    "RetractionWatchDataset", "combine_status", "crossref_notices", "crossref_record", "finding",
    "local_findings", "metadata_findings", "printed_volume_page", "registry_findings", "status_findings",
    "title_coverage", "urllib_registry_transport",
]
