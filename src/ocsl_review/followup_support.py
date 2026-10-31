# SPDX-License-Identifier: Apache-2.0
"""citation-support@2, unchanged, on the loci of the frozen strata at the abstract or the full-text tier.

Evidence per cited work, stored once in the branch (``fu_evidence``, ``fu_evidence_units``):

- abstract: the study's byte caches first (``census_inputs.InputCaches``: a pinned literature-evidence@2 store and a
  provider response cache), then Europe PMC ``resultType=core`` live; only a complete structured abstract counts
  (``evidence.complete_abstract``), otherwise the unit is unassessable with reason ``abstract-incomplete``;
- full text: the caches, then Europe PMC ``fullTextXML`` for open-access PMCIDs (``--europe-pmc``, through the scholarly
  transport's document route), then the study's entitled documents when it supplies them (provenance ``entitled``,
  on-premises judges only). The JATS is split into abstract, section paragraphs, tables and figure captions.

Each locus becomes one citation-support@2 task exactly as ``corpus_assessment.TaskPlanner`` expects (id
``<locus id>|<tier>``, the citing sentence as the claim, neighbouring sentences as context when the census's review
documents are given, the cited work's title and year, the evidence units, completeness and the source digest), with
its provenance class beside it (never sent). The evidence window is ``claim_check.select_evidence`` over a retrieval
index of the work (claim-check@1's k and window size); every text passes the sanitizer before it enters a prompt.
Items run through ``followup_judge.run_items`` one per call. ``fu_support`` gets one row per unit and tier: the
verdict, or why the unit is not assessable.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ValidationError
from .followup_branch import FollowupBranch, now, require_freeze
from .followup_function import (ReviewDocuments, block_new, check_model, citation_target, cited_work, close_judge,
                                judge_summary, judgment_state, prepare_judge, provenance_of, reason_of, resolve_marker,
                                sanitize, select_units, sentence_targets)
from .followup_model import canonical, digest
from .followup_status import ProviderSession, Recorder, normal_doi, normal_pmid

INSTRUMENT_ID = "citation-support@2"
TIERS = ("abstract", "full-text")
TIER_STRATA = {"abstract": ("S0", "S1", "S2", "S3", "co-cited"), "full-text": ("S0", "S1", "S2", "S3", "co-cited")}
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
SEARCH = EUROPE_PMC + "/search"
FULL_TEXT = EUROPE_PMC + "/{pmcid}/fullTextXML"
OPERATION = "followup_support"
ENTITLED_FORMAT = "ocsl-followup-entitled-v1"
EVIDENCE_REASONS = {
    "abstract": {"incomplete": "abstract-incomplete", "unavailable": "abstract-incomplete",
                 "no-open-full-text": "abstract-incomplete", "fetch-failed": "fetch-failed"},
    "full-text": {"no-open-full-text": "no-open-full-text", "unavailable": "no-open-full-text",
                  "incomplete": "no-open-full-text", "fetch-failed": "fetch-failed"},
}


def make_client(workspace: Path | None = None) -> Any:
    from . import followup_net
    return followup_net.client(workspace=workspace)


def retrieval_parameters() -> tuple[int, int]:
    """k and the evidence window in characters, as claim-check@1 declares them."""
    from . import instruments
    retrieval = instruments.load("claim-check@1").tier("full-text")["evidencePolicy"]["retrieval"]
    return int(retrieval["k"]), int(retrieval["maxEvidenceCharacters"])


def normal_pmcid(value: Any) -> str | None:
    match = re.fullmatch(r"(?:PMC)?(\d{1,12})", str(value or "").strip(), re.I)
    return "PMC" + match.group(1) if match else None


# ----------------------------------------------------------------------------- evidence
@dataclass
class Evidence:
    """The evidence of one cited work at one tier, or why there is none."""
    work_key: str
    unit: str
    outcome: str
    provider: str | None = None
    route: str | None = None
    provenance_class: str | None = None
    source_sha256: str | None = None
    units: list[dict[str, Any]] = field(default_factory=list)
    complete: bool | None = None
    request_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    retrieved_at: str = field(default_factory=now)
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id:
            basis = self.source_sha256 if self.outcome == "obtained" else f"{self.outcome}@{self.retrieved_at}"
            self.evidence_id = "evidence-" + digest(self.work_key, self.unit, self.provider or "", basis)[:32]

    @property
    def obtained(self) -> bool:
        return self.outcome == "obtained"


def jats_units(body: bytes) -> list[dict[str, Any]]:
    """Evidence units of a JATS article: abstract paragraphs, section paragraphs, tables and figure captions."""
    if re.search(br"<!ENTITY\b", body, re.I):
        raise ValidationError("full-text XML declares entities")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ValidationError("full-text XML is malformed") from error
    for node in root.iter():
        if isinstance(node.tag, str):
            node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag == "pmc-articleset":
        articles = root.findall("article")
        if len(articles) != 1:
            raise ValidationError("a PMC article set needs exactly one article")
        root = articles[0]
    if root.tag != "article" or root.find("body") is None:
        raise ValidationError("full-text XML needs an article body")
    units: list[dict[str, Any]] = []

    def text(node: ET.Element | None) -> str:
        return " ".join("".join(node.itertext()).split()) if node is not None else ""

    def add(kind: str, locator: str, value: str) -> None:
        if value:
            units.append({"unit_id": f"u{len(units) + 1:04d}", "kind": kind, "locator": locator, "text": value})

    for abstract in root.findall("./front/article-meta/abstract"):
        if abstract.get("abstract-type") in ("graphical", "teaser"):
            continue
        for paragraph in abstract.findall(".//p") or [abstract]:
            add("abstract", "Abstract", text(paragraph))

    def walk(node: ET.Element, trail: list[str]) -> None:
        for child in node:
            if child.tag == "sec":
                title = text(child.find("title"))
                walk(child, trail + [title] if title else trail)
            elif child.tag == "p":
                add("paragraph", " > ".join(trail) or "body", text(child))
            elif child.tag == "table-wrap":
                rows = [" | ".join(text(cell) for cell in row if cell.tag in ("td", "th"))
                        for row in child.iter("tr")]
                add("table", " > ".join(trail) or "body",
                    "\n".join(part for part in [text(child.find("caption"))] + rows if part))
            elif child.tag == "fig":
                add("caption", " > ".join(trail) or "body", text(child.find("caption")))
            elif child.tag not in ("title", "label"):
                walk(child, trail)

    before = len(units)
    walk(root.find("body"), [])
    if len(units) == before:
        raise ValidationError("full-text XML has an empty body")
    return units


class EntitledDocuments:
    """Entitled full texts the study supplies under its licence (a local directory; nothing is fetched).

    ``entitled-documents.json``: ``{"format": "ocsl-followup-entitled-v1", "documents": [{"doi": ..., "pmid": ...,
    "path": "<file in the directory>", "sha256": ...}]}``; each file is JATS and must hash to its recorded digest.
    """

    def __init__(self, directory: Path | str):
        self.directory = Path(directory).resolve()
        manifest = self.directory / "entitled-documents.json"
        if not manifest.is_file():
            raise ValidationError(f"the entitled documents directory has no entitled-documents.json: {self.directory}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValidationError("entitled-documents.json is not JSON") from error
        if not isinstance(data, dict) or data.get("format") != ENTITLED_FORMAT or not isinstance(data.get("documents"),
                                                                                                    list):
            raise ValidationError(f"entitled-documents.json must have format {ENTITLED_FORMAT} and a documents list")
        self.by_doi: dict[str, dict[str, str]] = {}
        self.by_pmid: dict[str, dict[str, str]] = {}
        for entry in data["documents"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", str(entry.get("sha256") or "")):
                raise ValidationError("every entitled document needs a path and a sha256")
            candidate = (self.directory / entry["path"]).resolve()
            if not candidate.is_relative_to(self.directory):
                raise ValidationError("an entitled document path escapes its directory")
            record = {"path": str(candidate), "sha256": entry["sha256"]}
            if normal_doi(entry.get("doi")):
                self.by_doi[normal_doi(entry["doi"])] = record
            if normal_pmid(entry.get("pmid")):
                self.by_pmid[normal_pmid(entry["pmid"])] = record

    def document(self, doi: str | None, pmid: str | None) -> tuple[bytes, str] | None:
        record = self.by_doi.get(doi or "") or self.by_pmid.get(pmid or "")
        if record is None:
            return None
        path = Path(record["path"])
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"an entitled document is missing: {path.name}")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise ValidationError(f"an entitled document differs from its recorded digest: {path.name}")
        return body, record["sha256"]


class Acquirer:
    """Evidence of cited works: byte caches, then live Europe PMC, then the study's entitled documents."""

    def __init__(self, session: ProviderSession | None, *, caches: Any = None, europe_pmc: bool = False,
                 entitled: EntitledDocuments | None = None):
        self.session, self.caches, self.europe_pmc, self.entitled = session, caches, europe_pmc, entitled

    def _search(self, identifiers: Mapping[str, str]) -> tuple[Any, dict[str, Any] | None, str]:
        """The Europe PMC core lookup the census makes (by DOI, else PMID): (answer, record, request URL)."""
        scheme = "doi" if identifiers.get("doi") else "pmid"
        query = (f'DOI:"{identifiers["doi"]}"' if scheme == "doi" else f"EXT_ID:{identifiers['pmid']} AND SRC:MED")
        params = {"query": query, "pageSize": 5, "resultType": "core", "format": "json"}
        answer = self.session.get("europe-pmc", SEARCH, params=params, egress={scheme: identifiers[scheme]})
        record = None
        if answer.ok:
            from .census_inputs import check_identity, identifiers_of
            from .scholarly import normalize_europe_pmc
            try:
                results = (answer.json().get("resultList") or {}).get("result") or []
            except AttributeError:
                results = []
            for raw in results if isinstance(results, list) else []:
                if not isinstance(raw, dict):
                    continue
                observed = identifiers_of(normalize_europe_pmc(raw))
                if observed.get(scheme) != identifiers[scheme]:
                    continue
                accepted, _ = check_identity(observed, identifiers, require=(scheme,))
                record = raw if accepted else None
                break
        import urllib.parse
        return answer, record, SEARCH + "?" + urllib.parse.urlencode(params)

    @staticmethod
    def identifiers(work: Mapping[str, Any]) -> dict[str, str]:
        found = {"doi": normal_doi(work.get("doi")), "pmid": normal_pmid(work.get("pmid")),
                 "pmcid": normal_pmcid(work.get("pmcid"))}
        return {key: value for key, value in found.items() if value}

    def abstract(self, work: Mapping[str, Any]) -> Evidence:
        from .evidence import abstract_candidate, complete_abstract
        from .scholarly import ProviderError, normalize_europe_pmc
        key, identifiers = work["work_key"], self.identifiers(work)
        if not identifiers.get("doi") and not identifiers.get("pmid"):
            return Evidence(key, "abstract", "unavailable", detail={"reason": "no-identifier"})
        rejected: list[Any] = []
        if self.caches is not None:
            hit, rejected = self.caches.abstract(identifiers)
            if hit is not None:
                provenance = hit.get("provenance") or {}
                candidate = abstract_candidate(normalize_europe_pmc(hit["record"]),
                                               source_url=str(provenance.get("sourceUrl") or ""),
                                               retrieved_at=str(provenance.get("retrievedAt") or now()))
                if complete_abstract(candidate):
                    return Evidence(key, "abstract", "obtained", "europe-pmc", str(provenance.get("cache")), "public",
                                    str(provenance.get("sourceSha256") or ""),
                                    [{"unit_id": "abstract", "kind": "abstract", "locator": "Abstract",
                                      "text": candidate["text"]}], True,
                                    detail={"cache": provenance.get("cache"), "cacheVersion":
                                            provenance.get("cacheVersion"), "identity": provenance.get("identity")})
                rejected = [*rejected, {"cache": provenance.get("cache"), "reason": "incomplete-abstract"}]
        if self.session is None:
            return Evidence(key, "abstract", "unavailable", detail={"reason": "no live route", "rejected": rejected})
        answer, record, url = self._search(identifiers)
        detail: dict[str, Any] = {"rejected": rejected} if rejected else {}
        if not answer.ok:
            return Evidence(key, "abstract", "fetch-failed", "europe-pmc", "live", request_id=answer.request_id,
                            detail={**detail, "error": answer.error})
        if record is None:
            return Evidence(key, "abstract", "incomplete", "europe-pmc", "live", request_id=answer.request_id,
                            detail={**detail, "reason": "no-record"})
        try:
            candidate = abstract_candidate(normalize_europe_pmc(record), source_url=url, retrieved_at=now())
        except ProviderError:
            candidate = {}
        if not complete_abstract(candidate):
            return Evidence(key, "abstract", "incomplete", "europe-pmc", "live", request_id=answer.request_id,
                            detail={**detail, "reason": "no complete structured abstract"})
        return Evidence(key, "abstract", "obtained", "europe-pmc", "live", "public", answer.sha256,
                        [{"unit_id": "abstract", "kind": "abstract", "locator": "Abstract", "text": candidate["text"]}],
                        True, answer.request_id, detail)

    def full_text(self, work: Mapping[str, Any]) -> Evidence:
        key, identifiers = work["work_key"], self.identifiers(work)
        detail: dict[str, Any] = {}
        if self.caches is not None and identifiers.get("pmcid"):
            hit, rejected = self.caches.full_text(identifiers)
            if rejected:
                detail["rejected"] = rejected
            if hit is not None:
                provenance = hit.get("provenance") or {}
                try:
                    units = jats_units(hit["body"])
                except ValidationError:
                    detail.setdefault("rejected", []).append({"cache": provenance.get("cache"), "reason": "unreadable"})
                else:
                    return Evidence(key, "full-text", "obtained", "europe-pmc-fulltextxml", str(provenance.get("cache")),
                                    "public", hashlib.sha256(hit["body"]).hexdigest(), units, True,
                                    detail={**detail, "cacheVersion": provenance.get("cacheVersion")})
        outcome, request = "unavailable", None
        if self.europe_pmc and self.session is not None:
            pmcid = identifiers.get("pmcid")
            if pmcid is None and (identifiers.get("doi") or identifiers.get("pmid")):
                answer, record, _ = self._search(identifiers)
                request = answer.request_id
                pmcid = normal_pmcid((record or {}).get("pmcid")) if answer.ok else None
                if not answer.ok:
                    outcome = "fetch-failed"
                    detail["error"] = answer.error
            if pmcid:
                answer = self.session.get("europe-pmc", FULL_TEXT.format(pmcid=pmcid), egress={"pmcid": pmcid},
                                          document=True)
                request = answer.request_id
                if answer.ok:
                    try:
                        units = jats_units(answer.body or b"")
                    except ValidationError as error:
                        outcome = "fetch-failed"
                        detail["error"] = f"unreadable: {error}"
                    else:
                        return Evidence(key, "full-text", "obtained", "europe-pmc-fulltextxml", "live", "public",
                                        answer.sha256, units, True, request, {**detail, "pmcid": pmcid})
                else:
                    outcome = "no-open-full-text" if answer.outcome == "not-found" else "fetch-failed"
                    detail["error"] = answer.error
            elif outcome != "fetch-failed":
                outcome = "no-open-full-text"
                detail["reason"] = "no PMCID"
        if self.entitled is not None:
            found = self.entitled.document(identifiers.get("doi"), identifiers.get("pmid"))
            if found is not None:
                body, sha = found
                return Evidence(key, "full-text", "obtained", "entitled", "entitled", "entitled", sha, jats_units(body),
                                True, detail={**detail, "publicOutcome": outcome})
        if outcome == "unavailable":
            detail.setdefault("reason", "no route the run had holds an open full text")
        return Evidence(key, "full-text", outcome, request_id=request, detail=detail)

    def acquire(self, tier: str, work: Mapping[str, Any]) -> Evidence:
        return self.abstract(work) if tier == "abstract" else self.full_text(work)


