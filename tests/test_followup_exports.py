# SPDX-License-Identifier: Apache-2.0
"""Exports of a follow-up branch: the compromised-claim database, alert records for affected claims, the propagation
graph and the manifest; entitled text only in a private export; facts still verify after an export."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import scores_db
from ocsl_review.cli import main
from ocsl_review.corpus_facts import verify_export
from ocsl_review.errors import ValidationError
from ocsl_review.followup_exports import export
from ocsl_review.followup_facts import export_facts

from test_followup_dependency import locus
from test_followup_facts import ENTITLED_SENTENCES, populated_branch
from test_followup_status import clean_environment, no_network


def exported_text(directory: Path) -> str:
    return "\n".join((directory / name).read_bytes().decode("utf-8", errors="replace")
                     for name in ("compromised-claims.sqlite3", "alerts.jsonl", "propagation-graph.json",
                                  "manifest.json"))


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        with mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network), \
                mock.patch.dict(os.environ, clean_environment(), clear=True):
            cls.branch = populated_branch(cls.root, cls.census, "populated")
        cls.public = cls.root / "public"
        cls.result = export(cls.branch.path, cls.public)
        cls.alerts = [json.loads(line) for line in (cls.public / "alerts.jsonl").read_text(encoding="utf-8")
                      .splitlines()]
        cls.graph = json.loads((cls.public / "propagation-graph.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def claims(self, directory):
        with closing(sqlite3.connect((directory / "compromised-claims.sqlite3").resolve().as_uri()
                                     + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM claims")}

    def test_the_compromised_claim_database_and_its_manifest(self):
        claims = self.claims(self.public)
        self.assertEqual({locus(name) for name in ("a1/1", "a2/1", "a2/2", "b1/1", "b1/2")}, set(claims))
        row = claims[locus("a1/1")]
        self.assertEqual(("status", "retracted", "2022-03-04", "crossref:retraction-watch", "contradicted", "relies"),
                         (row["basis"], row["status"], row["notice_date"], row["status_source"],
                          row["fulltext_verdict"], row["function_class"]))
        self.assertEqual("Marker zeta doubles the risk in adults ⟨R1⟩ ⟨R2⟩ .", row["sentence"])
        self.assertTrue(json.loads(row["judgments"]))
        database = self.public / "compromised-claims.sqlite3"
        self.assertFalse(stat.S_IMODE(database.stat().st_mode) & 0o222)
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            receipts = connection.execute("SELECT COUNT(*), SUM(fingerprint IS NOT NULL) FROM receipts").fetchone()
            observations = connection.execute("SELECT COUNT(DISTINCT work_key) FROM observations").fetchone()[0]
            adjudications = connection.execute("SELECT COUNT(*) FROM adjudications").fetchone()[0]
        self.assertGreater(receipts[0], 0)
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual((3, 0), (observations, adjudications))
        manifest = json.loads((self.public / "manifest.json").read_text(encoding="utf-8"))
        for record in manifest["files"]:
            self.assertEqual(scores_db.file_sha256(self.public / record["path"]), record["sha256"])
        self.assertEqual((False, "check suggested", 4), (manifest["private"], manifest["confidence"],
                                                         manifest["counts"]["alerts"]))

    def test_one_alert_per_affected_claim(self):
        self.assertEqual({locus(name) for name in ("a1/1", "a2/1", "b1/1", "b1/2")},
                         {alert["claim"]["locusId"] for alert in self.alerts},
                         "the review that notes the retraction itself gets no alert")
        alert = next(alert for alert in self.alerts if alert["claim"]["locusId"] == locus("a1/1"))
        self.assertEqual({"format", "alertId", "basis", "claim", "citedWork", "status", "evidence", "confidence",
                          "suggestedAction", "createdAt"}, set(alert))
        self.assertEqual(("check suggested", "retracted", "w-retracted", "10.5555/fu.notice.retraction1"),
                         (alert["confidence"], alert["status"]["status"], alert["citedWork"]["workKey"],
                          alert["status"]["noticeDoi"]))
        self.assertEqual({"support", "function", "dependency", "timing", "receipts"}, set(alert["evidence"]))
        self.assertEqual("contradicted", alert["evidence"]["support"]["full-text"]["verdict"])
        self.assertFalse(alert["evidence"]["timing"]["citedAfterNotice"])
        self.assertTrue(alert["suggestedAction"]["authors"].startswith("For the authors of the citing review"))
        self.assertTrue(alert["suggestedAction"]["studyTeam"].startswith("For a study team"))
        text = (self.public / "alerts.jsonl").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\bfor (a |the )?publishers?\b|\bpublisher[- ](service|facing)", text, re.I),
                          "alerts address the authors of the citing work and study teams")
        claims = (self.public / "compromised-claims.sqlite3").resolve().as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(claims, uri=True)) as connection:
            rows = connection.execute("SELECT confidence, COUNT(*) FROM alerts GROUP BY confidence").fetchall()
        self.assertEqual([("check suggested", 4)], [tuple(row) for row in rows])

    def test_an_export_leaves_the_branch_unchanged(self):
        before = scores_db.file_sha256(self.branch.path)
        export(self.branch.path, self.root / "again")
        self.assertEqual(before, scores_db.file_sha256(self.branch.path))

    def test_the_propagation_graph(self):
        kinds = {node["kind"] for node in self.graph["nodes"]}
        self.assertEqual({"work", "claim", "article"}, kinds)
        self.assertEqual({"works": 3, "claims": 5, "articles": 1, "edges": 6}, self.graph["counts"])
        restated = [edge for edge in self.graph["edges"] if edge["kind"] == "restated-by"]
        self.assertEqual(1, len(restated))
        article = next(node for node in self.graph["nodes"] if node["id"] == restated[0]["to"])
        self.assertEqual(("doi:10.5555/fu.later.1", "10.5555/fu.later.1", 2024),
                         (article["citingKey"], article["doi"], article["year"]))
        cited = [edge for edge in self.graph["edges"] if edge["kind"] == "cited-by"]
        self.assertTrue(all(edge["from"].startswith("work:") and edge["to"].startswith("claim:") for edge in cited))

    def test_public_exports_drop_entitled_text_and_private_exports_keep_it(self):
        public = exported_text(self.public)
        for sentence in ENTITLED_SENTENCES:
            self.assertFalse(sentence in public, "an entitled sentence is in a public export")
        claims = self.claims(self.public)
        self.assertTrue(claims[locus("a2/1")]["sentence"].startswith("sha256:"))
        self.assertIsNone(claims[locus("a2/2")]["function_quote"])
        self.assertGreater(self.result["redaction"]["sentences"], 0)
        private = self.root / "private"
        result = export(self.branch.path, private, allow_entitled_text=True)
        self.assertTrue(result["private"])
        self.assertTrue(any(sentence in exported_text(private) for sentence in ENTITLED_SENTENCES))
        self.assertEqual("A retracted cohort study had suggested the same association",
                         self.claims(private)[locus("a2/2")]["function_quote"])

    def test_facts_still_verify_after_an_export_and_exports_need_a_new_directory(self):
        facts = self.root / "facts-after-export"
        export_facts(self.branch.path, facts, code_revision="synthetic-revision")
        self.assertTrue(verify_export(facts / "facts.json")["ok"])
        with self.assertRaisesRegex(ValidationError, "not empty"):
            export(self.branch.path, self.public)
        target = self.root / "cli-export"
        record = self.root / "result-export.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(record), "followup", "export", "--database", str(self.branch.path),
                         "--output", str(target)])
        self.assertEqual(0, code, record.read_text())
        self.assertEqual(4, json.loads(record.read_text(encoding="utf-8"))["result"]["counts"]["alerts"])


if __name__ == "__main__":
    unittest.main()
