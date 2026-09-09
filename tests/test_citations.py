from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ocsl_review.citations import numeric_citations, parse_numeric_group
from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.ingest import _assemble, _bbox_blocks, ingest_manuscript


def _pdf_bytes(commands: str) -> bytes:
    """Independently authored one-page PDF, with explicit character geometry."""
    stream = commands.encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{number} 0 obj\n".encode() + value + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    result += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return result


def _line(*words: tuple[str, float, float, float, float]) -> str:
    return "<line>" + "".join(
        f'<word xMin="{x0}" yMin="{y0}" xMax="{x1}" yMax="{y1}">{value}</word>'
        for value, x0, y0, x1, y1 in words
    ) + "</line>"


class CitationGrammarTests(unittest.TestCase):
    def test_range_and_list_variants_reach_the_scanner(self):
        for text in ("[1-10]", "[1–10]", "[1—10]", "[1−10]", "[1‑10]"):
            with self.subTest(text=text):
                markers = numeric_citations("Prior work " + text + ".")
                self.assertEqual(1, len(markers))
                self.assertEqual(tuple(str(i) for i in range(1, 11)), markers[0].tokens)
                self.assertEqual(text, markers[0].raw)
        for text in ("[1, 3; 5–7 and 9]", "(1, 3; 5–7 and 9)"):
            self.assertEqual(("1", "3", "5", "6", "7", "9"), numeric_citations(text)[0].tokens)

    def test_shortened_range_needs_bibliography_corroboration(self):
        tokens, issues, compressed = parse_numeric_group("[123–5]")
        self.assertEqual(("123", "124", "125"), tokens)
        self.assertTrue(compressed)
        self.assertEqual(("compressed-citation-range-unconfirmed",), issues)
        self.assertEqual((), parse_numeric_group("[123–5]", labels=tokens)[1])
        self.assertTrue(parse_numeric_group("[123–5]", labels=["123", "125"])[1])
        self.assertEqual(150, len(parse_numeric_group("[1-150]")[0]))

    def test_malformed_and_excessive_groups_are_not_silently_partially_parsed(self):
        for raw in ("[1 2]", "[3,]", "[3-2]", "[0]", "[1a-2b]", "[1-100000000]"):
            with self.subTest(raw=raw):
                markers = numeric_citations(raw)
                self.assertEqual(1, len(markers))
                self.assertTrue(markers[0].issues)
                self.assertEqual((), markers[0].tokens)

    def test_superscripts_and_run_bridges_preserve_original_offsets(self):
        text = "Prior work¹–¹⁰."
        marker = numeric_citations(text)[0]
        self.assertEqual("¹–¹⁰", text[marker.start:marker.end])
        self.assertEqual(10, len(marker.tokens))
        text = "Prior work 2,7–9."
        start = text.index("2")
        markers = numeric_citations(text, superscript_spans=[(start, start + 3), (start + 4, start + 5)])
        self.assertEqual(1, len(markers))
        self.assertEqual(("2", "7", "8", "9"), markers[0].tokens)

    def test_negative_controls_detect_no_citations(self):
        text = "Fig. 4, Table 2, page 23, miR-12a, x², cm³, CI [0, 1], (2024), (n=3), [p<0.05]."
        self.assertEqual([], numeric_citations(text))