def store_evidence(connection: Any, evidence: Evidence) -> None:
    text = "\n\n".join(unit["text"] for unit in evidence.units)
    connection.execute("INSERT OR IGNORE INTO fu_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        evidence.evidence_id, evidence.work_key, evidence.unit, evidence.provider, evidence.route,
        evidence.provenance_class, evidence.source_sha256, hashlib.sha256(text.encode("utf-8")).hexdigest() if text
        else None, len(text) if text else None, None if evidence.complete is None else int(evidence.complete),
        evidence.outcome, evidence.request_id, evidence.retrieved_at, canonical(evidence.detail)))
    connection.executemany("INSERT OR IGNORE INTO fu_evidence_units VALUES(?,?,?,?,?,?)", [
        (evidence.evidence_id, unit["unit_id"], ordinal, unit["kind"], unit.get("locator") or "", unit["text"])
        for ordinal, unit in enumerate(evidence.units, 1)])


def best_evidence(connection: Any, tier: str, *, units_for: set[str] | None = None) -> dict[str, Evidence]:
    """Per work at one tier: its latest obtained evidence, else its latest failed attempt; the units' text is loaded
    only for the works in ``units_for``."""
    rows: dict[str, Any] = {}
    for row in connection.execute("SELECT * FROM fu_evidence WHERE unit=? ORDER BY retrieved_at, evidence_id",
                                  (tier,)):
        current = rows.get(row["work_key"])
        if current is None or row["outcome"] == "obtained" or current["outcome"] != "obtained":
            rows[row["work_key"]] = row
    found = {}
    for key, row in rows.items():
        units = []
        if row["outcome"] == "obtained" and units_for is not None and key in units_for:
            units = [{"unit_id": unit[0], "kind": unit[1], "locator": unit[2], "text": unit[3]} for unit in
                     connection.execute("SELECT unit_id, kind, locator, text FROM fu_evidence_units WHERE evidence_id=? "
                                        "ORDER BY ordinal", (row["evidence_id"],))]
        found[key] = Evidence(key, tier, row["outcome"], row["provider"], row["route"], row["provenance_class"],
                              row["source_sha256"], units, None if row["complete"] is None else bool(row["complete"]),
                              row["request_id"], json.loads(row["detail_json"] or "{}"), row["retrieved_at"],
                              row["evidence_id"])
    return found


