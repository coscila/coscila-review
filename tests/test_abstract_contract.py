"""Actual structured acquisition must satisfy the census evidence contract."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from ocsl_review.evidence import abstract_candidate, complete_abstract
from ocsl_review.scholarly import ScholarlyClient
from test_fulltext import SourceTransport


class AbstractContractTests(unittest.TestCase):
    def test_complete_provider_fields_do_not_need_terminal_punctuation(self):
        result = ScholarlyClient(transport=SourceTransport(abstract="Complete provider abstract without punctuation")).acquire_full_text(doi="10.1234/example")
        self.assertTrue(result["abstractComplete"])
        self.assertEqual(len(result["abstractCandidates"]), 2)
        self.assertTrue(all(complete_abstract(candidate) for candidate in result["abstractCandidates"]))
        self.assertTrue(all(candidate["provenance"]["identifiers"]["doi"] == "10.1234/example" for candidate in result["abstractCandidates"]))

    def test_ellipsis_snippets_and_legacy_candidate_flags_are_not_complete(self):
        result = ScholarlyClient(transport=SourceTransport(abstract="Truncated provider response...")).acquire_full_text(doi="10.1234/example")
        self.assertFalse(result["abstractComplete"])
        self.assertFalse(any(complete_abstract(candidate) for candidate in result["abstractCandidates"]))
        self.assertFalse(complete_abstract({"provider": "pubmed", "text": "A plausible sentence.", "complete": True}))
        self.assertFalse(complete_abstract(abstract_candidate({"provider": "pubmed", "pmid": "123", "abstract": "A plausible sentence."},
                                                           source_url="https://example.org", retrieved_at="2026-10-14T00:00:00Z")))

    def test_cached_evidence_rejects_altered_text_and_raw_provenance(self):
        original = ScholarlyClient(transport=SourceTransport()).acquire_full_text(doi="10.1234/example")["abstractCandidates"][0]
        for field, value in (("text", "An invented result."), ("rawMetadata", {"abstractText": "An invented result."})):
            with self.subTest(field=field):
                candidate = copy.deepcopy(original)
                candidate[field] = value
                self.assertFalse(complete_abstract(candidate))
        self.assertTrue(complete_abstract(original))

    def test_pubmed_abstract_for_a_different_doi_is_excluded(self):
        client = ScholarlyClient(transport=SourceTransport())
        structured = client._pubmed_structured("123", "acquire_full_text")
        structured["doi"] = "10.1234/unrelated"
        with patch.object(client, "_pubmed_structured", return_value=structured):
            result = client.acquire_full_text(doi="10.1234/example")
        self.assertEqual([candidate["provider"] for candidate in result["abstractCandidates"]], ["europe-pmc"])
        self.assertTrue(any("conflicts with the requested work" in message for message in result["limitations"]))


if __name__ == "__main__":
    unittest.main()
