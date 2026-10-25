-- Apache-2.0. Additive claim-check extension of the literature schema
-- (skills/ocsl-review-reference-analysis/assets/literature-schema.sql).
-- claim_loci binds one claim to one bibliography entry; a claim here carries 0..n citations,
-- so uncited claims and unresolved markers are first-class. Every statement is idempotent, so
-- the extension can be applied to an existing literature library without migrating it.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    review_id TEXT NOT NULL REFERENCES reviews,
    review_artifact_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    anchor TEXT NOT NULL,
    locator TEXT NOT NULL CHECK(json_valid(locator)),
    claim_text TEXT NOT NULL,
    claim_text_sha256 TEXT NOT NULL CHECK(length(claim_text_sha256) = 64),
    context_json TEXT NOT NULL CHECK(json_valid(context_json)),
    citation_count INTEGER NOT NULL CHECK(citation_count >= 0),
    citation_required INTEGER NOT NULL CHECK(citation_required IN (0, 1)),
    requirement_reason TEXT NOT NULL,
    origin TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    UNIQUE(analysis_id, review_artifact_sha256, anchor)
);
CREATE INDEX IF NOT EXISTS claims_by_analysis ON claims(analysis_id, citation_count);

CREATE TABLE IF NOT EXISTS claim_citations (
    claim_id TEXT NOT NULL REFERENCES claims,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    cluster_id TEXT NOT NULL,
    marker TEXT NOT NULL,
    style TEXT NOT NULL,
    entry_id TEXT REFERENCES bibliography_entries,
    resolution TEXT NOT NULL CHECK(resolution IN ('resolved', 'ambiguous', 'unresolved')),
    resolution_method TEXT NOT NULL,
    locus_id TEXT REFERENCES claim_loci,
    PRIMARY KEY(claim_id, ordinal),
    CHECK(resolution != 'resolved' OR entry_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS claim_citations_by_entry ON claim_citations(entry_id);

CREATE TABLE IF NOT EXISTS retrieval_receipts (
    retrieval_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    claim_id TEXT NOT NULL REFERENCES claims,
    citation_ordinal INTEGER,
    scope TEXT NOT NULL CHECK(scope IN ('cited-work', 'store')),
    receipt_id TEXT NOT NULL CHECK(length(receipt_id) = 64),
    index_digest TEXT NOT NULL CHECK(length(index_digest) = 64),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS retrieval_by_claim ON retrieval_receipts(analysis_id, claim_id);

CREATE TABLE IF NOT EXISTS integrity_datasets (
    dataset_version_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    declared_version TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    row_count INTEGER NOT NULL CHECK(row_count >= 0),
    file_name TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integrity_findings (
    analysis_id TEXT NOT NULL REFERENCES analyses,
    finding_id TEXT NOT NULL,
    finding_class TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('check suggested', 'info')),
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    dataset_version_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(analysis_id, finding_id)
);

CREATE TABLE IF NOT EXISTS registry_checks (
    analysis_id TEXT NOT NULL REFERENCES analyses,
    entry_id TEXT NOT NULL REFERENCES bibliography_entries,
    doi TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('registered', 'registered-endpoint-artefact', 'not-registered',
                                            'inconclusive', 'malformed')),
    endpoints_json TEXT NOT NULL CHECK(json_valid(endpoints_json)),
    checked_at TEXT NOT NULL,
    PRIMARY KEY(analysis_id, entry_id)
);

-- Dated publication status: the scholarly status result is kept as a content-addressed
-- artifact, next to the dataset version (for example a Retraction Watch release) it was read with.
CREATE TABLE IF NOT EXISTS work_status_observations (
    observation_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    entry_id TEXT REFERENCES bibliography_entries,
    work_id TEXT REFERENCES works,
    doi TEXT,
    pmid TEXT,
    state TEXT NOT NULL CHECK(state IN ('retracted', 'expression-of-concern', 'corrected', 'reinstated',
                                        'no-notice-found', 'unknown')),
    notices_json TEXT NOT NULL CHECK(json_valid(notices_json)),
    sources_json TEXT NOT NULL CHECK(json_valid(sources_json)),
    limitations_json TEXT NOT NULL CHECK(json_valid(limitations_json)),
    scholarly_state TEXT,
    result_sha256 TEXT REFERENCES source_artifacts(sha256),
    dataset_version_id TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sanitization_records (
    record_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses,
    scope TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    rule TEXT NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    length INTEGER NOT NULL CHECK(length >= 0),
    removed_sha256 TEXT NOT NULL CHECK(length(removed_sha256) = 64),
    sanitizer_version TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS immutable_claim_update BEFORE UPDATE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_claim_delete BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_claim_citation_update BEFORE UPDATE ON claim_citations
BEGIN SELECT RAISE(ABORT, 'claim citations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_claim_citation_delete BEFORE DELETE ON claim_citations
BEGIN SELECT RAISE(ABORT, 'claim citations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_retrieval_receipt_update BEFORE UPDATE ON retrieval_receipts
BEGIN SELECT RAISE(ABORT, 'retrieval receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_retrieval_receipt_delete BEFORE DELETE ON retrieval_receipts
BEGIN SELECT RAISE(ABORT, 'retrieval receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_integrity_finding_update BEFORE UPDATE ON integrity_findings
BEGIN SELECT RAISE(ABORT, 'integrity findings are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_integrity_finding_delete BEFORE DELETE ON integrity_findings
BEGIN SELECT RAISE(ABORT, 'integrity findings are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_registry_check_update BEFORE UPDATE ON registry_checks
BEGIN SELECT RAISE(ABORT, 'registry checks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_registry_check_delete BEFORE DELETE ON registry_checks
BEGIN SELECT RAISE(ABORT, 'registry checks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_status_observation_update BEFORE UPDATE ON work_status_observations
BEGIN SELECT RAISE(ABORT, 'status observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_status_observation_delete BEFORE DELETE ON work_status_observations
BEGIN SELECT RAISE(ABORT, 'status observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_sanitization_update BEFORE UPDATE ON sanitization_records
BEGIN SELECT RAISE(ABORT, 'sanitization records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_sanitization_delete BEFORE DELETE ON sanitization_records
BEGIN SELECT RAISE(ABORT, 'sanitization records are append-only'); END;
