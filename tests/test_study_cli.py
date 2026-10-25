# SPDX-License-Identifier: Apache-2.0
"""End to end through the study commands: census run, freezing, facts and the writing facts contract."""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from ocsl_review import corpus_facts, scores_db
from ocsl_review.census_runner import load_records, read_document
from ocsl_review.cli import main
from ocsl_review.errors import ValidationError

from test_census_runner import FIXTURES, JOURNAL, RunnerTestCase, full_text_fetcher, record


class FactsEndToEndTests(RunnerTestCase):
    def build(self):
        run = self.run_census()
        self.prepare(run, record(), record(doi="10.5555/second", journal="Second Synthetic Journal", year=2020))
        run.run_abstract()
        run.run_full_text(fetch_xml=full_text_fetcher([]))
        run.close()
        frozen = self.root / "frozen" / "scores.sqlite3"
        return scores_db.freeze(self.database, frozen), frozen

    def test_freeze_export_and_facts(self):
        report, frozen = self.build()
        self.assertFalse(os.access(frozen, os.W_OK))
        self.assertFalse(Path(str(frozen) + "-wal").exists())
        with closing(scores_db.open_frozen(frozen)) as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM evidence WHERE text IS NOT NULL").fetchone()[0])
            self.assertEqual("1", scores_db.meta(c)["frozen"])
            self.assertGreater(c.execute("SELECT COUNT(*) FROM data_dictionary").fetchone()[0], 100)
        with self.assertRaises(ValidationError):
            scores_db.freeze(self.database, frozen)
        manifest = scores_db.export(frozen, self.root / "export")
        names = {f["path"] for f in manifest["files"]}
        self.assertIn("citations.tsv", names)
        self.assertIn("data_dictionary.tsv", names)
        try:
            import pyarrow  # noqa: F401
            self.assertIn("citations.parquet", names)
        except ImportError:
            self.assertTrue(manifest["parquet"].startswith("unavailable"))
        facts = corpus_facts.export_facts(frozen, self.root / "facts", [JOURNAL, "Second Synthetic Journal"], b=200, seed=7,
                                          ratio_pair=(JOURNAL, "Second Synthetic Journal"), code_revision="test")
        bundle = json.loads((self.root / "facts" / "facts.json").read_text())
        self.assertEqual(1, bundle["schemaVersion"])
        headline = bundle["facts"]["census.synthetic-journal-of-examples.headline_share"]
        # Denominator 2: T for 1-3, 6, 9, 11, 13; F for 4, 7, 8, 12, 14 (5, 10, 15 are N) -> 5 of 12.
        self.assertEqual(5 / 12, headline["value"])
        self.assertEqual(12, bundle["facts"]["census.synthetic-journal-of-examples.d2_n"]["value"])
        self.assertIn("derivation", bundle["facts"]["census.synthetic-journal-of-examples.headline_bootstrap_lo"])
        self.assertEqual("test", headline["codeRevision"])
        self.assertEqual(report["sha256"], headline["inputs"]["scoresSha256"])
        self.assertEqual({"ok": True, "facts": facts["count"]}, corpus_facts.verify_export(self.root / "facts" / "facts.json"))
        self.assertFalse(Path(str(self.root / "facts" / "facts.sqlite3") + "-wal").exists())
        bundle["facts"]["census.synthetic-journal-of-examples.d2_n"]["value"] = 13
        tampered = self.root / "facts" / "tampered.json"
        tampered.write_text(json.dumps(bundle))
        with self.assertRaises(ValidationError):
            corpus_facts.verify_export(tampered)

    def test_facts_pass_the_writing_verifier(self):
        home = os.environ.get("OCSL_WRITING_HOME")
        candidates = [Path(home)] if home else []
        candidates += [Path(__file__).resolve().parents[2] / name for name in ("writing-e2", "coscila-writing")]
        source = next((c / "src" for c in candidates if (c / "src" / "ocsl_writing" / "facts.py").is_file()), None)
        if source is None:
            self.skipTest("no coscila-writing checkout available")
        _, frozen = self.build()
        corpus_facts.export_facts(frozen, self.root / "facts", [JOURNAL], b=100, seed=3, code_revision="test")
        sys.path.insert(0, str(source))
        self.addCleanup(sys.path.remove, str(source))
        writing = importlib.import_module("ocsl_writing.facts")
        report = writing.verify_facts(self.root / "facts" / "facts.json")
        self.assertTrue(report["ok"])
        self.assertIn("census.synthetic-journal-of-examples.headline_share", report["facts"])