def needs_acquisition(evidence: Evidence | None, *, retry_failed: bool, routes: bool) -> bool:
    """Obtained and definitive outcomes are reused; a failed fetch is retried on request, and an outcome no route
    could decide is retried when the run has a route (live, cache or entitled documents)."""
    if evidence is None:
        return True
    if evidence.outcome == "unavailable":
        return routes and evidence.detail.get("reason") != "no-identifier"
    return evidence.outcome == "fetch-failed" and retry_failed


def acquire_all(connection: Any, acquirer: Acquirer, tier: str, works: Sequence[Mapping[str, Any]], *,
                retry_failed: bool = False, workers: int = 1, lock: Any = None) -> tuple[dict[str, Evidence], Counter]:
    """Evidence for the works (reused from the branch where it is settled), stored as it arrives."""
    known = best_evidence(connection, tier, units_for={work["work_key"] for work in works})
    routes = acquirer.session is not None or acquirer.caches is not None or acquirer.entitled is not None
    if tier == "full-text":
        routes = acquirer.europe_pmc or acquirer.caches is not None or acquirer.entitled is not None
    wanted = [work for work in works
              if needs_acquisition(known.get(work["work_key"]), retry_failed=retry_failed, routes=routes)]
    counts: Counter = Counter()

    def one(work: Mapping[str, Any]) -> Evidence:
        return acquirer.acquire(tier, work)
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for evidence in pool.map(one, wanted):
            if lock is not None:
                with lock:
                    store_evidence(connection, evidence)
            else:
                store_evidence(connection, evidence)
            known[evidence.work_key] = evidence
            counts[f"{evidence.route or 'none'}:{evidence.outcome}"] += 1
    return known, counts


