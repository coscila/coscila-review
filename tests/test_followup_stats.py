# SPDX-License-Identifier: Apache-2.0
"""The follow-up statistics: the hotspot test (S2), the retraction-lag model and the weighted clustered bootstrap."""
from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import followup_fixtures as F
from ocsl_review import estimands, followup_stats
from ocsl_review.errors import ValidationError
from test_followup_strata import (A1, A2, B1, frozen_branch, insert_statuses, lid, make_branch, protocol_with,
                                  synthetic_parent)

JA, JB = F.JOURNAL_A, F.JOURNAL_B
SLUG_A, SLUG_B = "journal-of-synthetic-reviews-a", "journal-of-synthetic-reviews-b"


def hotspot_census():
    """Journal A (one year) with a work cited unsupported far above its base, a balanced work, a statistics report and
    a rarely cited work; journal B (another year) with a high base rate and a work at that base."""
    reviews, works, citations = [], [], []
    for index in range(1, 41):
        reviews.append((JA, f"10.5555/fu.hot.a{index:02d}", 2021, f"2021-{(index % 12) + 1:02d}-01", "public", "x"))
    for index in range(1, 25):
        reviews.append((JB, f"10.5555/fu.hot.b{index:02d}", 2022, f"2022-{(index % 12) + 1:02d}-01", "public", "x"))
    titles = {"w-hot": "Fictional cohort of marker eta", "w-balanced": "Fictional balanced study",
              "w-stats": "Synthetic disease statistics report, second edition", "w-rare": "Fictional rare study",
              "w-bhigh": "Fictional study cited in journal B"}
    titles.update({f"fa{i}": f"Fictional filler A{i}" for i in range(10)})
    titles.update({f"fb{i}": f"Fictional filler B{i}" for i in range(8)})
    works = [(key, f"10.5555/fu.hot.{key}", None, title, "2015") for key, title in titles.items()]

    def cite(journal, doi, label, work, category):
        citations.append((journal, doi, str(label), work, category, None,
                          [(label, f"Statement {label} of the review ⟨R{label}⟩ .")]))
    for index in range(1, 41):
        doi = f"10.5555/fu.hot.a{index:02d}"
        for j in range(6):
            cite(JA, doi, j + 1, f"fa{(index + j) % 10}", "false" if j == 0 else "true")
        if index <= 12:
            cite(JA, doi, 7, "w-hot", "false" if index <= 11 else "true")
        elif index <= 24:
            cite(JA, doi, 8, "w-balanced", "false" if index <= 14 else "true")
        elif index <= 36:
            cite(JA, doi, 9, "w-stats", "false" if index <= 35 else "true")
        elif index <= 39:
            cite(JA, doi, 10, "w-rare", "false")
    for index in range(1, 25):
        doi = f"10.5555/fu.hot.b{index:02d}"
        for j in range(4):
            cite(JB, doi, j + 1, f"fb{(index + j) % 8}", "false" if j < 2 else "true")
        if index <= 12:
            cite(JB, doi, 5, "w-bhigh", "false" if index <= 6 else "true")
    return reviews, works, citations


