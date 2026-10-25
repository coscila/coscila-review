# SPDX-License-Identifier: Apache-2.0
"""Census inputs from structured documents: citing sentences, in-body loci, gates, identity and metadata."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from ocsl_review import census_extraction as X
from ocsl_review.errors import ValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "citation_census"


def review(name="review.xml"):
    return X.extract_review((FIXTURES / name).read_bytes(), html=name.endswith(".html"))


class SentenceTests(unittest.TestCase):
    def test_clean_and_split(self):
        self.assertEqual('"a" - b \'c\'', X.clean("“a” –  b\n‘c’­"))
        text = "First claim ⟨B1⟩ . Second (claim) ⟨B2⟩ . e.g. lower case stays."
        self.assertEqual(["First claim ⟨B1⟩ .", "Second (claim) ⟨B2⟩ . e.g. lower case stays."],
                         [text[a:b] for a, b in X.sentence_spans(text)])

    def test_labels(self):
        self.assertEqual("12", X.normalize_label("12.", 4))
        self.assertEqual("12a", X.normalize_label("[12a]", 4))
        self.assertEqual("4", X.normalize_label("", 4))


class JatsExtractionTests(unittest.TestCase):
    def setUp(self):
        self.review = review()
        self.entries = {entry.label: entry for entry in self.review["entries"]}

    def test_entries_in_bibliography_order_with_identifiers(self):
        self.assertEqual([str(i) for i in range(1, 18)], [entry.label for entry in self.review["entries"]])
        self.assertEqual({"doi": "10.5555/w2", "pmid": "100002"}, self.entries["2"].identifiers)
        self.assertEqual({}, self.entries["12"].identifiers)
        self.assertEqual("Synthetic review of zeta-kinase signalling in breast tumours", self.review["title"])
        self.assertEqual("10.5555/synthetic.review.1", self.review["doi"])

    def test_citing_sentences_and_order(self):
        first = self.entries["1"]
        self.assertEqual(4, len(first.loci))
        self.assertEqual([{"sentence": "Zeta-kinase is a fictional enzyme⟨B1⟩ .", "section": "Introduction"},
                          {"sentence": "Another sentence cites the first source again ⟨B1⟩ .",
                           "section": "Introduction > Mechanisms"},
                          {"sentence": "Trial results were mixed ⟨B1⟩ .", "section": "Clinical evidence"}],
                         first.passages(3))
        self.assertEqual("It was described twice ⟨B2⟩ , ⟨B3⟩ .", self.entries["3"].loci[0].sentence)
        self.assertEqual('"Quoted claim" with an en dash - here ⟨B12⟩ .', self.entries["12"].loci[0].sentence)

    def test_ranges_expand_every_member(self):
        pair = "Later work disagreed (⟨B4⟩ ⟨B5⟩ ⟨B6⟩ ⟨B7⟩ )."
        for label in ("4", "5", "6", "7"):
            self.assertEqual(pair, self.entries[label].loci[0].sentence)
        text = "A range given as text ⟨B8⟩ ⟨B9⟩ ⟨B10⟩ supports the model."
        for label in ("8", "9", "10"):
            self.assertEqual(text, self.entries[label].loci[0].sentence)

    def test_display_blocks_and_body_scope(self):
        flags = {label: set().union(*(locus.flags for locus in entry.loci)) for label, entry in self.entries.items() if entry.loci}
        self.assertEqual({"list"}, flags["11"])
        self.assertEqual({"table"}, flags["13"])
        self.assertEqual({"table"}, flags["14"])
        self.assertEqual("td", self.entries["14"].loci[0].unit_tag)
        self.assertEqual({"fig"}, flags["15"])
        self.assertIn("box", flags["9"])
        self.assertFalse(self.entries["16"].cited_in_body)
        self.assertEqual(1, self.entries["16"].outside_body_loci)
        self.assertFalse(self.entries["17"].cited_in_body)
        self.assertEqual(0, self.entries["17"].outside_body_loci)

    def test_diagnostics_and_gate(self):
        diagnostics = self.review["diagnostics"]
        self.assertEqual((16, 16, 0), (diagnostics["anchors"], diagnostics["ownedAnchors"], diagnostics["uncovered"]))
        self.assertEqual((17, 15, 1), (diagnostics["entries"], diagnostics["citedInBody"], diagnostics["outsideBodyLoci"]))
        self.assertTrue(X.uncovered_gate(self.review)["ok"])
        broken = {"diagnostics": {**diagnostics, "uncovered": 2}}
        self.assertFalse(X.uncovered_gate(broken)["ok"])

    def test_unresolved_targets_fail_the_gate(self):
        text = (FIXTURES / "review.xml").read_text().replace('rid="B13">13', 'rid="B99">13')
        broken = X.extract_review(text.encode())
        self.assertFalse(X.uncovered_gate(broken)["ok"])
        self.assertFalse({entry.label: entry for entry in broken["entries"]}["13"].cited_in_body)

    def test_metadata(self):
        self.assertEqual({"country": "Italy", "source": "jats-corresponding-affiliation"}, self.review["country"])
        self.assertEqual("breast", self.review["topic"])
        self.assertEqual(X.TOPIC_MAP_VERSION, self.review["topicMap"])


class HtmlExtractionTests(unittest.TestCase):
    def test_publisher_html_matches_the_jats_reading(self):
        html, jats = review("review.html"), review("review.xml")
        entries = {entry.label: entry for entry in html["entries"]}
        self.assertEqual("Zeta-kinase is a fictional enzyme⟨CR1⟩ .", entries["1"].loci[0].sentence)
        self.assertEqual("ref-CR1", entries["1"].target)
        self.assertFalse(entries["16"].cited_in_body)
        self.assertEqual({"country": "Italy", "source": "html-meta-first-institution"}, html["country"])
        result = X.cross_validate(jats, html)
        self.assertEqual((19, 19, 1.0, 1.0), (result["jatsLoci"], result["matched"], result["share"], result["labelJaccard"]))
        self.assertTrue(X.cross_validation_gate([result])["ok"])

    def test_cross_validation_detects_shifted_numbering(self):
        html = (FIXTURES / "review.html").read_text().replace("#ref-CR12", "#ref-CR13")
        shifted = X.extract_review(html.encode(), html=True)
        result = X.cross_validate(review("review.xml"), shifted)
        self.assertLess(result["share"], 1.0)
        self.assertFalse(X.cross_validation_gate([result], minimum_median=0.99)["ok"])
        self.assertFalse(X.cross_validation_gate([])["ok"])


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.entries = {entry.label: entry for entry in review()["entries"]}
        works = json.loads((FIXTURES / "review_works.json").read_text())["works"]
        self.works = {key: {k: work[k] for k in ("doi", "pmid", "title")} for key, work in works.items()}

    def test_identity_ladder(self):
        resolved = X.resolve_identity(self.entries["1"], self.works)
        self.assertEqual(("work-1", "resolved", "markup-doi", True),
                         (resolved["workKey"], resolved["identity"], resolved["method"], resolved["titleAgrees"]))
        by_pmid = X.resolve_identity(self.entries["3"], self.works)
        self.assertEqual(("work-3", "markup-pmid"), (by_pmid["workKey"], by_pmid["method"]))
        printed = X.resolve_identity(self.entries["12"], self.works)
        self.assertEqual(("work-12", "printed-doi"), (printed["workKey"], printed["method"]))
        missing = X.resolve_identity(self.entries["17"], self.works)
        self.assertEqual(("unresolved", None), (missing["identity"], missing["workKey"]))

    def test_identifier_without_title_agreement_is_unverified(self):
        works = {**self.works, "work-1": {**self.works["work-1"], "title": "A completely different study of something"}}
        self.assertEqual("resolved_unverified", X.resolve_identity(self.entries["1"], works)["identity"])

    def test_title_and_label_fallbacks_and_conflicts(self):
        works = {"w": {"doi": None, "pmid": None, "title": "Never cited source without identifiers"}}
        self.assertEqual(("w", "title", "resolved"), tuple(X.resolve_identity(self.entries["17"], works)[k]
                                                          for k in ("workKey", "method", "identity")))
        label = X.resolve_identity(self.entries["17"], {"w": {"title": "unrelated"}}, numbering_verified=True,
                                   by_label={"17": "w"})
        self.assertEqual(("w", "label", "resolved_unverified"), (label["workKey"], label["method"], label["identity"]))
        self.assertEqual("unresolved", X.resolve_identity(self.entries["17"], {"w": {"title": "unrelated"}},
                                                          by_label={"17": "w"})["identity"])
        conflict = {"a": {"doi": "10.5555/w2", "title": "x"}, "b": {"pmid": "100002", "title": "y"}}
        result = X.resolve_identity(self.entries["2"], conflict)
        self.assertEqual(("unresolved", "conflict"), (result["identity"], result["method"]))

    def test_printed_doi_keeps_balanced_parentheses(self):
        self.assertEqual("10.1016/s0140-6736(20)30183-5", X.printed_doi("Lancet. doi:10.1016/S0140-6736(20)30183-5."))
        self.assertEqual("10.5555/abc", X.printed_doi("(see 10.5555/abc)."))
        self.assertIsNone(X.printed_doi("no identifier"))


class MetadataTests(unittest.TestCase):
    def test_topic_map_is_first_match(self):
        self.assertEqual("breast", X.topic("Breast and lung tumours"))
        self.assertEqual("immunotherapy (cross-cutting)", X.topic("Checkpoint blockade advances"))
        self.assertEqual("other", X.topic("A general overview"))

    def test_country_normalisation(self):
        self.assertEqual("USA", X.normalize_country("United States."))
        self.assertEqual("China", X.normalize_country("P.R. China"))
        self.assertEqual("Narnia", X.normalize_country("Narnia"))
        self.assertIsNone(X.normalize_country(""))

    def test_label_collisions_are_rejected(self):
        text = (FIXTURES / "review.xml").read_text().replace("<label>2.</label>", "<label>1a</label>").replace(
            "<label>3.</label>", "<label>1a.</label>")
        with self.assertRaises(ValidationError):
            X.extract_review(text.encode())


if __name__ == "__main__":
    unittest.main()
