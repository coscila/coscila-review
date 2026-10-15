# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ocsl_review.corpus_import import import_legacy_census, import_legacy_scores
from ocsl_review.corpus_store import CorpusRepository
from ocsl_review.errors import ValidationError


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = CorpusRepository.create(self.root, "work/library", "New scientific question", "v2", {"version": 2})

    def scores(self, path=None, *, wal=False):
        path = path or self.root / "scores.sqlite3"
        db = sqlite3.connect(path)
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
        db.executescript('''CREATE TABLE reviews(journal TEXT,doi TEXT,title TEXT,year INTEGER,n_printed INTEGER);
            CREATE TABLE citations(journal TEXT,doi TEXT,label TEXT,cited_doi TEXT,abs_support_verdict TEXT);
            INSERT INTO reviews VALUES('Journal','10.1234/review','Review',2024,2);
            INSERT INTO citations VALUES('Journal','10.1234/review','1','10.1234/source','supported');
            INSERT INTO citations VALUES('Journal','10.1234/review','2',NULL,'abstain');''')
        db.commit()
        if wal:
            self.addCleanup(db.close)
        else:
            db.close()
        return path

    def census(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.executescript('''CREATE TABLE runs(id TEXT PRIMARY KEY,spec TEXT);
            CREATE TABLE tasks(id TEXT,run_id TEXT,spec TEXT,PRIMARY KEY(id,run_id));
            CREATE TABLE assessments(id TEXT PRIMARY KEY,run_id TEXT,task_id TEXT,accepted INTEGER,payload TEXT,recorded_at TEXT);''')
        db.execute("INSERT INTO runs VALUES(?,?)", ("original-run", json.dumps({"mode": "abstract", "promptSha256": "a" * 64})))
        db.execute("INSERT INTO tasks VALUES(?,?,?)", ("task-1", "original-run", json.dumps({"id": "task-1", "input": {"messages": [{"role": "user", "content": "Exact original claim"}]}})))
        for index, accepted in enumerate([False, True, True]):
            payload = {"result": {"state": "completed", "providerId": "original-provider", "model": "original-model", "output": "raw answer", "usage": {"inputTokens": 20}},
                       "assessment": {"supportVerdict": "supported", "evidence": [{"quote": "Original quotation"}]},
                       "validationError": None if accepted else "bad output"}
            db.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?)", (f"attempt-{index}", "original-run", "task-1", accepted, json.dumps(payload), "2026-10-14T17:29:00Z"))
        db.commit(); db.close()
        return path

    def test_scores_are_legacy_observations_not_new_validated_assessments(self):
        path = self.scores()
        before = path.read_bytes()
        first = import_legacy_scores(self.repo, path)
        second = import_legacy_scores(self.repo, path)
        self.assertEqual(first, second)
        self.assertEqual(2, first["citations"])
        self.assertFalse(first["eligibleForV2Assessment"])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual("v2", self.repo.analysis()["analysis_id"])
        with self.repo.connect() as db:
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM work_observations").fetchone()[0])
            self.assertEqual(["unresolved"], [r[0] for r in db.execute("SELECT DISTINCT validation_state FROM work_observations")])
            for table in ["assessments", "claim_loci", "model_receipts", "work_identifiers"]:
                self.assertEqual(0, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        digest = hashlib.sha256(before).hexdigest()
        self.assertEqual(before, self.repo.read_artifact(digest))
        self.repo.library.verify()

    def test_wal_is_preserved_and_replayed_only_in_the_copy(self):
        path = self.scores(wal=True)
        wal = Path(str(path) + "-wal")
        before = path.read_bytes(), wal.read_bytes()
        original_files = {p.name for p in path.parent.glob('scores.sqlite3*')}
        result = import_legacy_scores(self.repo, path)
        self.assertEqual(2, result["citations"])
        self.assertEqual(before, (path.read_bytes(), wal.read_bytes()))
        self.assertEqual(original_files, {p.name for p in path.parent.glob('scores.sqlite3*')})
        self.assertEqual(before[1], self.repo.read_artifact(hashlib.sha256(before[1]).hexdigest()))

    def test_census_retains_failed_attempts_receipts_and_exact_companion_artifacts(self):
        source = self.root / "legacy-workspace"
        path = self.census(source / "work/corpus/ws/review-1/.ocsl-review/census/census.sqlite3")
        ledger = path.parent.parent
        manuscript = {"structuredSource": {"publication": {"doi": "10.1234/review", "article-title": "Review"}}}
        (ledger / "manuscript.json").write_text(json.dumps(manuscript))
        (ledger / "literature-audit.json").write_bytes(b'{"original":"ledger"}')
        source_dir = ledger / "literature/sources"
        source_dir.mkdir(parents=True)
        (source_dir / "original.txt").write_bytes(b"Unmodified original evidence")
        # A root pilot exists but workspace discovery must not import it.
        self.census(source / ".ocsl-review/census/census.sqlite3")
        before = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
        first = import_legacy_census(self.repo, source)
        self.assertEqual(first, import_legacy_census(self.repo, source))
        self.assertEqual(1, first["databases"])
        self.assertEqual(1, first["legacyAcceptedDistinctTasks"])
        imported = first["imports"][0]
        self.assertEqual(3, imported["attempts"])
        self.assertEqual(2, imported["legacyAcceptedAttempts"])
        self.assertEqual(1, imported["legacyRejectedAttempts"])
        with self.repo.connect() as db:
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM model_receipts").fetchone()[0])
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM work_observations").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0])
            receipt = db.execute("SELECT * FROM model_receipts LIMIT 1").fetchone()
            self.assertEqual("original-model", receipt["model"])
            self.assertIn(b"Exact original claim", self.repo.read_artifact(receipt["request_sha256"]))
            self.assertIn(b"Original quotation", self.repo.read_artifact(receipt["response_sha256"]))
        self.assertEqual(before, {p: p.read_bytes() for p in source.rglob('*') if p.is_file()})
        self.assertEqual(b"Unmodified original evidence", self.repo.read_artifact(hashlib.sha256(b"Unmodified original evidence").hexdigest()))
        self.repo.library.verify()

    def test_changed_scores_get_a_separate_legacy_namespace(self):
        path = self.scores()
        first = import_legacy_scores(self.repo, path)
        db = sqlite3.connect(path)
        db.execute("UPDATE citations SET abs_support_verdict='not-supported' WHERE label='1'")
        db.commit(); db.close()
        second = import_legacy_scores(self.repo, path)
        self.assertNotEqual(first["analysisId"], second["analysisId"])
        self.assertEqual("v2", self.repo.analysis()["analysis_id"])
        with self.repo.connect() as db:
            self.assertEqual(6, db.execute("SELECT COUNT(*) FROM work_observations").fetchone()[0])

    def test_rejects_unknown_schema_and_symlink_input(self):
        path = self.root / "unknown.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE unrelated(value TEXT)"); db.close()
        with self.assertRaisesRegex(ValidationError, "unsupported legacy"):
            import_legacy_scores(self.repo, path)
        alias = self.root / "alias.sqlite3"
        alias.symlink_to(path)
        with self.assertRaisesRegex(ValidationError, "symlink"):
            import_legacy_scores(self.repo, alias)


if __name__ == "__main__":
    unittest.main()
