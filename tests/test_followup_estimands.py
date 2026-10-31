# SPDX-License-Identifier: Apache-2.0
"""The follow-up estimands on a hand-built branch: numerators, denominators, weights, coverage and determinism.

The rows the evidence commands write in a study (status, support, function, dependency) are inserted directly; the
hotspot selection is inserted too, so S2 holds the loci of one chosen work.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import followup_fixtures as F
from ocsl_review import followup_estimands, followup_stats, followup_strata
from ocsl_review.errors import ValidationError
from test_followup_strata import A1, A2, B1, STATUSES, insert_statuses, lid, make_branch, protocol_with

JA, JB = F.JOURNAL_A, F.JOURNAL_B
SLUG_A, SLUG_B = "journal-of-synthetic-reviews-a", "journal-of-synthetic-reviews-b"
ALL_BY = ["journal", "year", "status"]


def declared(estimand, by=ALL_BY, **extra):
    return {"id": estimand, "numerator": f"numerator of {estimand}", "denominator": f"denominator of {estimand}",
            "by": by, "weights": "realised-inclusion-probability", **extra}


def estimands_protocol(*, licensing=True):
    protocol = protocol_with(S3={"licensingVerified": licensing})
    protocol["estimands"] = [declared(name) for name in followup_estimands.ESTIMANDS if name != "distortion-type"]
    protocol["estimands"].append(declared("distortion-type", labelSource="adjudication"))
    return protocol


def hand_built_branch(root: Path, name: str, protocol, census) -> object:
    """Statuses (one work unknown), a hotspot selection, frozen strata and the evidence rows of every estimand."""
    branch = make_branch(root, name, protocol, census=census)
    insert_statuses(branch, {**STATUSES, "w-plain-3": ("unknown", None, None)})
    with branch.writing() as connection:
        for work, selected in (("w-plain-1", 1), ("w-plain-2", 0)):
            connection.execute("INSERT INTO fu_hotspots(work_key, citing_reviews, n, k, expected, excess, rho, p_value, "
                               "q_value, candidate, family_excluded, selected, model) "
                               "VALUES(?,2,2,2,0.5,1.5,0.0,0.001,0.002,1,0,?,?)", (work, selected, followup_stats.MODEL))
    followup_strata.cmd_strata(SimpleNamespace(database=str(branch.path), freeze=True))
    with branch.writing() as connection:
        def claim(unit):
            return connection.execute("SELECT claim_id FROM fu_frame WHERE locus_id=?", (unit,)).fetchone()[0]
        for (journal, doi, label), dependency in {(JA, A1, "1"): "essential", (JA, A2, "2"): "sole",
                                                  (JB, B1, "2"): "redundant", (JA, A2, "1"): "undetermined"}.items():
            unit = lid(journal, doi, label)
            connection.execute("INSERT INTO fu_dependency(locus_id, claim_id, cocited, cocited_supporting, "
                               "cocited_unassessable, class, anchor_verdict, rule_version, detail_json) "
                               "VALUES(?,?,0,0,0,?,NULL,'dependency-aggregation@1','{}')", (unit, claim(unit), dependency))
        connection.execute("INSERT INTO fu_function(locus_id, class, assessable, reason, quote, quote_field) "
                           "VALUES(?,'notes-retraction',1,NULL,'retracted cohort study','citingSentence')",
                           (lid(JA, A2, "2"),))
        connection.execute("INSERT INTO fu_function(locus_id, class, assessable, reason) VALUES(?,NULL,0,'abstained')",
                           (lid(JB, B1, "2"),))
        for unit, verdict, assessable, reason in ((lid(JA, A1, "2"), "contradicted", 1, None),
                                                  (lid(JB, B1, "3"), "direct-support", 1, None)):
            connection.execute("INSERT INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                               "VALUES(?,'full-text',?,?,?)", (unit, verdict, assessable, reason))
        connection.execute("INSERT INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                           "VALUES(?,'abstract','not-addressed',1,NULL)", (lid(JB, B1, "3"),))
        for unit, klass in ((lid(JA, A1, "3"), "consistent"), (lid(JB, B1, "4"), "wrong-edition")):
            connection.execute("INSERT INTO fu_numeric(locus_id, class, assessable, reason, rule_version) "
                               "VALUES(?,?,1,NULL,'numeric-comparison@1')", (unit, klass))
        anchor, other = lid(JA, A1, "1"), lid(JA, A2, "2")
        for anchor_id, sentence, match, reliance, reason, probability in (
                (anchor, "s1", "same-claim", "cites-review-for-claim", None, 0.5),
                (anchor, "s2", "different-claim", "cites-review-otherwise", None, 0.5),
                (anchor, "s3", None, None, "not-judged", 0.5),
                (other, "s4", "same-claim", "cites-review-otherwise", None, 1.0)):
            connection.execute("INSERT INTO fu_secondgen_matches(anchor_locus_id, sentence_id, citing_key, rank, match, "
                               "reliance, assessable, reason, quote, inclusion_probability) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (anchor_id, sentence, "doi:10.5555/fu.later.1", 1 if match else None, match, reliance,
                                int(match is not None), reason, "quoted words" if match else None, probability))
        connection.execute("INSERT INTO fu_secondgen_sample(anchor_locus_id, edge_id, citing_key, target_doi, available, "
                           "sampled, inclusion_probability, seed, selected_at) VALUES(?,?,?,?,4,2,0.5,'s','t')",
                           (anchor, "edge-9", "doi:10.5555/fu.later.9", A1))
        connection.execute("INSERT INTO fu_secondgen_candidates(anchor_locus_id, citing_key, state, reason, k, sentences, "
                           "candidates, created_at) VALUES(?,?,'no-sentences','fetch-failed',3,0,0,'t')",
                           (anchor, "doi:10.5555/fu.later.9"))
    return branch


class EstimandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        (cls.root / "census").mkdir()
        cls.census = F.make_parent(cls.root / "census")
        cls.branch = hand_built_branch(cls.root, "estimands.sqlite3", estimands_protocol(), cls.census)
        cls.result = followup_estimands.cmd_estimands(SimpleNamespace(database=str(cls.branch.path), b=None,
                                                                      bootstrap_seed=None))
        cls.values = cls.estimates(cls.branch)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @staticmethod
    def estimates(branch):
        with closing(branch.connect()) as connection:
            return {row["key"]: dict(row) for row in connection.execute("SELECT * FROM fu_estimates")}

    def value(self, key):
        return self.values[key]["value"]

    def coverage(self, estimand, journal="*", year="*"):
        with closing(self.branch.connect()) as connection:
            return {(row["state"], row["reason"]): (row["units"], row["weighted"]) for row in connection.execute(
                "SELECT * FROM fu_coverage WHERE estimand=? AND journal=? AND year=?", (estimand, journal, year))}

    def test_compromised_share_over_loci_with_a_known_status(self):
        self.assertAlmostEqual(5 / 10, self.value("followup.compromised-share.all.all.share"))
        self.assertEqual((5.0, 10.0, 10), tuple(self.value(f"followup.compromised-share.all.all.{stat}")
                                                for stat in ("numerator", "denominator", "n")))
        self.assertAlmostEqual(3 / 10, self.value("followup.compromised-share.retracted.all.all.share"))
        self.assertAlmostEqual(1 / 10, self.value("followup.compromised-share.corrected.all.all.share"))
        # review a2: two compromised works and one work of unknown status (coverage, not denominator)
        self.assertAlmostEqual(1.0, self.value(f"followup.compromised-share.{SLUG_A}.2023.share"))
        self.assertEqual({("assessable", "-"): (2, 2.0), ("unassessable", "status-unknown"): (1, 1.0)},
                         self.coverage("compromised-share", JA, "2023"))
        self.assertAlmostEqual(2 / 4, self.value(f"followup.compromised-share.{SLUG_B}.all.share"))
        self.assertEqual({("assessable", "-"): (10, 10.0), ("unassessable", "status-unknown"): (1, 1.0)},
                         self.coverage("compromised-share"))
        self.assertIsNone(self.values["followup.compromised-share.all.all.share"]["stratum"])
        detail = json.loads(self.values["followup.compromised-share.retracted.all.all.share"]["detail_json"])
        self.assertEqual(("retracted", "numerator", 200, 13), (detail["level"], detail["levelRole"], detail["b"],
                                                                detail["seed"]))
        self.assertNotIn("followup.compromised-share.expression-of-concern.retracted.all.all.share", self.values)

    def test_sole_or_essential_leaves_undetermined_and_unjudged_loci_to_coverage(self):
        self.assertAlmostEqual(2 / 3, self.value("followup.sole-or-essential.all.all.share"))
        self.assertEqual((2.0, 3.0, 3), tuple(self.value(f"followup.sole-or-essential.all.all.{stat}")
                                              for stat in ("numerator", "denominator", "n")))
        self.assertEqual({("assessable", "-"): (3, 3.0), ("unassessable", "undetermined"): (1, 1.0),
                          ("unassessable", "not-judged"): (1, 1.0)}, self.coverage("sole-or-essential"))
        self.assertAlmostEqual(2 / 3, self.value("followup.sole-or-essential.retracted.all.all.share"))
        self.assertEqual({("unassessable", "not-judged"): (1, 1.0)}, self.coverage("sole-or-essential.corrected"))
        self.assertEqual("not-measured", self.value("followup.sole-or-essential.corrected.all.all.share"))
        self.assertEqual("S1", self.values["followup.sole-or-essential.all.all.share"]["stratum"])

    def test_cited_after_notice_with_the_lag_distribution(self):
        self.assertAlmostEqual(2 / 3, self.value("followup.cited-after-notice.all.all.share"))
        self.assertEqual({("assessable", "-"): (3, 3.0), ("unassessable", "undecidable-at-precision"): (1, 1.0),
                          ("unassessable", "notice-date-unknown"): (1, 1.0)}, self.coverage("cited-after-notice"))
        detail = json.loads(self.values["followup.cited-after-notice.all.all.share"]["detail_json"])
        self.assertEqual({"n": 2, "days-q1": 379.0, "days-median": 499.0, "days-q3": 619.0}, detail["lagDays"])
        followup_stats.cmd_lag(SimpleNamespace(database=str(self.branch.path)))
        lag = self.estimates(self.branch)
        for stat in ("share", "lo", "hi", "n"):
            self.assertEqual(lag[f"followup.lag.after-notice.all.all.{stat}"]["value"],
                             lag[f"followup.cited-after-notice.all.all.{stat}"]["value"])

    def test_acknowledgement_among_post_notice_loci_with_an_assessable_function(self):
        self.assertEqual(1.0, self.value("followup.acknowledges-notice.all.all.share"))
        self.assertEqual(1, self.value("followup.acknowledges-notice.all.all.n"))
        self.assertEqual({("assessable", "-"): (1, 1.0), ("unassessable", "abstained"): (1, 1.0)},
                         self.coverage("acknowledges-notice"))

    def test_second_generation_is_weighted_by_the_edge_sample(self):
        # judged sentences: s1 (restates, w 2), s2 (another claim, w 2), s4 (cites the review otherwise, w 1)
        self.assertAlmostEqual(2 / 5, self.value("followup.second-generation.all.all.share"))
        self.assertEqual((2.0, 5.0, 3), tuple(self.value(f"followup.second-generation.all.all.{stat}")
                                              for stat in ("numerator", "denominator", "n")))
        self.assertEqual({("assessable", "-"): (3, 5.0), ("unassessable", "not-judged"): (1, 2.0),
                          ("unassessable", "fetch-failed"): (1, 2.0)}, self.coverage("second-generation"))
        detail = json.loads(self.values["followup.second-generation.all.all.share"]["detail_json"])
        self.assertIn("inclusion probability", detail["weights"])

    def test_distortion_type_is_not_measured_and_counts_the_loci_awaiting_labels(self):
        self.assertEqual("not-measured", self.value("followup.distortion-type.all.all.share"))
        self.assertEqual(0, self.value("followup.distortion-type.all.all.n"))
        self.assertNotIn("followup.distortion-type.all.all.lo", self.values)
        detail = json.loads(self.values["followup.distortion-type.all.all.share"]["detail_json"])
        self.assertEqual((1, "adjudication"), (detail["awaitingLabels"], detail["labelSource"]))
        self.assertEqual({("unassessable", "awaiting-labels"): (1, 1.0)}, self.coverage("distortion-type"))
        self.assertEqual(1, self.result["estimands"]["distortion-type"]["awaitingLabels"])

    def test_numeric_classes_over_assessable_s3_loci(self):
        self.assertAlmostEqual(0.5, self.value("followup.numeric-classes.consistent.all.all.share"))
        self.assertAlmostEqual(0.5, self.value("followup.numeric-classes.wrong-edition.all.all.share"))
        self.assertEqual(0.0, self.value("followup.numeric-classes.wrong-geography.all.all.share"))
        self.assertEqual(1.0, self.value(f"followup.numeric-classes.consistent.{SLUG_A}.all.share"))
        self.assertEqual({("assessable", "-"): (2, 2.0)}, self.coverage("numeric-classes"))
        dropped = hand_built_branch(self.root, "estimands-dropped.sqlite3", estimands_protocol(licensing=False),
                                    self.census)
        followup_estimands.cmd_estimands(SimpleNamespace(database=str(dropped.path), b=50, bootstrap_seed=None))
        with closing(dropped.connect()) as connection:
            coverage = {(row["journal"], row["reason"]): row["units"] for row in connection.execute(
                "SELECT * FROM fu_coverage WHERE estimand='numeric-classes'")}
        self.assertEqual({("*", "excluded-by-protocol"): 2, (JA, "excluded-by-protocol"): 1,
                          (JB, "excluded-by-protocol"): 1}, coverage)

    def test_unassessable_units_never_reach_a_numerator(self):
        for key, row in self.values.items():
            if key.endswith(".share") and isinstance(row["value"], float):
                with self.subTest(key=key):
                    self.assertLessEqual(row["numerator"], row["denominator"])
                    self.assertAlmostEqual(row["value"], row["numerator"] / row["denominator"])

    def test_estimates_are_reproducible_and_follow_the_bootstrap_options(self):
        before = self.estimates(self.branch)
        followup_estimands.cmd_estimands(SimpleNamespace(database=str(self.branch.path), b=None, bootstrap_seed=None))
        after = self.estimates(self.branch)
        self.assertEqual({key: row for key, row in before.items() if not key.startswith("followup.lag.")},
                         {key: row for key, row in after.items() if not key.startswith("followup.lag.")})
        followup_estimands.cmd_estimands(SimpleNamespace(database=str(self.branch.path), b=120, bootstrap_seed=7))
        detail = json.loads(self.estimates(self.branch)["followup.compromised-share.all.all.share"]["detail_json"])
        self.assertEqual((120, 7), (detail["b"], detail["seed"]))
        followup_estimands.cmd_estimands(SimpleNamespace(database=str(self.branch.path), b=None, bootstrap_seed=None))
        with self.assertRaisesRegex(ValidationError, "positive"):
            followup_estimands.cmd_estimands(SimpleNamespace(database=str(self.branch.path), b=0, bootstrap_seed=None))

    def test_only_declared_breakdowns_and_estimands_are_written(self):
        protocol = estimands_protocol()
        protocol["estimands"] = [declared("sole-or-essential", by=["journal"])]
        branch = hand_built_branch(self.root, "estimands-narrow.sqlite3", protocol, self.census)
        followup_estimands.cmd_estimands(SimpleNamespace(database=str(branch.path), b=None, bootstrap_seed=None))
        keys = set(self.estimates(branch))
        self.assertIn(f"followup.sole-or-essential.{SLUG_B}.all.share", keys)
        self.assertFalse(any(".2021." in key or "retracted" in key for key in keys))
        self.assertFalse(any(key.startswith("followup.compromised-share") for key in keys))

    def test_estimands_need_a_freeze(self):
        unfrozen = make_branch(self.root, "estimands-unfrozen.sqlite3", estimands_protocol(), census=self.census)
        with self.assertRaisesRegex(ValidationError, "not frozen"):
            followup_estimands.cmd_estimands(SimpleNamespace(database=str(unfrozen.path), b=None,
                                                             bootstrap_seed=None))

    def test_the_command_line_writes_a_result_file(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from ocsl_review.cli import main
        target = self.root / "estimands-result.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "estimands", "--database", str(self.branch.path),
                         "--b", "100", "--bootstrap-seed", "5"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual((100, 5), (record["result"]["b"], record["result"]["seed"]))


if __name__ == "__main__":
    unittest.main()
