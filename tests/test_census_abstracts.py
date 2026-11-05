# SPDX-License-Identifier: Apache-2.0
"""Europe PMC core abstracts for cited works: 40 DOIs per query, PMID fallback, identifier-only requests."""
from __future__ import annotations

import re
import unittest

from ocsl_review.census_abstracts import DOIS_PER_QUERY, fetch_abstracts, fill_record, http_get

from test_census_runner import unpaced


class FakeSearch:
    def __init__(self, failing=False):
        self.queries = []
        self.failing = failing

    def __call__(self, params):
        self.queries.append(dict(params))
        if self.failing:
            return None
        results = []
        for doi in re.findall(r'DOI:"([^"]+)"', params["query"]):
            number = int(doi.rsplit(".", 1)[1])
            if number % 10 == 0:
                continue
            results.append({"doi": doi.upper(), "abstractText": None if number % 7 == 0 else "Abstract of %s." % doi})
        match = re.fullmatch(r"EXT_ID:(\d+) AND SRC:MED", params["query"])
        if match:
            results.append({"pmid": match.group(1), "abstractText": "PMID abstract %s." % match.group(1)})
        return {"resultList": {"result": results}}


class AbstractTests(unittest.TestCase):
    def test_batches_fallback_and_sources(self):
        works = {"w%d" % i: {"doi": "10.5555/n.%d" % i} for i in range(1, 46)}
        works["p"] = {"pmid": "123456"}
        works["none"] = {}
        search = FakeSearch()
        result = fetch_abstracts(works, search)
        doi_queries = [q for q in search.queries if "DOI:" in q["query"]]
        self.assertEqual(2, len(doi_queries))
        self.assertEqual(DOIS_PER_QUERY, doi_queries[0]["query"].count("DOI:"))
        self.assertEqual(("core", "json"), (doi_queries[0]["resultType"], doi_queries[0]["format"]))
        self.assertEqual(("Abstract of 10.5555/n.1.", "europe-pmc-core"), (result["w1"]["abstract"], result["w1"]["abstractSource"]))
        self.assertEqual("europe-pmc-no-hit", result["w10"]["abstractSource"])
        self.assertEqual(("europe-pmc-no-abstract", None), (result["w7"]["abstractSource"], result["w7"]["abstract"]))
        self.assertEqual("europe-pmc-core-pmid", result["p"]["abstractSource"])
        self.assertEqual("no-identifier", result["none"]["abstractSource"])
        self.assertTrue(all(set(q) <= {"query", "format", "resultType", "pageSize"} for q in search.queries))

    def test_failures_are_recorded_not_guessed(self):
        result = fetch_abstracts({"w1": {"doi": "10.5555/n.1"}, "p": {"pmid": "1"}}, FakeSearch(failing=True))
        self.assertEqual({"europe-pmc-failed"}, {r["abstractSource"] for r in result.values()})
        self.assertIsNone(result["w1"]["abstract"])

    def test_europe_pmc_comes_first_and_other_providers_are_recorded_fallbacks(self):
        record = {"doi": "x", "works": {
            "a": {"doi": "10.5555/n.1", "abstract": "Other provider text.", "abstractSource": "semantic-scholar"},
            "b": {"doi": "10.5555/n.10", "abstract": "Fallback text.", "abstractSource": "semantic-scholar"},
            "c": {"doi": "10.5555/n.7"},
            "d": {"doi": "10.5555/n.20", "abstract": "Supplied without a provider."}}}
        filled = fill_record(record, FakeSearch())["works"]
        self.assertEqual(("Abstract of 10.5555/n.1.", "europe-pmc-core"), (filled["a"]["abstract"], filled["a"]["abstractSource"]))
        self.assertEqual(("Fallback text.", "fallback:semantic-scholar", "europe-pmc-no-hit"),
                         (filled["b"]["abstract"], filled["b"]["abstractSource"], filled["b"]["europePmcOutcome"]))
        self.assertEqual((None, "europe-pmc-no-abstract"), (filled["c"]["abstract"], filled["c"]["abstractSource"]))
        self.assertEqual("fallback:supplied", filled["d"]["abstractSource"])
        # Looked-up works are not queried again; overwrite queries them again and still keeps the fallbacks.
        search = FakeSearch()
        again = fill_record({"doi": "x", "works": filled}, search)["works"]
        self.assertEqual([], search.queries)
        self.assertEqual(filled, again)
        refreshed = fill_record({"doi": "x", "works": filled}, FakeSearch(), overwrite=True)["works"]
        self.assertEqual(("Fallback text.", "fallback:semantic-scholar"), (refreshed["b"]["abstract"], refreshed["b"]["abstractSource"]))

    def test_failed_lookups_change_nothing_and_are_retried(self):
        record = {"doi": "x", "works": {"a": {"doi": "10.5555/n.1", "abstract": "Other.", "abstractSource": "semantic-scholar"}}}
        failed = fill_record(record, FakeSearch(failing=True))["works"]["a"]
        self.assertEqual(("Other.", "semantic-scholar", "europe-pmc-failed"),
                         (failed["abstract"], failed["abstractSource"], failed["europePmcOutcome"]))
        retried = fill_record({"doi": "x", "works": {"a": failed}}, FakeSearch())["works"]["a"]
        self.assertEqual(("Abstract of 10.5555/n.1.", "europe-pmc-core"), (retried["abstract"], retried["abstractSource"]))


    def test_answers_by_doi_are_kept_for_the_invocation_only_from_a_complete_result_list(self):
        from ocsl_review.census_abstracts import DoiRecords
        memo, asked = DoiRecords(), []
        answers = [
            # A page cut at its size: work a's record is kept, work b's missing hit is no answer.
            {"hitCount": 300, "resultList": {"result": [{"doi": "10.5555/a", "abstractText": "Abstract a."}]}},
            {"unexpected": "body"},                                   # no result list: nothing is kept
            {"hitCount": 0, "resultList": {"result": []}},            # a complete page: b has no hit
        ]

        def get(params):
            asked.append(sorted(part.split('"')[1] for part in params["query"].split(" OR ")))
            return answers.pop(0)
        works = {"a": {"doi": "10.5555/A"}, "b": {"doi": "10.5555/b"}}
        first = fetch_abstracts(works, get, memo=memo)
        self.assertEqual(("europe-pmc-core", "europe-pmc-no-hit"),
                         (first["a"]["abstractSource"], first["b"]["abstractSource"]))
        fetch_abstracts(works, get, memo=memo)
        last = fetch_abstracts(works, get, memo=memo)
        self.assertEqual([["10.5555/a", "10.5555/b"], ["10.5555/b"], ["10.5555/b"]], asked)
        self.assertEqual(("Abstract a.", first["a"]["abstractRetrievedAt"], "europe-pmc-no-hit"),
                         (last["a"]["abstract"], last["a"]["abstractRetrievedAt"], last["b"]["abstractSource"]))
        fetch_abstracts(works, get, memo=memo)
        self.assertEqual(3, len(asked), "both DOIs are answered for the rest of the invocation")
        # A failed lookup (no answer) is never kept.
        failed = fetch_abstracts({"c": {"doi": "10.5555/c"}}, lambda params: None, memo=memo)
        self.assertEqual(("europe-pmc-failed", None), (failed["c"]["abstractSource"], memo.get("10.5555/c")))

    def test_live_lookups_name_the_census_user_agent_and_carry_identifiers_only(self):
        import io
        import json
        import urllib.error

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False
        sent, sleeps = [], []

        def opener(request, timeout=None):
            sent.append(request)
            if len(sent) == 1:
                raise urllib.error.URLError("dropped")
            return Response(json.dumps({"resultList": {"result": []}}).encode())
        get = http_get(opener=opener, sleep=sleeps.append, limit=unpaced())
        self.assertEqual({"resultList": {"result": []}}, get({"query": 'DOI:"10.5555/n.1"', "format": "json"}))
        self.assertEqual(2, len(sent))
        self.assertEqual([3], sleeps, "one retry after the dropped connection")
        self.assertTrue(sent[0].full_url.startswith("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="))
        self.assertEqual({"Accept": "application/json", "User-agent": "ocsl-review census inputs"},
                         dict(sent[1].header_items()))
        self.assertNotIn("@", sent[1].full_url + json.dumps(dict(sent[1].header_items())))


if __name__ == "__main__":
    unittest.main()
