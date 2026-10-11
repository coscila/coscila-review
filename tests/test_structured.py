# SPDX-License-Identifier: Apache-2.0
"""Apache-2.0 synthetic fixtures; no publisher content is redistributed."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def article(body: str, *, numbering=(1, 2, 3)) -> str:
    refs = "".join(f'<ref id="R{i}"><label>{label}</label><mixed-citation>Author{i} A. Example source {i}. 2024. '
                   f'<pub-id pub-id-type="doi">10.1234/source{i}</pub-id></mixed-citation></ref>'
                   for i, label in enumerate(numbering, 1))
    return '<article><front><article-meta><article-id pub-id-type="doi">10.1234/review</article-id></article-meta></front>' + \
        '<body><sec><title>Background</title>' + body + '</sec></body><back><ref-list>' + refs + '</ref-list></back></article>'


class StructuredTests(unittest.TestCase):
    def ingest(self, text: str, suffix=".xml"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("paper" + suffix)
            path.write_text(text)
            value = ingest_manuscript(path)
        jsonschema.validate(value, json.loads((ROOT / "schemas/manuscript.schema.json").read_text()))
        return value

    def test_superscript_tail_order_and_no_locus_cap(self):
        body = '<p>First finding<sup><xref ref-type="bibr" rid="R1">1</xref></sup>. Second finding <xref ref-type="bibr" rid="R2">2</xref>.</p>'
        body += ''.join('<p>Repeated finding <xref ref-type="bibr" rid="R1">1</xref>.</p>' for _ in range(5))
        value = self.ingest(article(body))
        self.assertEqual(7, len(value["citationClusters"]))
        self.assertEqual("First finding[1].", value["sentences"][0]["text"])
        self.assertEqual(["ref-00001"], value["citationClusters"][0]["referenceIds"])
        self.assertEqual(7, value["structuredSource"]["ownedAnchors"])
        self.assertEqual(7, len(build_claim_inventory(value)["citationClusters"]))

    def test_ranges_and_display_blocks_owned_once(self):
        body = '<p>Findings <xref ref-type="bibr" rid="R1">1</xref>–<xref ref-type="bibr" rid="R3">3</xref>.</p>'
        body += '<table-wrap><caption><p>Caption <xref ref-type="bibr" rid="R2">2</xref>.</p></caption><table><tr><td>Cell <xref ref-type="bibr" rid="R1">1</xref>.</td></tr></table></table-wrap>'
        body += '<fig><caption><p>Figure <xref ref-type="bibr" rid="R3">3</xref>.</p></caption></fig>'
        body += '<boxed-text><p>Box <xref ref-type="bibr" rid="R2">2</xref>.</p></boxed-text><list><list-item><p>List <xref ref-type="bibr" rid="R1">1</xref>.</p></list-item></list>'
        value = self.ingest(article(body))
        self.assertEqual(["R1", "R2", "R3"], value["citationClusters"][0]["sourceTargets"])
        self.assertEqual(6, len(value["citationClusters"]))
        self.assertTrue(value["ingestion"]["complete"])
        self.assertEqual(7, value["structuredSource"]["anchors"])
        self.assertEqual(7, value["structuredSource"]["ownedAnchors"])

    def test_single_anchor_range_and_missing_target(self):
        value = self.ingest(article('<p>Range <xref ref-type="bibr" rid="R1 R3">1–3</xref>.</p><p>Missing <xref ref-type="bibr" rid="missing">4</xref>.</p>'))
        self.assertEqual(3, len(value["citationClusters"][0]["referenceIds"]))
        self.assertFalse(value["ingestion"]["complete"])
        self.assertEqual(["missing"], value["citationClusters"][1]["unresolvedMarkers"])

    def test_punctuated_bibliography_labels_expand_both_range_forms(self):
        # Real Frontiers markup uses labels such as "1.". Text remains original.
        body = '<p>First findings (<xref ref-type="bibr" rid="R1">1</xref>–<xref ref-type="bibr" rid="R3">3</xref>).</p>'
        body += '<p>Other findings <xref ref-type="bibr" rid="R1 R3">1–3</xref>.</p>'
        for labels in [("1.", "2.", "3."), ("1)", "2)", "3)"), ("[1]", "[2]", "[3]")]:
            with self.subTest(labels=labels):
                value = self.ingest(article(body, numbering=labels))
                self.assertTrue(value["ingestion"]["complete"])
                self.assertEqual(2, len(value["citationClusters"]))
                for cluster in value["citationClusters"]:
                    self.assertEqual(["R1", "R2", "R3"], cluster["sourceTargets"])
                    self.assertEqual(["ref-00001", "ref-00002", "ref-00003"], cluster["referenceIds"])
                self.assertEqual(list(labels), [entry["sourceIdentity"]["printedLabel"]
                                               for entry in value["bibliographyEntries"]])
                self.assertEqual(3, value["structuredSource"]["ownedAnchors"])

    def test_punctuated_ranges_do_not_hide_missing_or_ambiguous_members(self):
        body = '<p>Findings <xref ref-type="bibr" rid="R1">1</xref>–<xref ref-type="bibr" rid="R2">3</xref>.</p>'
        value = self.ingest(article(body, numbering=("1.", "3.")))
        self.assertFalse(value["ingestion"]["complete"])
        self.assertIn("<missing-label:2>", value["citationClusters"][0]["unresolvedMarkers"])
        with self.assertRaisesRegex(ValidationError, "ambiguous numeric labels"):
            self.ingest(article(body, numbering=("1", "1.", "3.")))

    def test_single_pmc_article_set_preserves_identity_and_original_paths(self):
        text = article('<p>Finding <xref ref-type="bibr" rid="R1">1</xref>.</p>')
        value = self.ingest('<pmc-articleset>' + text + '</pmc-articleset>')
        self.assertTrue(value["ingestion"]["complete"])
        self.assertEqual("10.1234/review", value["structuredSource"]["publication"]["doi"])
        anchor = value["citationClusters"][0]["sourceAnchors"][0]["elementPath"]
        self.assertTrue(anchor.startswith('/pmc-articleset[1]/article[1]/body[1]/'))
        with self.assertRaisesRegex(ValidationError, "exactly one article"):
            self.ingest('<pmc-articleset>' + text + text + '</pmc-articleset>')
        with self.assertRaisesRegex(ValidationError, "exactly one article"):
            self.ingest('<pmc-articleset/>')

    def test_publisher_html_uses_target_identity_not_numbering(self):
        text = '''<html><head><meta name="citation_doi" content="10.1234/review"></head><body><article><h2>Background</h2><p>Finding <sup><a href="#ref-B" class="bibr">7</a></sup>. Other <a href="#ref-A" class="bibr">3</a>.</p><ol class="references"><li id="ref-A" data-label="3">Alpha A. First source. <a href="https://doi.org/10.1234/source1">DOI</a></li><li id="ref-B" data-label="7">Beta B. Second source. <a href="https://doi.org/10.1234/source2">DOI</a></li></ol></article></body></html>'''
        value = self.ingest(text, ".html")
        self.assertEqual(["ref-00002"], value["citationClusters"][0]["referenceIds"])
        self.assertEqual("7", value["bibliographyEntries"][1]["sourceIdentity"]["printedLabel"])
        self.assertEqual(["10.1234/source2"], value["bibliographyEntries"][1]["identifiers"]["doi"])
        self.assertEqual("10.1234/review", value["structuredSource"]["publication"]["citation_doi"])

    def test_publisher_paragraph_targets_inside_unidentified_reference_items(self):
        text = '''<html><body><article><p>Finding <a href="#ref-CR1">1</a>–<a href="#ref-CR3">3</a>.</p>
        <section data-title="References"><ol class="c-article-references">
        <li class="c-article-references__item" data-counter="1."><p class="c-article-references__text" id="ref-CR1">Alpha A. Source one. <a href="https://doi.org/10.1234/source1">DOI</a></p></li>
        <li class="c-article-references__item" data-counter="2."><p class="c-article-references__text" id="ref-CR2">Beta B. Source two.</p></li>
        <li class="c-article-references__item" data-counter="3."><p class="c-article-references__text" id="ref-CR3">Gamma C. Source three.</p></li>
        <li class="c-article-references__item" data-counter="4."><p class="c-article-references__text" id="ref-CR4">Delta D. Uncited source four.</p></li>
        </ol></section></article></body></html>'''
        value = self.ingest(text, '.html')
        self.assertTrue(value["ingestion"]["complete"])
        self.assertEqual(4, len(value["bibliographyEntries"]))
        self.assertEqual(["ref-CR1", "ref-CR2", "ref-CR3"], value["citationClusters"][0]["sourceTargets"])
        self.assertEqual(["1.", "2.", "3.", "4."], [entry["sourceIdentity"]["printedLabel"]
                                                  for entry in value["bibliographyEntries"]])
        self.assertEqual(["10.1234/source1"], value["bibliographyEntries"][0]["identifiers"]["doi"])
        self.assertEqual(1, len(value["citationClusters"]))

    def test_entities_and_html_without_article_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.ingest('<!DOCTYPE article [<!ENTITY x "x">]>' + article('<p>&x;</p>'))
        with self.assertRaises(ValidationError):
            self.ingest('<html><body><p>Preview.</p></body></html>', '.html')

    def test_front_abstract_back_footnote_and_original_anchor_paths_survive(self):
        text = article('<p id="main">Body finding <xref ref-type="bibr" rid="R2">2</xref>.</p>')
        text = text.replace('</article-meta>', '<abstract id="A1"><p>Abstract finding <xref ref-type="bibr" rid="R1">1</xref>.</p></abstract></article-meta>')
        text = text.replace('<back>', '<back><fn-group><fn id="N1"><p>Back note <xref ref-type="bibr" rid="R3">3</xref>.</p></fn></fn-group>')
        value = self.ingest(text)
        self.assertEqual(3, len(value["citationClusters"]))
        self.assertTrue(value["ingestion"]["complete"])
        self.assertEqual(["ref-00001"], value["citationClusters"][0]["referenceIds"])
        self.assertIn('/front[1]/article-meta[1]/abstract[1]/p[1]/xref[1]', value["citationClusters"][0]["sourceAnchors"][0]["elementPath"])
        body = next(paragraph for paragraph in value["paragraphs"] if paragraph["text"].startswith('Body finding'))
        self.assertEqual("main", body["sourceLocator"]["elementId"])
        self.assertIn('/back[1]/fn-group[1]/fn[1]/p[1]/xref[1]', value["citationClusters"][-1]["sourceAnchors"][0]["elementPath"])


if __name__ == "__main__":
    unittest.main()
