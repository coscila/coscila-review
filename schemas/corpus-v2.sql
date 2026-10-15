-- SPDX-License-Identifier: Apache-2.0
-- Additive migration: original evidence and observations remain untouched.
CREATE TABLE IF NOT EXISTS corpus_reviews (
 review_id TEXT PRIMARY KEY REFERENCES reviews, phase TEXT NOT NULL DEFAULT 'pending',
 state TEXT NOT NULL DEFAULT 'pending', reason TEXT, document_sha256 TEXT REFERENCES source_artifacts,
 checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_documents (
 document_id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works,
 artifact_sha256 TEXT NOT NULL REFERENCES source_artifacts, evidence_tier TEXT NOT NULL,
 source_url TEXT NOT NULL, provider TEXT NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1)),
 metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)), created_at TEXT NOT NULL,
 UNIQUE(work_id,artifact_sha256,evidence_tier)
);
CREATE INDEX IF NOT EXISTS corpus_documents_by_work ON corpus_documents(work_id,evidence_tier);
CREATE TABLE IF NOT EXISTS corpus_tasks (
 task_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses,
 locus_id TEXT NOT NULL REFERENCES claim_loci, tier TEXT NOT NULL,
 spec_sha256 TEXT NOT NULL REFERENCES source_artifacts,
 state TEXT NOT NULL CHECK(state IN ('pending','running','completed','blocked','failed','cancelled')),
 reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS corpus_tasks_queue ON corpus_tasks(analysis_id,state,tier);
CREATE TABLE IF NOT EXISTS corpus_batches (
 batch_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses,
 worker_id TEXT NOT NULL, worker_revision INTEGER NOT NULL,
 worker_json TEXT NOT NULL CHECK(json_valid(worker_json)),
 input_sha256 TEXT NOT NULL REFERENCES source_artifacts,
 task_ids_json TEXT NOT NULL CHECK(json_valid(task_ids_json)),
 job_id TEXT, owner_attempt TEXT, state TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 attempt INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS corpus_batches_pending ON corpus_batches(analysis_id,state);
CREATE TABLE IF NOT EXISTS corpus_imports (
 import_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL REFERENCES source_artifacts,
 source_kind TEXT NOT NULL, summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
 imported_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS corpus_document_immutable BEFORE UPDATE ON corpus_documents
BEGIN SELECT RAISE(ABORT,'source document versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS corpus_task_input_immutable BEFORE UPDATE OF analysis_id,locus_id,tier,spec_sha256 ON corpus_tasks
BEGIN SELECT RAISE(ABORT,'task scientific inputs are immutable'); END;
PRAGMA user_version=2;
