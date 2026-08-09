from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from ocsl_review.errors import ReviewError, ValidationError
from ocsl_review.ingest import (
    IngestionError,
    audit_identifier_leaks,
    ingest_manuscript,
    require_no_identifier_leaks,
)
from ocsl_review.rendering import render_review_package, validate_review_package
from ocsl_review.review_plan import (
    attach_concern,
    build_review_plan,
    empty_audit_summary,
    make_concern,
    make_revision_action,
    new_review_package,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "manuscript_with_citations.md"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _minimal_docx(path: Path) -> None:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}"><w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Introduction</w:t></w:r></w:p>
  <w:p><w:r><w:t>A local result was consistent with earlier work [1].</w:t></w:r></w:p>
  <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Measure</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>References</w:t></w:r></w:p>
  <w:p><w:r><w:t>1. Local A. Verification note. 2021. doi:10.5555/local.1</w:t></w:r></w:p>
  <w:sectPr/>
</w:body></w:document>""".encode()
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def _validate_schema(instance: dict, schema_name: str) -> None:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - the package declares this dependency
        return
    schema = json.loads((ROOT / "schemas" / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(instance)


class ManuscriptIngestionTests(unittest.TestCase):
    def test_markdown_ingestion_has_stable_locations_and_coverage(self) -> None:
        first = ingest_manuscript(FIXTURE)
        second = ingest_manuscript(FIXTURE)

        self.assertTrue(first["confidential"])
        self.assertTrue(first["ingestion"]["complete"])
        self.assertEqual(first["manuscriptId"], second["manuscriptId"])
        self.assertEqual(first["sections"], second["sections"])
        self.assertEqual(first["sentences"], second["sentences"])
        self.assertEqual(2, len(first["bibliographyEntries"]))
        self.assertGreaterEqual(len(first["citationClusters"]), 2)
        self.assertEqual(1, len(first["tables"]))
        self.assertEqual(1, len(first["figures"]))
        self.assertNotIn(str(FIXTURE.parent), json.dumps(first["source"]))
        for sentence in first["sentences"]:
            self.assertEqual(sentence["paragraphId"], sentence["locator"]["paragraphId"])
            self.assertIn("sectionId", sentence["locator"])
        _validate_schema(first, "manuscript.schema.json")

    def test_docx_is_read_with_standard_library_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "small.docx"
            _minimal_docx(path)
            manuscript = ingest_manuscript(path, confidential=False)

        self.assertFalse(manuscript["confidential"])
        self.assertTrue(manuscript["ingestion"]["complete"])
        self.assertEqual("docx", manuscript["source"]["format"])
        self.assertEqual(1, len(manuscript["tables"]))
        self.assertEqual(1, len(manuscript["citationClusters"]))
        self.assertEqual(1, len(manuscript["bibliographyEntries"]))
        _validate_schema(manuscript, "manuscript.schema.json")

    def test_plain_text_ingestion_extracts_numbered_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manuscript.txt"
            path.write_text(
                "INTRODUCTION\n\nA bounded observation was reported [1].\n\n"
                "REFERENCES\n\n1. Author A. Bounded observation. 2020.\n",
                encoding="utf-8",
            )
            manuscript = ingest_manuscript(path)
        self.assertTrue(manuscript["ingestion"]["complete"])
        self.assertEqual("text", manuscript["source"]["format"])
        self.assertEqual(1, len(manuscript["citationClusters"]))
        self.assertEqual(1, len(manuscript["bibliographyEntries"]))

    def test_pdf_extraction_has_no_silent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "unreadable.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            with self.assertRaisesRegex(IngestionError, "extractor is not available"):
                ingest_manuscript(pdf, pdftotext=root / "missing-pdftotext")

    def test_pdf_extractor_is_timed_and_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "bounded.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            extractor = root / "pdftotext-fixture"
            extractor.write_text(
                "#!/bin/sh\n"
                "printf 'INTRODUCTION\\n\\nAn observation was reported [1].\\fREFERENCES\\n\\n1. Author. Record. 2020.\\n' > \"$5\"\n",
                encoding="utf-8",
            )
            extractor.chmod(0o755)
            manuscript = ingest_manuscript(pdf, pdftotext=extractor, pdf_timeout=2)
            self.assertEqual(2, manuscript["ingestion"]["coverage"]["pagesObserved"])
            with self.assertRaisesRegex(IngestionError, "extraction limit"):
                ingest_manuscript(
                    pdf,
                    pdftotext=extractor,
                    pdf_timeout=2,
                    max_extracted_bytes=16,
                )

    def test_identifier_audit_reports_only_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = Path(temporary) / "notes.jsonl"
            knowledge.write_text(
                '{"note":"contact unpublished-author@example.invalid"}\n', encoding="utf-8"
            )
            findings = audit_identifier_leaks(knowledge)
            self.assertEqual(1, len(findings))
            self.assertEqual("email", findings[0]["kind"])
            self.assertNotIn("unpublished-author", json.dumps(findings))
            with self.assertRaisesRegex(IngestionError, "identifier-leak audit failed"):
                require_no_identifier_leaks(knowledge)


class ReviewPackageTests(unittest.TestCase):
    def _package(self) -> dict:
        manuscript = ingest_manuscript(FIXTURE)
        plan = build_review_plan(manuscript)
        sentence_id = next(
            sentence["id"] for sentence in manuscript["sentences"] if "interval" in sentence["text"]
        )
        concern = make_concern(
            manuscript,
            lens_id="uncertainty-budget",
            locator_id=sentence_id,
            finding="The interval is reported without naming its estimand or construction method.",
            basis="The anchored sentence gives endpoints but the local context does not define the interval.",
            requested_resolution="Name the estimand and interval procedure at the result and method locations.",
            priority="substantial",
            confidence=0.91,
        )
        action = make_revision_action(
            concern,
            action="Add the estimand and interval procedure to the manuscript.",
            state="not-started",
            verification="Both anchored locations contain mutually consistent definitions.",
        )
        attach_concern(plan, concern, action)
        return new_review_package(manuscript, plan)

    def test_original_lens_route_and_location_bound_ledger(self) -> None:
        package = self._package()
        lens_ids = {lens["id"] for lens in package["reviewPlan"]["lenses"]}
        self.assertIn("participant-stewardship", lens_ids)
        self.assertIn("computational-replay", lens_ids)
        concern = package["concerns"][0]
        self.assertTrue(concern["anchor"]["locatorId"].startswith("s-"))
        self.assertEqual(concern["concernId"], package["revisionLedger"][0]["concernId"])
        validate_review_package(package)
        _validate_schema(package, "review-package.schema.json")

    def test_bundle_has_same_stem_and_valid_docx(self) -> None:
        package = self._package()
        with tempfile.TemporaryDirectory() as temporary:
            stem = Path(temporary) / "audit-result"
            paths = render_review_package(package, stem)
            self.assertEqual({".json", ".md", ".docx"}, set(paths))
            self.assertEqual({stem.name}, {path.stem for path in paths.values()})
            self.assertTrue(all((path.stat().st_mode & 0o777) == 0o600 for path in paths.values()))
            self.assertEqual(package, json.loads(paths[".json"].read_text(encoding="utf-8")))
            markdown = paths[".md"].read_text(encoding="utf-8")
            self.assertIn("Audit determination", markdown)
            self.assertIn(package["concerns"][0]["finding"], markdown)
            with zipfile.ZipFile(paths[".docx"]) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("word/document.xml", archive.namelist())
                document = ET.fromstring(archive.read("word/document.xml"))
                text = " ".join(node.text or "" for node in document.iter(f"{{{W}}}t"))
                self.assertIn(package["concerns"][0]["finding"], text)

    def test_generation_failure_leaves_existing_bundle_untouched(self) -> None:
        package = self._package()
        with tempfile.TemporaryDirectory() as temporary:
            stem = Path(temporary) / "audit-result"
            old = {}
            for suffix in (".json", ".md", ".docx"):
                path = stem.with_suffix(suffix)
                value = f"old-{suffix}".encode()
                path.write_bytes(value)
                old[path] = value
            with (
                mock.patch(
                    "ocsl_review.rendering.build_docx_bytes",
                    side_effect=RuntimeError("synthetic failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "synthetic failure"),
            ):
                render_review_package(package, stem)
            for path, value in old.items():
                self.assertEqual(value, path.read_bytes())

    def test_commit_failure_restores_the_previous_complete_bundle(self) -> None:
        package = self._package()
        with tempfile.TemporaryDirectory() as temporary:
            stem = Path(temporary) / "audit.release-v1"
            expected = {}
            for suffix in (".json", ".md", ".docx"):
                path = Path(f"{stem}{suffix}")
                value = f"previous-{suffix}".encode()
                path.write_bytes(value)
                expected[path] = value
            real_replace = __import__("os").replace
            failed = False

            def fail_second_install(source, destination):
                nonlocal failed
                if Path(source).name == "staged.md" and not failed:
                    failed = True
                    raise OSError("synthetic rename failure")
                return real_replace(source, destination)

            with (
                mock.patch("ocsl_review.rendering.os.replace", side_effect=fail_second_install),
                self.assertRaisesRegex(ReviewError, "publish review bundle atomically"),
            ):
                render_review_package(package, stem)
            for path, value in expected.items():
                self.assertEqual(value, path.read_bytes())

    def test_affirmative_verdict_is_rejected_when_coverage_is_incomplete(self) -> None:
        package = self._package()
        manuscript = ingest_manuscript(FIXTURE)
        package["auditSummary"] = empty_audit_summary(manuscript)
        package["auditSummary"]["citedCorrectly"]["status"] = "yes"
        with self.assertRaisesRegex(ValidationError, "affirmative citation verdict is forbidden"):
            validate_review_package(package)


if __name__ == "__main__":
    unittest.main()
