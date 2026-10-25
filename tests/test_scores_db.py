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

    def test_incomplete_runs_freeze_only_on_request_and_facts_refuse_them(self):
        from ocsl_review import corpus_facts
        with closing(scores_db.connect(self.path)) as c:
            c.execute("UPDATE reviews SET full_text_state='pending' WHERE doi='r2'")
            c.execute("INSERT INTO meta VALUES('runState', 'paused:model_drift')")
            c.commit()
        with self.assertRaisesRegex(ValidationError, "incomplete: run state is paused:model_drift; 1 reviews pending"):
            scores_db.freeze(self.path, self.root / "refused.sqlite3")
        self.assertFalse((self.root / "refused.sqlite3").exists())
        report = scores_db.freeze(self.path, self.root / "partial.sqlite3", allow_incomplete=True)
        self.assertFalse(report["complete"])
        with closing(scores_db.open_frozen(self.root / "partial.sqlite3")) as c:
            meta = scores_db.meta(c)
            self.assertEqual("0", meta["complete"])
            self.assertIn("paused:model_drift", meta["incompleteReason"])
        with self.assertRaisesRegex(ValidationError, "incomplete run"):
            corpus_facts.export_facts(self.root / "partial.sqlite3", self.root / "facts", ["J1"], b=10, seed=1)
        result = corpus_facts.export_facts(self.root / "partial.sqlite3", self.root / "facts", ["J1"], b=10, seed=1,
                                           allow_incomplete=True, code_revision="t")
        bundle = json.loads(Path(result["facts"]).read_text())
        self.assertFalse(bundle["provenance"]["inputs"]["runComplete"])

    def test_freeze_redacts_entitled_review_text(self):
        with closing(scores_db.connect(self.path)) as c:
            c.execute("UPDATE citations SET printed='Printed entry', abs_reason='because', abs_quote='abstract words', "
                      "ft_quote='full text words', ft_source='entitled' WHERE journal='J1' AND doi='r2' AND label='6'")
            c.execute("UPDATE citations SET printed='Public entry', abs_reason='public reason' WHERE doi='r1' AND label='1'")
            c.execute("INSERT INTO loci VALUES('J1','r2','1',0,'Entitled citing sentence.','Intro',0,0,'p',0,0,0,0,1)")
            c.execute("INSERT INTO loci VALUES('J1','r1','1',0,'Public citing sentence.','Intro',0,0,'p',0,0,0,0,1)")
            c.execute("INSERT INTO history(journal, doi, label, tier, variant, outcome, quote, reason, record_json, created_at) "
                      "VALUES('J1','r2','1','abstract','primary','accepted','q','why','{}','t')")
            c.commit()
        frozen = self.root / "redacted.sqlite3"
        report = scores_db.freeze(self.path, frozen)
        self.assertEqual(1, report["entitledTextRedacted"]["sentences"])
        with closing(scores_db.open_frozen(frozen)) as c:
            sentences = {r["doi"]: r["sentence"] for r in c.execute("SELECT doi, sentence FROM loci")}
            self.assertTrue(sentences["r2"].startswith("sha256:"))
            self.assertEqual("Public citing sentence.", sentences["r1"])
            entitled = c.execute("SELECT printed, abs_reason, ft_quote, abs_quote FROM citations WHERE doi='r2' AND label='6'").fetchone()
            self.assertEqual((None, None, None, "abstract words"), tuple(entitled))
            public = c.execute("SELECT printed, abs_reason FROM citations WHERE doi='r1' AND label='1'").fetchone()
            self.assertEqual(("Public entry", "public reason"), tuple(public))
            history = c.execute("SELECT quote, reason, record_json FROM history WHERE doi='r2' AND label='1' "
                                "AND variant='primary'").fetchone()
            self.assertEqual((None, None, None), tuple(history))
            self.assertEqual("1", scores_db.meta(c)["entitledTextRedacted"])
            triggers = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertLessEqual({"history_no_update", "history_no_delete", "calls_no_update"}, triggers)
        scores_db.export(frozen, self.root / "export-redacted", parquet=False)
        kept = self.root / "kept-text.sqlite3"
        scores_db.freeze(self.path, kept, keep_text=True)
        with self.assertRaisesRegex(ValidationError, "entitled reviews keep their text"):
            scores_db.export(kept, self.root / "export-kept", parquet=False)
        manifest = scores_db.export(kept, self.root / "export-private", parquet=False, allow_entitled_text=True)
        self.assertTrue(manifest["files"])

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