class CommandLineTests(RunnerTestCase):
    def test_study_commands(self):
        manifest = self.root / "input" / "reviews.jsonl"
        manifest.parent.mkdir()
        (manifest.parent / "review.xml").write_bytes((FIXTURES / "review.xml").read_bytes())
        manifest.write_text(json.dumps(record()) + "\n")
        self.assertEqual(1, len(load_records(manifest)))
        self.assertEqual((FIXTURES / "review.xml").read_bytes(), read_document(manifest.parent, record()))
        outputs = []
        for argv in (["study", "prepare", "--database", str(self.database), "--seed", "5", "--input", str(manifest)],
                     ["study", "judge", "--database", str(self.database), "--seed", "5", "--base-url", self.base],
                     ["study", "full-text", "--database", str(self.database), "--seed", "5", "--base-url", self.base],
                     ["study", "gates", "--database", str(self.database)],
                     ["study", "freeze", "--database", str(self.database), "--output", str(self.root / "frozen.sqlite3")],
                     ["study", "facts", "--scores", str(self.root / "frozen.sqlite3"), "--output", str(self.root / "facts"),
                      "--journal", JOURNAL, "--b", "50", "--bootstrap-seed", "5"],
                     ["study", "verify-facts", "--facts", str(self.root / "facts" / "facts.json")],
                     ["instruments", "verify"]):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(0, main(argv), argv)
            outputs.append(json.loads(buffer.getvalue()))
        self.assertEqual(1, outputs[0]["prepared"])
        self.assertEqual("completed", outputs[1]["state"])
        self.assertEqual("completed", outputs[2]["state"])
        self.assertTrue(outputs[3]["ok"], outputs[3])
        self.assertTrue(outputs[4]["complete"])
        self.assertTrue(outputs[6]["ok"])

    def test_prepare_records_unreadable_reviews_and_continues(self):
        folder = self.root / "input"
        folder.mkdir()
        good = (FIXTURES / "review.xml").read_bytes()
        (folder / "review.xml").write_bytes(good)
        # Two bibliography labels that collide after normalisation make the structured extraction refuse it.
        (folder / "ambiguous.xml").write_bytes(good.replace(b"<label>2.</label>", b"<label>1</label>", 1))
        failed_works = {**record()["works"], "work-1": {**record()["works"]["work-1"], "europePmcOutcome": "europe-pmc-failed"}}
        records = [
            {**record(doi="10.5555/ambiguous"), "document": {"path": "ambiguous.xml", "kind": "jats"}},
            {**record(doi="10.5555/missing"), "document": {"path": "missing.xml", "kind": "jats"}},
            {**record(doi="10.5555/digest"), "document": {"path": "review.xml", "kind": "jats", "sha256": "0" * 64}},
            {**record(doi="10.5555/lookup"), "works": failed_works},
            record(),
        ]
        manifest = folder / "reviews.jsonl"
        manifest.write_text("".join(json.dumps(item) + "\n" for item in records))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(0, main(["study", "prepare", "--database", str(self.database), "--seed", "5", "--input",
                                      str(manifest)]))
        result = json.loads(buffer.getvalue())
        self.assertEqual(1, result["prepared"])
        self.assertEqual({"prepared": 1, "extraction-failed": 1, "unavailable": 2, "skipped-abstract-lookup-failed": 1},
                         result["states"])
        reviews = {r["doi"]: r for r in self.rows("SELECT doi, provenance_class, abstract_state, extraction_json FROM reviews")}
        self.assertNotIn("10.5555/lookup", reviews, "a failed abstract lookup waits for study abstracts")
        self.assertIn("extractionFailed", reviews["10.5555/ambiguous"]["extraction_json"])
        self.assertEqual(("unavailable", "completed"), (reviews["10.5555/missing"]["provenance_class"],
                                                        reviews["10.5555/missing"]["abstract_state"]))
        self.assertIn("digest", reviews["10.5555/digest"]["extraction_json"])
        from ocsl_review.census_runner import census_status
        with closing(scores_db.open_readonly(self.database)) as connection:
            self.assertEqual(1, census_status(connection)["extractionFailedReviews"])
            coverage = {row["cell"]: row for row in __import__("ocsl_review.estimands", fromlist=["coverage"]).coverage(
                connection, JOURNAL, "year")}
        self.assertEqual((5 - 1, 1, 1), (coverage[2021]["reviews"], coverage[2021]["reviews_read"],
                                         coverage[2021]["reviews_extraction_failed"]))

    def test_abstracts_stream_into_a_partial_file_and_resume(self):
        from ocsl_review.study_cli import fill_abstracts
        source = self.root / "reviews.jsonl"
        output = self.root / "out" / "reviews.abstracts.jsonl"
        records = [{"journal": JOURNAL, "doi": "10.5555/r%d" % i, "works": {"w": {"doi": "10.5555/n.%d" % (i + 1)}}}
                   for i in range(3)]
        source.write_text("".join(json.dumps(item) + "\n" for item in records))
        queries = []

        def get(params):
            queries.append(params)
            doi = params["query"].split('"')[1]
            return {"resultList": {"result": [{"doi": doi, "abstractText": "Abstract of %s." % doi}]}}
        output.parent.mkdir()
        first = json.dumps({**records[0], "works": {"w": {"doi": "10.5555/n.1", "abstract": "A.", "abstractSource": "europe-pmc-core"}}})
        Path(str(output) + ".partial").write_text(first + "\n" + '{"journal": "cut off')
        result = fill_abstracts(source, output, get)
        self.assertEqual((3, 1, 0), (result["records"], result["resumed"], result["lookupFailed"]))
        self.assertEqual(2, len(queries), "the record written before the crash is not looked up again")
        lines = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([r["doi"] for r in records], [line["doi"] for line in lines])
        self.assertFalse(Path(str(output) + ".partial").exists())
        Path(str(output) + ".partial").write_text(json.dumps({"journal": JOURNAL, "doi": "10.5555/other"}) + "\n")
        with self.assertRaisesRegex(ValidationError, "does not continue this input"):
            fill_abstracts(source, output, get)

    def test_cross_validate_command(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(0, main(["study", "cross-validate", "--jats", str(FIXTURES / "review.xml"),
                                      "--html", str(FIXTURES / "review.html")]))
        result = json.loads(buffer.getvalue())
        self.assertEqual((1.0, True, True), (result["share"], result["gate"]["ok"], result["uncovered"]["html"]["ok"]))

    def test_document_paths_cannot_escape(self):
        with self.assertRaises(ValidationError):
            read_document(self.root, {"document": {"path": "../x.xml"}})
        (self.root / "doc.xml").write_bytes(b"<article/>")
        with self.assertRaises(ValidationError):
            read_document(self.root, {"document": {"path": "doc.xml", "sha256": "0" * 64}})


if __name__ == "__main__":
    unittest.main()
