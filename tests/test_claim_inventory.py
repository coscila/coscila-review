from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.ingest import ingest_manuscript

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "manuscript_with_citations.md"


class ClaimInventoryTests(unittest.TestCase):
    def test_results_are_not_automatically_flagged_for_external_citation(self):
        text = """# Results

We observed a 17% increase in the measured outcome.

# Introduction

The intervention causes durable recovery [1].

# References

[1] A. Example. Study. 2024. doi:10.1000/example
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text(text)
            graph = build_claim_inventory(ingest_manuscript(path))
        self.assertEqual(len(graph["claims"]), 2)
        result = next(item for item in graph["claims"] if item["origin"] == "manuscript-result")
        background = next(
            item for item in graph["claims"] if item["origin"] == "external-background"
        )
        self.assertFalse(result["citationRequired"])
        self.assertEqual(result["citationBindingStatus"], "not-applicable")
        self.assertTrue(background["citationRequired"])
        self.assertEqual(background["citationBindingStatus"], "exact")

    def test_compound_sentence_is_split_into_location_stable_clauses(self):
        text = (
            "# Discussion\n\nThe association was strong; the study does not establish causation.\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text(text)
            graph = build_claim_inventory(ingest_manuscript(path))
        self.assertEqual(len(graph["claims"]), 2)
        self.assertEqual([item["locator"]["clause"] for item in graph["claims"]], [1, 2])

    def test_priority_claim_requires_search_and_starts_unverified(self):
        text = "# Introduction\n\nThis is the first study of the phenomenon.\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text(text)
            graph = build_claim_inventory(ingest_manuscript(path))
        claim = graph["claims"][0]
        self.assertEqual(claim["claimType"], "novelty")
        self.assertTrue(claim["citationRequired"])
        self.assertFalse(claim["processed"])

    def test_fixture_inventory_excludes_references_splits_methods_and_adds_table_cells(self):
        graph = build_claim_inventory(ingest_manuscript(FIXTURE))

        self.assertEqual(len(graph["claims"]), 12)
        self.assertEqual(sum(item["citationRequired"] for item in graph["claims"]), 3)
        self.assertEqual(len(graph["references"]), 2)
        self.assertFalse(
            any(item["locator"]["section"] == "References" for item in graph["claims"])
        )

        by_text = {item["text"]: item for item in graph["claims"]}
        software = by_text["The analysis used a locked software environment"]
        held_out = by_text["The analysis used an independently held test set"]
        self.assertEqual(software["claimType"], "software")
        self.assertTrue(software["citationRequired"])
        self.assertEqual(software["citationBindingStatus"], "missing")
        self.assertEqual(held_out["claimType"], "method")
        self.assertFalse(held_out["citationRequired"])
        self.assertEqual(held_out["citationBindingStatus"], "not-applicable")

        expected_table_claims = {
            "tbl-0001:r2:c2": "Group: Development; Participants: 56",
            "tbl-0001:r2:c3": "Group: Development; Events: 14",
            "tbl-0001:r3:c2": "Group: Test; Participants: 28",
            "tbl-0001:r3:c3": "Group: Test; Events: 6",
        }
        table_claims = {
            item["locator"]["anchor"]: item
            for item in graph["claims"]
            if str(item["locator"].get("anchor", "")).startswith("tbl-")
        }
        self.assertEqual(set(table_claims), set(expected_table_claims))
        for anchor, expected_text in expected_table_claims.items():
            claim = table_claims[anchor]
            self.assertEqual(claim["text"], expected_text)
            self.assertEqual(claim["origin"], "manuscript-method")
            self.assertEqual(claim["claimType"], "numeric")
            self.assertFalse(claim["citationRequired"])
            self.assertEqual(claim["citationBindingStatus"], "not-applicable")

    def test_catalog_declares_the_canonical_audit_graph(self):
        catalog = tomllib.loads((ROOT / "skills" / "catalog.toml").read_text(encoding="utf-8"))
        record = next(
            item for item in catalog["skill"] if item["id"] == "ocsl-review-claim-inventory"
        )
        descriptor = tomllib.loads(
            (ROOT / "skills" / "ocsl-review-claim-inventory" / "skill.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["outputs"], ["audit-graph.json"])
        self.assertEqual(descriptor["outputs"], ["audit-graph.json"])


if __name__ == "__main__":
    unittest.main()
