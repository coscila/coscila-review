# SPDX-License-Identifier: Apache-2.0
"""Follow-up branch tables private to the analysis modules: second-generation sampling, documents, sentences and
candidate searches, and the retraction-lag rows (followup_secondgen, followup_stats).

Only those modules write these tables. Interface tables that other modules read live in ``followup_schema``.
"""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS fu_secondgen_sample(
  anchor_locus_id TEXT NOT NULL, edge_id TEXT NOT NULL, citing_key TEXT NOT NULL, target_doi TEXT NOT NULL,
  available INTEGER NOT NULL CHECK(available >= 1), sampled INTEGER NOT NULL CHECK(sampled >= 1),
  inclusion_probability REAL NOT NULL CHECK(inclusion_probability > 0 AND inclusion_probability <= 1),
  seed TEXT NOT NULL, selected_at TEXT NOT NULL, PRIMARY KEY(anchor_locus_id, edge_id));
CREATE INDEX IF NOT EXISTS ix_fu_secondgen_sample_citing ON fu_secondgen_sample(citing_key);
CREATE TABLE IF NOT EXISTS fu_secondgen_documents(
  citing_key TEXT PRIMARY KEY, provider TEXT, provenance_class TEXT, source_sha256 TEXT,
  outcome TEXT NOT NULL CHECK(outcome IN ('obtained','fetch-failed','no-open-full-text','unavailable',
                                          'citation-not-located')),
  request_id TEXT, retrieved_at TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS fu_secondgen_sentences(
  sentence_id TEXT PRIMARY KEY, citing_key TEXT NOT NULL, target_doi TEXT NOT NULL, ordinal INTEGER NOT NULL,
  section TEXT, marker TEXT, text TEXT NOT NULL, text_sha256 TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fu_secondgen_sentences_work ON fu_secondgen_sentences(citing_key, target_doi);
CREATE TABLE IF NOT EXISTS fu_secondgen_candidates(
  anchor_locus_id TEXT NOT NULL, citing_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('ranked','anchor-text-unavailable','no-sentences')),
  reason TEXT, receipt_id TEXT, receipt_sha256 TEXT, index_digest TEXT, k INTEGER NOT NULL, sentences INTEGER NOT NULL,
  candidates INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(anchor_locus_id, citing_key));
CREATE TABLE IF NOT EXISTS fu_lag(
  locus_id TEXT PRIMARY KEY, work_key TEXT NOT NULL, status TEXT NOT NULL, notice_date TEXT, citing_first_online TEXT,
  lag_days INTEGER, after_notice INTEGER CHECK(after_notice IS NULL OR after_notice IN (0,1)), date_basis TEXT NOT NULL,
  reason TEXT);
"""

DICTIONARY: dict[str, dict[str, str]] = {
    "fu_secondgen_sample": {
        "anchor_locus_id": "S1 locus whose claim is followed (dependency sole or essential)",
        "edge_id": "sampled cited-by edge (fu_citedby_edges)", "citing_key": "citing work",
        "target_doi": "the anchor's review, DOI in lower case",
        "available": "cited-by edges of the anchor's review", "sampled": "edges sampled for the anchor",
        "inclusion_probability": "realised inclusion probability of the edge (sampled / available)",
        "seed": "selection seed (protocol seed and scope)", "selected_at": "time selected"},
    "fu_secondgen_documents": {
        "citing_key": "citing work", "provider": "provider of the full text", "provenance_class": "public or entitled",
        "source_sha256": "sha256 of the document bytes (fu_artifacts)", "outcome": "obtained, fetch-failed, "
        "no-open-full-text, unavailable or citation-not-located", "request_id": "provider request (fu_requests)",
        "retrieved_at": "time retrieved", "detail_json": "retrieval details (identifiers asked, failure class)"},
    "fu_secondgen_sentences": {
        "sentence_id": "sentence id (digest of citing work, target and sentence digest)", "citing_key": "citing work",
        "target_doi": "the cited review", "ordinal": "document order", "section": "section trail",
        "marker": "marker of the cited review as rendered in the sentence", "text": "sentence text",
        "text_sha256": "sha256 of the sentence"},
    "fu_secondgen_candidates": {
        "anchor_locus_id": "S1 locus whose claim is followed", "citing_key": "sampled citing work",
        "state": "ranked, anchor-text-unavailable or no-sentences", "reason": "why no search ran",
        "receipt_id": "retrieval receipt id", "receipt_sha256": "stored retrieval receipt (fu_artifacts)",
        "index_digest": "digest of the BM25 index searched", "k": "candidates kept per citing work (protocol)",
        "sentences": "sentences of the citing work that cite the review", "candidates": "sentences ranked within k",
        "created_at": "time searched"},
    "fu_lag": {
        "locus_id": "S1 locus", "work_key": "compromised cited work", "status": "status of the cited work",
        "notice_date": "date of the deciding notice", "citing_first_online": "first-online date of the citing review",
        "lag_days": "citing date minus notice date in days (both dates given to the day)",
        "after_notice": "1 when the review went online after the notice, 0 before, NULL when undecidable",
        "date_basis": "precision both dates were compared at (day, month or year), or none",
        "reason": "why the comparison is undecidable"},
}