# ----------------------------------------------------------------------------- tasks
class SupportItems:
    """citation-support@2 as ``followup_judge.SupportModule`` runs it; a response its validator cannot read at all
    rejects every item of the call (retryable) instead of ending the run."""

    def __init__(self):
        from .followup_judge import SupportModule
        self.inner = SupportModule()

    def build_messages(self, items: Sequence[Mapping[str, Any]], tier: str) -> list[dict[str, str]]:
        return self.inner.build_messages(items, tier)

    def validate_response(self, items: Sequence[Mapping[str, Any]], text: Any) -> dict[str, Any]:
        from .corpus_assessment import AssessmentError
        try:
            return self.inner.validate_response(items, text)
        except AssessmentError as error:
            return {"accepted": {}, "unexpected": [], "unidentifiedItems": 0,
                    "rejected": {item["id"]: {"code": error.code, "message": str(error), "retryable": error.retryable}
                                 for item in items}}


class WindowIndex:
    """A retrieval index of one work's evidence units (temporary, closed after the work's loci)."""

    def __init__(self, directory: Path, evidence: Evidence, number: int):
        from .retrieval import EvidenceIndex, EvidenceUnit
        kind = "abstract" if evidence.unit == "abstract" else "full-text"
        self.index = EvidenceIndex.build(directory / f"index-{number}.sqlite3", [
            EvidenceUnit(unit["unit_id"], evidence.work_key, kind, unit["text"], unit.get("locator") or "",
                         bool(evidence.complete), evidence.source_sha256 or "") for unit in evidence.units])
        self.path = directory / f"index-{number}.sqlite3"

    def select(self, work_key: str, claim: str, *, k: int, window: int) -> Any:
        from .claim_check import select_evidence
        return select_evidence(self.index, work_key, claim, k=k, max_characters=window)

    def close(self) -> None:
        self.index.close()
        self.path.unlink(missing_ok=True)


