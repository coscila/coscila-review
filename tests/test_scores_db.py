# SPDX-License-Identifier: Apache-2.0
"""Census scores database: schema, data dictionary, append-only history, freezing, scores and exports."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ocsl_review import scores_db
from ocsl_review.errors import ValidationError

from test_estimands import build_scores


class ScoresDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "census.sqlite3"
        build_scores(self.path)

    def test_dictionary_covers_every_column(self):
        with closing(scores_db.connect(self.path)) as c:
            for table in scores_db.TABLES:
                columns = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                documented = {r[0] for r in c.execute("SELECT column_name FROM data_dictionary WHERE table_name=?", (table,))}
                self.assertEqual(columns, documented, table)
            reference_columns = {"journal", "doi", "label", "cited_inbody", "identity", "cited_doi", "cited_title",
                                 "abs_category", "abs_support_verdict", "abs_escalated", "abs_class", "abs_source",
                                 "ft_category", "ft_support_verdict", "ft_class", "ft_source"}
            self.assertLessEqual(reference_columns, {r[1] for r in c.execute("PRAGMA table_info(citations)")})

    def test_history_is_append_only_and_receipts_immutable(self):
        with closing(scores_db.connect(self.path)) as c:
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("UPDATE history SET category='true'")
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("DELETE FROM history")

    def test_metadata_pins(self):
        with closing(scores_db.connect(self.path)) as c:
            scores_db.set_meta(c, {"instrumentDigest": "a" * 64})
            scores_db.set_meta(c, {"instrumentDigest": "a" * 64})
            with self.assertRaises(ValidationError):
                scores_db.set_meta(c, {"instrumentDigest": "b" * 64})
        with self.assertRaises(ValidationError):
            scores_db.connect(self.root / "missing.sqlite3")

    def test_aggregates_follow_the_class_map(self):
        with closing(scores_db.connect(self.path)) as c:
            row = c.execute("SELECT * FROM reviews WHERE doi='r1'").fetchone()
            self.assertEqual((7, 6, 6, 1, 3, 2, 1), (row["n_printed"], row["n_cited_inbody"], row["n_judged"], row["n_not_cited"],
                                                     row["abs_sure_supported"], row["abs_unsure"], row["abs_sure_not_supported"]))
            self.assertAlmostEqual(0.25, row["abs_not_supported_share"])

    def test_freeze_writes_a_closed_read_only_snapshot_with_scores(self):
        with closing(scores_db.connect(self.path)) as c:
            c.execute("INSERT INTO evidence VALUES('e1','w1','v1','abstract','s',NULL,NULL,'public',NULL,4,NULL,'text')")
            c.commit()
        frozen = self.root / "out" / "scores.sqlite3"
        report = scores_db.freeze(self.path, frozen)
        self.assertEqual(scores_db.file_sha256(frozen), report["sha256"])
        self.assertFalse(os.access(frozen, os.W_OK))
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(frozen) + suffix).exists())
        with closing(scores_db.open_frozen(frozen)) as c:
            self.assertEqual("delete", c.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertIsNone(c.execute("SELECT text FROM evidence").fetchone()[0])
            self.assertEqual(("1", "0"), (scores_db.meta(c)["frozen"], scores_db.meta(c)["evidenceTextKept"]))
            scores = {r["doi"]: r for r in c.execute("SELECT * FROM review_scores WHERE journal='J1'")}
            self.assertEqual((4, 1), (scores["r1"]["n_classifiable"], scores["r1"]["n_not_supported"]))
            works = c.execute("SELECT COUNT(*) FROM work_scores").fetchone()[0]
            self.assertEqual(0, works)
            self.assertTrue(all(0 <= r["q_value"] <= 1 for r in scores.values() if r["q_value"] is not None))
        kept = scores_db.freeze(self.path, self.root / "kept.sqlite3", keep_text=True)
        self.assertTrue(kept["evidenceTextKept"])
        with self.assertRaises(ValidationError):
            scores_db.freeze(self.path, frozen)

    def test_export_tsv_and_manifest(self):
        frozen = self.root / "scores.sqlite3"
        scores_db.freeze(self.path, frozen)
        manifest = scores_db.export(frozen, self.root / "export", parquet=False)
        self.assertEqual("not requested", manifest["parquet"])
        with (self.root / "export" / "citations.tsv").open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
        self.assertEqual("journal", rows[0][0])
        self.assertEqual(len(rows) - 1, next(f["rows"] for f in manifest["files"] if f["path"] == "citations.tsv"))
        written = json.loads((self.root / "export" / "manifest.json").read_text())
        self.assertEqual(scores_db.file_sha256(frozen), written["source"]["sha256"])


if __name__ == "__main__":
    unittest.main()
