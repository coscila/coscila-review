# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review screen``: a reference and claim screen of one manuscript, with receipts.

Research-integrity tooling for authors (a self-check before submission), reviewers (a dossier),
study teams (re-checking after a revision with ``screen --diff``) and corpus studies. One run:

1. strips hidden document content, then ingests the manuscript (author-year citations resolved);
2. identifies every cited work in the evidence store (identifier first, then exact title);
3. records reference integrity findings (local classes; with network, registry and dated status);
4. retrieves evidence per citation (BM25 receipts), sanitises it and judges each citation against
   its whole sentence with claim-check@1 (citation-support@2 semantics);
5. writes a deterministic report, a Markdown report and a reviewer dossier.

``ocsl-review replay`` re-derives every verdict from the stored request and response bytes and
the receipts, with no model call, and rebuilds the report to the same digest.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import instruments
from . import integrity as I
from .claim_check import (
    CALIBRATION_STATEMENT,
    TASK,
    CheckContext,
    ConfidentialPolicy,
    EgressLedger,
    HttpJudge,
    PolicyTransport,
    RunStore,
    error_category,
    extract_claims,
    instrument_definition,
    instrument_digest,
    judge_tasks,
    plan_tasks,
    policy_registry_transport,
    record_claims,
    sha256_bytes,
)
from .corpus_assessment import AssessmentValidator
from .errors import ReviewError, ValidationError
from .ingest import ingest_manuscript
from .retrieval import EvidenceIndex, Work, WorkCatalog, canonical, load_library, load_store, sha256_text
from .sanitize import SANITIZER_VERSION, find_injections, sanitize_document

REPORT_FORMAT = "ocsl-claim-screen"
REPORT_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identity_rows(manuscript: Mapping[str, Any], catalog: WorkCatalog) -> dict[str, dict[str, Any]]:
    identities = {}
    for entry in manuscript.get("bibliographyEntries", []):
        identities[entry["id"]] = catalog.match(entry.get("identifiers") or {}, entry.get("raw", ""))
    return identities


def _store_snapshot(works: Sequence[Work]) -> bytes:
    return canonical({"format": "ocsl-evidence-store", "version": 1, "works": [{
        "workId": work.work_id, "identifiers": work.identifiers, "title": work.title, "authors": work.authors,
        "year": work.year, "container": work.container,
        "units": [{"unitId": unit.unit_id, "kind": unit.kind, "text": unit.text, "locator": unit.locator,
                   "complete": unit.complete, "sourceSha256": unit.source_sha256} for unit in work.units]}
        for work in sorted(works, key=lambda item: item.work_id)]}).encode("utf-8")


def _insert_finding(connection: sqlite3.Connection, analysis_id: str, item: Mapping[str, Any]) -> None:
    connection.execute("INSERT OR IGNORE INTO integrity_findings VALUES (?,?,?,?,?,?,?,?,?)", (
        analysis_id, item["findingId"], item["class"], item["subjectKind"], item["subjectId"], item["severity"],
        canonical(item["detail"]), item.get("datasetVersion"), _now()))


