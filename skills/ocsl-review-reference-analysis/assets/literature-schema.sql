-- Apache-2.0. Corpus evidence is independent of any particular research question.
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE corpora (
    corpus_id TEXT PRIMARY KEY, description TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES corpora,
    protocol_json TEXT NOT NULL CHECK(json_valid(protocol_json)),
    protocol_sha256 TEXT NOT NULL CHECK(length(protocol_sha256) = 64),
    code_revision TEXT, created_at TEXT NOT NULL
);
CREATE TABLE analyses (
    analysis_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
    question TEXT NOT NULL CHECK(length(trim(question)) > 0),
    parent_manifest_sha256 TEXT CHECK(length(parent_manifest_sha256) = 64),
    created_at TEXT NOT NULL
);
CREATE TABLE works (
    work_id TEXT PRIMARY KEY, title TEXT NOT NULL, publication_year INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json))
);
-- Content-addressed files contain original bytes; no host-specific absolute paths.
CREATE TABLE source_artifacts (
    sha256 TEXT PRIMARY KEY CHECK(length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    relative_path TEXT NOT NULL UNIQUE CHECK(relative_path = 'sources/' || sha256),
    byte_count INTEGER NOT NULL CHECK(byte_count >= 0), media_type TEXT NOT NULL,
    artifact_kind TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER immutable_artifact_update BEFORE UPDATE ON source_artifacts
BEGIN SELECT RAISE(ABORT, 'source artifacts are immutable'); END;
CREATE TRIGGER immutable_artifact_delete BEFORE DELETE ON source_artifacts
BEGIN SELECT RAISE(ABORT, 'source artifacts are immutable'); END;
CREATE TABLE work_identifiers (
    work_id TEXT NOT NULL REFERENCES works, scheme TEXT NOT NULL, value TEXT NOT NULL,
    verified_by_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    PRIMARY KEY(scheme, value)
);
CREATE INDEX identifiers_by_work ON work_identifiers(work_id);
CREATE TABLE retrievals (
    retrieval_id TEXT PRIMARY KEY, work_id TEXT REFERENCES works,
    artifact_sha256 TEXT REFERENCES source_artifacts(sha256), provider TEXT NOT NULL,
    request_url TEXT NOT NULL, retrieved_at TEXT NOT NULL, response_status INTEGER,
    access_status TEXT NOT NULL CHECK(access_status IN ('acquired','unavailable','blocked','failed')),
    license TEXT, detail TEXT,
    CHECK(access_status != 'acquired' OR artifact_sha256 IS NOT NULL)
);
CREATE INDEX retrievals_by_work ON retrievals(work_id);
CREATE TABLE source_sections (
    section_id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works,
    artifact_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    locator TEXT NOT NULL, section_kind TEXT NOT NULL,
    extracted_text TEXT NOT NULL, text_sha256 TEXT NOT NULL CHECK(length(text_sha256) = 64),
    extractor_version TEXT NOT NULL, is_complete INTEGER NOT NULL CHECK(is_complete IN (0,1))
);
CREATE INDEX sections_by_work ON source_sections(work_id, section_kind);
CREATE TABLE reviews (
    review_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES corpora,
    work_id TEXT NOT NULL REFERENCES works,
    inclusion_state TEXT NOT NULL CHECK(inclusion_state IN ('included','excluded','unresolved')),
    strata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(strata_json)),
    selection_reason TEXT NOT NULL, UNIQUE(corpus_id, work_id)
);
CREATE TABLE bibliography_entries (
    entry_id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews,
    reference_label TEXT NOT NULL, raw_reference TEXT NOT NULL,
    candidate_identifiers_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(candidate_identifiers_json)),
    resolved_work_id TEXT REFERENCES works,
    identity_state TEXT NOT NULL CHECK(identity_state IN ('unresolved','verified','conflict','unavailable')),
    identity_evidence_sha256 TEXT REFERENCES source_artifacts(sha256),
    UNIQUE(review_id, reference_label),
    CHECK(identity_state != 'verified' OR
          (resolved_work_id IS NOT NULL AND identity_evidence_sha256 IS NOT NULL))
);
CREATE INDEX entries_by_work ON bibliography_entries(resolved_work_id);
-- Each repeated citation occurrence is retained, even for the same bibliography slot.
CREATE TABLE claim_loci (
    locus_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES bibliography_entries,
    review_artifact_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    locator TEXT NOT NULL, claim_text TEXT NOT NULL,
    context_text TEXT NOT NULL, extractor_version TEXT NOT NULL,
    UNIQUE(entry_id, review_artifact_sha256, locator)
);
CREATE INDEX loci_by_entry ON claim_loci(entry_id);
CREATE TABLE model_receipts (
    receipt_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses,
    task_key TEXT NOT NULL, request_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    response_sha256 TEXT REFERENCES source_artifacts(sha256),
    provider TEXT NOT NULL, model TEXT NOT NULL,
    parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json)),
    usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),
    outcome TEXT NOT NULL CHECK(outcome IN ('completed','failed','uncertain')),
    started_at TEXT NOT NULL, finished_at TEXT,
    CHECK(outcome != 'completed' OR response_sha256 IS NOT NULL),
    UNIQUE(receipt_id, analysis_id)
);
CREATE INDEX receipts_by_analysis ON model_receipts(analysis_id, task_key);
CREATE TABLE assessments (
    assessment_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses,
    locus_id TEXT NOT NULL REFERENCES claim_loci, receipt_id TEXT NOT NULL,
    protocol_version TEXT NOT NULL, evidence_level TEXT NOT NULL,
    verdict_json TEXT NOT NULL CHECK(json_valid(verdict_json)),
    validation_state TEXT NOT NULL CHECK(validation_state IN ('accepted','rejected','unresolved')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(receipt_id, analysis_id) REFERENCES model_receipts(receipt_id, analysis_id)
);
CREATE INDEX assessments_by_analysis_locus ON assessments(analysis_id, locus_id);
CREATE TABLE assessment_evidence (
    assessment_id TEXT NOT NULL REFERENCES assessments,
    section_id TEXT NOT NULL REFERENCES source_sections,
    quote TEXT NOT NULL, locator TEXT NOT NULL,
    PRIMARY KEY(assessment_id, section_id, locator)
);
-- Other research questions reuse works and source sections without inventing citations.
CREATE TABLE work_observations (
    observation_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses,
    work_id TEXT NOT NULL REFERENCES works,
    property TEXT NOT NULL CHECK(length(trim(property)) > 0),
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    method_version TEXT NOT NULL,
    method_artifact_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    receipt_id TEXT,
    validation_state TEXT NOT NULL CHECK(validation_state IN ('accepted','rejected','unresolved')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(receipt_id, analysis_id) REFERENCES model_receipts(receipt_id, analysis_id)
);
CREATE INDEX observations_by_analysis_work_property
    ON work_observations(analysis_id, work_id, property);
CREATE TABLE work_observation_evidence (
    observation_id TEXT NOT NULL REFERENCES work_observations,
    section_id TEXT NOT NULL REFERENCES source_sections,
    quote TEXT NOT NULL, locator TEXT NOT NULL,
    PRIMARY KEY(observation_id, section_id, locator)
);
CREATE TABLE processing_state (
    analysis_id TEXT NOT NULL REFERENCES analyses, task_key TEXT NOT NULL,
    phase TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
        ('pending','running','completed','blocked','failed','cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
    updated_at TEXT NOT NULL, PRIMARY KEY(analysis_id, task_key, phase)
);
CREATE INDEX tasks_by_status ON processing_state(analysis_id, phase, status);
CREATE TABLE facts (
    fact_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses,
    name TEXT NOT NULL, value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    derivation_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    input_manifest_sha256 TEXT NOT NULL CHECK(length(input_manifest_sha256) = 64),
    created_at TEXT NOT NULL, UNIQUE(analysis_id, name, input_manifest_sha256)
);
-- Corrections are new versioned observations. Mutable queue state is separate.
CREATE TRIGGER immutable_run_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'run protocols are append-only'); END;
CREATE TRIGGER immutable_run_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'run protocols are append-only'); END;
CREATE TRIGGER immutable_analysis_update BEFORE UPDATE ON analyses
BEGIN SELECT RAISE(ABORT, 'analysis namespaces are append-only'); END;
CREATE TRIGGER immutable_analysis_delete BEFORE DELETE ON analyses
BEGIN SELECT RAISE(ABORT, 'analysis namespaces are append-only'); END;
CREATE TRIGGER immutable_section_update BEFORE UPDATE ON source_sections
BEGIN SELECT RAISE(ABORT, 'source extractions are append-only'); END;
CREATE TRIGGER immutable_section_delete BEFORE DELETE ON source_sections
BEGIN SELECT RAISE(ABORT, 'source extractions are append-only'); END;
CREATE TRIGGER immutable_receipt_update BEFORE UPDATE ON model_receipts
BEGIN SELECT RAISE(ABORT, 'model receipts are append-only'); END;
CREATE TRIGGER immutable_receipt_delete BEFORE DELETE ON model_receipts
BEGIN SELECT RAISE(ABORT, 'model receipts are append-only'); END;
CREATE TRIGGER immutable_assessment_update BEFORE UPDATE ON assessments
BEGIN SELECT RAISE(ABORT, 'assessments are append-only'); END;
CREATE TRIGGER immutable_assessment_delete BEFORE DELETE ON assessments
BEGIN SELECT RAISE(ABORT, 'assessments are append-only'); END;
CREATE TRIGGER immutable_evidence_update BEFORE UPDATE ON assessment_evidence
BEGIN SELECT RAISE(ABORT, 'assessment evidence is append-only'); END;
CREATE TRIGGER immutable_evidence_delete BEFORE DELETE ON assessment_evidence
BEGIN SELECT RAISE(ABORT, 'assessment evidence is append-only'); END;
CREATE TRIGGER immutable_fact_update BEFORE UPDATE ON facts
BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER immutable_fact_delete BEFORE DELETE ON facts
BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER immutable_retrieval_update BEFORE UPDATE ON retrievals
BEGIN SELECT RAISE(ABORT, 'retrieval provenance is append-only'); END;
CREATE TRIGGER immutable_retrieval_delete BEFORE DELETE ON retrievals
BEGIN SELECT RAISE(ABORT, 'retrieval provenance is append-only'); END;
CREATE TRIGGER immutable_observation_update BEFORE UPDATE ON work_observations
BEGIN SELECT RAISE(ABORT, 'work observations are append-only'); END;
CREATE TRIGGER immutable_observation_delete BEFORE DELETE ON work_observations
BEGIN SELECT RAISE(ABORT, 'work observations are append-only'); END;
CREATE TRIGGER immutable_observation_evidence_update BEFORE UPDATE ON work_observation_evidence
BEGIN SELECT RAISE(ABORT, 'work observation evidence is append-only'); END;
CREATE TRIGGER immutable_observation_evidence_delete BEFORE DELETE ON work_observation_evidence
BEGIN SELECT RAISE(ABORT, 'work observation evidence is append-only'); END;
