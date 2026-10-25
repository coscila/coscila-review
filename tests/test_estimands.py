# SPDX-License-Identifier: Apache-2.0
"""Estimands with known answers: intervals, ICC, design effect, bootstrap, ratio, trend, screening, database queries."""
from __future__ import annotations

import math
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ocsl_review import estimands as E
from ocsl_review import scores_db


class IntervalTests(unittest.TestCase):
    def test_wilson_known_values(self):
        # 50/100: centre 0.5, half-width 1.96 * sqrt(0.0025 + 3.8416/40000) / 1.038416 (40-digit decimal arithmetic).
        lo, hi = E.wilson(0.5, 100)
        self.assertAlmostEqual(0.40382982859014715, lo, places=12)
        self.assertAlmostEqual(0.59617017140985285, hi, places=12)
        # 0/10: the upper bound is z^2 / (n + z^2) = 3.8416 / 13.8416.
        lo, hi = E.wilson(0.0, 10)
        self.assertAlmostEqual(0.0, lo, places=12)
        self.assertAlmostEqual(3.8416 / 13.8416, hi, places=12)
        self.assertEqual((0.0, 0.0), E.wilson(0.3, 0))

    def test_icc_known_values(self):
        self.assertAlmostEqual(1.0, E.icc_anova([(4, 4), (0, 4)]))
        self.assertAlmostEqual(-1 / 3, E.icc_anova([(2, 4), (2, 4)]))
        # (1,2), (3,4), (0,3): MSB = 35/72, MSW = 5/24, n0 = 26/9, so ICC = 6/19.
        self.assertAlmostEqual(6 / 19, E.icc_anova([(1, 2), (3, 4), (0, 3)]))
        self.assertEqual(0.0, E.icc_anova([(1, 3)]))
        self.assertEqual(0.0, E.icc_anova([(0, 0), (1, 1)]))

    def test_design_effect_and_wilson_deff(self):
        self.assertAlmostEqual(1.9, E.design_effect(10, 0.1))
        self.assertEqual(1.0, E.design_effect(10, -0.2))
        result = E.wilson_deff(50, 200, 0.1, 10)
        self.assertAlmostEqual(0.25, result["share"])
        self.assertAlmostEqual(1.9, result["deff"])
        self.assertAlmostEqual(200 / 1.9, result["nEffective"])
        # Wilson(0.25, 200/1.9) from 40-digit decimal arithmetic.
        self.assertAlmostEqual(0.17707497260452083, result["lo"], places=12)
        self.assertAlmostEqual(0.34053012580789825, result["hi"], places=12)
        self.assertIsNone(E.wilson_deff(0, 0, 0.1, 1)["share"])

    def test_icc_recovers_a_simulated_value(self):
        rng = random.Random(11)
        units = []
        for _ in range(400):
            p = min(0.99, max(0.01, rng.betavariate(2, 8)))
            units.append((sum(rng.random() < p for _ in range(60)), 60))
        # Beta(2, 8) has ICC 1 / (2 + 8 + 1).
        self.assertAlmostEqual(1 / 11, E.icc_anova(units), delta=0.03)


