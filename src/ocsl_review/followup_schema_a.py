# SPDX-License-Identifier: Apache-2.0
"""Follow-up branch tables private to the evidence modules: cited-by coverage, support evidence and alert records
(followup_citedby, followup_support, followup_exports).

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
  kind TEXT NOT NULL, locator TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, PRIMARY KEY(evidence_id, unit_id));
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
        "detail_json": "retrieval details (attempts, cache hits, rejected cache candidates)"},
    "fu_evidence_units": {"evidence_id": "evidence", "unit_id": "unit id", "ordinal": "document order",
                          "kind": "abstract, paragraph, caption or table", "locator": "section trail of the unit",
                          "text": "unit text"},
}
