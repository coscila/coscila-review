"""Exercise portable evidence snapshots and independent question branches."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = (Path(__file__).resolve().parents[1] / "skills" /
          "ocsl-review-reference-analysis" / "scripts" / "literature_db.py")
spec = importlib.util.spec_from_file_location("literature_db", SCRIPT)
library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(library)


class ReferenceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = library.Workspace(self.root)
        library.initialize(self.workspace, "work/library", "Original question", "original")
        self.active = library.Library(self.workspace, "work/library")

    def add_evidence(self, connection=None):
        data = b"A cached source containing reusable scientific evidence.\n"
        digest = hashlib.sha256(data).hexdigest()
        path = self.active.root / "sources" / digest
        path.write_bytes(data)
        conn = connection or sqlite3.connect(self.active.db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO source_artifacts VALUES (?, ?, ?, ?, ?, ?)",
                     (digest, "sources/" + digest, len(data), "text/plain", "fulltext", library.now()))
        conn.execute("INSERT INTO works VALUES ('paper', 'A real fixture paper', 2026, '{}')")
        conn.execute("INSERT INTO source_sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("section", "paper", digest, "p1", "fulltext", data.decode(), digest, "fixture-v1", 1))
        conn.commit()
        if connection is None:
            conn.close()
        return path, digest

    def test_init_creates_empty_evidence_and_unconfigured_analysis(self):
        checked = self.active.verify()
        self.assertEqual(checked["counts"]["works"], 0)
        self.assertEqual(checked["counts"]["assessments"], 0)
        self.assertEqual(checked["analyses"][0]["question"], "Original question")
        with self.active.connect() as conn:
            protocol = json.loads(conn.execute("SELECT protocol_json FROM runs").fetchone()[0])
        self.assertEqual(protocol["status"], "unconfigured")

    def test_snapshot_includes_committed_wal_and_branch_reuses_sources_without_mutating_parent(self):
        writer = sqlite3.connect(self.active.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        path, digest = self.add_evidence(writer)
        self.assertTrue(Path(str(self.active.db) + "-wal").exists())
        saved = self.active.snapshot("results/snapshot")
        parent = library.Library(self.workspace, "results/snapshot")
        self.assertEqual(parent.verify()["counts"]["works"], 1)
        parent_hash = library.digest_file(parent.db)
        self.assertEqual((parent.root / "sources" / digest).read_bytes(), path.read_bytes())
        result = parent.branch("work/new-question", "A completely different question", "followup")
        branch = library.Library(self.workspace, "work/new-question")
        self.assertEqual(result["parent_manifest_sha256"], saved["manifest_sha256"])
        self.assertEqual(list((branch.root / "sources").iterdir()), [])
        self.assertEqual(branch.verify()["counts"]["analyses"], 2)
        with library.database(branch.db) as conn:
            conn.execute("UPDATE works SET title='Branch-only correction' WHERE work_id='paper'")
        self.assertEqual(library.digest_file(parent.db), parent_hash)
        with parent.connect() as conn:
            self.assertEqual(conn.execute("SELECT title FROM works").fetchone()[0], "A real fixture paper")
        branch.snapshot("results/followup-portable")
        portable = library.Library(self.workspace, "results/followup-portable")
        self.assertIsNone(portable.parent_root)
        self.assertEqual(portable.verify()["counts"]["source_artifacts"], 1)

    def test_portable_snapshot_survives_different_workspace_path(self):
        self.add_evidence()
        self.active.snapshot("results/snapshot")
        moved_root = self.root / "separate-workspace"
        moved_root.mkdir()
        (self.root / "results/snapshot").rename(moved_root / "library")
        moved = library.Library(library.Workspace(moved_root), "library")
        self.assertEqual(moved.verify()["counts"]["source_sections"], 1)

    def test_missing_or_tampered_source_cannot_produce_complete_snapshot(self):
        path, _ = self.add_evidence()
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
            self.active.snapshot("results/bad")
        self.assertFalse((self.root / "results/bad").exists())
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing source"):
            self.active.snapshot("results/missing")
        self.assertFalse((self.root / "results/missing").exists())

    def test_detect_snapshot_database_and_parent_manifest_tampering(self):
        self.active.snapshot("results/snapshot")
        parent = library.Library(self.workspace, "results/snapshot")
        parent.branch("work/branch", "New question", "second")
        parent.db.chmod(0o600)
        with library.database(parent.db) as conn:
            conn.execute("INSERT INTO works VALUES ('extra', 'Modified', NULL, '{}')")
        with self.assertRaisesRegex(ValueError, "database hash/size mismatch"):
            parent.verify()
        with self.assertRaisesRegex(ValueError, "database hash/size mismatch"):
            parent.snapshot("results/do-not-bless-tampering")
        manifest = parent.root / "manifest.json"
        manifest.chmod(0o600)
        manifest.write_text(manifest.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "parent snapshot manifest has changed"):
            library.Library(self.workspace, "work/branch")

    def test_verify_detects_foreign_key_and_extracted_text_damage(self):
        _, digest = self.add_evidence()
        with library.database(self.active.db) as conn:
            conn.execute("DROP TRIGGER immutable_section_update")
            conn.execute("UPDATE source_sections SET work_id='missing'")
        with self.assertRaisesRegex(ValueError, "foreign key"):
            self.active.verify()
        with library.database(self.active.db) as conn:
            conn.execute("UPDATE source_sections SET work_id='paper', extracted_text='changed'")
        with self.assertRaisesRegex(ValueError, "text hash mismatch"):
            self.active.verify()

    def test_snapshot_rejects_changes_in_wal_even_when_base_db_stays_unchanged(self):
        self.add_evidence()
        self.active.snapshot("results/snapshot")
        parent = library.Library(self.workspace, "results/snapshot")
        parent.db.chmod(0o600)
        writer = sqlite3.connect(parent.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        checkpointed_hash = library.digest_file(parent.db)
        writer.execute("UPDATE works SET title='Only the WAL has this title'")
        writer.commit()
        self.assertEqual(library.digest_file(parent.db), checkpointed_hash)
        with self.assertRaisesRegex(ValueError, "unexpected SQLite journal"):
            parent.verify()
        with self.assertRaisesRegex(ValueError, "unexpected SQLite journal"):
            parent.branch("work/invalid", "New question", "invalid")

    def test_confines_paths_rejects_symlinks_and_preserves_existing_destination(self):
        for relative in ("../outside", "/tmp/outside"):
            with self.assertRaisesRegex(ValueError, "workspace-relative"):
                library.initialize(self.workspace, relative, "Question", "bad")
        (self.root / "link").symlink_to(self.root / "work", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            library.initialize(self.workspace, "link/escape", "Question", "bad")
        original = library.digest_file(self.active.db)
        with self.assertRaises(FileExistsError):
            library.initialize(self.workspace, "work/library", "Replacement", "bad")
        self.assertEqual(library.digest_file(self.active.db), original)
        path, _ = self.add_evidence()
        saved = path.read_bytes()
        path.unlink()
        outside = self.root / "unregistered"
        outside.write_bytes(saved)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.active.snapshot("results/symlink")

    def test_cli_returns_real_verification_and_nonzero_on_missing_input(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(self.root),
                                 "verify", "work/library"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])
        failed = subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(self.root),
                                 "verify", "work/missing"], capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("database is missing", failed.stderr)

    def test_immutable_histories_require_new_rows_and_allow_queue_updates(self):
        self.add_evidence()
        with library.database(self.active.db) as conn:
            for statement in ("UPDATE analyses SET question='Changed'",
                              "DELETE FROM runs", "UPDATE source_sections SET extracted_text='Changed'",
                              "DELETE FROM source_artifacts"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)
            conn.execute("INSERT INTO processing_state VALUES ('original','task','fetch','pending',0,'{}',?)",
                         (library.now(),))
            conn.execute("UPDATE processing_state SET status='completed'")
            self.assertEqual(conn.execute("SELECT status FROM processing_state").fetchone()[0], "completed")

    def test_protocol_file_is_frozen_with_hash_and_code_revision(self):
        protocol = {"version": "fixture-v1", "rubric": "source evidence only"}
        (self.root / "protocol.json").write_text(json.dumps(protocol))
        result = subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(self.root), "init",
                                 "work/configured", "--question", "Configured question", "--analysis-id", "study",
                                 "--protocol", "protocol.json", "--code-revision", "fixture-revision"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        configured = library.Library(self.workspace, "work/configured")
        configured.verify()
        with configured.connect() as conn:
            row = conn.execute("SELECT protocol_json, code_revision FROM runs").fetchone()
        self.assertEqual(json.loads(row[0]), protocol)
        self.assertEqual(row[1], "fixture-revision")

    def add_citation_history(self):
        _, digest = self.add_evidence()
        with library.database(self.active.db) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            corpus = conn.execute("SELECT corpus_id FROM corpora").fetchone()[0]
            conn.execute("INSERT INTO reviews VALUES ('review',?,'paper','included','{}','fixture')", (corpus,))
            conn.execute("INSERT INTO bibliography_entries VALUES ('entry','review','1','Source','{}','paper','verified',?)",
                         (digest,))
            conn.execute("INSERT INTO claim_loci VALUES ('locus','entry',?,'p1','Claim','Context','fixture')", (digest,))
            conn.execute("INSERT INTO model_receipts VALUES ('receipt','original','task',?,?,'fixture','fixture','{}','{}','completed',?,?)",
                         (digest, digest, library.now(), library.now()))
            conn.execute("INSERT INTO assessments VALUES ('assessment','original','locus','receipt','v1','fulltext','{}','accepted',?)",
                         (library.now(),))
        return corpus

    def test_model_assessment_history_is_append_only_and_analysis_scoped(self):
        corpus = self.add_citation_history()
        with library.database(self.active.db) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            for statement in ("UPDATE assessments SET verdict_json='{}'", "DELETE FROM assessments",
                              "UPDATE model_receipts SET model='changed'", "DELETE FROM model_receipts"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(statement)
            library.add_analysis(conn, corpus, "Other question", "other")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                conn.execute("INSERT INTO assessments VALUES ('wrong-analysis','other','locus','receipt','v1','fulltext','{}','accepted',?)",
                             (library.now(),))
        self.assertEqual(self.active.verify()["counts"]["assessments"], 1)

    def test_branch_can_answer_non_citation_question_without_new_loci(self):
        self.add_citation_history()
        self.active.snapshot("results/snapshot")
        parent = library.Library(self.workspace, "results/snapshot")
        parent_hash = library.digest_file(parent.db)
        parent.branch("work/designs", "What kinds of evidence were used?", "study-designs")
        branch = library.Library(self.workspace, "work/designs")
        with library.database(branch.db) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            artifact = conn.execute("SELECT sha256 FROM source_artifacts").fetchone()[0]
            conn.execute("INSERT INTO work_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("design", "study-designs", "paper", "evidence_kind",
                          json.dumps("scientific evidence"), "fixture-extractor-v1", artifact,
                          None, "accepted", library.now()))
            conn.execute("INSERT INTO work_observation_evidence VALUES (?,?,?,?)",
                         ("design", "section", "reusable scientific evidence", "p1"))
            result = conn.execute("""SELECT o.value_json, e.quote, s.extracted_text
                FROM work_observations o JOIN work_observation_evidence e USING(observation_id)
                JOIN source_sections s USING(section_id)
                WHERE o.analysis_id='study-designs' AND o.property='evidence_kind'""").fetchone()
            self.assertEqual(json.loads(result[0]), "scientific evidence")
            self.assertIn(result[1], result[2])
            self.assertEqual(conn.execute("SELECT count(*) FROM claim_loci").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM assessments WHERE analysis_id='original'")
                             .fetchone()[0], 1)
            for statement in ("UPDATE work_observations SET property='changed'",
                              "DELETE FROM work_observations",
                              "UPDATE work_observation_evidence SET quote='changed'",
                              "DELETE FROM work_observation_evidence"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(statement)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                conn.execute("INSERT INTO work_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                             ("wrong-receipt", "study-designs", "paper", "cohort_size", "42",
                              "fixture-v1", artifact, "receipt", "accepted", library.now()))
        self.assertEqual(branch.verify()["counts"]["work_observations"], 1)
        self.assertEqual(parent.verify()["counts"]["work_observations"], 0)
        self.assertEqual(library.digest_file(parent.db), parent_hash)
        self.assertEqual(list((branch.root / "sources").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
