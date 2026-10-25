# SPDX-License-Identifier: Apache-2.0
"""Europe PMC core abstracts for cited works: 40 DOIs per query, PMID fallback, identifier-only requests."""
from __future__ import annotations

import re
import unittest

from ocsl_review.census_abstracts import DOIS_PER_QUERY, fetch_abstracts, fill_record


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

    def test_fill_record_keeps_existing_abstracts(self):
        record = {"doi": "x", "works": {"a": {"doi": "10.5555/n.1", "abstract": "Kept."}, "b": {"doi": "10.5555/n.2"}}}
        search = FakeSearch()
        filled = fill_record(record, search)
        self.assertEqual("Kept.", filled["works"]["a"]["abstract"])
        self.assertEqual("Abstract of 10.5555/n.2.", filled["works"]["b"]["abstract"])
        self.assertEqual(1, search.queries[0]["query"].count("DOI:"))
        self.assertEqual("Abstract of 10.5555/n.1.", fill_record(record, FakeSearch(), overwrite=True)["works"]["a"]["abstract"])


if __name__ == "__main__":
    unittest.main()
