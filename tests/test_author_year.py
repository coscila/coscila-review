# SPDX-License-Identifier: Apache-2.0
"""Author-year detection and resolution; synthetic fixtures only."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import jsonschema

from ocsl_review import author_year as A
from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.ingest import ingest_manuscript

import author_year_fixture as F

ROOT = Path(__file__).resolve().parents[1]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _items(text: str) -> list[tuple[str, tuple[str, ...], bool, str | None, str]]:
    return [(item.text, item.names, item.et_al, item.year, item.suffix)
            for marker in A.detect(text) for item in marker.items]


class DetectionTests(unittest.TestCase):
    def test_parenthetical_groups_with_prefixes_suffixes_and_locators(self):
        text = "Prior work (e.g., Smith et al., 2020a, b; see also Lee & Park 2019, p. 12) differs."
        markers = A.detect(text)
        self.assertEqual(1, len(markers))
        self.assertEqual("parenthetical", markers[0].form)
        self.assertEqual(text[markers[0].start:markers[0].end], markers[0].raw)
        self.assertEqual([("Smith",), ("Smith",), ("Lee", "Park")], [item.names for item in markers[0].items])
        self.assertEqual(["a", "b", ""], [item.suffix for item in markers[0].items])
        self.assertEqual("p. 12", markers[0].items[2].locator)
        for item in markers[0].items:
            self.assertEqual(item.text, text[item.start:item.end])

    def test_narrative_forms_keep_the_author_outside_the_marker_span(self):
        text = "Smith and Jones (2019) argued this, Lee's (2018a, b) model agreed and van der Berg et al. (2017) did not."
        markers = A.detect(text)
        self.assertEqual(["narrative"] * 3, [marker.form for marker in markers])
        self.assertEqual(["(2019)", "(2018a, b)", "(2017)"], [marker.raw for marker in markers])
        self.assertEqual("Smith and Jones", text[markers[0].author_start:markers[0].author_end])
        self.assertEqual(("van der Berg",), markers[2].items[0].names)
        self.assertTrue(markers[2].items[0].et_al)
        self.assertTrue(all(marker.strong for marker in markers))

    def test_non_citation_parentheses_are_not_markers(self):
        for text in ("The drug was approved (approved in 2018).", "Enrolment (n = 2020) was high.",
                     "Data from June (2020) were used.", "As shown (Fig. 3, 2020 data).", "Between (2019-2020)."):
            with self.subTest(text=text):
                self.assertEqual([], A.detect(text))

    def test_weak_narrative_candidates_are_flagged(self):
        markers = A.detect("In the United States (2020) incidence rose.")
        self.assertEqual(1, len(markers))
        self.assertFalse(markers[0].strong)

    def test_special_years_initials_and_organisations(self):
        self.assertEqual([("Smith", True, None)], [(i.names[0], i.et_al, i.year) for m in A.detect("Smith et al. (in press) agree.")
                                                  for i in m.items])
        self.assertEqual([("Jones",), ("Smith",)], [i.names for m in A.detect("(Jones, n.d.; Smith, J., 2020)") for i in m.items])
        parses = A.detect("(World Health Organization [WHO], 2019)")[0].items[0].alternatives
        self.assertIn(A.AuthorParse(("World Health Organization",), False, True), parses)
        self.assertIn(A.AuthorParse(("WHO",), False, True), parses)


class ReferenceParsingTests(unittest.TestCase):
    def test_common_author_year_and_numbered_formats(self):
        cases = {
            "Smith, J. A., Jones, B. B., & Lee, C. (2020a). Title. Journal, 12(3), 45-67.": (("Smith", "Jones", "Lee"), "2020", "a"),
            "Smith, J.A., Jones, B.B. and Lee, C. (2020) 'Title', Journal, 12, pp. 45-67.": (("Smith", "Jones", "Lee"), "2020", ""),
            "Smith JA, Jones BB, Lee C, et al. (2020) Title. J Name 12:45-67": (("Smith", "Jones", "Lee"), "2020", ""),
            "Smith, J.A., Jones, B.B., Lee, C., 2020b. Title. J. Name 12, 45-67.": (("Smith", "Jones", "Lee"), "2020", "b"),
            "Smith, John A., Bob B. Jones, and Carol van der Lee. 2020. \"Title.\" Journal 12 (3): 45-67.": (("Smith", "Jones", "van der Lee"), "2020", ""),
            "Smith, John, Lee, Carol, & Ng, Kim (2021). Title. Journal, 1, 2.": (("Smith", "Lee", "Ng"), "2021", ""),
            "Alder, A. D., Brook, J. M. & Cove, M. J. A guide to placeholders. Ex. Rev. 20, 651-668 (2020).": (("Alder", "Brook", "Cove"), "2020", ""),
            "Dale MS, Eyre SA, Fenn CJ. Synthetic activity. Ex Med. 2015;160:48-61.": (("Dale", "Eyre", "Fenn"), "2015", ""),
            "1. Nolan A. A bounded association study. Journal of Example Methods. 2022.": (("Nolan",), "2022", ""),
        }
        for raw, (names, year, suffix) in cases.items():
            with self.subTest(raw=raw):
                parsed = A.parse_reference(raw)
                self.assertEqual(names, parsed.names)
                self.assertEqual((year, suffix), (parsed.year, parsed.suffix))
        self.assertTrue(A.parse_reference("Smith JA, Jones BB, Lee C, et al. (2020) T.").et_al)

    def test_organisations_and_special_years(self):
        parsed = A.parse_reference("World Health Organization (WHO). (2019). Global report. Geneva: WHO.")
        self.assertTrue(parsed.corporate)
        self.assertEqual(("World Health Organization",), parsed.names)
        self.assertIn("WHO", parsed.aliases)
        self.assertEqual("in press", A.parse_reference("O'Brien, P. (in press). Work. Journal.").special)

    def test_name_keys_fold_diacritics_particles_and_hyphens(self):
        self.assertEqual(A.name_key("Ødegaard"), A.name_key("Odegaard"))
        self.assertEqual(A.name_key("García-López"), A.name_key("Garcia Lopez"))
        self.assertEqual(A.core_key("van der Berg"), A.core_key("Berg"))
        self.assertEqual(A.name_key("Łukasiewicz"), A.name_key("Lukasiewicz"))


class ResolutionTests(unittest.TestCase):
    REFERENCES = [
        {"id": "r1", "raw": "Smith, J. A., Jones, B. B., & Lee, C. (2020a). Alpha. J, 1, 1."},
        {"id": "r2", "raw": "Smith, J. A., Ng, D., & Park, D. (2020b). Beta. J, 1, 1."},
        {"id": "r3", "raw": "Smith, J. (2019). Gamma. J, 1, 1."},
        {"id": "r4", "raw": "Lee, C., & Park, D. (2019). Delta. J, 1, 1."},
        {"id": "r5", "raw": "World Health Organization. (2019). Global report. Geneva: WHO."},
        {"id": "r6", "raw": "van der Berg, A., Smith, J., & Ng, K. (2017). Epsilon. J, 1, 1."},
        {"id": "r7", "raw": "Kim, S., & Choi, H. (2021). Iota. J."},
        {"id": "r8", "raw": "Kim, S., & Park, H. (2021). Kappa. J."},
        {"id": "r9", "raw": "Müller, K. (2016). Lambda. J."},
        {"id": "r10", "raw": "Doe, J. (2010). Mu. J."},
    ]

    def resolve(self, text: str) -> list[A.Resolution]:
        index = A.ReferenceIndex(self.REFERENCES)
        return [index.resolve(item) for marker in A.detect(text) for item in marker.items]

    def test_unique_bindings(self):
        cases = {
            "(Smith et al., 2020a; Lee & Park 2019)": ["r1", "r4"],
            "Smith (2019) showed": ["r3"],
            "(WHO, 2019)": ["r5"],
            "the World Health Organization (2019) recommends": ["r5"],
            "Van der Berg et al. (2017)": ["r6"],
            "(Berg et al., 2017)": ["r6"],
            "Kim and Choi (2021)": ["r7"],
            "(Kim & Park, 2021)": ["r8"],
            "(Muller, 2016)": ["r9"],
            "Recently Smith (2019) found": ["r3"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                resolutions = self.resolve(text)
                self.assertEqual(["resolved"] * len(expected), [r.status for r in resolutions])
                self.assertEqual(expected, [r.reference_id for r in resolutions])

    def test_ambiguity_is_never_guessed(self):
        for text in ("(Smith, 2020)", "(Kim, 2021)", "(Smith et al., 2020)"):
            with self.subTest(text=text):
                resolutions = self.resolve(text)
                self.assertEqual("ambiguous", resolutions[0].status)
                self.assertIsNone(resolutions[0].reference_id)
                self.assertGreaterEqual(len(resolutions[0].candidates), 2)

    def test_unlisted_works_stay_unresolved_with_hints(self):
        resolution = self.resolve("(Doe, 2011)")[0]
        self.assertEqual("unresolved", resolution.status)
        self.assertEqual([{"referenceId": "r10", "hint": "year-differs", "printedYear": "2010"}],
                         [dict(hint) for hint in resolution.near])
        self.assertEqual("unresolved", self.resolve("(Smith, 2020c)")[0].status)

    def test_suffix_order_is_inferred_only_without_printed_suffixes(self):
        index = A.ReferenceIndex([
            {"id": "a", "raw": "Rossi, M. (2018). First title. J."},
            {"id": "b", "raw": "Rossi, M. (2018). Second title. J."},
        ])
        items = [item for marker in A.detect("(Rossi, 2018a, 2018b)") for item in marker.items]
        resolutions = [index.resolve(item) for item in items]
        self.assertEqual(["a", "b"], [r.reference_id for r in resolutions])
        self.assertTrue(all(r.method.endswith("suffix-by-order") for r in resolutions))


class IngestIntegrationTests(unittest.TestCase):
    def ingest(self, name: str, content: str | bytes) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
            manuscript = ingest_manuscript(path)
        schema = json.loads((ROOT / "schemas/manuscript.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(manuscript)
        return manuscript

    MARKDOWN = """# Introduction