def build_task(connection: Any, unit: Mapping[str, Any], sentence: str, evidence: Evidence, selection: Any, *,
               tier: str, documents: ReviewDocuments) -> tuple[dict[str, Any] | None, str | None]:
    """The citation-support@2 task of one locus (with its provenance class), or why it cannot be sent."""
    from .corpus_assessment import AssessmentError, TaskPlanner
    from .sanitize import residual_findings
    if selection.tier is None:
        return None, "not-judged"
    claim = sanitize(sentence, "claim")
    before, after, _ = documents.context(connection, unit)
    context = [text for text in (sanitize(before, "context"), sanitize(after, "context")) if text]
    marker, _ = resolve_marker(claim, citation_target(connection, unit), sentence_targets(connection, unit))
    title, year = cited_work(connection, unit["work_key"])
    cited: dict[str, Any] = {"title": sanitize(title, "cited-title")}
    if str(year or "").strip().isdigit():
        cited["year"] = int(str(year).strip())
    units = [{"id": item.unit_id, "text": text} for item in selection.units
             if (text := sanitize(item.text, "evidence"))]
    entitled = provenance_of(unit) == "entitled" or evidence.provenance_class == "entitled"
    task: dict[str, Any] = {
        "id": f"{unit['locus_id']}|{tier}", "claim": claim, "context": context,
        "citationLocus": {"bindingStatus": "exact", "marker": marker or "",
                          "section": sanitize(unit.get("section"), "section")},
        "citedWork": cited, "evidenceTier": selection.tier, "complete": bool(selection.complete),
        "evidenceUnits": units, "sourceSha256": evidence.source_sha256,
        "provenance": "entitled" if entitled else "public"}
    if unit.get("identity") == "resolved":
        task["identityVerified"] = True
    if not claim or residual_findings({key: value for key, value in task.items() if key not in ("id", "provenance")}):
        return None, "not-judged"
    try:
        decision = TaskPlanner().plan({key: value for key, value in task.items() if key != "provenance"}, tier)
    except AssessmentError:
        return None, "not-judged"
    if decision["action"] != "judge":
        return None, "abstract-incomplete" if "abstract" in str(decision["reason"]) else "not-judged"
    return task, None


