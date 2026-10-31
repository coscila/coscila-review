# SPDX-License-Identifier: Apache-2.0
"""citation-support@2 runs on the frozen strata: cache-first abstracts, Europe PMC full text through the document
route, entitled routing, redacted sentences with and without a text parent, one fu_support row with a verdict or a
reason for every unit, and resumption. No test reaches the network."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_net, followup_support as SP
from ocsl_review.census_inputs import InputCaches
from ocsl_review.cli import main
from ocsl_review.corpus_acquisition import CorpusResponseCache
from ocsl_review.errors import ConfidentialityError, ReviewError
from ocsl_review.followup_branch import FollowupBranch
from ocsl_review.scholarly import RawResponse
from ocsl_review.store_client import ProviderCache

from test_followup_dependency import insert_frame, locus
from test_followup_status import clean_environment, epmc_search, no_network

FULL_TEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{}/fullTextXML"


def record(doi, pmid=None, abstract=None, pmcid=None):
    raw = {"id": pmid or "PPR1", "source": "MED" if pmid else "PPR", "doi": doi, "title": "A cited work",
           "pubYear": "2020"}
    raw.update({key: value for key, value in (("pmid", pmid), ("pmcid", pmcid), ("abstractText", abstract)) if value})
    return raw


def search_body(*records) -> bytes:
    return json.dumps({"hitCount": len(records), "resultList": {"result": list(records)}}).encode()


def jats(doi: str, pmcid: str, sentence: str) -> bytes:
    return (f'<article><front><article-meta><article-id pub-id-type="doi">{doi}</article-id>'
            f'<article-id pub-id-type="pmc">{pmcid}</article-id><title-group><article-title>A cited work'
            '</article-title></title-group><abstract><p>Abstract of the cited work.</p></abstract></article-meta>'
            f'</front><body><sec><title>Results</title><p>{sentence}</p><table-wrap><caption><p>Risks by group.</p>'
            '</caption><table><tr><th>Group</th><th>Risk</th></tr><tr><td>Adults</td><td>higher</td></tr></table>'
            '</table-wrap><fig><caption><p>Survival curves.</p></caption></fig></sec></body></article>').encode()


RETRACTED_FULL_TEXT = jats("10.5555/fu.work.retracted", "PMC900001", "Marker zeta doubled the risk in adults.")
ABSTRACTS = {
    "10.5555/fu.work.retracted": search_body(record("10.5555/fu.work.retracted", "9000001",
                                                    "Marker zeta doubled the risk in adults.", "PMC900001")),
    "10.5555/fu.work.concern": search_body(record("10.5555/fu.work.concern", "9000002",
                                                  "The trial reported a large benefit.")),
    "10.5555/fu.work.plain1": search_body(record("10.5555/fu.work.plain1", "9000004",
                                                 "Marker zeta predicts outcome in adults.", "PMC900004")),
    "10.5555/fu.work.plain3": search_body(record("10.5555/fu.work.plain3", None, "Registries cover most of the …")),
}


def routes(*, full_text=True) -> dict:
    value = {epmc_search(doi): (200, body) for doi, body in ABSTRACTS.items()}
    value[epmc_search("10.5555/fu.work.corrected")] = (503, b"{}")
    value[epmc_search("10.5555/fu.work.plain2")] = (200, search_body(record("10.5555/fu.work.plain2", "9000005",
                                                                            "Imaging was used in adults.")))
    if full_text:
        value[FULL_TEXT.format("PMC900001")] = (200, RETRACTED_FULL_TEXT)
    return value


def provider_cache(root: Path) -> Path:
    """A provider response cache holding the Europe PMC lookup of w-plain-2 and a fullTextXML of a further work."""
    path = root / "provider" / "cache.sqlite3"
    cache = CorpusResponseCache(path, transport=None)
    lookup = {"query": 'DOI:"10.5555/fu.work.plain2"', "pageSize": 5, "resultType": "core", "format": "json"}
    body = search_body(record("10.5555/fu.work.plain2", "9000005", "Cached abstract: imaging was used in adults."))
    url = SP.SEARCH
    cache.store(url, RawResponse(body, 200, {"Content-Type": "application/json"}, url), params=lookup)
    document = jats("10.5555/fu.work.cached", "PMC900099", "Cached full text sentence.")
    cache.store(FULL_TEXT.format("PMC900099"), RawResponse(document, 200, {"Content-Type": "application/xml"},
                                                           FULL_TEXT.format("PMC900099")), document=True)
    return path


def quoting(verdict="direct-support"):
    def answer(messages, settings):
        payload = json.loads(messages[1]["content"])
        return json.dumps({"items": [{"id": item["id"], "supportVerdict": verdict, "rationale": "The evidence states it.",
                                      "escalate": verdict in ("abstain", "partial-indirect"),
                                      "escalationReason": "unsettled" if verdict in ("abstain", "partial-indirect")
                                      else "",
                                      "evidence": [] if verdict == "abstain" else
                                      [{"unitId": item["evidenceUnits"][0]["id"],
                                        "quote": item["evidenceUnits"][0]["text"][:20]}]}
                                     for item in payload["items"]]})
    return answer


class SupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        cls.cache = provider_cache(cls.root)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        for patcher in (mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network),
                        mock.patch.dict(os.environ, clean_environment(), clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def branch(self, *, text=False, pilot=()):
        type(self).count += 1
        path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(path, self.census["parent"], parent_text=self.census["text"] if text else None,
                              protocol=F.protocol())
        branch = FollowupBranch(path)
        with branch.writing() as connection:
            insert_frame(connection, pilot=pilot)
        return branch

    def run_support(self, branch, judge, tier="abstract", transport=None, **options):
        transport = transport or F.FixtureTransport(routes())
        scholarly = followup_net.client(transport=transport, sleep=lambda seconds: None)
        with branch.writing() as connection:
            return SP.run_support(connection, branch, judge, None, tier=tier, model=F.MODEL, scholarly=scholarly,
                                  **options)

    def rows(self, branch, tier):
        with closing(branch.connect()) as connection:
            return {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM fu_support WHERE tier=?",
                                                                               (tier,))}

    def test_abstracts_come_from_the_caches_first_then_live(self):
        branch = self.branch()
        transport = F.FixtureTransport(routes())
        caches = InputCaches(None, ProviderCache(self.cache))
        self.addCleanup(caches.close)
        judge = F.ScriptedJudge(quoting())
        result = self.run_support(branch, judge, transport=transport, caches=caches)
        self.assertEqual(5, result["sent"])
        self.assertEqual({"provider-cache:obtained": 1, "live:obtained": 2, "live:fetch-failed": 1},
                         result["evidence"])
        searched = [request["params"]["query"] for request in transport.requests]
        self.assertNotIn('DOI:"10.5555/fu.work.plain2"', searched)
        rows = self.rows(branch, "abstract")
        self.assertEqual(9, len(rows))
        self.assertEqual(("direct-support", 1, "complete", "public"),
                         tuple(rows[locus("a1/4")][key] for key in ("verdict", "assessable", "evidence_coverage",
                                                                    "provenance_class")))
        self.assertTrue(rows[locus("a1/4")]["quote"].startswith("Cached abstract"))
        self.assertEqual("fetch-failed", rows[locus("b1/1")]["reason"])
        self.assertEqual({"text-redacted"}, {rows[locus(name)]["reason"] for name in ("a2/1", "a2/2", "a2/3")})
        with closing(branch.connect()) as connection:
            route = connection.execute("SELECT route, provenance_class FROM fu_evidence WHERE work_key='w-plain-2'"
                                       ).fetchone()
            blocked = connection.execute("SELECT COUNT(*) FROM fu_judgments WHERE outcome='blocked'").fetchone()[0]
        self.assertEqual(("provider-cache", "public"), tuple(route))
        self.assertEqual(3, blocked)
        item = json.loads(judge.requests[0][0][1]["content"])["items"][0]
        self.assertEqual({"id", "claim", "context", "citationLocus", "citedWork", "identifiers", "evidenceUnits",
                          "sourceSha256", "evidenceDigest", "complete", "evidenceCoverage"}, set(item))
        self.assertEqual({}, item["identifiers"], "no identifier of the cited work is sent")
        self.assertNotIn("provenance", judge.requests[0][0][1]["content"])
        self.assertTrue(item["id"].endswith("|abstract"))

    def test_full_text_from_europe_pmc_and_definitive_outcomes(self):
        branch = self.branch()
        judge = F.ScriptedJudge(quoting("contradicted"))
        transport = F.FixtureTransport(routes())
        result = self.run_support(branch, judge, tier="full-text", transport=transport, europe_pmc=True)
        self.assertEqual({"live:obtained": 1, "none:fetch-failed": 1, "none:no-open-full-text": 2},
                         result["evidence"])
        self.assertEqual(2, result["sent"], "the two loci citing the work whose full text was obtained")
        fetched = [request for request in transport.requests if request["url"].endswith("/fullTextXML")]
        self.assertEqual({FULL_TEXT.format("PMC900001"), FULL_TEXT.format("PMC900004")},
                         {request["url"] for request in fetched})
        item = json.loads(judge.requests[0][0][1]["content"])["items"][0]
        self.assertEqual(("full-text", True, "complete", hashlib.sha256(RETRACTED_FULL_TEXT).hexdigest()),
                         (json.loads(judge.requests[0][0][1]["content"])["evidenceTier"], item["complete"],
                          item["evidenceCoverage"], item["sourceSha256"]))
        self.assertEqual(["u0001", "u0002", "u0003", "u0004"], [unit["id"] for unit in item["evidenceUnits"]])
        rows = self.rows(branch, "full-text")
        self.assertEqual("contradicted", rows[locus("a1/1")]["verdict"])
        self.assertEqual("no-open-full-text", rows[locus("a1/2")]["reason"], "Europe PMC answered 404")
        self.assertEqual("no-open-full-text", rows[locus("a1/4")]["reason"], "the work has no PMCID")
        self.assertEqual("fetch-failed", rows[locus("b1/1")]["reason"])
        with closing(branch.connect()) as connection:
            kinds = [row[0] for row in connection.execute(
                "SELECT u.kind FROM fu_evidence_units u JOIN fu_evidence e USING(evidence_id) WHERE "
                "e.work_key='w-retracted' ORDER BY u.ordinal")]
        self.assertEqual(["abstract", "paragraph", "table", "caption"], kinds)
        again = self.run_support(branch, judge, tier="full-text", transport=transport, europe_pmc=True)
        self.assertEqual(({}, 0), (again["evidence"], again["sent"]), "settled evidence and verdicts are reused")
        retried = self.run_support(branch, judge, tier="full-text", transport=transport, europe_pmc=True,
                                   retry_failed=True)
        self.assertEqual({"none:fetch-failed": 1}, retried["evidence"])

    def test_cached_full_text_is_taken_before_europe_pmc(self):
        caches = InputCaches(None, ProviderCache(self.cache))
        self.addCleanup(caches.close)
        evidence = SP.Acquirer(None, caches=caches).full_text(
            {"work_key": "w-cached", "doi": "10.5555/fu.work.cached", "pmcid": "PMC900099"})
        self.assertEqual(("obtained", "provider-cache", "public"), (evidence.outcome, evidence.route,
                                                                    evidence.provenance_class))
        self.assertIn("Cached full text sentence.", [unit["text"] for unit in evidence.units])
        missing = SP.Acquirer(None, caches=caches).full_text({"work_key": "w-x", "doi": "10.5555/fu.work.x"})
        self.assertEqual("unavailable", missing.outcome)

    def test_entitled_text_reaches_only_on_premises_judges(self):
        branch = self.branch(text=True)
        external = F.ScriptedJudge(quoting(), location="external")
        with self.assertRaisesRegex(ConfidentialityError, "on premises"):
            self.run_support(branch, external)
        self.assertEqual([], external.requests)
        result = self.run_support(branch, external, public_only=True)
        self.assertEqual(5, result["sent"])
        rows = self.rows(branch, "abstract")
        self.assertEqual({"not-judged"}, {rows[locus(name)]["reason"] for name in ("a2/1", "a2/2")})
        self.assertEqual("abstract-incomplete", rows[locus("a2/3")]["reason"])
        onprem = F.ScriptedJudge(quoting())
        result = self.run_support(branch, onprem)
        self.assertEqual(2, result["sent"])
        rows = self.rows(branch, "abstract")
        self.assertEqual(("direct-support", "entitled"), (rows[locus("a2/1")]["verdict"],
                                                          rows[locus("a2/1")]["provenance_class"]))
        sentences = [item["claim"] for messages, _ in onprem.requests
                     for item in json.loads(messages[1]["content"])["items"]]
        self.assertIn("The trial reported a large benefit ⟨S1⟩ , although it was later questioned .", sentences)

    def test_entitled_full_text_documents_are_routed_as_entitled(self):
        branch = self.branch()
        documents = self.root / f"entitled-{self.count}"
        documents.mkdir()
        body = jats("10.5555/fu.work.plain2", "PMC0", "Imaging was used as described.")
        (documents / "plain2.xml").write_bytes(body)
        (documents / "entitled-documents.json").write_text(json.dumps({
            "format": "ocsl-followup-entitled-v1",
            "documents": [{"doi": "10.5555/fu.work.plain2", "path": "plain2.xml",
                           "sha256": hashlib.sha256(body).hexdigest()}]}))
        entitled = SP.EntitledDocuments(documents)
        external = F.ScriptedJudge(quoting(), location="external")
        with self.assertRaisesRegex(ConfidentialityError, "on premises"):
            self.run_support(branch, external, tier="full-text", europe_pmc=True, entitled=entitled)
        result = self.run_support(branch, external, tier="full-text", europe_pmc=True, entitled=entitled,
                                  public_only=True)
        self.assertEqual((2, 1), (result["sent"], result["withheldEntitled"]))
        rows = self.rows(branch, "full-text")
        self.assertEqual(("not-judged", "entitled"), (rows[locus("a1/4")]["reason"],
                                                      rows[locus("a1/4")]["provenance_class"]))

    def test_every_reason_is_written(self):
        branch = self.branch(text=True)
        with branch.writing() as connection:
            connection.execute("UPDATE fu_frame SET work_key=NULL WHERE locus_id=?", (locus("b1/3"),))
        abstain, invalid = locus("a1/1"), locus("a1/2")

        def answer(messages, settings):
            payload = json.loads(messages[1]["content"])
            item = payload["items"][0]
            if item["id"].startswith(abstain):
                return quoting("abstain")(messages, settings)
            if item["id"].startswith(invalid):
                return json.dumps({"items": [{"id": item["id"], "supportVerdict": "direct-support",
                                              "rationale": "x", "escalate": False, "escalationReason": "",
                                              "evidence": [{"unitId": item["evidenceUnits"][0]["id"],
                                                            "quote": "a quote that is not in the evidence"}]}]})
            return quoting()(messages, settings)
        result = self.run_support(branch, F.ScriptedJudge(answer), limit=4)
        self.assertEqual(4, result["sent"])
        reasons = {row["reason"] for row in self.rows(branch, "abstract").values() if not row["assessable"]}
        self.assertEqual({"abstained", "invalid-output", "identity-unresolved", "not-judged"}, reasons)
        # Items rejected after repair are sent again by a later run; this judge rejects that one again.
        self.run_support(branch, F.ScriptedJudge(answer))
        rows = self.rows(branch, "abstract")
        self.assertEqual({"abstained", "invalid-output", "identity-unresolved", "fetch-failed",
                          "abstract-incomplete"},
                         {row["reason"] for row in rows.values() if not row["assessable"]})
        self.assertEqual(("abstain", 0), (rows[abstain]["verdict"], rows[abstain]["assessable"]))
        no_text = self.branch()
        self.run_support(no_text, F.ScriptedJudge(quoting()))
        self.assertIn("text-redacted", {row["reason"] for row in self.rows(no_text, "abstract").values()})

    def test_an_unreadable_response_rejects_its_items_and_the_run_goes_on(self):
        branch = self.branch()
        unreadable = locus("a1/1")

        def answer(messages, settings):
            item = json.loads(messages[1]["content"])["items"][0]
            return "not a JSON response" if item["id"].startswith(unreadable) else quoting()(messages, settings)
        result = self.run_support(branch, F.ScriptedJudge(answer))
        # the unreadable response and its repair are rejected; every other item of the run is accepted
        self.assertEqual((4, 2, 1), (result["run"]["accepted"], result["run"]["rejected"],
                                     result["run"]["repairCalls"]))
        self.assertEqual("invalid-output", self.rows(branch, "abstract")[unreadable]["reason"])
        with closing(branch.connect()) as connection:
            codes = {row[0] for row in connection.execute("SELECT error_code FROM fu_judgments WHERE item_key=?",
                                                          (unreadable + "|abstract",))}
        self.assertEqual({"invalid_json"}, codes)

    def test_a_run_resumes_after_calls_without_a_response(self):
        branch = self.branch()

        def down(messages, settings):
            raise ReviewError("judge HTTP 503")
        transport = F.FixtureTransport(routes())
        first = self.run_support(branch, F.ScriptedJudge(down), transport=transport)
        self.assertEqual((5, 5, 0), (first["run"]["failedCalls"], first["run"]["open"], first["run"]["accepted"]))
        self.assertEqual({"not-judged"}, {row["reason"] for name, row in self.rows(branch, "abstract").items()
                                          if name in (locus("a1/1"), locus("b1/2"))})
        requests = len(transport.requests)
        second = self.run_support(branch, F.ScriptedJudge(quoting()), transport=transport)
        self.assertEqual((5, 0), (second["run"]["accepted"], second["run"]["open"]))
        self.assertEqual(requests, len(transport.requests), "settled evidence is not fetched again")
        self.assertEqual("direct-support", self.rows(branch, "abstract")[locus("b1/2")]["verdict"])

    def test_the_command_writes_a_result_file(self):
        branch = self.branch()
        judge = F.ScriptedJudge(quoting())
        transport = F.FixtureTransport(routes())
        target = self.root / f"result-support-{self.count}.json"
        with mock.patch("ocsl_review.followup_judge.judge_from_args", lambda args: (judge, None)), \
                mock.patch.object(SP, "make_client",
                                  lambda workspace=None: followup_net.client(transport=transport,
                                                                             sleep=lambda seconds: None)), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "support", "--database", str(branch.path),
                         "--tier", "abstract", "--model", F.MODEL, "--provider-cache", str(self.cache),
                         "--concurrency", "1", "--fetch-workers", "2"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual((5, "abstract"), (record["result"]["sent"], record["result"]["tier"]))


if __name__ == "__main__":
    unittest.main()