class CitationIntegrationTests(unittest.TestCase):
    def _ingest(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "paper.md"
            source.write_text(text, encoding="utf-8")
            return ingest_manuscript(source)

    def test_ten_references_are_all_bound_and_missing_members_block_exact(self):
        text = "# Introduction\n\nEarlier work [1-10].\n\n# References\n\n"
        references = "\n".join(f"{i}. Author. Source {i}. 2024." for i in range(1, 11))
        model = self._ingest(text + references)
        graph = build_claim_inventory(model)
        self.assertEqual(1, len(graph["citationClusters"]))
        self.assertEqual(10, len(graph["citationClusters"][0]["referenceIds"]))
        self.assertEqual("exact", graph["claims"][0]["citationBindingStatus"])
        self.assertEqual(10, model["ingestion"]["coverage"]["citationReferenceLinks"])
        incomplete = self._ingest(text + "1. Author. First.\n10. Author. Last.")
        graph = build_claim_inventory(incomplete)
        self.assertFalse(incomplete["ingestion"]["complete"])
        self.assertEqual("ambiguous", graph["claims"][0]["citationBindingStatus"])
        self.assertEqual([str(i) for i in range(2, 10)], graph["citationClusters"][0]["unresolvedMarkers"])

    def test_duplicate_labels_are_not_resolved_by_overwriting(self):
        model = self._ingest("# Introduction\n\nEvidence [1].\n\n# References\n\n1. First.\n1. Second.")
        graph = build_claim_inventory(model)
        self.assertEqual(2, len(graph["references"]))
        self.assertEqual([], graph["citationClusters"][0]["referenceIds"])
        self.assertFalse(model["ingestion"]["complete"])

    def test_numbered_bibliography_continuations_preserve_all_paragraphs(self):
        model = self._ingest(
            "# Introduction\n\nEvidence [1–2].\n\n# References\n\n"
            "1 Author. First source.\n\nJournal details. doi:10.1234/first\n\n"
            "2 Author. Second source."
        )
        references = model["bibliographyEntries"]
        self.assertEqual(2, len(references))
        self.assertEqual(2, len(references[0]["paragraphIds"]))
        self.assertIn("Journal details", references[0]["raw"])
        self.assertEqual(["10.1234/first"], references[0]["identifiers"]["doi"])
        self.assertTrue(model["ingestion"]["complete"])

    def test_clause_binding_and_semicolons_inside_markers(self):
        model = self._ingest("# Introduction\n\nFirst result [1; 2]; second result [3].\n\n# References\n\n1. First.\n2. Second.\n3. Third.")
        graph = build_claim_inventory(model)
        self.assertEqual(["First result", "second result"], [claim["text"] for claim in graph["claims"]])
        self.assertEqual([["cite-000001"], ["cite-000002"]], [claim["citationClusterIds"] for claim in graph["claims"]])

    def test_citation_after_sentence_punctuation_stays_with_its_sentence(self):
        for source in ("First result.[1] Second result.[2]", "First result. [1] Second result. [2]"):
            with self.subTest(source=source):
                model = self._ingest("# Introduction\n\n" + source + "\n\n# References\n\n1. First.\n2. Second.")
                graph = build_claim_inventory(model)
                self.assertEqual(["First result", "Second result"], [item["text"] for item in graph["claims"]])
                self.assertEqual([["cite-000001"], ["cite-000002"]], [claim["citationClusterIds"] for claim in graph["claims"]])

    def test_table_reference_column_is_bound_to_its_row(self):
        model = self._ingest("# Introduction\n\n| Study | Finding | Ref. |\n| --- | --- | --- |\n| Alpha | Increased response | 1–3 |\n| Beta | Decreased response [2] | 2 |\n\n# References\n\n1. First.\n2. Second.\n3. Third.")
        graph = build_claim_inventory(model)
        self.assertEqual(2, len(graph["claims"]))
        self.assertEqual("Study: Alpha; Finding: Increased response", graph["claims"][0]["text"])
        self.assertEqual("exact", graph["claims"][0]["citationBindingStatus"])
        self.assertEqual(3, len(graph["citationClusters"][0]["referenceIds"]))
        self.assertEqual(2, graph["citationClusters"][0]["locator"]["row"])
        self.assertEqual("table-cell", graph["citationClusters"][0]["locusType"])

    def test_pdf_geometry_recovers_a_reference_table(self):
        xml = '<html><body><doc><page width="600" height="800"><flow><block>' + "".join([
            _line(("Study", 40, 100, 70, 110), ("Finding", 150, 100, 190, 110), ("Ref.", 350, 100, 375, 110)),
            _line(("Alpha", 40, 120, 70, 130), ("Increased", 150, 120, 200, 130), ("1-3", 350, 120, 367, 130)),
            _line(("Beta", 40, 140, 65, 150), ("Decreased", 150, 140, 200, 150), ("4", 350, 140, 356, 150)),
        ]) + '</block></flow></page></doc></body></html>'
        warnings = []
        blocks = _bbox_blocks(xml, warnings)
        self.assertEqual([], warnings)
        self.assertEqual(1, len(blocks))
        self.assertEqual("table", blocks[0]["kind"])
        self.assertEqual(["Alpha", "Increased", "1-3"], blocks[0]["rows"][1])
        reference_blocks = [
            {"kind": "paragraph", "text": "References", "heading": True},
            *[{"kind": "paragraph", "text": f"{i}. Author. Source."} for i in range(1, 5)],
        ]
        model = _assemble(source=Path("fixture.pdf"), source_data=xml.encode(), source_format="pdf", parser="bbox-test", blocks=blocks + reference_blocks, confidential=True, warnings=warnings)
        graph = build_claim_inventory(model)
        self.assertEqual(2, len(graph["claims"]))
        self.assertEqual(3, len(graph["citationClusters"][0]["referenceIds"]))

    def test_pdf_table_wrapped_reference_continues_the_same_row_once(self):
        xml = '<html><page width="600" height="800"><block>' + "".join([
            _line(("Study", 40, 100, 70, 110), ("Finding", 150, 100, 190, 110), ("Ref.", 350, 100, 375, 110)),
            _line(("Alpha", 40, 120, 70, 130), ("Increased", 150, 120, 200, 130), ("1 and", 350, 120, 377, 130)),
            _line(("response", 150, 133, 190, 143), ("2", 350, 133, 356, 143)),
        ]) + '</block></page></html>'
        warnings = []
        blocks = _bbox_blocks(xml, warnings)
        self.assertEqual([], warnings)
        self.assertEqual([["Study", "Finding", "Ref."], ["Alpha", "Increased response", "1 and 2"]], blocks[0]["rows"])
        self.assertEqual(("1", "2"), parse_numeric_group(blocks[0]["rows"][1][2])[0])

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler integration requires pdftotext")
    def test_real_pdf_superscript_range_two_columns_and_negative_controls(self):
        commands = "\n".join([
            "BT /F1 12 Tf 40 740 Td (Introduction) Tj ET",
            "BT /F1 12 Tf 40 705 Td (Earlier evidence) Tj ET",
            "BT /F1 8 Tf 125 710 Td (1) Tj ET",
            "BT /F1 8 Tf 130 710 Td (-) Tj ET",
            "BT /F1 8 Tf 133 710 Td (3) Tj ET",
            "BT /F1 12 Tf 340 705 Td (Later evidence) Tj ET",
            "BT /F1 8 Tf 420 710 Td (4) Tj ET",
            "BT /F1 8 Tf 40 670 Td (Fig. 2 and miR-12a) Tj ET",
            "BT /F1 12 Tf 40 600 Td (References) Tj ET",
            *[f"BT /F1 12 Tf 40 {575 - i * 20} Td ({i}. Author. Source.) Tj ET" for i in range(1, 5)],
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "columns.pdf"
            path.write_bytes(_pdf_bytes(commands))
            model = ingest_manuscript(path)
        self.assertEqual([list(map(str, range(1, 4))), ["4"]], [marker["tokens"] for marker in model["citationClusters"]])
        self.assertTrue(all(marker.get("boundingBoxes") for marker in model["citationClusters"]))
        self.assertTrue(model["ingestion"]["complete"])
        claims = build_claim_inventory(model)["claims"]
        self.assertEqual(["cite-000001"], next(c for c in claims if "Earlier" in c["text"])["citationClusterIds"])
        self.assertEqual(["cite-000002"], next(c for c in claims if "Later" in c["text"])["citationClusterIds"])


if __name__ == "__main__":
    unittest.main()