# ----------------------------------------------------------------------------- rows
def support_rows(connection: Any, instrument: Any, tier: str, units: Sequence[Mapping[str, Any]],
                 reasons: Mapping[str, str], evidence: Mapping[str, Evidence]) -> Counter:
    """One fu_support row per unit at the tier from the primary judgments, the evidence and the blocked units."""
    keys = {unit["locus_id"]: f"{unit['locus_id']}|{tier}" for unit in units}
    state = judgment_state(connection, instrument.digest, tier, list(keys.values()))
    by_source = {(row[0], row[1]): (row[2], row[3]) for row in connection.execute(
        "SELECT work_key, source_sha256, evidence_id, provenance_class FROM fu_evidence WHERE unit=? AND "
        "outcome='obtained'", (tier,))}
    counts: Counter = Counter()
    values = []
    for unit in units:
        locus = unit["locus_id"]
        entry = state.get(keys[locus], {})
        found = evidence.get(unit["work_key"]) if unit["work_key"] else None
        if entry.get("accepted"):
            judgment_id, verdict = entry["accepted"]
            evidence_id, evidence_class = by_source.get((unit["work_key"], verdict.get("sourceSha256")), (None, None))
            klass = "entitled" if provenance_of(unit) == "entitled" or evidence_class == "entitled" else "public"
            quotes = verdict.get("evidence") or []
            quote = quotes[0].get("quote") if quotes and isinstance(quotes[0], dict) else None
            label = verdict["supportVerdict"]
            if label == "abstain":
                values.append((locus, tier, "abstain", 0, "abstained", verdict.get("evidenceCoverage"), klass,
                               judgment_id, evidence_id, quote))
                counts["abstained"] += 1
            else:
                values.append((locus, tier, label, 1, None, verdict.get("evidenceCoverage"), klass, judgment_id,
                               evidence_id, quote))
                counts[label] += 1
            continue
        if locus in reasons and reasons[locus] != "not-judged":
            reason = reason_of(reasons[locus])
        elif entry.get("blocked"):
            reason = reason_of(entry["blocked"])
        elif found is not None and not found.obtained:
            reason = EVIDENCE_REASONS[tier].get(found.outcome, "not-judged")
        elif entry.get("rejected"):
            reason = "invalid-output"
        else:
            reason = reasons.get(locus, "not-judged")
        values.append((locus, tier, None, 0, reason, "missing" if found is None or not found.obtained else None,
                       found.provenance_class if found is not None else None, None,
                       found.evidence_id if found is not None else None, None))
        counts[reason] += 1
    connection.executemany("INSERT OR REPLACE INTO fu_support VALUES(?,?,?,?,?,?,?,?,?,?)", values)
    return counts