class BootstrapTests(unittest.TestCase):
    def test_identical_reviews_give_a_degenerate_interval(self):
        result = E.clustered_bootstrap([(i, 2, 10) for i in range(30)], b=500, seed=3)
        self.assertEqual((0.2, 0.2, 0.2), (result["estimate"], result["lo"], result["hi"]))

    def test_two_reviews_have_an_enumerable_distribution(self):
        # Resampling two reviews gives 0, 0.5 or 1 with probabilities 1/4, 1/2, 1/4.
        result = E.clustered_bootstrap([("a", 0, 10), ("b", 10, 10)], b=10_000, seed=5)
        self.assertEqual((0.5, 0.0, 1.0), (result["estimate"], result["lo"], result["hi"]))
        self.assertAlmostEqual(math.sqrt(0.125), result["se"], delta=0.01)

    def test_seed_reproducibility_and_scope(self):
        units = [(i, i % 4, 8) for i in range(40)]
        first = E.clustered_bootstrap(units, b=300, seed=9, scope=("headline", "x"))
        self.assertEqual(first, E.clustered_bootstrap(units, b=300, seed=9, scope=("headline", "x")))
        self.assertNotEqual(first["se"], E.clustered_bootstrap(units, b=300, seed=9, scope=("panel", "x"))["se"])

    def test_bootstrap_se_matches_the_design_effect(self):
        rng = random.Random(21)
        units = []
        for i in range(300):
            p = min(0.95, max(0.02, rng.betavariate(3, 12)))
            n = 20
            units.append((i, sum(rng.random() < p for _ in range(n)), n))
        k, n = sum(u[1] for u in units), sum(u[2] for u in units)
        share = k / n
        icc = E.icc_anova((u[1], u[2]) for u in units)
        analytic = math.sqrt(share * (1 - share) / n * E.design_effect(n / len(units), icc))
        result = E.clustered_bootstrap(units, b=2000, seed=1)
        self.assertAlmostEqual(analytic, result["se"], delta=0.15 * analytic)

    def test_empty_inputs(self):
        self.assertIsNone(E.clustered_bootstrap([], b=10, seed=1)["lo"])
        self.assertEqual((None, None), E.percentile_bounds([]))

    def test_ratio_bootstrap(self):
        same = [(i, 2, 10) for i in range(20)]
        result = E.ratio_bootstrap(same, same, b=500, seed=2)
        self.assertEqual((1.0, False), (result["estimate"], result["excludesOne"]))
        high = [(i, 4, 10) for i in range(30)] + [(i + 30, 5, 10) for i in range(30)]
        low = [(i, 1, 10) for i in range(30)] + [(i + 30, 2, 10) for i in range(30)]
        result = E.ratio_bootstrap(high, low, b=1000, seed=2)
        self.assertAlmostEqual(3.0, result["estimate"])
        self.assertTrue(result["excludesOne"])
        self.assertLess(result["lo"], 3.0)
        self.assertGreater(result["hi"], 3.0)

    def test_year_trend(self):
        by_year = {2020: [(f"a{i}", 1, 10) for i in range(5)], 2021: [(f"b{i}", 2, 10) for i in range(5)],
                   2022: [(f"c{i}", 3, 10) for i in range(5)]}
        result = E.year_trend(by_year, b=200, seed=4)
        self.assertAlmostEqual(0.1, result["slope"])
        self.assertAlmostEqual(0.1, result["lo"])
        self.assertAlmostEqual(0.1, result["hi"])
        self.assertFalse(result["coversZero"])
        flat = {2020: [("a", 1, 10), ("b", 3, 10)], 2021: [("c", 1, 10), ("d", 3, 10)]}
        result = E.year_trend(flat, b=500, seed=4)
        self.assertAlmostEqual(0.0, result["slope"])
        self.assertTrue(result["coversZero"])
        self.assertIsNone(E.year_trend({2020: [("a", 1, 4)]}, b=10, seed=1)["slope"])
        self.assertIsNone(E.ols_slope([(1, 1)]))


