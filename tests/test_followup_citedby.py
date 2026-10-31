# SPDX-License-Identifier: Apache-2.0
"""Cited-by traversal: two Europe PMC pages, OpenAlex cursor pages, OpenAlex failures as the state unavailable,
edges united by citing key across sources, coverage states, the graph as a dataset version, and no identity or key
in any recorded request. No test reaches the network."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_citedby as C
from ocsl_review import followup_dependency, followup_net
from ocsl_review.cli import main
from ocsl_review.followup_branch import FollowupBranch
from ocsl_review.followup_model import CitedByEdge

from test_followup_dependency import insert_frame, support
from test_followup_status import clean_environment, epmc_search, no_network

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX_WORK = "https://api.openalex.org/works/doi:10.5555/fu.review.a1"
OPENALEX_CITES = "https://api.openalex.org/works?" + urllib.parse.urlencode(
    {"filter": "cites:W100000001", "per-page": 200, "cursor": "*"})
EMPTY_SEARCH = json.dumps({"hitCount": 0, "resultList": {"result": []}}).encode()


def citations(page: int) -> str:
    return f"{EPMC}/MED/9300001/citations?" + urllib.parse.urlencode({"page": page, "pageSize": 1000,
                                                                       "format": "json"})


def routes(**changes) -> dict:
    value = {
        epmc_search("10.5555/fu.review.a1"): (200, "citedby/europe-pmc-review-record.json"),
        epmc_search("10.5555/fu.review.a2"): (200, EMPTY_SEARCH),
        epmc_search("10.5555/fu.review.b1"): (200, EMPTY_SEARCH),
        citations(1): (200, "citedby/europe-pmc-citations-page1.json"),
        citations(2): (200, "citedby/europe-pmc-citations-page2.json"),
        OPENALEX_WORK: (200, "citedby/openalex-work.json"),
        OPENALEX_CITES: (200, "citedby/openalex-cites-page1.json"),
    }
    value.update(changes)
    return value


class CitedByTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        for patcher in (mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network),
                        mock.patch.dict(os.environ, clean_environment(), clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        type(self).count += 1
        self.path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(self.path, self.census["parent"], protocol=F.protocol())
        self.branch = FollowupBranch(self.path)
        with self.branch.writing() as connection:
            insert_frame(connection)
            # The shared sentence of review a1 now rests on the retracted work alone: essential.
            support(connection, "a1/2", "full-text", "contradicted")
            followup_dependency.compute_dependency(connection)

    def traverse(self, transport, **options):
        scholarly = followup_net.client(transport=transport, sleep=lambda seconds: None)
        with self.branch.writing() as connection:
            return C.traverse(connection, scholarly, sources=options.pop("sources", ["europe-pmc", "openalex"]),
                              **options)

    def edges(self, target="10.5555/fu.review.a1"):
        with closing(self.branch.connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM fu_citedby_edges WHERE target_doi=? ORDER BY source, citing_key", (target,))]

    def coverage(self):
        with closing(self.branch.connect()) as connection:
            return {(row["target_doi"], row["source"]): dict(row) for row in connection.execute(
                "SELECT * FROM fu_citedby_coverage")}

    def test_pages_of_both_sources_unite_by_citing_key(self):
        transport = F.FixtureTransport(routes())
        result = self.traverse(transport)
        self.assertEqual(3, result["targets"])
        self.assertEqual({"europe-pmc": {"complete": 1, "no-identifier": 2},
                          "openalex": {"complete": 1, "unavailable": 2}}, result["states"])
        edges = self.edges()
        self.assertEqual([("europe-pmc", "doi:10.5555/fu.later.1"), ("europe-pmc", "doi:10.5555/fu.later.2"),
                          ("europe-pmc", "pmid:9200003"), ("openalex", "doi:10.5555/fu.later.1"),
                          ("openalex", "doi:10.5555/fu.later.4")], [(e["source"], e["citing_key"]) for e in edges])
        self.assertEqual(4, result["unitedCitingWorks"])
        later = next(e for e in edges if e["source"] == "openalex" and e["citing_key"] == "doi:10.5555/fu.later.1")
        self.assertEqual(("W200000001", 2024, "2024-02-01", F.JOURNAL_A),
                         (later["citing_openalex"], later["citing_year"], later["citing_first_online"],
                          later["target_journal"]))
        coverage = self.coverage()
        self.assertEqual(("complete", 3, 3), tuple(coverage[("10.5555/fu.review.a1", "europe-pmc")][key]
                                                   for key in ("state", "hit_count", "edges")))
        self.assertEqual(("complete", 2, 2), tuple(coverage[("10.5555/fu.review.a1", "openalex")][key]
                                                   for key in ("state", "hit_count", "edges")))
        self.assertEqual("no-identifier", coverage[("10.5555/fu.review.b1", "europe-pmc")]["state"])
        with closing(self.branch.connect()) as connection:
            dataset = connection.execute("SELECT * FROM fu_datasets WHERE kind='cited-by-graph'").fetchone()
        self.assertEqual((result["dataset"], 5), (dataset["dataset_id"], dataset["rows"]))
        self.assertTrue(self.branch.verify()["ok"])

    def test_an_openalex_failure_is_a_state_never_an_exception(self):
        transport = F.FixtureTransport(routes(**{OPENALEX_WORK: (503, b"{}")}))
        result = self.traverse(transport)
        coverage = self.coverage()
        self.assertEqual(("unavailable", "work lookup: http-503"),
                         (coverage[("10.5555/fu.review.a1", "openalex")]["state"],
                          coverage[("10.5555/fu.review.a1", "openalex")]["reason"]))
        self.assertEqual("complete", coverage[("10.5555/fu.review.a1", "europe-pmc")]["state"])
        self.assertEqual(3, len(self.edges()))
        broken = F.FixtureTransport(routes(**{OPENALEX_CITES: (200, b"[1, 2]")}))
        self.traverse(broken, sources=["openalex"])
        self.assertEqual("unavailable", self.coverage()[("10.5555/fu.review.a1", "openalex")]["state"])
        self.assertEqual(3, len(self.edges()), "Europe PMC edges are kept when only OpenAlex is traversed again")
        self.assertEqual({"complete": 1, "no-identifier": 2}, result["states"]["europe-pmc"])

    def test_the_page_cap_truncates(self):
        result = self.traverse(F.FixtureTransport(routes()), sources=["europe-pmc"], max_pages=1)
        state = self.coverage()[("10.5555/fu.review.a1", "europe-pmc")]
        self.assertEqual(("truncated", 2, 3), (state["state"], state["edges"], state["hit_count"]))
        self.assertIn("page cap", state["reason"])
        self.assertEqual({"truncated": 1, "no-identifier": 2}, result["states"]["europe-pmc"])

    def test_requests_carry_no_identity_and_the_key_is_never_recorded(self):
        secret = "openalex-synthetic-key-value"
        transport = F.FixtureTransport(routes())
        with mock.patch.dict(os.environ, {C.OPENALEX_KEY_ENV: secret}):
            self.traverse(transport)
        sent = [request for request in transport.requests if "openalex" in request["url"]]
        self.assertTrue(all(request["params"].get("api_key") == secret for request in sent if request["params"]))
        for request in transport.requests:
            self.assertNotIn("@", json.dumps(request))
            self.assertNotIn("mailto", json.dumps(request).lower())
        with closing(self.branch.connect()) as connection:
            for row in connection.execute("SELECT url, params_json, error FROM fu_requests"):
                text = json.dumps(list(row))
                self.assertNotIn(secret, text)
                self.assertNotIn("@", text)

    def test_targets_follow_the_dependency_or_every_s1_review_when_widened(self):
        with self.branch.writing() as connection:
            connection.execute("DELETE FROM fu_support")
            followup_dependency.compute_dependency(connection)
            narrow = [target.doi for target in C.targets(connection)]
            wide = [target.doi for target in C.targets(connection, widen=True)]
        self.assertEqual(["10.5555/fu.review.a2", "10.5555/fu.review.b1"], narrow)
        self.assertEqual(["10.5555/fu.review.a1", "10.5555/fu.review.a2", "10.5555/fu.review.b1"], wide)

    def test_records_sharing_an_identifier_carry_one_key(self):
        edges = [CitedByEdge("t", "pmid:1", "europe-pmc", "r", "x", citing_pmid="1"),
                 CitedByEdge("t", "openalex:W1", "openalex", "r", "x", citing_pmid="1", citing_doi="10.1/a",
                             citing_openalex="W1"),
                 CitedByEdge("t", "openalex:W2", "openalex", "r", "x", citing_openalex="W2")]
        self.assertEqual(["doi:10.1/a", "doi:10.1/a", "openalex:W2"], [edge.citing_key for edge in C.unite(edges)])

    def test_the_command_writes_a_result_file(self):
        transport = F.FixtureTransport(routes())
        target = self.root / f"result-citedby-{self.count}.json"
        with mock.patch.object(C, "make_client", lambda workspace=None: followup_net.client(
                transport=transport, sleep=lambda seconds: None)), redirect_stdout(StringIO()), \
                redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "citedby", "--database", str(self.path),
                         "--source", "europe-pmc", "--limit", "1"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual((1, "freeze-synthetic"), (record["result"]["targets"], record["result"]["freeze"]))


if __name__ == "__main__":
    unittest.main()