Prior work (Smith et al., 2020a; Lee & Park 2019) showed X. Smith and Park (2020b) argued Y. In the United States (2020) rates rose.
The World Health Organization (2019) recommends Z (WHO, 2019). A missing one (Doe, 2011) and an ambiguous one (Smith, 2020).

## References

- Smith, J. A., Jones, B. B., & Lee, C. (2020a). Alpha. J, 1, 1.
- Smith, J. A., & Park, D. (2020b). Beta. J, 1, 1.
- Lee, C., & Park, D. (2019). Delta. J, 1, 1.
- World Health Organization. (2019). Global report. Geneva: WHO.
- Unused, Q. (2001). Never cited. J, 1, 1.
"""

    def test_markdown_binding_bullets_and_renumbering(self):
        manuscript = self.ingest("paper.md", self.MARKDOWN)
        self.assertEqual(5, len(manuscript["bibliographyEntries"]))
        self.assertTrue(manuscript["bibliographyEntries"][0]["raw"].startswith("Smith, J. A."))
        clusters = manuscript["citationClusters"]
        self.assertEqual([f"cite-{n:06d}" for n in range(1, 7)], [c["id"] for c in clusters])
        self.assertEqual([1, 2, 3, 4, 5, 6], [c["ordinal"] for c in clusters])
        self.assertNotIn("(2020)", [c["raw"] for c in clusters])  # "United States (2020)" is prose
        bound = {c["raw"]: (c["referenceIds"], c["bindingStatus"]) for c in clusters}
        self.assertEqual((["ref-00001", "ref-00003"], "exact"), bound["(Smith et al., 2020a; Lee & Park 2019)"])
        self.assertEqual((["ref-00002"], "exact"), bound["(2020b)"])
        self.assertEqual((["ref-00004"], "exact"), bound["(WHO, 2019)"])
        self.assertEqual(([], "unresolved"), bound["(Doe, 2011)"])
        self.assertEqual(([], "ambiguous"), bound["(Smith, 2020)"])
        sentence_ids = [cid for sentence in manuscript["sentences"] for cid in sentence["citationClusterIds"]]
        self.assertEqual([c["id"] for c in clusters], sentence_ids)
        warnings = [w for w in manuscript["ingestion"]["warnings"] if w["code"] == "unresolved-author-year-citation"]
        self.assertEqual({"cite-000005", "cite-000006"}, {w["citationId"] for w in warnings})
        self.assertTrue(manuscript["ingestion"]["complete"])
        graph = build_claim_inventory(manuscript)
        by_id = {c["id"]: c for c in graph["citationClusters"]}
        self.assertEqual(["ref-00001", "ref-00003"], by_id["cite-000001"]["referenceIds"])
        self.assertEqual("exact", by_id["cite-000001"]["bindingStatus"])
        self.assertEqual("unresolved", by_id["cite-000005"]["bindingStatus"])

    def test_docx_author_year_citations_bind(self):
        body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>" for text in (
            "Results", "Growth slowed (García-López et al., 2015), as Nolan (2022) predicted.",
            "References", "García-López, M., Kim, S., &amp; Ng, P. (2015). A title. J, 1, 2.",
            "Nolan, A. (2022). Another title. J, 3, 4."))
        document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}">'
                    f"<w:body>{body}<w:sectPr/></w:body></w:document>")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
                archive.writestr("word/document.xml", document)
            manuscript = self.ingest("draft.docx", path.read_bytes())
        self.assertEqual([["ref-00001"], ["ref-00002"]], [c["referenceIds"] for c in manuscript["citationClusters"]])
        self.assertEqual(["parenthetical", "narrative"], [c["authorYear"]["form"] for c in manuscript["citationClusters"]])

    def test_numbered_references_still_bind_by_label(self):
        manuscript = self.ingest("numbers.md", "# Introduction\n\nKnown [1]. Also (Nolan, 2022).\n\n## References\n\n"
                                               "1. Nolan A. A bounded association study. Journal of Example Methods. 2022.\n")
        self.assertEqual([["ref-00001"], ["ref-00001"]], [c["referenceIds"] for c in manuscript["citationClusters"]])
        self.assertEqual(["numeric", "author-year"], [c["style"] for c in manuscript["citationClusters"]])


class ScaleTests(unittest.TestCase):
    def test_large_reference_lists_ingest_without_quadratic_scans(self):
        bodies, references = [], []
        for seed in range(1, 31):
            markdown, _, _ = F.jats_to_markdown(F.to_jats(F.build_article(seed)))
            head, tail = markdown.split("## References", 1)
            bodies.append(head.replace("# Introduction", "").strip())
            references.append(tail.strip())
        document = "# Introduction\n\n" + "\n\n".join(bodies) + "\n\n## References\n\n" + "\n\n".join(references) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.md"
            path.write_text(document, encoding="utf-8")
            started = time.perf_counter()
            manuscript = ingest_manuscript(path)
            elapsed = time.perf_counter() - started
        self.assertGreater(len(manuscript["bibliographyEntries"]), 900)
        self.assertGreater(len(manuscript["citationClusters"]), 1500)
        self.assertLess(elapsed, 30.0)


class LabelledCorpusAcceptanceTests(unittest.TestCase):
    """Plan acceptance: precision >= 0.98 and recall >= 0.95 on at least 200 citations."""

    SEEDS = range(1, 25)

    def test_generator_is_deterministic_and_labelled_by_jats_targets(self):
        self.assertEqual(F.corpus_digest(range(1, 4)), F.corpus_digest(range(1, 4)))
        article = F.build_article(3)
        markdown, gold, rids = F.jats_to_markdown(F.to_jats(article))
        self.assertTrue(gold)
        self.assertTrue(set(link.rid for link in gold) <= set(rids))
        self.assertNotIn("<xref", markdown)

    def test_resolution_precision_and_recall_on_generated_jats(self):
        with tempfile.TemporaryDirectory() as directory:
            result = F.evaluate(self.SEEDS, Path(directory))
        summary = {key: result[key] for key in ("gold", "predicted", "correct", "wrong", "unmatched", "missed",
                                                "precision", "recall", "documents")}
        self.assertGreaterEqual(result["gold"], 200, summary)
        self.assertGreaterEqual(len(result["styles"]), 12, summary)
        self.assertGreaterEqual(result["precision"], 0.98, summary)
        self.assertGreaterEqual(result["recall"], 0.95, summary)


if __name__ == "__main__":
    unittest.main()
