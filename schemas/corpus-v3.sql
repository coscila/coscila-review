-- SPDX-License-Identifier: Apache-2.0
-- Additive migration. Progress counters are kept by triggers inside every writing
-- transaction, so status never scans evidence tables; queue indexes serve ordered
-- selection and per-locus lookups. Evidence, observations and receipts are untouched.
CREATE TABLE IF NOT EXISTS progress_counters (
 counter TEXT NOT NULL, analysis_id TEXT NOT NULL DEFAULT '', dimension TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT '', value INTEGER NOT NULL CHECK(value >= 0),
 PRIMARY KEY(counter, analysis_id, dimension, state)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS progress_rows_reviews_insert AFTER INSERT ON reviews BEGIN
 INSERT INTO progress_counters VALUES('rows','','reviews','',1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_reviews_delete AFTER DELETE ON reviews BEGIN
 UPDATE progress_counters SET value=value-1 WHERE counter='rows' AND analysis_id='' AND dimension='reviews' AND state=''; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_entries_insert AFTER INSERT ON bibliography_entries BEGIN
 INSERT INTO progress_counters VALUES('rows','','bibliography_entries','',1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_entries_delete AFTER DELETE ON bibliography_entries BEGIN
 UPDATE progress_counters SET value=value-1 WHERE counter='rows' AND analysis_id='' AND dimension='bibliography_entries' AND state=''; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_works_insert AFTER INSERT ON works BEGIN
 INSERT INTO progress_counters VALUES('rows','','works','',1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_works_delete AFTER DELETE ON works BEGIN
 UPDATE progress_counters SET value=value-1 WHERE counter='rows' AND analysis_id='' AND dimension='works' AND state=''; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_loci_insert AFTER INSERT ON claim_loci BEGIN
 INSERT INTO progress_counters VALUES('rows','','claim_loci','',1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_loci_delete AFTER DELETE ON claim_loci BEGIN
 UPDATE progress_counters SET value=value-1 WHERE counter='rows' AND analysis_id='' AND dimension='claim_loci' AND state=''; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_artifacts_insert AFTER INSERT ON source_artifacts BEGIN
 INSERT INTO progress_counters VALUES('rows','','source_artifacts','',1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_rows_sections_insert AFTER INSERT ON source_sections BEGIN
 INSERT INTO progress_counters VALUES('rows','','source_sections','',1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;

CREATE TRIGGER IF NOT EXISTS progress_tasks_insert AFTER INSERT ON corpus_tasks BEGIN
 INSERT INTO progress_counters VALUES('tasks',NEW.analysis_id,NEW.tier,NEW.state,1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_tasks_state AFTER UPDATE OF state ON corpus_tasks
WHEN OLD.state IS NOT NEW.state BEGIN
 UPDATE progress_counters SET value=value-1
 WHERE counter='tasks' AND analysis_id=OLD.analysis_id AND dimension=OLD.tier AND state=OLD.state;
 INSERT INTO progress_counters VALUES('tasks',NEW.analysis_id,NEW.tier,NEW.state,1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_tasks_delete AFTER DELETE ON corpus_tasks BEGIN
 UPDATE progress_counters SET value=value-1
 WHERE counter='tasks' AND analysis_id=OLD.analysis_id AND dimension=OLD.tier AND state=OLD.state; END;

CREATE TRIGGER IF NOT EXISTS progress_reviews_insert AFTER INSERT ON corpus_reviews BEGIN
 INSERT INTO progress_counters VALUES('reviews','',NEW.phase,NEW.state,1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_reviews_state AFTER UPDATE OF phase,state ON corpus_reviews
WHEN OLD.phase IS NOT NEW.phase OR OLD.state IS NOT NEW.state BEGIN
 UPDATE progress_counters SET value=value-1
 WHERE counter='reviews' AND analysis_id='' AND dimension=OLD.phase AND state=OLD.state;
 INSERT INTO progress_counters VALUES('reviews','',NEW.phase,NEW.state,1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;
CREATE TRIGGER IF NOT EXISTS progress_reviews_delete AFTER DELETE ON corpus_reviews BEGIN
 UPDATE progress_counters SET value=value-1
 WHERE counter='reviews' AND analysis_id='' AND dimension=OLD.phase AND state=OLD.state; END;

-- Assessments are append-only; the counter holds distinct loci per evidence level and state.
CREATE TRIGGER IF NOT EXISTS progress_assessment_loci AFTER INSERT ON assessments
WHEN NOT EXISTS (SELECT 1 FROM assessments a WHERE a.analysis_id=NEW.analysis_id AND a.locus_id=NEW.locus_id
                 AND a.evidence_level=NEW.evidence_level AND a.validation_state=NEW.validation_state
                 AND a.rowid<>NEW.rowid) BEGIN
 INSERT INTO progress_counters VALUES('assessment-loci',NEW.analysis_id,NEW.evidence_level,NEW.validation_state,1)
 ON CONFLICT(counter,analysis_id,dimension,state) DO UPDATE SET value=value+1; END;

INSERT INTO progress_counters SELECT 'rows','','reviews','',count(*) FROM reviews;
INSERT INTO progress_counters SELECT 'rows','','bibliography_entries','',count(*) FROM bibliography_entries;
INSERT INTO progress_counters SELECT 'rows','','works','',count(*) FROM works;
INSERT INTO progress_counters SELECT 'rows','','claim_loci','',count(*) FROM claim_loci;
INSERT INTO progress_counters SELECT 'rows','','source_artifacts','',count(*) FROM source_artifacts;
INSERT INTO progress_counters SELECT 'rows','','source_sections','',count(*) FROM source_sections;
INSERT INTO progress_counters SELECT 'tasks',analysis_id,tier,state,count(*) FROM corpus_tasks GROUP BY analysis_id,tier,state;
INSERT INTO progress_counters SELECT 'reviews','',phase,state,count(*) FROM corpus_reviews GROUP BY phase,state;
INSERT INTO progress_counters SELECT 'assessment-loci',analysis_id,evidence_level,validation_state,count(DISTINCT locus_id)
 FROM assessments GROUP BY analysis_id,evidence_level,validation_state;

DROP INDEX IF EXISTS corpus_tasks_queue;
CREATE INDEX IF NOT EXISTS corpus_tasks_queue_ordered ON corpus_tasks(analysis_id,state,tier,task_id);
CREATE INDEX IF NOT EXISTS corpus_tasks_by_locus ON corpus_tasks(analysis_id,locus_id,tier);
PRAGMA user_version=3;