def run_screen(manuscript_path: Path, *, run_root: Path, works: Sequence[Work], evidence_name: str = "evidence",
               policy: ConfidentialPolicy | None = None, judge: Any = None, offline: bool = True,
               registry_transport: Any = None, scholarly_transport: Any = None,
               datasets: Sequence[I.RetractionWatchDataset] = (), reference_year: int | None = None,
               earlier_report: Mapping[str, Any] | None = None, k: int = 5, max_evidence_characters: int = 11000,
               scholarly_workspace: Path | None = None) -> dict[str, Any]:
    """Run one screen into a new run directory and return the report."""

    policy = policy or ConfidentialPolicy()
    manuscript_path = Path(manuscript_path)
    suffix = manuscript_path.suffix.lower()
    original = manuscript_path.read_bytes()
    cleaned, document_removals, limitations = sanitize_document(original, suffix)
    store = RunStore(run_root, create=True)
    ledger = EgressLedger(store.root / "egress.jsonl", workspace=scholarly_workspace)
    if judge is not None and not policy.judge_allowed(judge):
        # Refuse before any manuscript text is read into a prompt; the refusal is recorded.
        policy.authorize_model(ledger, judge, payload_sha256=sha256_bytes(b""), items=0)
    input_dir = store.root / "input"
    input_dir.mkdir(mode=0o700, exist_ok=True)
    sanitized_path = input_dir / ("manuscript" + suffix)
    sanitized_path.write_bytes(cleaned)
    os.chmod(sanitized_path, 0o600)
    manuscript = ingest_manuscript(sanitized_path, confidential=policy.confidential)
    definition = instrument_definition(k=k, max_evidence_characters=max_evidence_characters)
    evidence_bytes = _store_snapshot(works)
    index = EvidenceIndex.build(store.root / "evidence.sqlite3", [unit for work in works for unit in work.units])
    os.chmod(store.root / "evidence.sqlite3", 0o600)
    catalog = WorkCatalog(works)
    identities = _identity_rows(manuscript, catalog)
    reference_year = reference_year or date.today().year
    connection = store.connect(write=True)
    try:
        with connection:
            original_sha = store.artifact(connection, original, "manuscript-original", "application/octet-stream")
            sanitized_sha = store.artifact(connection, cleaned, "manuscript-sanitized", "application/octet-stream")
            manuscript_sha = store.artifact(connection, canonical(manuscript).encode("utf-8"), "manuscript-json",
                                            "application/json")
            evidence_sha = store.artifact(connection, evidence_bytes, "evidence-store", "application/json")
            protocol = {"task": TASK, "instrument": definition, "instrumentDigest": instrument_digest(definition),
                        "registryDigest": instruments.load(TASK).digest,
                        "policy": policy.record(), "policyDigest": policy.digest,
                        "evidence": {"name": evidence_name, "snapshotSha256": evidence_sha, "indexDigest": index.digest},
                        "datasets": [dataset.version.record() for dataset in datasets], "offline": bool(offline),
                        "referenceYear": reference_year, "manuscript": {"originalSha256": original_sha,
                                                                         "sanitizedSha256": sanitized_sha,
                                                                         "format": manuscript["source"]["format"]}}
            corpus_id = "corpus-" + manuscript["manuscriptId"]
            run_id = "run-" + sha256_text(canonical([protocol, _now()]))[:24]
            analysis_id = "screen-" + sha256_text(canonical([run_id, manuscript_sha]))[:24]
            protocol_json = json.dumps(protocol, sort_keys=True)
            connection.execute("INSERT INTO corpora VALUES (?,?,?)", (corpus_id, "Claim screen of one manuscript", _now()))
            connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (
                run_id, corpus_id, protocol_json, sha256_text(protocol_json), None, _now()))
            connection.execute("INSERT INTO analyses VALUES (?,?,?,?,?)", (
                analysis_id, run_id, "Reference and claim screen (claim-check@1)", None, _now()))
            manuscript_work = "manuscript:" + manuscript["manuscriptId"]
            connection.execute("INSERT INTO works VALUES (?,?,?,?)", (
                manuscript_work, manuscript.get("structuredSource", {}).get("publication", {}).get("article-title") or "Screened manuscript",
                None, canonical({"role": "screened-manuscript"})))
            for work in works:
                connection.execute("INSERT INTO works VALUES (?,?,?,?)", (
                    work.work_id, work.title or work.work_id, work.year,
                    canonical({"authors": work.authors, "container": work.container, "identifiers": work.identifiers})))
                for scheme, value in sorted(work.identifiers.items()):
                    connection.execute("INSERT OR IGNORE INTO work_identifiers VALUES (?,?,?,?)",
                                       (work.work_id, scheme, value, evidence_sha))
                for unit in work.units:
                    connection.execute("INSERT INTO source_sections VALUES (?,?,?,?,?,?,?,?,?)", (
                        unit.unit_id, work.work_id, evidence_sha, unit.locator or unit.unit_id, unit.kind, unit.text,
                        unit.text_sha256, "evidence-store-1", int(unit.complete)))
            review_id = "review-" + manuscript["manuscriptId"]
            connection.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)", (
                review_id, corpus_id, manuscript_work, "included", "{}", "screen input"))
            for entry in manuscript.get("bibliographyEntries", []):
                identity = identities[entry["id"]]
                state = {"resolved": "verified", "conflict": "conflict"}.get(identity["state"], "unresolved")
                connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", (
                    entry["id"], review_id, entry.get("label") or entry["id"], entry.get("raw", ""),
                    canonical({"identifiers": entry.get("identifiers", {}), "identity": identity}),
                    identity.get("workId"), state, evidence_sha if state == "verified" else None))
            context = CheckContext(store, analysis_id, review_id, manuscript_sha, index, policy, ledger, definition,
                                   {work.work_id: work for work in works}, k, max_evidence_characters)
            claims = extract_claims(manuscript)
            loci = record_claims(connection, context, claims)

            findings = I.local_findings(manuscript, identities)
            for removal in document_removals:
                connection.execute("INSERT OR IGNORE INTO sanitization_records VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    "san-" + sha256_text(canonical([analysis_id, "document", removal]))[:24], analysis_id,
                    "manuscript-document", f"position-{removal['position']}", removal["kind"], removal["rule"], None, None,
                    removal["length"], removal["sha256"], SANITIZER_VERSION))
            if document_removals:
                findings.append(I.finding("hidden-text", "manuscript", manuscript["manuscriptId"], detail={
                    "removed": len(document_removals), "rules": sorted({item["rule"] for item in document_removals}),
                    "digests": sorted(item["sha256"] for item in document_removals)},
                    key=sorted(item["sha256"] for item in document_removals)))
            injected = []
            for sentence in manuscript.get("sentences", []):
                for _, _, rule in find_injections(sentence["text"]):
                    injected.append((sentence["id"], rule))
            if injected:
                findings.append(I.finding("prompt-injection-payload", "manuscript", manuscript["manuscriptId"], detail={
                    "sentences": sorted({item[0] for item in injected}), "rules": sorted({item[1] for item in injected}),
                    "handling": "removed from every judge prompt; the sentence is still reported"},
                    key=sorted(set(injected))))
            for claim in claims:
                if not claim.citations and claim.citation_required:
                    findings.append(I.finding("uncited-claim", "claim", claim.claim_id, detail={
                        "anchor": claim.anchor, "reason": claim.requirement_reason, "claimType": claim.claim_type},
                        key=[sha256_text(claim.text)]))
            for dataset in datasets:
                version = dataset.version
                connection.execute("INSERT OR IGNORE INTO integrity_datasets VALUES (?,?,?,?,?,?,?)", (
                    version.identifier, version.name, version.declared_version, version.sha256, version.rows,
                    version.path_name, _now()))
            registry = None
            scholarly = None
            if not offline:
                from .integrity import RegistryCheck, urllib_registry_transport
                from .scholarly import HttpTransport, ScholarlyClient
                registry = RegistryCheck(policy_registry_transport(registry_transport or urllib_registry_transport(),
                                                                   policy, ledger),
                                         authorize=lambda provider, operation, arguments:
                                         policy.authorize_provider(ledger, provider, operation, arguments),
                                         min_interval=0 if registry_transport is not None else 0.15)
                inner = scholarly_transport or HttpTransport(user_agent="ocsl-review/0.1 (reference integrity check)")
                scholarly = ScholarlyClient(plan=policy.egress_plan(), workspace=scholarly_workspace,
                                            transport=PolicyTransport(inner, policy, ledger))
            checker = I.IntegrityChecker(registry=registry, scholarly=scholarly, datasets=datasets,
                                         reference_year=reference_year)
            if not offline or datasets:
                for entry in manuscript.get("bibliographyEntries", []):
                    result = checker.check_entry(entry)
                    findings.extend(result["findings"])
                    if result.get("registry"):
                        registry_result = result["registry"]
                        connection.execute("INSERT OR IGNORE INTO registry_checks VALUES (?,?,?,?,?,?)", (
                            analysis_id, entry["id"], registry_result["doi"], registry_result["verdict"],
                            canonical(registry_result["endpoints"]), registry_result["checkedAt"]))
                    status = result.get("status")
                    if status is not None:
                        result_sha = None
                        if result.get("scholarly") is not None:
                            result_sha = store.artifact(connection, canonical(result["scholarly"]).encode("utf-8"),
                                                        "publication-status-result", "application/json")
                        connection.execute("INSERT INTO work_status_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                            "status-" + sha256_text(canonical([analysis_id, entry["id"]]))[:24], analysis_id, entry["id"],
                            identities[entry["id"]].get("workId"), result.get("doi"), result.get("pmid"), status["state"],
                            canonical(status["notices"]), canonical(status["sources"]), canonical(status["limitations"]),
                            status.get("scholarlyState"), result_sha,
                            ",".join(dataset.version.identifier for dataset in datasets) or None, status["checkedAt"]))
                    elif result.get("statusError"):
                        findings.append(I.finding("status-unchecked", "entry", entry["id"], severity=I.INFO,
                                                  detail={"error": result["statusError"]}, key=[result["statusError"]]))
            else:
                for entry in manuscript.get("bibliographyEntries", []):
                    # Offline, only the printed entry itself can be checked (impossible year).
                    findings.extend(I.metadata_findings(entry["id"], entry.get("raw", ""), {}, reference_year=reference_year))
            for item in findings:
                _insert_finding(connection, analysis_id, item)
            planned = plan_tasks(connection, context, claims, identities, loci)
            if judge is not None:
                judge_summary = judge_tasks(connection, context, planned, judge)
            else:
                judge_summary = {"calls": 0, "repairCalls": 0, "accepted": 0, "rejected": 0, "failedCalls": 0}
                for item in planned:
                    connection.execute("INSERT OR REPLACE INTO processing_state VALUES (?,?,?,?,?,?,?)", (
                        analysis_id, item.task["id"], TASK, "blocked", 0, canonical({"reason": "no judge configured"}), _now()))
            connection.execute("INSERT OR REPLACE INTO processing_state VALUES (?,?,?,?,?,?,?)", (
                analysis_id, "run", "screen", "completed", 1, canonical({"judge": judge_summary, "limitations": limitations}),
                _now()))
    finally:
        connection.close()
        index.close()
    report = build_report(store)
    if earlier_report is not None:
        report_diff = diff_reports(earlier_report, report)
        (store.root / "diff.json").write_text(json.dumps(report_diff, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
        report = {**report, "diff": report_diff}
    write_outputs(store.root, report)
    return report


# Report ------------------------------------------------------------------------------------

def _rows(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters)]


def _reference_key(entry: Mapping[str, Any]) -> str:
    identifiers = entry.get("identifiers", {})
    for scheme in ("doi", "pmid", "pmcid"):
        values = identifiers.get(scheme) or []
        if values:
            return f"{scheme}:{str(values[0]).lower().rstrip('.,;')}"
    if entry.get("workId"):
        return "work:" + entry["workId"]
    return "printed:" + sha256_text(re.sub(r"\W+", " ", entry.get("printed", "").casefold()).strip())[:24]


def build_report(store: RunStore) -> dict[str, Any]:
    """Deterministic report from the run database; replay rebuilds it to the same digest."""

    connection = store.connect()
    try:
        run = _rows(connection, "SELECT r.*, a.analysis_id, a.created_at AS analysis_created FROM runs r JOIN analyses a USING(run_id)")[0]
        analysis_id = run["analysis_id"]
        protocol = json.loads(run["protocol_json"])
        entries = {}
        for row in _rows(connection, "SELECT * FROM bibliography_entries ORDER BY entry_id"):
            candidate = json.loads(row["candidate_identifiers_json"])
            entries[row["entry_id"]] = {
                "entryId": row["entry_id"], "label": row["reference_label"], "printed": row["raw_reference"][:600],
                "identifiers": candidate.get("identifiers", {}), "identity": candidate.get("identity", {}),
                "workId": row["resolved_work_id"], "identityState": row["identity_state"]}
        findings = []
        for row in _rows(connection, "SELECT * FROM integrity_findings WHERE analysis_id=? ORDER BY finding_class, subject_id, finding_id",
                         (analysis_id,)):
            findings.append({"findingId": row["finding_id"], "class": row["finding_class"], "subjectKind": row["subject_kind"],
                             "subjectId": row["subject_id"], "severity": row["severity"],
                             "detail": json.loads(row["detail_json"]), "datasetVersion": row["dataset_version_id"]})
        findings_by_entry: dict[str, list[str]] = {}
        for item in findings:
            if item["subjectKind"] == "entry":
                findings_by_entry.setdefault(item["subjectId"], []).append(item["class"])
            for member in item["detail"].get("entries", []) if isinstance(item["detail"], dict) else []:
                findings_by_entry.setdefault(member, []).append(item["class"])
        statuses = {row["entry_id"]: {"state": row["state"], "notices": json.loads(row["notices_json"]),
                                      "sources": json.loads(row["sources_json"])}
                    for row in _rows(connection, "SELECT * FROM work_status_observations WHERE analysis_id=? ORDER BY entry_id",
                                     (analysis_id,))}
        registry = {row["entry_id"]: row["verdict"] for row in
                    _rows(connection, "SELECT entry_id, verdict FROM registry_checks WHERE analysis_id=?", (analysis_id,))}
        claims = {row["claim_id"]: row for row in _rows(connection, "SELECT * FROM claims WHERE analysis_id=? ORDER BY claim_id",
                                                        (analysis_id,))}
        retrievals = {(row["claim_id"], row["citation_ordinal"]): row for row in
                      _rows(connection, "SELECT * FROM retrieval_receipts WHERE analysis_id=?", (analysis_id,))}
        receipts = {row["receipt_id"]: row for row in _rows(connection, "SELECT * FROM model_receipts WHERE analysis_id=?", (analysis_id,))}
        assessments = {}
        for row in _rows(connection, "SELECT * FROM assessments WHERE analysis_id=?", (analysis_id,)):
            verdict = json.loads(row["verdict_json"])
            assessments[verdict.get("taskId") or verdict.get("id")] = (row, verdict)
        blocked = {row["task_key"]: json.loads(row["checkpoint_json"]) for row in
                   _rows(connection, "SELECT task_key, checkpoint_json FROM processing_state WHERE analysis_id=? AND status='blocked'",
                         (analysis_id,))}
        units = {row["section_id"]: row["extracted_text"] for row in _rows(connection, "SELECT section_id, extracted_text FROM source_sections")}
        citations = []
        for row in _rows(connection, "SELECT * FROM claim_citations ORDER BY claim_id, ordinal"):
            claim = claims[row["claim_id"]]
            task_id = f"{row['claim_id']}#c{row['ordinal']}"
            entry = entries.get(row["entry_id"]) if row["entry_id"] else None
            verdict_record: dict[str, Any]
            if task_id in assessments:
                assessment, verdict = assessments[task_id]
                receipt = receipts[assessment["receipt_id"]]
                if assessment["validation_state"] == "accepted":
                    category = error_category(verdict, _ClaimView(claim), [{"text": units.get(q["unitId"], "")}
                                                                           for q in verdict.get("evidence", [])])
                    verdict_record = {
                        "state": "judged", "supportVerdict": verdict["supportVerdict"], "errorCategory": category,
                        "abstain": verdict["supportVerdict"] == "abstain", "escalate": verdict.get("escalate", False),
                        "quotes": [{"unitId": q["unitId"], "quote": q["quote"]} for q in verdict.get("evidence", [])],
                        "rationale": verdict.get("rationale", ""), "evidenceTier": verdict.get("evidenceTier"),
                        "evidenceCoverage": verdict.get("evidenceCoverage"),
                        "flag": "check suggested" if category else None}
                else:
                    verdict_record = {"state": "invalid output", "error": verdict.get("code"), "flag": None}
                verdict_record["receipt"] = {"modelReceiptId": receipt["receipt_id"], "requestSha256": receipt["request_sha256"],
                                             "responseSha256": receipt["response_sha256"], "model": receipt["model"],
                                             "provider": receipt["provider"]}
            else:
                reason = blocked.get(task_id, {}).get("reason")
                if reason is None and row["entry_id"]:
                    first = next((other for other in _rows(connection,
                                  "SELECT ordinal FROM claim_citations WHERE claim_id=? AND entry_id=? ORDER BY ordinal LIMIT 1",
                                  (row["claim_id"], row["entry_id"]))), None)
                    reason = f"same work as citation {first['ordinal']} of this sentence" if first else "not judged"
                verdict_record = {"state": "not judged", "reason": reason or "not judged", "flag": None}
            retrieval = retrievals.get((row["claim_id"], row["ordinal"]))
            citations.append({
                "claimId": row["claim_id"], "ordinal": row["ordinal"], "anchor": claim["anchor"],
                "section": json.loads(claim["locator"]).get("section"), "sentence": claim["claim_text"],
                "marker": row["marker"], "style": row["style"], "resolution": row["resolution"],
                "reference": None if entry is None else {
                    "entryId": entry["entryId"], "label": entry["label"], "printed": entry["printed"],
                    "identity": entry["identity"], "key": _reference_key(entry)},
                "integrity": sorted(set(findings_by_entry.get(row["entry_id"], []))) if row["entry_id"] else [],
                "status": statuses.get(row["entry_id"], {}).get("state") if row["entry_id"] else None,
                "verdict": verdict_record,
                "retrieval": None if retrieval is None else {"retrievalId": retrieval["retrieval_id"],
                                                             "receiptId": retrieval["receipt_id"]},
            })
        uncited = []
        for claim_id, claim in claims.items():
            if claim["citation_count"] == 0 and claim["citation_required"]:
                retrieval = retrievals.get((claim_id, None))
                candidates = []
                if retrieval is not None:
                    seen = []
                    for hit in json.loads(retrieval["receipt_json"])["results"]:
                        if hit["workId"] not in seen:
                            seen.append(hit["workId"])
                    candidates = seen[:5]
                uncited.append({"claimId": claim_id, "anchor": claim["anchor"], "sentence": claim["claim_text"],
                                "reason": claim["requirement_reason"], "candidateWorks": candidates,
                                "retrievalReceiptId": retrieval["receipt_id"] if retrieval else None,
                                "flag": "check suggested"})
        references = []
        cited_counts = Counter(row["entry_id"] for row in _rows(connection, "SELECT entry_id FROM claim_citations WHERE entry_id IS NOT NULL"))
        for entry_id, entry in entries.items():
            references.append({**{key: entry[key] for key in ("entryId", "label", "printed", "identifiers", "identity", "workId")},
                               "key": _reference_key(entry), "citations": cited_counts.get(entry_id, 0),
                               "registry": registry.get(entry_id), "status": statuses.get(entry_id),
                               "integrity": sorted(set(findings_by_entry.get(entry_id, [])))})
        sanitization = Counter((row["scope"], row["kind"]) for row in
                               _rows(connection, "SELECT scope, kind FROM sanitization_records WHERE analysis_id=?", (analysis_id,)))
        run_state = next((json.loads(row["checkpoint_json"]) for row in _rows(
            connection, "SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND task_key='run'", (analysis_id,))), {})
    finally:
        connection.close()
    judged = [item for item in citations if item["verdict"]["state"] == "judged"]
    ledger = EgressLedger(store.root / "egress.jsonl")
    ledger_events = ledger.events()
    summary = {
        "claims": len(claims), "citedClaims": sum(1 for claim in claims.values() if claim["citation_count"]),
        "uncitedClaimsNeedingCitation": len(uncited), "citations": len(citations),
        "citationLoci": len({(item["claimId"], (item["reference"] or {}).get("entryId")) for item in citations if item["reference"]}),
        "resolution": dict(sorted(Counter(item["resolution"] for item in citations).items())),
        "verdictStates": dict(sorted(Counter(item["verdict"]["state"] for item in citations).items())),
        "supportVerdicts": dict(sorted(Counter(item["verdict"]["supportVerdict"] for item in judged).items())),
        "errorCategories": dict(sorted(Counter(item["verdict"]["errorCategory"] for item in judged if item["verdict"]["errorCategory"]).items())),
        "flags": sum(1 for item in citations if item["verdict"].get("flag"))
                 + sum(1 for item in findings if item["severity"] == "check suggested"),
        "integrity": dict(sorted(Counter(item["class"] for item in findings).items())),
        "references": len(references),
        "statuses": dict(sorted(Counter((item["status"] or {}).get("state", "unchecked") for item in references).items())),
        "sanitization": {f"{scope}:{kind}": count for (scope, kind), count in sorted(sanitization.items())},
        "modelCalls": run_state.get("judge", {}).get("calls", 0) + run_state.get("judge", {}).get("repairCalls", 0),
    }
    report = {
        "format": REPORT_FORMAT, "version": REPORT_VERSION, "analysisId": analysis_id, "createdAt": run["analysis_created"],
        "instrument": {"id": TASK, "digest": protocol["instrumentDigest"], "registryDigest": protocol.get("registryDigest"),
                       "definition": protocol["instrument"]},
        "policy": {"mode": protocol["policy"]["mode"], "digest": protocol["policyDigest"],
                   "evidencePolicy": protocol["policy"]["mode"]},
        "manuscript": protocol["manuscript"], "evidence": protocol["evidence"], "datasets": protocol["datasets"],
        "offline": protocol["offline"], "referenceYear": protocol["referenceYear"],
        "calibration": CALIBRATION_STATEMENT, "limitations": run_state.get("limitations", []),
        "summary": summary, "citations": citations, "uncitedClaims": uncited, "integrity": findings,
        "references": references,
        "egress": {"events": len(ledger_events),
                   "byClass": dict(sorted(Counter(f"{e['destinationClass']}:{e['decision']}" for e in ledger_events).items()))},
    }
    report["reportSha256"] = sha256_text(canonical(report))
    return report


class _ClaimView:
    def __init__(self, row: Mapping[str, Any]):
        self.text = row["claim_text"]
        self.claim_type = row["claim_type"]


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = ["# Reference and claim screen", "",
             f"Instrument `{report['instrument']['id']}` (digest `{report['instrument']['digest'][:16]}…`), policy "
             f"`{report['policy']['mode']}`, evidence index `{report['evidence']['indexDigest'][:16]}…`.", "",
             f"> {report['calibration']}", "", "## Summary", "",
             "| Measure | Value |", "| --- | --- |"]
    for key in ("claims", "citedClaims", "uncitedClaimsNeedingCitation", "citations", "citationLoci", "references", "flags", "modelCalls"):
        lines.append(f"| {key} | {summary[key]} |")
    for key in ("resolution", "verdictStates", "supportVerdicts", "errorCategories", "integrity", "statuses", "sanitization"):
        if summary[key]:
            lines.append(f"| {key} | " + ", ".join(f"{name}: {count}" for name, count in summary[key].items()) + " |")
    lines += ["", "## Integrity findings", ""]
    if not report["integrity"]:
        lines.append("None.")
    for item in report["integrity"]:
        lines.append(f"- **{item['class']}** ({item['severity']}) — {item['subjectKind']} `{item['subjectId']}`")
    lines += ["", "## Citations", "", "| Sentence | Marker | Reference | Verdict | Category | Quote |", "| --- | --- | --- | --- | --- | --- |"]
    for item in report["citations"]:
        verdict = item["verdict"]
        quote = verdict.get("quotes", [{}])[0].get("quote", "") if verdict.get("quotes") else ""
        reference = (item["reference"] or {}).get("printed", "")[:80] if item["reference"] else f"({item['resolution']})"
        state = verdict.get("supportVerdict") or verdict["state"] + (f": {verdict.get('reason')}" if verdict.get("reason") else "")
        lines.append(f"| {_md(item['sentence'][:160])} | {_md(item['marker'])} | {_md(reference)} | {_md(state)} | "
                     f"{_md(verdict.get('errorCategory') or '')} | {_md(quote[:120])} |")
    if report["uncitedClaims"]:
        lines += ["", "## Uncited claims that need a citation (check suggested)", ""]
        for item in report["uncitedClaims"]:
            lines.append(f"- {_md(item['sentence'][:200])} — candidates: {', '.join(item['candidateWorks']) or 'none'}")
    if report.get("diff"):
        diff = report["diff"]
        lines += ["", "## Changes since the earlier report", ""]
        for key in ("referencesAdded", "referencesRemoved", "referencesChanged", "findingsNew", "findingsResolved",
                    "verdictsChanged", "statusChanges"):
            lines.append(f"- {key}: {len(diff[key])}")
    lines += ["", f"Report digest `{report['reportSha256']}`.", ""]
    return "\n".join(lines)


def render_dossier(report: Mapping[str, Any], *, controls: int = 10) -> str:
    """Spot-check items for a reviewer: what the judge saw, what it said, and boxes to tick."""

    flagged = [item for item in report["citations"] if item["verdict"].get("flag")]
    supported = [item for item in report["citations"] if item["verdict"].get("supportVerdict") == "direct-support"]
    picker = random.Random(int(report["reportSha256"][:12], 16))
    sample = picker.sample(supported, min(controls, len(supported)))
    lines = ["# Spot-check dossier", "",
             "Each item shows the sentence the judge assessed, the cited reference, the verdict with its exact quote and "
             "rationale, and the receipt that lets the call be replayed. Tick one box per item; the boxes are for your own "
             "check of the verdict, not for re-judging the citation from memory.", "",
             f"> {report['calibration']}", ""]
    for heading, items in (("Flagged citations (check suggested)", flagged), ("Controls: sampled supported verdicts", sample)):
        lines += [f"## {heading} ({len(items)})", ""]
        for number, item in enumerate(items, 1):
            verdict = item["verdict"]
            lines.append(f"**Item {number}** — `{item['claimId']}` citation {item['ordinal']} — verdict "
                         f"**{verdict.get('supportVerdict')}** ({verdict.get('errorCategory') or 'no category'}), "
                         f"tier {verdict.get('evidenceTier')}")
            lines.append(f"- Sentence: {_md(item['sentence'])}")
            lines.append(f"- Reference: {_md((item['reference'] or {}).get('printed', ''))}")
            for quote in verdict.get("quotes", []):
                lines.append(f"- Quote ({quote['unitId']}): “{_md(quote['quote'][:400])}”")
            lines.append(f"- Rationale: {_md(verdict.get('rationale', ''))}")
            receipt = verdict.get("receipt", {})
            lines.append(f"- Receipt: request `{receipt.get('requestSha256', '')[:16]}…`, response "
                         f"`{(receipt.get('responseSha256') or '')[:16]}…`, model {receipt.get('model')}")
            lines.append("- Your check: [ ] agree   [ ] disagree   [ ] cannot tell")
            lines.append("")
    integrity = [item for item in report["integrity"] if item["severity"] == "check suggested"]
    lines += [f"## Integrity findings to confirm ({len(integrity)})", ""]
    for item in integrity:
        lines.append(f"- **{item['class']}** — {item['subjectKind']} `{item['subjectId']}` — "
                     "[ ] confirmed   [ ] not confirmed   [ ] cannot tell")
    lines.append("")
    return "\n".join(lines)


def write_outputs(root: Path, report: Mapping[str, Any]) -> None:
    for name, content in (("report.json", json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"),
                          ("report.md", render_markdown(report)), ("dossier.md", render_dossier(report))):
        path = Path(root) / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)


# Diff --------------------------------------------------------------------------------------

def diff_reports(earlier: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """What changed between two screens of the same instrument, e.g. after a revision."""

    old_refs = {item["key"]: item for item in earlier.get("references", [])}
    new_refs = {item["key"]: item for item in current.get("references", [])}
    old_findings = {item["findingId"]: item for item in earlier.get("integrity", [])}
    new_findings = {item["findingId"]: item for item in current.get("integrity", [])}

    def verdicts(report: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
        values = {}
        for item in report.get("citations", []):
            if item.get("reference") and item["verdict"]["state"] == "judged":
                values[(sha256_text(item["sentence"]), item["reference"]["key"])] = item["verdict"]
        return values

    old_verdicts, new_verdicts = verdicts(earlier), verdicts(current)
    changed_verdicts = []
    for key in sorted(set(old_verdicts) & set(new_verdicts)):
        before, after = old_verdicts[key], new_verdicts[key]
        if (before.get("supportVerdict"), before.get("errorCategory")) != (after.get("supportVerdict"), after.get("errorCategory")):
            changed_verdicts.append({"claimSha256": key[0], "reference": key[1], "before": before.get("supportVerdict"),
                                     "after": after.get("supportVerdict"), "categoryBefore": before.get("errorCategory"),
                                     "categoryAfter": after.get("errorCategory")})
    status_changes = []
    for key in sorted(set(old_refs) & set(new_refs)):
        before = (old_refs[key].get("status") or {}).get("state")
        after = (new_refs[key].get("status") or {}).get("state")
        if before != after:
            status_changes.append({"reference": key, "before": before, "after": after,
                                   "notices": (new_refs[key].get("status") or {}).get("notices", [])})
    same_instrument = earlier.get("instrument", {}).get("digest") == current.get("instrument", {}).get("digest")
    return {
        "earlierReportSha256": earlier.get("reportSha256"), "currentReportSha256": current.get("reportSha256"),
        "sameInstrument": same_instrument,
        "referencesAdded": sorted(set(new_refs) - set(old_refs)),
        "referencesRemoved": sorted(set(old_refs) - set(new_refs)),
        "referencesChanged": sorted(key for key in set(old_refs) & set(new_refs)
                                    if old_refs[key].get("printed") != new_refs[key].get("printed")),
        "findingsNew": [{"findingId": key, "class": new_findings[key]["class"], "subjectId": new_findings[key]["subjectId"]}
                        for key in sorted(set(new_findings) - set(old_findings))],
        "findingsResolved": [{"findingId": key, "class": old_findings[key]["class"], "subjectId": old_findings[key]["subjectId"]}
                             for key in sorted(set(old_findings) - set(new_findings))],
        "verdictsChanged": changed_verdicts,
        "verdictsNew": len(set(new_verdicts) - set(old_verdicts)),
        "verdictsDropped": len(set(old_verdicts) - set(new_verdicts)),
        "statusChanges": status_changes,
    }


# Replay ------------------------------------------------------------------------------------

def replay(run_root: Path) -> dict[str, Any]:
    """Re-derive every verdict from stored request/response bytes; no model call is made."""

    store = RunStore(run_root)
    validator = AssessmentValidator()
    connection = store.connect()
    mismatches: list[dict[str, Any]] = []
    rederived = matching = 0
    try:
        receipts = {row["receipt_id"]: dict(row) for row in connection.execute("SELECT * FROM model_receipts")}
        cache: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM assessments ORDER BY assessment_id"):
            stored = json.loads(row["verdict_json"])
            task_id = stored.get("taskId") or stored.get("id")
            receipt = receipts[row["receipt_id"]]
            if receipt["response_sha256"] is None:
                rederived += 1
                ok = row["validation_state"] == "rejected"
                matching += ok
                if not ok:
                    mismatches.append({"taskId": task_id, "reason": "failed call has an accepted verdict"})
                continue
            if receipt["receipt_id"] not in cache:
                request = json.loads(store.read_artifact(receipt["request_sha256"]))
                payload = json.loads(request["messages"][1]["content"])
                tasks = [{**item, "evidenceTier": payload["evidenceTier"]} for item in payload["items"]]
                response = store.read_artifact(receipt["response_sha256"]).decode("utf-8")
                cache[receipt["receipt_id"]] = validator.validate_response(tasks, response)
            result = cache[receipt["receipt_id"]]
            rederived += 1
            if row["validation_state"] == "accepted":
                derived = result["accepted"].get(task_id)
                same = derived is not None and all(derived.get(key) == stored.get(key) for key in
                                                   ("supportVerdict", "evidence", "rationale", "escalate", "evidenceDigest"))
            else:
                same = task_id in result["rejected"] or task_id not in result["accepted"]
            matching += bool(same)
            if not same:
                mismatches.append({"taskId": task_id, "receiptId": receipt["receipt_id"]})
        retrievals = [json.loads(row["receipt_json"]) for row in connection.execute("SELECT receipt_json FROM retrieval_receipts")]
    finally:
        connection.close()
    reproduced = 0
    with EvidenceIndex(store.root / "evidence.sqlite3") as index:
        for receipt in retrievals:
            reproduced += bool(index.replay(receipt)["reproduced"])
    stored_report = json.loads((store.root / "report.json").read_text(encoding="utf-8"))
    rebuilt = build_report(store)
    # The digest covers the report as built from the database; a diff section is added afterwards.
    stored_digest = stored_report.get("reportSha256")
    return {"modelCalls": 0, "verdicts": rederived, "verdictsMatching": matching, "mismatches": mismatches,
            "retrievalReceipts": len(retrievals), "retrievalReproduced": reproduced,
            "reportReproduced": rebuilt["reportSha256"] == stored_digest, "reportSha256": rebuilt["reportSha256"],
            "ledger": EgressLedger(store.root / "egress.jsonl").verify()}


# CLI ---------------------------------------------------------------------------------------

def add_commands(families: Any, register: Any) -> None:
    screen = families.add_parser("screen", help="screen a manuscript's references and claims with receipts")
    screen.add_argument("input", help="manuscript inside the selected workspace (md, txt, docx, pdf, jats/xml, html)")
    screen.add_argument("--workspace", default=".")
    source = screen.add_mutually_exclusive_group()
    source.add_argument("--evidence", help="evidence store (JSON or JSON Lines of works with units) inside the workspace")
    source.add_argument("--library", help="literature library database (corpus.sqlite3) inside the workspace")
    screen.add_argument("--policy", help="operator-managed claim-check egress policy (JSON)")
    screen.add_argument("--public", action="store_true", help="the manuscript is public (policy mode public-literature)")
    screen.add_argument("--judge-url", help="OpenAI-compatible chat-completions URL of the judge")
    screen.add_argument("--judge-model", help="model name sent to the judge")
    screen.add_argument("--judge-location", choices=("on-prem", "external"), default="on-prem")
    screen.add_argument("--offline", action="store_true", help="no provider requests (local classes only)")
    screen.add_argument("--retraction-watch", help="offline Retraction Watch CSV inside the workspace")
    screen.add_argument("--retraction-watch-version", help="declared release of that file, recorded with the run")
    screen.add_argument("--diff", help="earlier report.json to compare against, e.g. after a revision")
    screen.add_argument("--output", default=".ocsl-review/screen")
    screen.add_argument("--reference-year", type=int, help="the manuscript's own year (impossible-year check)")
    screen.add_argument("--k", type=int, default=5)
    screen.add_argument("--max-evidence-characters", type=int, default=11000)
    register(screen, "screen", "cmd_screen", "workspace", state_required=False)
    replay_parser = families.add_parser("replay", help="re-derive a screen's verdicts from its receipts without model calls")
    replay_parser.add_argument("--run", required=True, help="screen run directory inside the workspace")
    replay_parser.add_argument("--workspace", default=".")
    register(replay_parser, "screen", "cmd_replay", "read-only", state_required=False)


def _workspace(args: Any) -> Path:
    from .workflow import _workspace as review_workspace
    return review_workspace(args)


def _inside(workspace: Path, value: str, *, must_exist: bool = True) -> Path:
    from .workflow import _inside as review_inside
    return review_inside(workspace, value, must_exist=must_exist)


def cmd_screen(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    manuscript = _inside(workspace, args.input)
    policy = ConfidentialPolicy.load(_inside(workspace, args.policy) if args.policy else None)
    if args.public:
        policy = policy.with_mode("public-literature")
    if args.evidence:
        works = load_store(_inside(workspace, args.evidence))
        evidence_name = Path(args.evidence).name
    elif args.library:
        works = load_library(_inside(workspace, args.library))
        evidence_name = Path(args.library).name
    else:
        works, evidence_name = [], "none"
    judge = None
    if args.judge_url:
        if not args.judge_model:
            raise ValidationError("--judge-url needs --judge-model")
        judge = HttpJudge(args.judge_url, args.judge_model, location=args.judge_location)
    datasets = []
    if args.retraction_watch:
        datasets.append(I.RetractionWatchDataset(_inside(workspace, args.retraction_watch),
                                                 declared_version=args.retraction_watch_version))
    earlier = None
    if args.diff:
        earlier = json.loads(_inside(workspace, args.diff).read_text(encoding="utf-8"))
    output = _inside(workspace, args.output, must_exist=False)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = output / f"screen-{stamp}-{sha256_bytes(manuscript.read_bytes())[:8]}"
    report = run_screen(manuscript, run_root=run_root, works=works, evidence_name=evidence_name, policy=policy,
                        judge=judge, offline=bool(args.offline), datasets=datasets, reference_year=args.reference_year,
                        earlier_report=earlier, k=args.k, max_evidence_characters=args.max_evidence_characters,
                        scholarly_workspace=workspace)
    return {"run": str(run_root), "report": str(run_root / "report.json"), "markdown": str(run_root / "report.md"),
            "dossier": str(run_root / "dossier.md"), "reportSha256": report["reportSha256"], "summary": report["summary"],
            "diff": {key: len(value) if isinstance(value, list) else value for key, value in report.get("diff", {}).items()}}


def cmd_replay(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    run_root = _inside(workspace, args.run, must_exist=False)
    if not (run_root / "screen.sqlite3").is_file():
        raise ReviewError(f"not a screen run: {run_root}")
    return replay(run_root)


__all__ = ["REPORT_FORMAT", "add_commands", "build_report", "cmd_replay", "cmd_screen", "diff_reports",
           "render_dossier", "render_markdown", "replay", "run_screen", "write_outputs"]
