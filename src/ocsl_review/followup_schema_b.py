# SPDX-License-Identifier: Apache-2.0
"""Follow-up branch tables private to the analysis modules: second-generation sampling, documents and sentences, and
the retraction-lag rows (followup_secondgen, followup_stats).

Only those modules write these tables. Interface tables that other modules read live in ``followup_schema``.
"""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS fu_secondgen_sample(
  edge_id TEXT PRIMARY KEY, anchor_locus_id TEXT NOT NULL, citing_key TEXT NOT NULL,
  inclusion_probability REAL NOT NULL CHECK(inclusion_probability > 0 AND inclusion_probability <= 1),
  seed TEXT NOT NULL, selected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_secondgen_documents(
  citing_key TEXT PRIMARY KEY, provider TEXT, provenance_class TEXT, source_sha256 TEXT,
  outcome TEXT NOT NULL CHECK(outcome IN ('obtained','fetch-failed','no-open-full-text','unavailable',
                                          'citation-not-located')),
  request_id TEXT, retrieved_at TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS fu_secondgen_sentences(
  sentence_id TEXT PRIMARY KEY, citing_key TEXT NOT NULL, target_doi TEXT NOT NULL, ordinal INTEGER NOT NULL,
  section TEXT, marker TEXT, text TEXT NOT NULL, text_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_lag(
  locus_id TEXT PRIMARY KEY, work_key TEXT NOT NULL, notice_date TEXT, citing_first_online TEXT, lag_days INTEGER,
  after_notice INTEGER CHECK(after_notice IS NULL OR after_notice IN (0,1)), date_basis TEXT NOT NULL);
"""

DICTIONARY: dict[str, dict[str, str]] = {
    "fu_secondgen_sample": {
        "edge_id": "sampled cited-by edge", "anchor_locus_id": "S1 locus whose claim is followed",
        "citing_key": "citing work", "inclusion_probability": "realised inclusion probability of the edge",
        "seed": "selection seed (protocol seed and scope)", "selected_at": "time selected"},
    "fu_secondgen_documents": {
        "citing_key": "citing work", "provider": "provider of the full text", "provenance_class": "public or entitled",
        "source_sha256": "sha256 of the document bytes", "outcome": "obtained, fetch-failed, no-open-full-text, "
        "unavailable or citation-not-located", "request_id": "provider request", "retrieved_at": "time retrieved",
        "detail_json": "retrieval details"},
    "fu_secondgen_sentences": {
        "sentence_id": "sentence id (digest of citing work, target and sentence digest)", "citing_key": "citing work",
        "target_doi": "the cited review", "ordinal": "document order", "section": "section trail",
        "marker": "marker of the cited review as rendered in the sentence", "text": "sentence text",
        "text_sha256": "sha256 of the sentence"},
    "fu_lag": {
        "locus_id": "S1 locus", "work_key": "compromised cited work", "notice_date": "notice date",
        "citing_first_online": "first-online date of the citing review", "lag_days": "citing date minus notice date",
        "after_notice": "1 when the review went online after the notice", "date_basis": "precision rule applied"},
}
