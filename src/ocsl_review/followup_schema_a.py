# SPDX-License-Identifier: Apache-2.0
"""Follow-up branch tables private to the evidence modules: status lookups, cited-by traversal, support evidence and
alert records (followup_status, followup_citedby, followup_support, followup_exports).

Only those modules write these tables. Interface tables that other modules read live in ``followup_schema``.
"""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS fu_citedby_coverage(
  target_doi TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL
    CHECK(state IN ('complete','truncated','unavailable','error','no-identifier')),
  hit_count INTEGER, edges INTEGER NOT NULL, reason TEXT, checked_at TEXT NOT NULL,
  PRIMARY KEY(target_doi, source));
CREATE TABLE IF NOT EXISTS fu_evidence(
  evidence_id TEXT PRIMARY KEY, work_key TEXT NOT NULL, unit TEXT NOT NULL CHECK(unit IN ('abstract','full-text')),
  provider TEXT, route TEXT, provenance_class TEXT CHECK(provenance_class IS NULL OR
    provenance_class IN ('public','entitled')), source_sha256 TEXT, text_sha256 TEXT, characters INTEGER,
  complete INTEGER CHECK(complete IS NULL OR complete IN (0,1)),
  outcome TEXT NOT NULL CHECK(outcome IN ('obtained','fetch-failed','no-open-full-text','unavailable','incomplete')),
  request_id TEXT, retrieved_at TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_fu_evidence_work ON fu_evidence(work_key, unit);
CREATE TABLE IF NOT EXISTS fu_evidence_units(
  evidence_id TEXT NOT NULL REFERENCES fu_evidence(evidence_id), unit_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(evidence_id, unit_id));
CREATE TABLE IF NOT EXISTS fu_alerts(
  alert_id TEXT PRIMARY KEY, journal TEXT NOT NULL, doi TEXT NOT NULL, claim_id TEXT NOT NULL, locus_id TEXT NOT NULL,
  work_key TEXT NOT NULL, status TEXT NOT NULL, notice_date TEXT, function_class TEXT, dependency_class TEXT,
  support_json TEXT NOT NULL, confidence TEXT NOT NULL, suggested_action TEXT NOT NULL, created_at TEXT NOT NULL);
"""

DICTIONARY: dict[str, dict[str, str]] = {
    "fu_citedby_coverage": {
        "target_doi": "cited article (an affected review)", "source": "europe-pmc or openalex",
        "state": "complete, truncated, unavailable, error or no-identifier", "hit_count": "total the source reported",
        "edges": "edges recorded", "reason": "why the traversal is not complete", "checked_at": "time checked"},
    "fu_evidence": {
        "evidence_id": "evidence id (digest of work, unit, provider and source digest)", "work_key": "cited work",
        "unit": "abstract or full-text", "provider": "provider of the bytes", "route": "retrieval route",
        "provenance_class": "public or entitled (entitled text reaches on-premises judges only)",
        "source_sha256": "sha256 of the source bytes", "text_sha256": "sha256 of the extracted text",
        "characters": "characters of extracted text", "complete": "1 when the evidence covers the whole unit",
        "outcome": "obtained, fetch-failed, no-open-full-text, unavailable or incomplete",
        "request_id": "provider request (fu_requests)", "retrieved_at": "time retrieved",
        "detail_json": "retrieval details (attempts, cache hits)"},
    "fu_evidence_units": {"evidence_id": "evidence", "unit_id": "unit id", "ordinal": "document order",
                          "kind": "abstract, paragraph, caption, table", "text": "unit text"},
    "fu_alerts": {
        "alert_id": "alert id", "journal": "journal of the citing review", "doi": "citing review DOI",
        "claim_id": "citing sentence", "locus_id": "frame locus", "work_key": "compromised cited work",
        "status": "status of the cited work", "notice_date": "notice date", "function_class": "citation function",
        "dependency_class": "dependency class", "support_json": "support verdicts with quotes",
        "confidence": "check suggested (no calibration set measures a positive predictive value)",
        "suggested_action": "suggested action for the authors of the citing work or a study team",
        "created_at": "time created"},
}
