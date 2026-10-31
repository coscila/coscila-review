# SPDX-License-Identifier: Apache-2.0
"""Second-generation propagation: seeded edge sample, public full-text acquisition, BM25 candidates, claim-match@1.

No test reaches the network: provider responses come from tests/fixtures/followup/secondgen and the HTTPS opener of the
scholarly transport is replaced by a failing stub. Cited-by edges and dependency rows, which the evidence commands
write in a study, are inserted directly.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_net, followup_secondgen, followup_strata
from ocsl_review.claim_check import ConfidentialPolicy
from ocsl_review.errors import ValidationError
from ocsl_review.followup_branch import read_artifact
from ocsl_review.followup_model import CitedByEdge
from test_followup_strata import A1, A2, B1, frozen_branch, lid

JA, JB = F.JOURNAL_A, F.JOURNAL_B
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ROUTES = {
    **{f"{EPMC}/search?query=DOI%3A%2210.5555%2Ffu.later.{n}%22": (200, f"secondgen/europe-pmc-later-{n}.json")
       for n in (1, 2, 3, 4)},
    f"{EPMC}/PMC9100001/fullTextXML": (200, "secondgen/later-article-1.xml"),
    f"{EPMC}/PMC9100003/fullTextXML": (200, "secondgen/later-article-3.xml"),
    f"{EPMC}/PMC9100004/fullTextXML": (503, b""),
}
RESTATING = "Marker zeta is reported to double the risk in adults ⟨b1⟩ ."
OTHER = "The same review also summarised registry coverage in adults ⟨b1⟩ ."
VERDICTS = {
    RESTATING: {"match": "same-claim", "reliance": "cites-review-for-claim",
                "quote": "Marker zeta is reported to double the risk in adults"},
    OTHER: {"match": "different-claim", "reliance": "cites-review-otherwise", "quote": "summarised registry coverage"},
}


def answer(messages, settings):
    return json.dumps({"items": [{"id": item["id"], **VERDICTS[item["candidateSentence"]], "rationale": "Compared.",
                                  "abstainReason": ""} for item in F.items_of(messages)]})


def no_network(*args, **kwargs):
    raise AssertionError("a follow-up test tried to open a network connection")


def second_generation_protocol(**spec):
    protocol = F.protocol()
    protocol["secondGeneration"] = {**protocol["secondGeneration"], **spec}
    return protocol


def seed_edges_and_dependency(branch):
    """Cited-by edges of reviews a1 and a2 and dependency rows (as the evidence commands write them)."""
    with branch.writing() as connection:
        for target, number in [(A1, n) for n in (1, 2, 3, 4)] + [(A2, 1)]:
            edge = CitedByEdge(target, f"doi:10.5555/fu.later.{number}", "europe-pmc", "request-fixture",
                               "2026-01-01T00:00:00Z", citing_doi=f"10.5555/fu.later.{number}",
                               citing_pmid=f"920000{number}", citing_title=f"A later synthetic article {number}",
                               citing_year=2024)
            connection.execute(
                "INSERT INTO fu_citedby_edges(edge_id, target_doi, target_journal, citing_key, citing_doi, citing_pmid, "
                "citing_pmcid, citing_openalex, citing_title, citing_year, citing_first_online, source, request_id, "
                "retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (edge.edge_id, target, JA, edge.citing_key, edge.citing_doi, edge.citing_pmid, None, None,
                 edge.citing_title, edge.citing_year, None, edge.source, edge.request_id, edge.retrieved_at))
        openalex = CitedByEdge(A1, "doi:10.5555/fu.later.1", "openalex", "request-fixture", "2026-01-01T00:00:00Z")
        connection.execute("INSERT INTO fu_citedby_edges(edge_id, target_doi, citing_key, source, request_id, "
                           "retrieved_at) VALUES(?,?,?,?,?,?)", (openalex.edge_id, A1, openalex.citing_key, "openalex",
                                                                 "request-fixture", "2026-01-01T00:00:00Z"))
        for (journal, doi, label), dependency in {(JA, A1, "1"): "essential", (JA, A2, "2"): "sole",
                                                  (JB, B1, "2"): "redundant", (JA, A2, "1"): "undetermined",
                                                  (JB, B1, "1"): "sole"}.items():
            unit = lid(journal, doi, label)
            claim = connection.execute("SELECT claim_id FROM fu_frame WHERE locus_id=?", (unit,)).fetchone()[0]
            connection.execute("INSERT INTO fu_dependency(locus_id, claim_id, cocited, cocited_supporting, "
                               "cocited_unassessable, class, anchor_verdict, rule_version, detail_json) "
                               "VALUES(?,?,0,0,0,?,NULL,'dependency-aggregation@1','{}')", (unit, claim, dependency))


class SecondGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        (cls.root / "census").mkdir()
        cls.census = F.make_parent(cls.root / "census")
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        patcher = mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.transport = F.FixtureTransport(ROUTES)
        self.client = followup_net.client(transport=self.transport, spacing={"www.ebi.ac.uk": 0.0})

    def branch(self, protocol=None):
        type(self).count += 1
        branch = frozen_branch(self.root, f"secondgen-{self.count}.sqlite3",
                               protocol or second_generation_protocol(edges=4), census=self.census)
        seed_edges_and_dependency(branch)
        return branch

    def run_stages(self, branch, stages=followup_secondgen.STAGES, judge=None, **options):
        return followup_secondgen.run_second_gen(branch, stages=stages, client=self.client,
                                                 judge_=judge or F.ScriptedJudge(answer),
                                                 policy=ConfidentialPolicy("public-literature"), model=F.MODEL,
                                                 **options)

    def table(self, branch, name, order="1"):
        with closing(branch.connect()) as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY {order}")]

    def test_the_edge_sample_is_seeded_and_carries_inclusion_probabilities(self):
        branch = self.branch(second_generation_protocol(edges=2))
        result = self.run_stages(branch, ["sample"])
        rows = self.table(branch, "fu_secondgen_sample", "anchor_locus_id, citing_key")
        anchor = lid(JA, A1, "1")
        mine = [row for row in rows if row["anchor_locus_id"] == anchor]
        self.assertEqual((2, 4, 0.5), (len(mine), mine[0]["available"], mine[0]["inclusion_probability"]))
        keys = [f"doi:10.5555/fu.later.{n}" for n in (1, 2, 3, 4)]
        expected = followup_strata.seeded_sample(F.PROTOCOL["seed"], ("second-generation", anchor), keys, 2)
        self.assertEqual(sorted(expected), [row["citing_key"] for row in mine])
        self.assertEqual({lid(JA, A1, "1"), lid(JA, A2, "2")}, {row["anchor_locus_id"] for row in rows})
        self.assertEqual((3, 1), (result["sample"]["anchors"], result["sample"]["anchorsWithoutEdges"]))

        def selection(values):
            return [(row["anchor_locus_id"], row["edge_id"], row["citing_key"], row["inclusion_probability"])
                    for row in values]
        self.run_stages(branch, ["sample"])
        self.assertEqual(selection(rows), selection(self.table(branch, "fu_secondgen_sample",
                                                               "anchor_locus_id, citing_key")))
        edge_ids = {row["edge_id"] for row in rows}
        with closing(branch.connect()) as connection:
            sources = {row[0] for row in connection.execute(
                f"SELECT source FROM fu_citedby_edges WHERE edge_id IN ({','.join('?' * len(edge_ids))})", list(edge_ids))}
        self.assertEqual({"europe-pmc"}, sources)

    def test_acquisition_records_every_outcome_without_a_contact_identity(self):
        branch = self.branch()
        result = self.run_stages(branch, ["sample", "acquire"])
        documents = {row["citing_key"]: row for row in self.table(branch, "fu_secondgen_documents")}
        self.assertEqual({"doi:10.5555/fu.later.1": "obtained", "doi:10.5555/fu.later.2": "no-open-full-text",
                          "doi:10.5555/fu.later.3": "obtained", "doi:10.5555/fu.later.4": "fetch-failed"},
                         {key: row["outcome"] for key, row in documents.items()})
        failed = json.loads(documents["doi:10.5555/fu.later.4"]["detail_json"])
        self.assertEqual(("full-text", "http-503", "PMC9100004"), (failed["stage"], failed["failure"], failed["pmcid"]))
        obtained = documents["doi:10.5555/fu.later.1"]
        self.assertEqual(("europe-pmc", "public"), (obtained["provider"], obtained["provenance_class"]))
        with closing(branch.connect()) as connection:
            body = read_artifact(connection, obtained["source_sha256"])
            self.assertEqual((F.FIXTURES / "secondgen" / "later-article-1.xml").read_bytes(), body)
            requests = [dict(row) for row in connection.execute("SELECT * FROM fu_requests ORDER BY retrieved_at")]
        self.assertEqual(7, len(requests))
        self.assertIn(("unavailable", "http-503", 503), {(r["outcome"], r["error"], r["http_status"]) for r in requests})
        for request in requests + [dict(row) for row in self.transport.requests]:
            self.assertNotIn("@", json.dumps(request))
            self.assertNotIn("mailto", json.dumps(request).lower())
        self.assertEqual({"fetch-failed": 1, "no-open-full-text": 1, "obtained": 2}, result["acquire"]["outcomes"])
        again = self.run_stages(branch, ["acquire"])
        self.assertEqual(1, again["acquire"]["fetched"])

    def test_candidates_rank_the_restating_sentence_first_and_judging_writes_matches(self):
        branch = self.branch()
        result = self.run_stages(branch)
        anchor = lid(JA, A1, "1")
        sentences = self.table(branch, "fu_secondgen_sentences", "citing_key, ordinal")
        self.assertEqual([(RESTATING, "⟨b1⟩", "Introduction", 1), (OTHER, "⟨b1⟩", "Introduction", 2)],
                         [(row["text"], row["marker"], row["section"], row["ordinal"]) for row in sentences])
        self.assertEqual({A1}, {row["target_doi"] for row in sentences})
        matches = {row["sentence_id"]: row for row in self.table(branch, "fu_secondgen_matches")
                   if row["anchor_locus_id"] == anchor}
        by_text = {row["text"]: matches[row["sentence_id"]] for row in sentences}
        first, second = by_text[RESTATING], by_text[OTHER]
        self.assertEqual((1, 2), (first["rank"], second["rank"]))
        self.assertGreater(first["score"], second["score"])
        self.assertEqual((1, "same-claim", "cites-review-for-claim", 1.0),
                         (first["assessable"], first["match"], first["reliance"], first["inclusion_probability"]))
        self.assertEqual(("different-claim", "cites-review-otherwise"), (second["match"], second["reliance"]))
        self.assertIsNotNone(first["judgment_id"])
        searches = {row["citing_key"]: row for row in self.table(branch, "fu_secondgen_candidates")
                    if row["anchor_locus_id"] == anchor}
        self.assertEqual({"doi:10.5555/fu.later.1": ("ranked", None), "doi:10.5555/fu.later.2": ("no-sentences",
                                                                                                "no-open-full-text"),
                          "doi:10.5555/fu.later.3": ("no-sentences", "citation-not-located"),
                          "doi:10.5555/fu.later.4": ("no-sentences", "fetch-failed")},
                         {key: (row["state"], row["reason"]) for key, row in searches.items()})
        with closing(branch.connect()) as connection:
            receipt = json.loads(read_artifact(connection, searches["doi:10.5555/fu.later.1"]["receipt_sha256"]))
            outcome = connection.execute("SELECT outcome FROM fu_secondgen_documents WHERE citing_key=?",
                                         ("doi:10.5555/fu.later.3",)).fetchone()[0]
        self.assertEqual([first["sentence_id"], second["sentence_id"]], [hit["unitId"] for hit in receipt["results"]])
        self.assertNotIn("r1", receipt["terms"])
        self.assertEqual(3, receipt["k"])
        self.assertEqual("citation-not-located", outcome)
        redacted = [row for row in self.table(branch, "fu_secondgen_candidates")
                    if row["anchor_locus_id"] == lid(JA, A2, "2")]
        self.assertEqual([("no-sentences", "citation-not-located")], [(row["state"], row["reason"]) for row in redacted])
        self.assertEqual((2, 2, 0), (result["judge"]["sent"], result["judge"]["judging"]["accepted"],
                                     result["judge"]["judging"]["rejected"]))
        self.assertTrue(branch.verify()["ok"], branch.verify()["problems"])

    def test_sentences_beyond_k_are_not_judged_and_never_different(self):
        branch = self.branch(second_generation_protocol(edges=4, k=1))
        judge = F.ScriptedJudge(answer)
        result = self.run_stages(branch, judge=judge)
        matches = {row["sentence_id"]: row for row in self.table(branch, "fu_secondgen_matches")}
        sentences = {row["text"]: row["sentence_id"] for row in self.table(branch, "fu_secondgen_sentences")}
        beyond = matches[sentences[OTHER]]
        self.assertEqual((None, None, 0, "not-judged", None), (beyond["rank"], beyond["match"], beyond["assessable"],
                                                               beyond["reason"], beyond["score"]))
        self.assertEqual(1, matches[sentences[RESTATING]]["rank"])
        self.assertEqual([RESTATING], [item["candidateSentence"] for messages, _ in judge.requests
                                       for item in F.items_of(messages)])
        self.assertEqual(1, result["candidates"]["notJudgedBeyondK"])

    def test_items_carry_only_the_declared_fields_and_routing(self):
        branch = self.branch()
        judge = F.ScriptedJudge(answer)
        self.run_stages(branch, judge=judge)
        (messages, _), = judge.requests
        for item in F.items_of(messages):
            self.assertEqual({"id", "originalClaim", "originalReview", "candidateSentence", "candidateMarker",
                              "candidateSection"}, set(item))
            self.assertEqual("Marker zeta doubles the risk in adults ⟨R1⟩ ⟨R2⟩ .", item["originalClaim"])
            self.assertEqual("Synthetic review 1", item["originalReview"])
            self.assertTrue(item["id"].startswith(lid(JA, A1, "1") + "|sentence-"))
        external = F.ScriptedJudge(answer, location="external")
        public = self.run_stages(self.branch(), judge=external, public_only=True)
        self.assertEqual(2, public["judge"]["sent"])

    def test_rejections_replicates_reruns_and_the_pinned_model(self):
        branch = self.branch()

        def unquoted(messages, settings):
            return json.dumps({"items": [{"id": item["id"], **VERDICTS[item["candidateSentence"]], "quote": "⟨b1⟩",
                                          "rationale": "Compared.", "abstainReason": ""}
                                         for item in F.items_of(messages)]})
        first = self.run_stages(branch, judge=F.ScriptedJudge(unquoted))
        self.assertEqual(0, first["judge"]["judging"]["accepted"])
        reasons = {row["reason"] for row in self.table(branch, "fu_secondgen_matches") if row["rank"]}
        self.assertEqual({"invalid-output"}, reasons)
        again = self.run_stages(branch, ["candidates", "judge"])
        self.assertEqual(2, again["judge"]["judging"]["accepted"])
        replicate = self.run_stages(branch, ["judge"], variant="replicate")
        self.assertEqual(1, replicate["judge"]["sent"])
        self.assertIsNone(replicate["judge"]["verdictsApplied"])
        refreshed = self.run_stages(branch, ["candidates"])
        self.assertEqual(2, refreshed["candidates"]["verdictsApplied"])
        with self.assertRaisesRegex(ValidationError, "model.served"):
            followup_secondgen.run_second_gen(branch, stages=["judge"], judge_=F.ScriptedJudge(answer), model="other")
        with self.assertRaisesRegex(ValidationError, "unknown second-gen stages"):
            followup_secondgen.run_second_gen(branch, stages=["fetch"])

    def test_sampling_needs_dependency_rows_and_a_protocol_block(self):
        bare = frozen_branch(self.root, "secondgen-bare.sqlite3", second_generation_protocol(edges=4),
                             census=self.census)
        with self.assertRaisesRegex(ValidationError, "dependency"):
            followup_secondgen.run_second_gen(bare, stages=["sample"])
        protocol = F.protocol()
        del protocol["secondGeneration"]
        without = frozen_branch(self.root, "secondgen-none.sqlite3", protocol, census=self.census)
        with self.assertRaisesRegex(ValidationError, "second-generation"):
            followup_secondgen.run_second_gen(without, stages=["sample"])

    def test_the_bundle_is_active_with_its_validator_pinned(self):
        from ocsl_review import claim_match, instruments
        instrument = instruments.load("claim-match@1")
        self.assertEqual("active", instrument.manifest["status"])
        self.assertEqual(instrument.module_digests()["validator"], instrument.manifest["validator"]["sha256"])
        self.assertTrue(instruments.verify(instrument)["ok"])
        self.assertTrue(claim_match.propagates({"match": "same-claim", "reliance": "cites-review-for-claim"}))
        self.assertFalse(claim_match.propagates({"match": "same-claim", "reliance": "unclear"}))
        self.assertEqual({"match": None, "reliance": None, "assessable": 0, "reason": "abstained", "quote": None},
                         claim_match.row({"match": "abstain", "reliance": "unclear", "quote": "", "abstainReason": "x"}))

    def test_the_command_line_runs_every_stage(self):
        branch = self.branch()
        from ocsl_review.cli import main
        target = self.root / "second-gen-result.json"
        judge = F.ScriptedJudge(answer)
        with mock.patch("ocsl_review.followup_net.client", return_value=self.client), \
                mock.patch("ocsl_review.followup_secondgen.judge_from_args",
                           return_value=(judge, ConfidentialPolicy("public-literature"))):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["--result-file", str(target), "followup", "second-gen", "--database", str(branch.path),
                             "--model", F.MODEL, "--base-url", "http://judge.example/v1", "--scope", "pilot"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual(["sample", "acquire", "candidates", "judge"], record["result"]["stages"])
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "second-gen", "--database", str(branch.path),
                         "--stage", "sample"])
        self.assertEqual(0, code, target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