class MomentEstimatorTests(unittest.TestCase):
    def test_rho_by_hand(self):
        units = [(3, 4, 0.25), (0, 4, 0.25), (2, 5, 0.4)]
        # numerator: (3-1)^2-0.75 + (0-1)^2-0.75 + (2-2)^2-1.2 = 2.3; denominator: 0.75*3 + 0.75*3 + 1.2*4 = 9.3
        self.assertAlmostEqual(2.3 / 9.3, followup_stats.moment_rho(units), places=12)
        self.assertEqual(0.0, followup_stats.moment_rho([(1, 4, 0.25), (1, 4, 0.25)]))
        self.assertEqual(0.0, followup_stats.moment_rho([(1, 1, 0.5)]))
        clipped = followup_stats.moment_rho([(10, 10, 0.1), (0, 10, 0.1), (10, 10, 0.1)])
        self.assertLess(clipped, 1.0)
        self.assertGreater(clipped, 0.999999)

    def test_hotspot_test_on_entries_by_hand(self):
        entries = [("J", f"r{i}", "w1", "false", 2020) for i in range(4)]
        entries += [("J", f"r{i}", "w2", "false" if i == 0 else "true", 2020) for i in range(4)]
        entries += [("J", "r9", "w3", "false", 2020)]
        result = followup_stats.hotspot_test(entries, min_reviews=2, fdr=0.05, base=["journal", "year"],
                                             excluded=lambda work: False)
        rows = {row["work_key"]: row for row in result["rows"]}
        rate = 6 / 9
        self.assertAlmostEqual(4 * rate, rows["w1"]["expected"])
        self.assertAlmostEqual(4 - 4 * rate, rows["w1"]["excess"])
        self.assertEqual((1, 1, 0), (rows["w1"]["candidate"], rows["w2"]["candidate"], rows["w3"]["candidate"]))
        self.assertIsNone(rows["w3"]["p_value"])
        expected_rho = followup_stats.moment_rho([(4, 4, rate), (1, 4, rate)])
        self.assertAlmostEqual(expected_rho, result["rho"])
        self.assertAlmostEqual(estimands.upper_tail(4, 4, rate, expected_rho), rows["w1"]["p_value"])
        p_values = [rows["w1"]["p_value"], rows["w2"]["p_value"]]
        self.assertEqual(estimands.benjamini_hochberg(p_values), [rows["w1"]["q_value"], rows["w2"]["q_value"]])


class HotspotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        reviews, works, citations = hotspot_census()
        cls.census = synthetic_parent(cls.root / "census", reviews=reviews, works=works, citations=citations)
        cls.protocol = protocol_with(S2={"minCitingReviews": 10, "fdr": 0.05, "base": ["journal", "year"],
                                         "category": "abstract-tier-false-among-true-and-false",
                                         "excludeFamilies": ["statistics-report"]})
        cls.protocol["journals"] = [JA, JB]
        cls.branch = make_branch(cls.root, "hotspots.sqlite3", cls.protocol, census=cls.census)
        cls.result = followup_stats.cmd_hotspots(SimpleNamespace(database=str(cls.branch.path)))
        with closing(cls.branch.connect()) as connection:
            cls.rows = {row["work_key"]: dict(row) for row in connection.execute("SELECT * FROM fu_hotspots")}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_a_work_cited_unsupported_far_above_its_base_is_selected(self):
        hot, balanced = self.rows["w-hot"], self.rows["w-balanced"]
        base_a = (40 + 11 + 2 + 11 + 3) / (240 + 12 + 12 + 12 + 3)
        self.assertEqual((12, 12, 11), (hot["citing_reviews"], hot["n"], hot["k"]))
        self.assertAlmostEqual(12 * base_a, hot["expected"])
        self.assertAlmostEqual(11 - 12 * base_a, hot["excess"])
        self.assertEqual((1, 1), (hot["candidate"], hot["selected"]))
        self.assertLessEqual(hot["q_value"], 0.05)
        self.assertEqual((1, 0), (balanced["candidate"], balanced["selected"]))
        self.assertGreater(balanced["q_value"], 0.05)
        self.assertEqual(followup_stats.MODEL, hot["model"])
        self.assertEqual(1, self.result["selected"])

    def test_the_base_rate_is_the_works_own_journal_and_year(self):
        high = self.rows["w-bhigh"]
        self.assertAlmostEqual(12 * 0.5, high["expected"])
        self.assertAlmostEqual(0.0, high["excess"])
        self.assertEqual((1, 0), (high["candidate"], high["selected"]))

    def test_candidates_need_the_citing_review_minimum_and_no_excluded_family(self):
        rare, report = self.rows["w-rare"], self.rows["w-stats"]
        self.assertEqual((3, 3, 0, 0, None), (rare["citing_reviews"], rare["k"], rare["candidate"], rare["selected"],
                                              rare["p_value"]))
        self.assertEqual((1, 0, 0, None), (report["family_excluded"], report["candidate"], report["selected"],
                                           report["q_value"]))
        candidates = [row for row in self.rows.values() if row["candidate"]]
        self.assertEqual(1 + 1 + 1 + 10 + 8, len(candidates))
        self.assertAlmostEqual(followup_stats.moment_rho((row["k"], row["n"], row["expected"] / row["n"])
                                                         for row in candidates), self.result["rho"])
        self.assertTrue(all(row["rho"] == self.result["rho"] for row in self.rows.values()))
        self.assertEqual(1, self.result["familyExcluded"])

    def test_the_hotspots_reference_parent_works_only(self):
        report = self.branch.verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(0, report["references"]["hotspotsForUnknownWorks"])

    def test_the_command_line_writes_a_result_file(self):
        from ocsl_review.cli import main
        target = self.root / "hotspots-result.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "hotspots", "--database", str(self.branch.path)])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual(self.result["selected"], record["result"]["selected"])


class LagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.branch = frozen_branch(cls.root, "lag.sqlite3")
        cls.result = followup_stats.cmd_lag(SimpleNamespace(database=str(cls.branch.path)))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def estimates(self):
        with closing(self.branch.connect()) as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM fu_estimates")}

    def test_dates_are_compared_at_the_coarser_precision(self):
        compare = followup_stats.compare_dates
        self.assertEqual({"after_notice": 1, "lag_days": 295, "date_basis": "day", "reason": None},
                         compare("2020-05-10", "day", "2021-03-01"))
        self.assertEqual({"after_notice": 0, "lag_days": -19, "date_basis": "day", "reason": None},
                         compare("2021-03-20", None, "2021-03-01"))
        self.assertEqual({"after_notice": None, "lag_days": None, "date_basis": "month",
                          "reason": "undecidable-at-precision"}, compare("2021-03", "month", "2021-03-15"))
        self.assertEqual({"after_notice": 1, "lag_days": None, "date_basis": "month", "reason": None},
                         compare("2021-03", "month", "2021-04-02"))
        self.assertEqual({"after_notice": 1, "lag_days": None, "date_basis": "year", "reason": None},
                         compare("2021", "year", "2022-01-05"))
        self.assertEqual("undecidable-at-precision", compare("2021-03-20", "year", "2021-11-01")["reason"])
        self.assertEqual({"after_notice": None, "lag_days": 0, "date_basis": "day",
                          "reason": "undecidable-at-precision"}, compare("2021-03-01", "day", "2021-03-01"))
        self.assertEqual("notice-date-unknown", compare(None, None, "2021-03-01")["reason"])
        self.assertEqual("first-online-unknown", compare("2021-03-01", "day", None)["reason"])
        self.assertEqual("notice-date-unknown", compare("2021-02-30", "day", "2021-03-01")["reason"])
        # Grace days: an article online within them is not after the notice; one that straddles them is undecidable.
        self.assertEqual(0, compare("2021-03-01", "day", "2021-03-20", 30)["after_notice"])
        self.assertEqual(1, compare("2021-03-01", "day", "2021-04-01", 30)["after_notice"])
        self.assertEqual("undecidable-at-precision", compare("2021-03-01", "day", "2021-03", 20)["reason"])
        self.assertEqual(1, compare("2021-03", "month", "2021-05-01", 30)["after_notice"])

    def test_lag_rows_for_every_s1_locus(self):
        with closing(self.branch.connect()) as connection:
            rows = {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM fu_lag")}
        self.assertEqual(5, len(rows))
        self.assertEqual((0, -92, "day"), (rows[lid(JA, A1, "1")]["after_notice"], rows[lid(JA, A1, "1")]["lag_days"],
                                          rows[lid(JA, A1, "1")]["date_basis"]))
        self.assertEqual((1, 739), (rows[lid(JA, A2, "2")]["after_notice"], rows[lid(JA, A2, "2")]["lag_days"]))
        self.assertEqual((1, 259), (rows[lid(JB, B1, "2")]["after_notice"], rows[lid(JB, B1, "2")]["lag_days"]))
        concern = rows[lid(JA, A2, "1")]
        self.assertEqual((None, "month", "undecidable-at-precision", "expression-of-concern"),
                         (concern["after_notice"], concern["date_basis"], concern["reason"], concern["status"]))
        self.assertEqual((None, "notice-date-unknown"), (rows[lid(JB, B1, "1")]["after_notice"],
                                                         rows[lid(JB, B1, "1")]["reason"]))
        self.assertEqual((2, 1), (self.result["afterNotice"], self.result["beforeNotice"]))

    def test_after_notice_share_by_journal_and_year_with_coverage(self):
        values = self.estimates()
        self.assertAlmostEqual(2 / 3, values["followup.lag.after-notice.all.all.share"])
        self.assertEqual(3, values["followup.lag.after-notice.all.all.n"])
        self.assertEqual(2.0, values["followup.lag.after-notice.all.all.numerator"])
        self.assertAlmostEqual(0.5, values[f"followup.lag.after-notice.{SLUG_A}.all.share"])
        self.assertAlmostEqual(1.0, values[f"followup.lag.after-notice.{SLUG_B}.all.share"])
        self.assertAlmostEqual(0.0, values["followup.lag.after-notice.all.2021.share"])
        self.assertAlmostEqual(1.0, values[f"followup.lag.after-notice.{SLUG_A}.2023.share"])
        self.assertIn("followup.lag.after-notice.all.all.lo", values)
        with closing(self.branch.connect()) as connection:
            coverage = {(row["state"], row["reason"]): row["units"] for row in connection.execute(
                "SELECT * FROM fu_coverage WHERE estimand='lag.after-notice' AND journal='*' AND year='*'")}
        self.assertEqual({("assessable", "-"): 3, ("unassessable", "undecidable-at-precision"): 1,
                          ("unassessable", "notice-date-unknown"): 1}, coverage)

    def test_quartiles_rate_ratio_and_trend(self):
        values = self.estimates()
        self.assertAlmostEqual(499.0, values["followup.lag.days-median.all.all.value"])
        self.assertAlmostEqual(379.0, values["followup.lag.days-q1.all.all.value"])
        self.assertAlmostEqual(619.0, values["followup.lag.days-q3.all.all.value"])
        self.assertEqual(2, values["followup.lag.days-median.all.all.n"])
        # retracted only (notice 2021-06-01): of the three census reviews one went online before the notice and cites
        # the work, two after it and both cite it: (2 / 2) / (1 / 1)
        self.assertAlmostEqual(1.0, values["followup.lag.rate-ratio.retracted.all.all.value"])
        # all statuses: the concern notice (dated to the month) adds two reviews before it and none after it as
        # exposure; its citing review falls in the notice's own month, in neither side: (2 / 2) / (1 / 3)
        self.assertAlmostEqual(3.0, values["followup.lag.rate-ratio.all.all.value"])
        self.assertAlmostEqual(1.0, values[f"followup.lag.rate-ratio.retracted.{SLUG_A}.all.value"])
        self.assertAlmostEqual(0.5, values["followup.lag.trend.all.all.value"])
        self.assertEqual("not-measured", values["followup.lag.rate-ratio.corrected.all.all.value"])
        with closing(self.branch.connect()) as connection:
            detail = json.loads(connection.execute("SELECT detail_json FROM fu_estimates WHERE key=?",
                                                   ("followup.lag.rate-ratio.all.all.value",)).fetchone()[0])
        self.assertTrue(detail["descriptive"])
        self.assertEqual((2, 2, 1, 3), (detail["citationsAfter"], detail["reviewsAfter"], detail["citationsBefore"],
                                        detail["reviewsBefore"]))

    def test_the_lag_model_is_reproducible_and_needs_a_freeze(self):
        before = self.estimates()
        followup_stats.cmd_lag(SimpleNamespace(database=str(self.branch.path)))
        self.assertEqual(before, self.estimates())
        unfrozen = make_branch(self.root, "unfrozen.sqlite3")
        insert_statuses(unfrozen)
        with self.assertRaisesRegex(ValidationError, "not frozen"):
            followup_stats.cmd_lag(SimpleNamespace(database=str(unfrozen.path)))


class BootstrapTests(unittest.TestCase):
    def test_the_clustered_bootstrap_is_reproducible_with_its_seed(self):
        groups = [[[1.0, 1.0], [1.0, 0.0], [2.0, 1.0], [1.0, 1.0]], [[1.0, 0.0], [3.0, 2.0], [1.0, 1.0]]]
        first = followup_stats.cluster_ratio_bootstrap(groups, b=300, seed=13, scope_parts=("x", "all"))
        again = followup_stats.cluster_ratio_bootstrap(groups, b=300, seed=13, scope_parts=("x", "all"))
        other = followup_stats.cluster_ratio_bootstrap(groups, b=300, seed=14, scope_parts=("x", "all"))
        self.assertEqual(first, again)
        self.assertAlmostEqual(6 / 10, first["estimates"][0])
        self.assertNotEqual(first["bounds"], other["bounds"])
        lo, hi = first["bounds"][0]
        self.assertLessEqual(lo, first["estimates"][0])
        self.assertGreaterEqual(hi, first["estimates"][0])
        self.assertEqual(300, first["replicates"])
        empty = followup_stats.cluster_ratio_bootstrap([[], []], b=10, seed=1, scope_parts=())
        self.assertEqual(([None], [(None, None)]), (empty["estimates"], empty["bounds"]))

    def test_works_are_resampled_jointly_for_the_rate_ratio(self):
        """A work's citations before and after its notice are correlated (citation intensity differs between works);
        resampling works jointly keeps the pairs and gives a narrower interval than resampling the two sides apart."""
        from ocsl_review import estimands
        generator = random.Random(7)
        units = []
        for index in range(300):
            intensity = generator.lognormvariate(0, 1.2)
            exposure_before, exposure_after = generator.randint(5, 30), generator.randint(5, 30)
            before = sum(1 for _ in range(exposure_before) if generator.random() < min(1.0, 0.05 * intensity))
            after = sum(1 for _ in range(exposure_after) if generator.random() < min(1.0, 0.05 * intensity))
            units.append((f"w{index}", after, exposure_after, before, exposure_before))
        paired = followup_stats.paired_ratio_bootstrap(units, b=2000, seed=1, scope_parts=("rr",))
        unpaired = estimands.ratio_bootstrap([(u[0], u[1], u[2]) for u in units], [(u[0], u[3], u[4]) for u in units],
                                             b=2000, seed=1, scope=("rr",))
        self.assertAlmostEqual(unpaired["estimate"], paired["estimate"])
        self.assertLess(paired["hi"] - paired["lo"], unpaired["hi"] - unpaired["lo"])
        self.assertLessEqual(paired["lo"], paired["estimate"])
        self.assertGreaterEqual(paired["hi"], paired["estimate"])
        self.assertEqual(paired, followup_stats.paired_ratio_bootstrap(units, b=2000, seed=1, scope_parts=("rr",)))
        self.assertIsNone(followup_stats.paired_ratio_bootstrap([], b=10, seed=1, scope_parts=())["estimate"])
        self.assertIsNone(followup_stats.paired_ratio_bootstrap([("w", 1, 2, 0, 3)], b=10, seed=1,
                                                                scope_parts=())["estimate"])

    def test_quantiles_interpolate_linearly(self):
        self.assertEqual(2.5, followup_stats.quantile([1.0, 2.0, 3.0, 4.0], 0.5))
        self.assertEqual(1.75, followup_stats.quantile([1.0, 2.0, 3.0, 4.0], 0.25))
        self.assertIsNone(followup_stats.quantile([], 0.5))
        result = followup_stats.cluster_quantile_bootstrap([[[1.0, 9.0], [3.0]], [[5.0]]], [0.5], b=50, seed=3,
                                                           scope_parts=("q",))
        self.assertEqual([4.0], result["estimates"])
        self.assertTrue(all(not math.isnan(value) for value in result["bounds"][0]))


if __name__ == "__main__":
    unittest.main()