# ----------------------------------------------------------------------------- run
def run_support(connection: Any, branch: FollowupBranch, judge: Any, policy: Any, *, tier: str, model: str,
                scholarly: Any = None, caches: Any = None, europe_pmc: bool = False,
                entitled: EntitledDocuments | None = None, documents: ReviewDocuments | None = None,
                scope: str = "all", variant: str = "primary", public_only: bool = False, limit: int | None = None,
                concurrency: int = 1, fetch_workers: int = 1, retry_failed: bool = False, transport_retries: int = 0,
                cooldown: float = 0.0, code_revision: str | None = None, sleep: Any = None) -> dict[str, Any]:
    from . import instruments
    from .followup_judge import accepted, run_items
    if tier not in TIERS:
        raise ValidationError("--tier is abstract or full-text")
    freeze = require_freeze(connection)
    protocol = branch.protocol()
    check_model(model, protocol)
    instrument = instruments.load(INSTRUMENT_ID)
    prepare_judge(judge, instrument, tier)
    documents = documents or ReviewDocuments(None)
    everything, _ = select_units(connection, TIER_STRATA[tier])
    units, selection = select_units(connection, TIER_STRATA[tier], scope=scope, variant=variant, protocol=protocol,
                                    include_cocited_of_pilot=True, instrument_id=instrument.id)
    reasons: dict[str, str] = {}
    blocked: list[tuple[str, str, str]] = []
    sentences: dict[str, str] = {}
    for unit in everything:
        if unit["work_key"] is None:
            reasons[unit["locus_id"]] = "identity-unresolved"
            continue
        text, reason = branch.sentence(connection, unit["journal"], unit["doi"], unit["label"], unit["seq"])
        if text is None:
            reasons[unit["locus_id"]] = reason or "no-citing-text"
        else:
            sentences[unit["locus_id"]] = text
    scoped = {unit["locus_id"] for unit in units}
    blocked = [(f"{locus}|{tier}", code, "never sent: " + code) for locus, code in reasons.items() if locus in scoped]
    blocks = block_new(connection, instrument, tier, blocked, variant=variant)
    done = set(accepted(connection, instrument.digest, tier, variant))
    pending = [unit for unit in units if unit["locus_id"] in sentences and f"{unit['locus_id']}|{tier}" not in done
               and not (public_only and provenance_of(unit) != "public")]
    if limit is not None:
        if limit < 1:
            raise ValidationError("--limit must be positive")
        pending = pending[:limit]
    works = {}
    for unit in pending:
        if unit["work_key"] not in works:
            row = connection.execute("SELECT work_key, doi, pmid, pmcid FROM census.works WHERE work_key=?",
                                     (unit["work_key"],)).fetchone()
            works[unit["work_key"]] = dict(row) if row else {"work_key": unit["work_key"]}
    session = ProviderSession(scholarly, Recorder(connection), operation=OPERATION) if scholarly is not None else None
    acquirer = Acquirer(session, caches=caches, europe_pmc=europe_pmc, entitled=entitled)
    evidence, fetched = acquire_all(connection, acquirer, tier, list(works.values()), retry_failed=retry_failed,
                                    workers=fetch_workers, lock=session.recorder.lock if session else None)
    connection.commit()
    k, window = retrieval_parameters()
    items: list[dict[str, Any]] = []
    unsent: Counter = Counter()
    by_work: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for unit in pending:
        by_work[unit["work_key"]].append(unit)
    with tempfile.TemporaryDirectory(prefix="ocsl-followup-windows-") as directory:
        for number, (work_key, loci) in enumerate(sorted(by_work.items())):
            found = evidence.get(work_key)
            if found is None or not found.obtained or not found.units:
                unsent["no-evidence"] += len(loci)
                continue
            index = WindowIndex(Path(directory), found, number)
            try:
                for unit in loci:
                    claim = sanitize(sentences[unit["locus_id"]], "claim")
                    selection_ = index.select(work_key, claim, k=k, window=window)
                    task, reason = build_task(connection, unit, sentences[unit["locus_id"]], found, selection_,
                                              tier=tier, documents=documents)
                    if task is None:
                        reasons[unit["locus_id"]] = reason or "not-judged"
                        unsent[reason or "not-judged"] += 1
                    else:
                        items.append(task)
            finally:
                index.close()
    sendable = [item for item in items if not public_only or item["provenance"] == "public"]
    options = {"sleep": sleep} if sleep is not None else {}
    summary = run_items(connection, instrument, sendable, judge, tier=tier, model=model, policy=policy,
                        module=SupportItems(), variant=variant, concurrency=concurrency, code_revision=code_revision,
                        transport_retries=transport_retries, cooldown=cooldown, **options) if sendable else {
        "instrument": instrument.id, "digest": instrument.digest, "items": 0, "calls": 0, "accepted": 0,
        "rejected": 0, "failedCalls": 0, "open": 0, "stopped": None}
    rows: Counter = Counter()
    if variant == "primary":
        rows = support_rows(connection, instrument, tier, everything, reasons, best_evidence(connection, tier))
    return {"instrument": instrument.id, "digest": instrument.digest, "tier": tier, "freeze": freeze["freeze_id"],
            "selection": selection, "pending": len(pending), "sent": len(sendable),
            "withheldEntitled": len(items) - len(sendable), "blocked": dict(Counter(code for _, code, _ in blocked)),
            "blockedRecorded": blocks, "evidence": dict(sorted(fetched.items())), "unsent": dict(unsent),
            "contexts": dict(documents.counts), "retrieval": {"k": k, "maxEvidenceCharacters": window},
            "requests": session.recorder.summary() if session else {}, "run": summary,
            "rows": dict(sorted(rows.items())), "judge": judge_summary(judge)}