class ScreeningTests(unittest.TestCase):
    def test_benjamini_hochberg(self):
        self.assertEqual([0.02, 0.04, 0.04, 0.02], [round(q, 12) for q in E.benjamini_hochberg([0.01, 0.04, 0.03, 0.005])])
        self.assertEqual([None, 0.5], E.benjamini_hochberg([None, 0.5]))

    def test_upper_tails(self):
        self.assertAlmostEqual(0.5, E.upper_tail(2, 3, 0.5, 0.0))
        self.assertAlmostEqual(1 / 8, E.upper_tail(3, 3, 0.5, 0.0))
        # mean 1/2 and rho 1/3 is Beta(1, 1): every count 0..n has probability 1/(n+1).
        self.assertAlmostEqual(0.5, E.upper_tail(2, 3, 0.5, 1 / 3))
        self.assertAlmostEqual(0.25, E.upper_tail(3, 3, 0.5, 1 / 3))
        self.assertAlmostEqual(0.75, E.upper_tail(1, 3, 0.5, 1 / 3))
        self.assertEqual((1.0, 0.0), (E.upper_tail(0, 3, 0.4, 0.1), E.upper_tail(4, 3, 0.4, 0.1)))
        self.assertEqual(0.4, E.upper_tail(2, 3, 0.4, 1.0))
        rows = E.beta_binomial_screen([("a", 3, 3), ("b", 0, 3), ("c", 3, 3), ("d", 0, 3)])
        self.assertEqual([0.5, 1.0, 0.5, 1.0], [row[5] for row in rows])

    def test_screen_flags_the_outlier(self):
        units = [(f"r{i}", 1 + (i % 3 == 0), 20) for i in range(100)] + [("outlier", 15, 20)]
        rows = {row[0]: row for row in E.beta_binomial_screen(units)}
        self.assertLess(rows["outlier"][6], 0.001)
        self.assertGreater(rows["r0"][6], 0.5)
        total_k, total_n = sum(u[1] for u in units), sum(u[2] for u in units)
        self.assertAlmostEqual(15 - 20 * total_k / total_n, rows["outlier"][4])
        self.assertEqual(min(row[6] for row in rows.values()), rows["outlier"][6])


def build_scores(path: Path) -> None:
    """A small closed census with hand-countable cells."""
    with closing(scores_db.connect(path, create=True)) as c:
        reviews = [("J1", "r1", 2020, "Review", "jats", "public"), ("J1", "r2", 2021, "Review", "html", "entitled"),
                   ("J1", "r3", 2021, "Mini Review", "jats", "public"), ("J2", "s1", 2020, "Review", "html", "entitled"),
                   ("J2", "s2", 2021, "Review", "html", "entitled"), ("J1", "r4", 2022, "Review", None, "unavailable")]
        for order, (journal, doi, year, stratum, route, provenance) in enumerate(reviews):
            c.execute("INSERT INTO reviews(journal, doi, year, stratum, route, provenance_class, topic, country, review_order,"
                      " document_sha256, abstract_state, full_text_state) VALUES(?,?,?,?,?,?,?,?,?,?,'completed','completed')",
                      (journal, doi, year, stratum, route, provenance, "breast", "Italy", order, None if route is None else "d"))
        rows = []

        def add(journal, doi, label, category, escalate=0, *, cited=1, identity="resolved", judged=1, position=None, cut=0,
                quote_present=None, ft=None, source="europe-pmc-core", basis="model"):
            klass = ("not_cited" if not cited else "unsure" if identity == "unresolved" or not judged else
                     "sure_supported" if category == "true" and not escalate else
                     "sure_not_supported" if category == "false" and not escalate else "unsure")
            rows.append((journal, doi, label, len(rows), cited, identity, judged, category, escalate, klass, position, cut,
                         quote_present, ft, None if ft is None else {"true": "sure_supported", "false": "sure_not_supported"}.get(ft, "unsure"),
                         source, basis))
        # J1/r1 (2020): 3 T, 1 F, 1 escalated F without full text, 1 never cited, 1 unresolved.
        for label, category, escalate in (("1", "true", 0), ("2", "true", 0), ("3", "true", 0), ("4", "false", 0), ("5", "false", 1)):
            add("J1", "r1", label, category, escalate, quote_present=1 if category == "false" else None,
                position=int(label))
        add("J1", "r1", "6", None, cited=0, judged=0, basis="deterministic:not-cited")
        add("J1", "r1", "7", "unsure", identity="unresolved", judged=0, basis="deterministic:unresolved-identity")
        # J1/r2 (2021): 2 T, 2 F (one without a quote), one N, one escalated F read in full text as T.
        add("J1", "r2", "1", "true", position=31)
        add("J1", "r2", "2", "true", position=32, cut=1)
        add("J1", "r2", "3", "false", quote_present=0, position=33, cut=1)
        add("J1", "r2", "4", "false", quote_present=1, position=34)
        add("J1", "r2", "5", "not_assessable", 1)
        add("J1", "r2", "6", "false", 1, ft="true", identity="resolved_unverified", quote_present=1)
        # J1/r3 (2021, Mini Review): 1 T, 1 F, one invalid output.
        add("J1", "r3", "1", "true")
        add("J1", "r3", "2", "false", quote_present=1, source="semantic-scholar")
        add("J1", "r3", "3", None, judged=0, basis="invalid:no-parseable-output")
        # J2: s1 (2020) 4 T, s2 (2021) 3 T and 1 F.
        for label in "1234":
            add("J2", "s1", label, "true")
        for label, category in (("1", "true"), ("2", "true"), ("3", "true"), ("4", "false")):
            add("J2", "s2", label, category, quote_present=1 if category == "false" else None)
        c.executemany(
            "INSERT INTO citations(journal, doi, label, ordinal, cited_inbody, n_loci, identity, model_judged, abs_category,"
            " abs_escalated, abs_class, abs_position, abs_cut, abs_quote_present, ft_category, ft_class, abs_source, abs_basis)"
            " VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("INSERT INTO history(journal, doi, label, tier, variant, outcome, category, created_at) VALUES"
                  "('J1','r2','2','abstract','no-truncation','accepted','false','t')")
        c.execute("INSERT INTO history(journal, doi, label, tier, variant, outcome, category, created_at) VALUES"
                  "('J1','r2','3','abstract','no-truncation','accepted','false','t')")
        scores_db.aggregate_reviews(c)
        c.commit()


class DatabaseEstimandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "scores.sqlite3"
        build_scores(cls.path)
        cls.c = sqlite3.connect(cls.path)
        cls.c.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls.temp.cleanup()

    def test_denominator_2_and_headline(self):
        units = E.denominator_units(self.c, "J1")
        # r1: T T T F F(esc); r2: T T F F F(esc, unverified identity); r3: T F. N, unresolved, invalid and never cited are out.
        self.assertEqual([("r1", 2, 5), ("r2", 3, 5), ("r3", 1, 2)], units)
        result = E.headline(self.c, "J1", b=200, seed=1)
        self.assertEqual((6, 12, 3), (result["k"], result["n"], result["reviews"]))
        self.assertAlmostEqual(0.5, result["share"])
        self.assertAlmostEqual(4.0, result["meanCluster"])
        self.assertLessEqual(result["wilsonDeff"][0], 0.5)
        self.assertGreaterEqual(result["wilsonDeff"][1], 0.5)

    def test_three_classes(self):
        classes = E.three_classes(self.c, "J1")
        self.assertEqual({"sure_supported": 6, "unsure": 5, "sure_not_supported": 4}, classes["abstract"])
        self.assertEqual(1, classes["notCited"])
        self.assertAlmostEqual(0.4, classes["abstractShareOfSure"])
        self.assertEqual({"sure_supported": 1, "unsure": 0, "sure_not_supported": 0}, classes["fullText"])
        self.assertEqual({"sure_supported": 1}, classes["composite"]["full text read"])

    def test_panels_ratio_and_trend(self):
        years = E.panel(self.c, "J1", "year", b=100, seed=1)["cells"]
        self.assertEqual({"2020": (2, 5), "2021": (4, 7)}, {y: (v["k"], v["n"]) for y, v in years.items()})
        strata = E.panel(self.c, "J1", "stratum", b=100, seed=1)["cells"]
        self.assertEqual({"Mini Review": (1, 2), "Review": (5, 10)}, {s: (v["k"], v["n"]) for s, v in strata.items()})
        ratio = E.ratio(self.c, "J1", "J2", b=300, seed=1)
        self.assertAlmostEqual(0.5 / 0.125, ratio["estimate"])
        trend = E.trend(self.c, "J2", b=100, seed=1)
        self.assertAlmostEqual(0.25, trend["slope"])
        with self.assertRaises(ValueError):
            E.panel(self.c, "J1", "planet")

    def test_sensitivity_ladder(self):
        steps = {step["step"]: (step["k"], step["n"]) for step in E.sensitivity_ladder(self.c, "J1")["steps"]}
        self.assertEqual((6, 12), steps["headline"])
        self.assertEqual((5, 11), steps["escalated-unread-F-not-assessable"])
        self.assertEqual((5, 11), steps["F-without-quote-excluded"])
        self.assertEqual((5, 11), steps["resolved-identity-only"])
        self.assertEqual((2, 5), steps["position-1-10"])
        self.assertEqual((2, 4), steps["position-31-40"])
        self.assertEqual((1, 2), steps["cut-abstracts"])
        self.assertEqual((5, 10), steps["stratum:Review"])
        self.assertEqual((1, 2), steps["stratum:Mini Review"])
        self.assertEqual((3, 5), steps["route:html"])
        self.assertEqual((3, 7), steps["route:jats"])
        self.assertEqual((5, 11), steps["europe-pmc-abstracts-only"])
        self.assertEqual((2, 2), steps["no-truncation"])

    def test_europe_pmc_ladder_counts_only_europe_pmc_abstracts(self):
        rows = self.c.execute("SELECT COUNT(*) FROM citations WHERE journal='J1' AND " + dict(
            (name, condition) for name, _, condition in E.LADDERS)["europe-pmc-abstracts-only"]).fetchone()[0]
        total = self.c.execute("SELECT COUNT(*) FROM citations WHERE journal='J1' AND abs_source LIKE 'europe-pmc-core%'").fetchone()[0]
        self.assertEqual(total, rows)
        for source in ("europe-pmc-no-abstract", "europe-pmc-no-hit", "europe-pmc-failed", "fallback:semantic-scholar"):
            self.assertEqual(0, self.c.execute("SELECT ? IN ('europe-pmc-core','europe-pmc-core-pmid')", (source,)).fetchone()[0])

    def test_no_truncation_comparison(self):
        result = E.no_truncation_comparison(self.c, "J1")
        self.assertEqual(2, result["entries"])
        self.assertEqual((0.5, 1.0, 0.5), (result["all"]["primaryShare"], result["all"]["variantShare"], result["all"]["agreement"]))

    def test_variant_agreement(self):
        result = E.variant_agreement(self.c, "J1", "no-truncation")
        self.assertEqual((2, 0.5, 2), (result["entries"], result["categoryAgreement"], result["trueFalsePairs"]))
        # Pairs (T->F) and (F->F): observed 1/2, P(F) 1/2 and 1, expected 1/2, kappa 0.
        self.assertAlmostEqual(0.0, result["trueFalseKappa"])
        self.assertEqual(0, E.variant_agreement(self.c, "J1", "replicate")["entries"])

    def test_coverage_and_decomposition(self):
        coverage = {row["cell"]: row for row in E.coverage(self.c, "J1", "year")}
        self.assertEqual((1, 1, 7, 6, 5, 1), tuple(coverage[2020][k] for k in
                                                   ("reviews", "reviews_read", "printed", "cited_inbody", "denominator_2", "escalated")))
        self.assertEqual(1, coverage[2021]["invalid"])
        self.assertEqual(1, coverage[2021]["full_text_read"])
        self.assertEqual(0, coverage[2022]["reviews_read"])
        provenance = {row["cell"]: row["reviews"] for row in E.coverage(self.c, "J1", "provenance")}
        self.assertEqual({"entitled": 1, "public": 2, "unavailable": 1}, provenance)
        decomposition = E.decompose(self.c, "J1", year=2021, dimensions=("abstract_provider", "route", "identity"))
        providers = {row["level"]: (row["k"], row["n"]) for row in decomposition["dimensions"]["abstract_provider"]}
        self.assertEqual({"europe-pmc-core": (3, 6), "semantic-scholar": (1, 1)}, providers)

    def test_report_runs_every_estimand(self):
        report = E.report(self.c, ["J1", "J2"], b=50, seed=3, ratio_pair=("J1", "J2"))
        self.assertEqual({"J1", "J2"}, set(report["journals"]))
        self.assertIn("ratio", report)
        self.assertEqual({"year", "stratum", "topic", "country"}, set(report["journals"]["J1"]["panels"]))


if __name__ == "__main__":
    unittest.main()
