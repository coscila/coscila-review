# SPDX-License-Identifier: Apache-2.0
"""Facts export: closed facts.sqlite3, per-fact SQL, input hashes, code revision and the version-1 contract."""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ocsl_review import corpus_facts, scores_db
from ocsl_review.errors import ValidationError

from test_estimands import build_scores


def writing_source():
    """src/ of a coscila-writing checkout (OCSL_WRITING_HOME, or a sibling worktree), if present."""
    home = os.environ.get("OCSL_WRITING_HOME")
    candidates = [Path(home)] if home else []
    candidates += [Path(__file__).resolve().parents[2] / name for name in ("writing-e2", "coscila-writing")]
    return next((c / "src" for c in candidates if (c / "src" / "ocsl_writing" / "facts.py").is_file()), None)


class FactsExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        build_scores(root / "work.sqlite3")
        cls.frozen = root / "scores.sqlite3"
        scores_db.freeze(root / "work.sqlite3", cls.frozen)
        cls.output = root / "facts"
        cls.result = corpus_facts.export_facts(cls.frozen, cls.output, ["J1", "J2"], b=200, seed=11, ratio_pair=("J1", "J2"),
                                               trend_years=(2020, 2021), code_revision="rev-test")
        cls.bundle = json.loads((cls.output / "facts.json").read_text())
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fact(self, key):
        return self.bundle["facts"][key]

    def test_paired_no_truncation_and_composite_classes(self):
        from ocsl_review import estimands
        with closing(scores_db.open_frozen(self.frozen)) as c:
            comparison = estimands.no_truncation_comparison(c, "J1")
            classes = estimands.three_classes(c, "J1")
        for subset in ("all", "cut", "uncut"):
            prefix = f"census.j1.sensitivity.no-truncation.{subset}"
            self.assertEqual(comparison[subset]["n"], self.fact(prefix + ".n")["value"])
            for name, key in (("primary_share", "primaryShare"), ("variant_share", "variantShare"),
                              ("agreement", "agreement")):
                if comparison[subset][key] is None:
                    self.assertNotIn(f"{prefix}.{name}", self.bundle["facts"])
                else:
                    self.assertAlmostEqual(comparison[subset][key], self.fact(f"{prefix}.{name}")["value"])
        # The enriched sample's share and the primary share on the same entries are both available.
        self.assertEqual((0.5, 1.0), (self.fact("census.j1.sensitivity.no-truncation.all.primary_share")["value"],
                                      self.fact("census.j1.sensitivity.no-truncation.all.variant_share")["value"]))
        names = {"full text read": "full-text-read", "escalated, full text unavailable": "escalated-full-text-unavailable",
                 "not escalated": "not-escalated"}
        for stratum, counts in classes["composite"].items():
            key = names[stratum]
            self.assertEqual(sum(counts.values()), self.fact(f"census.j1.composite.{key}.n")["value"])
            for klass, value in counts.items():
                self.assertEqual(value, self.fact(f"census.j1.composite.{key}.{klass}")["value"])
        self.assertIn("census.j1.composite.share_of_sure", self.bundle["facts"])

    def test_counts_and_headline(self):
        self.assertEqual(12, self.fact("census.j1.d2_n")["value"])
        self.assertEqual(6, self.fact("census.j1.d2_false")["value"])
        self.assertEqual(0.5, self.fact("census.j1.headline_share")["value"])
        self.assertEqual(4, self.fact("census.j1.reviews")["value"])
        self.assertEqual(1, self.fact("census.j1.entries_not_cited")["value"])
        self.assertEqual(1, self.fact("census.j1.entries_invalid")["value"])
        self.assertEqual(1 / 14, self.fact("census.j1.invalid_share")["value"])
        self.assertEqual(1.0, self.fact("census.j1.ft_overturn_false_share")["value"])
        self.assertIsInstance(self.fact("census.j1.year.2021.n")["value"], int)

    def test_coverage_cells_are_facts(self):
        self.assertEqual(1, self.fact("census.j1.coverage.year.2020.reviews")["value"])
        self.assertEqual(7, self.fact("census.j1.coverage.year.2021.d2_n")["value"])
        self.assertEqual(4, self.fact("census.j1.coverage.year.2021.d2_false")["value"])
        self.assertEqual(0, self.fact("census.j1.coverage.year.2022.reviews_read")["value"])
        self.assertEqual(10, self.fact("census.j1.coverage.stratum.review.d2_n")["value"])
        self.assertEqual(1, self.fact("census.j1.coverage.provenance.unavailable.reviews")["value"])
        self.assertEqual(1, self.fact("census.j1.coverage.year.2021.entries_invalid")["value"])

    def test_interval_and_derivation_facts(self):
        lo, hi = self.fact("census.j1.headline_bootstrap_lo"), self.fact("census.j1.headline_bootstrap_hi")
        self.assertLessEqual(lo["value"], 0.5)
        self.assertGreaterEqual(hi["value"], 0.5)
        self.assertEqual({"b": 200, "seed": 11}, {k: lo["derivation"][k] for k in ("b", "seed")})
        self.assertIn("estimand", lo["derivation"])
        self.assertIn("census.ratio.j1.j2.estimate", self.bundle["facts"])
        self.assertEqual(4.0, self.fact("census.ratio.j1.j2.estimate")["value"])
        self.assertIn("census.j2.trend_slope", self.bundle["facts"])

    def test_every_fact_carries_sql_source_inputs_and_revision(self):
        database_sha = scores_db.file_sha256(self.output / "facts.sqlite3")
        for key, fact in self.bundle["facts"].items():
            self.assertRegex(key, r"^[A-Za-z0-9_.-]+$")
            self.assertTrue(fact["sql"].startswith("SELECT"))
            self.assertEqual({"path": "facts.sqlite3", "sha256": database_sha}, fact["source"])
            self.assertEqual(scores_db.file_sha256(self.frozen), fact["inputs"]["scoresSha256"])
            self.assertEqual("rev-test", fact["codeRevision"])
        self.assertEqual(1, self.bundle["schemaVersion"])

    def test_contract_verification(self):
        self.assertTrue(corpus_facts.verify_export(self.output / "facts.json")["ok"])
        with closing(sqlite3.connect((self.output / "facts.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as c:
            self.assertEqual("delete", c.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertGreater(c.execute("SELECT COUNT(*) FROM estimates").fetchone()[0], 10)
        for mutation in ({"sql": "SELECT 3"}, {"value": 13}, {"source": {"path": "../x", "sha256": "0" * 64}}):
            bundle = json.loads(json.dumps(self.bundle))
            bundle["facts"]["census.j1.d2_n"].update(mutation)
            path = self.output / "mutated.json"
            path.write_text(json.dumps(bundle))
            with self.subTest(mutation=list(mutation)), self.assertRaises(ValidationError):
                corpus_facts.verify_export(path)

    def test_writing_verifier_accepts_the_export(self):
        source = writing_source()
        if source is None:
            self.skipTest("no coscila-writing checkout available")
        sys.path.insert(0, str(source))
        self.addCleanup(sys.path.remove, str(source))
        report = importlib.import_module("ocsl_writing.facts").verify_facts(self.output / "facts.json")
        self.assertTrue(report["ok"])
        self.assertEqual(set(self.bundle["facts"]), set(report["facts"]))

    def test_preconditions(self):
        with self.assertRaises(ValidationError):
            corpus_facts.export_facts(self.frozen, self.output, ["J1"], b=10, seed=1)
        with self.assertRaises(ValidationError):
            corpus_facts.export_facts(self.frozen, self.root / "other", ["J9"], b=10, seed=1)
        build_scores(self.root / "open.sqlite3")
        with self.assertRaises(ValidationError):
            corpus_facts.export_facts(self.root / "open.sqlite3", self.root / "third", ["J1"], b=10, seed=1)
        self.assertEqual("frontiers-in-x", corpus_facts.slug("Frontiers in X"))
        with self.assertRaises(ValidationError):
            corpus_facts.slug("  ")


if __name__ == "__main__":
    unittest.main()