def cmd_support(args: Any) -> dict[str, Any]:
    from .census_inputs import InputCaches
    from .census_runner import code_revision
    from .followup_judge import judge_from_args
    branch = FollowupBranch(Path(args.database))
    judge, policy = judge_from_args(args)
    entitled = EntitledDocuments(args.entitled_documents) if getattr(args, "entitled_documents", None) else None
    documents = ReviewDocuments(getattr(args, "documents", None))
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else None
    caches = InputCaches.open(store=getattr(args, "store", None), store_root=getattr(args, "store_root", None),
                              provider_cache=getattr(args, "provider_cache", None))
    try:
        if args.tier not in TIERS:
            raise ValidationError("--tier is abstract or full-text")
        with branch.writing() as connection:
            return run_support(connection, branch, judge, policy, tier=args.tier, model=args.model,
                               scholarly=make_client(workspace), caches=caches, europe_pmc=args.europe_pmc,
                               entitled=entitled, documents=documents, scope=args.scope, variant=args.variant,
                               public_only=args.public_only, limit=args.limit, concurrency=args.concurrency,
                               fetch_workers=args.fetch_workers, retry_failed=args.retry_failed,
                               transport_retries=args.transport_retries, cooldown=args.transport_cooldown,
                               code_revision=code_revision())
    finally:
        if caches is not None:
            caches.close()
        close_judge(judge)
