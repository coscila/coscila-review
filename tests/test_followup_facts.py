# SPDX-License-Identifier: Apache-2.0
"""Facts v1 of a follow-up branch: a closed facts.sqlite3 without entitled text, one scalar SQL per fact, the census
denominators and flags materialized, fact inputs, and a postflight through corpus_facts.verify_export.

``populated_branch`` runs the evidence commands offline on the synthetic census (status, function, support at both
tiers, dependency, cited-by) and writes the rows the analysis track owns (strata sizes, a numeric row, a confirmed
second-generation restatement, estimates and coverage) directly.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_citedby, followup_dependency, followup_function, followup_net, followup_status
from ocsl_review import followup_support
from ocsl_review.cli import main
from ocsl_review.corpus_facts import verify_export
from ocsl_review.errors import ValidationError
from ocsl_review.followup_branch import FollowupBranch
from ocsl_review.followup_facts import export_facts

from test_followup_citedby import routes as citedby_routes
from test_followup_dependency import insert_frame, locus
from test_followup_status import RW_CSV, clean_environment, no_network, status_routes
from test_followup_support import quoting, routes as support_routes

ENTITLED_SENTENCES = ("The trial reported a large benefit", "A retracted cohort study had suggested")


def function_answer(messages, settings):
    outputs = []
    for item in F.items_of(messages):
        sentence = item["citingSentence"]
        klass = "notes-retraction" if sentence.startswith("A retracted cohort study") else "relies"
        outputs.append({"id": item["id"], "function": klass, "quote": sentence.split(" ⟨")[0],
                        "quoteField": "citingSentence", "rationale": "As the sentence states.", "abstainReason": ""})
    return json.dumps({"items": outputs})


def client(transport):
    return followup_net.client(transport=transport, sleep=lambda seconds: None)


def populated_branch(root: Path, census: dict, name: str) -> FollowupBranch:
    path = root / f"{name}.sqlite3"
    FollowupBranch.create(path, census["parent"], parent_text=census["text"], protocol=F.protocol())
    branch = FollowupBranch(path)
    with branch.writing() as connection:
        followup_status.import_retraction_watch(connection, RW_CSV, "synthetic-snapshot", protocol=F.protocol())
        followup_status.fetch_status(connection, client(F.FixtureTransport(status_routes())),
                                     sources=["crossref", "pubmed", "europe-pmc"])
        followup_status.resolve_status(connection, protocol=F.protocol())
        insert_frame(connection, pilot=("a1/1",))
    judge = F.ScriptedJudge(function_answer)
    with branch.writing() as connection:
        followup_function.run_function(connection, branch, judge, None, model=F.MODEL)
    for tier, verdict in (("abstract", "direct-support"), ("full-text", "contradicted")):
        with branch.writing() as connection:
            followup_support.run_support(connection, branch, F.ScriptedJudge(quoting(verdict)), None, tier=tier,
                                         model=F.MODEL, scholarly=client(F.FixtureTransport(support_routes())),
                                         europe_pmc=True)
    with branch.writing() as connection:
        connection.execute("INSERT OR REPLACE INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                           "VALUES(?, 'full-text', 'contradicted', 1, NULL)", (locus("a1/2"),))
        followup_dependency.compute_dependency(connection)
        followup_citedby.traverse(connection, client(F.FixtureTransport(citedby_routes())),
                                  sources=["europe-pmc", "openalex"])
        for stratum, journal, eligible, selected in (("S1", F.JOURNAL_A, 3, 3), ("S1", F.JOURNAL_B, 2, 2),
                                                     ("S0", "*", 2, 2)):
            connection.execute("INSERT INTO fu_stratum_sizes VALUES(?,?,?,?,0)", (stratum, journal, eligible, selected))
        connection.execute("INSERT INTO fu_numeric(locus_id, epidemiological, condition, metric, geography, year, "
                           "value, value_text, unit, quote, table_source, class, assessable, reason, rule_version) "
                           "VALUES(?, 1, 'disease', 'deaths', 'Exampleland', 2019, 12500, '12.5 thousand', 'count', "
                           "'The disease caused', 'tables@1', 'consistent', 1, NULL, 'numeric-comparison@1')",
                           (locus("a1/3"),))
        connection.execute("INSERT INTO fu_secondgen_matches VALUES(?, 'sentence-1', 'doi:10.5555/fu.later.1', 1, "
                           "2.5, 'same-claim', 'cites-review-for-claim', 1, NULL, 'repeats the association', NULL, "
                           "1.0)", (locus("a2/2"),))
        connection.execute("INSERT INTO fu_estimates VALUES('compromised-share.journal-a.all.retracted.share', 0.25, "
                           "'compromised-share', ?, NULL, 'S1', 2.0, 8.0, 8, ?)",
                           (F.JOURNAL_A, json.dumps({"b": 200, "seed": 13, "interval": "percentile"})))
        connection.execute("INSERT INTO fu_coverage VALUES('sole-or-essential', ?, '*', 'S1', 'unassessable', "
                           "'fetch-failed', 1, 1.0)", (F.JOURNAL_B,))
    return branch


class FactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        with mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network), \
                mock.patch.dict(os.environ, clean_environment(), clear=True):
            cls.branch = populated_branch(cls.root, cls.census, "populated")
        cls.output = cls.root / "facts"
        cls.result = export_facts(cls.branch.path, cls.output, code_revision="synthetic-revision")
        cls.facts = json.loads((cls.output / "facts.json").read_text(encoding="utf-8"))["facts"]

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def value(self, key):
        return self.facts[key]["value"]

    def test_the_export_verifies_and_every_fact_is_one_scalar_query(self):
        report = verify_export(self.output / "facts.json")
        self.assertEqual((True, len(self.facts)), (report["ok"], report["facts"]))
        self.assertGreater(len(self.facts), 100)
        database = self.output / "facts.sqlite3"
        self.assertFalse(stat.S_IMODE(database.stat().st_mode) & 0o222)
        self.assertFalse(Path(str(database) + "-wal").exists())
        for fact in self.facts.values():
            self.assertEqual(("facts.sqlite3", self.result["databaseSha256"]),
                             (fact["source"]["path"], fact["source"]["sha256"]))
            self.assertEqual("synthetic-revision", fact["codeRevision"])
        with self.assertRaisesRegex(ValidationError, "already exists"):
            export_facts(self.branch.path, self.output)

    def test_status_census_and_strata_facts(self):
        self.assertEqual((7, 1, 1, 1, 0), (self.value("followup.status.works"), self.value("followup.status.retracted"),
                                           self.value("followup.status.expression-of-concern"),
                                           self.value("followup.status.corrected"),
                                           self.value("followup.status.cited-works-without-status-row")))
        journal_a = "journal-of-synthetic-reviews-a"
        self.assertEqual((7, 2, 1), (self.value(f"followup.census.{journal_a}.loci"),
                                     self.value(f"followup.census.{journal_a}.retracted.loci"),
                                     self.value(f"followup.census.{journal_a}.2023.expression-of-concern.loci")))
        self.assertEqual(4, self.value("followup.census.journal-of-synthetic-reviews-b.loci"))
        self.assertEqual(7, self.value("followup.status.census-flag.unflagged.none")
                         + self.value("followup.status.census-flag.unflagged.retracted")
                         + self.value("followup.status.census-flag.unflagged.expression-of-concern")
                         + self.value("followup.status.census-flag.unflagged.corrected")
                         + self.facts.get("followup.status.census-flag.unflagged.unknown", {"value": 0})["value"])
        self.assertEqual((5, 1, 3), (self.value("followup.strata.s1.units"), self.value("followup.strata.s1.pilot"),
                                     self.value(f"followup.strata.s1.{journal_a}.selected")))

    def test_evidence_and_judging_facts(self):
        self.assertEqual(9, self.value("followup.support.abstract.units"))
        # Full text was obtained for the retracted work only: its three S1 loci, plus one co-cited row set by hand.
        self.assertEqual((3, 4, 1), (self.value("followup.support.full-text.s1.verdict.contradicted"),
                                     self.value("followup.support.full-text.verdict.contradicted"),
                                     self.value("followup.support.full-text.reason.fetch-failed")))
        self.assertEqual(1, self.value("followup.function.class.notes-retraction"))
        self.assertEqual(6, self.value("followup.function.class.relies"))
        self.assertEqual(self.value("followup.dependency.anchors"),
                         sum(self.value(f"followup.dependency.class.{klass}") for klass in
                             ("sole", "essential", "redundant", "undetermined")))
        self.assertEqual(1, self.value("followup.numeric.class.consistent"))
        self.assertEqual((1, 4), (self.value("followup.secondgen.confirmed"), self.value("followup.citedby.citing-works")))
        self.assertEqual(self.value("followup.calls.total"), self.value("followup.calls.with-fingerprint"))
        self.assertEqual(0, self.value("followup.calls.entitled-to-external"))
        self.assertEqual(7, self.value("followup.calls.citation-function-1.locus.primary.accepted-items"))
        self.assertIn("followup.requests.crossref.ok", self.facts)

    def test_estimates_and_coverage_of_the_analysis_track(self):
        estimate = self.facts["followup.estimates.compromised-share.journal-a.all.retracted.share"]
        self.assertEqual((0.25, "compromised-share", 13), (estimate["value"], estimate["method"],
                                                           estimate["derivation"]["seed"]))
        key = ("followup.coverage.sole-or-essential.journal-of-synthetic-reviews-b.all.s1.unassessable."
               "fetch-failed.units")
        self.assertEqual(1, self.value(key))

    def test_no_text_of_a_review_or_a_cited_work_reaches_the_facts(self):
        raw = (self.output / "facts.sqlite3").read_bytes()
        bundle = (self.output / "facts.json").read_text(encoding="utf-8")
        for text in ENTITLED_SENTENCES + ("Marker zeta doubled the risk in adults", "repeats the association",
                                          "Survival curves"):
            with self.subTest(text=text):
                self.assertFalse(text.encode("utf-8") in raw, "the text is in facts.sqlite3")
                self.assertFalse(text in bundle, "the text is in facts.json")
        with closing(sqlite3.connect((self.output / "facts.sqlite3").resolve().as_uri() + "?mode=ro&immutable=1",
                                     uri=True)) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM fu_artifacts").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM fu_judgments WHERE verdict_json IS NOT NULL"
                                                   ).fetchone()[0])
            inputs = dict(connection.execute("SELECT key, value FROM fact_inputs").fetchall())
        self.assertEqual(self.branch.meta()["parentSha256"], inputs["parentSha256"])
        self.assertEqual(("200", "13", "11"), (inputs["bootstrapB"], inputs["bootstrapSeed"], inputs["selectionSeed"]))
        self.assertIn("citation-function@1", json.loads(inputs["instrumentDigests"]))

    def test_the_commands_export_and_verify(self):
        target = self.root / "cli-facts"
        record_path = self.root / "result-facts.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(record_path), "followup", "facts", "--database", str(self.branch.path),
                         "--output", str(target)])
        self.assertEqual(0, code, record_path.read_text())
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(record_path), "followup", "verify-facts", "--facts", str(target)])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual((0, True), (code, record["result"]["ok"]))


if __name__ == "__main__":
    unittest.main()
