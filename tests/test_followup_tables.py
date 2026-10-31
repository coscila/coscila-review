# SPDX-License-Identifier: Apache-2.0
"""Statistics-report table sources: the ocsl-followup-tables-v1 reader, identifier matching and synonym lists."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import followup_fixtures as F
from ocsl_review import followup_tables
from ocsl_review.errors import ValidationError
from ocsl_review.followup_model import TableCell, TableSource, WorkRef

TABLES = F.FIXTURES / "tables" / "synthetic-statistics-tables.json"


def variant(directory: Path, name: str, change) -> Path:
    data = json.loads(TABLES.read_text(encoding="utf-8"))
    change(data)
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TableSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = followup_tables.load(TABLES)

    def tearDown(self):
        self.directory.cleanup()

    def test_the_synthetic_source_describes_itself(self):
        self.assertIsInstance(self.source, TableSource)
        sha = hashlib.sha256(TABLES.read_bytes()).hexdigest()
        described = self.source.describe()
        self.assertEqual(("synthetic", sha, "synthetic-tables-1"),
                         (described["licensing"], described["sha256"], described["declaredVersion"]))
        self.assertEqual((1, 2, 5), (described["reports"], described["editions"], described["cells"]))
        self.assertEqual({"builtin": followup_tables.BUILTIN_SYNONYMS_VERSION, "source": "synthetic-synonyms-1"},
                         described["synonyms"])
        self.assertEqual(f"synthetic-statistics-tables@synthetic-tables-1#{sha[:12]}", self.source.source_id)

    def test_cited_works_map_to_a_report_and_edition(self):
        report = "synthetic-disease-statistics"
        self.assertEqual((report, "second"), self.source.report_for(WorkRef("w-report", doi="10.5555/FU.WORK.REPORT")))
        self.assertEqual((report, "first"),
                         self.source.report_for(WorkRef("w-first", doi="https://doi.org/10.5555/fu.work.report.first")))
        self.assertIsNone(self.source.report_for(WorkRef("w-plain-1", doi="10.5555/fu.work.plain1",
                                                         title="Fictional mechanism study four")))
        self.assertEqual(["second", "first"], list(self.source.editions(report)))
        cells = self.source.cells(report, "second")
        self.assertTrue(all(isinstance(cell, TableCell) for cell in cells))
        self.assertEqual(TableCell(report, "second", "all", "mortality-count", "Otherland", 2019, 7300, "deaths",
                                   "table 3"), cells[2])
        self.assertEqual([], list(self.source.cells(report, "third")))
        self.assertEqual([], list(self.source.editions("unknown")))

    def test_pmid_title_and_report_level_matches(self):
        def change(data):
            report = data["reports"][0]
            report["identifiers"] = {"doi": ["10.5555/fu.work.report.any"], "pmid": ["9100001"]}
            report["editions"]["first"]["citedAs"] = {"title": "Synthetic disease statistics report, first edition"}
        source = followup_tables.load(variant(self.root, "matches.json", change))
        report = "synthetic-disease-statistics"
        self.assertEqual((report, ""), source.report_for(WorkRef("w", pmid="9100001")))
        self.assertEqual((report, ""), source.report_for(WorkRef("w", doi="10.5555/fu.work.report.any")))
        self.assertEqual((report, "first"),
                         source.report_for(WorkRef("w", title="Synthetic Disease Statistics Report: first edition")))
        self.assertEqual((report, "second"), source.report_for(WorkRef("w", doi="10.5555/fu.work.report",
                                                                       title="Synthetic disease statistics report, "
                                                                             "first edition")))

    def test_labels_are_folded_and_mapped_through_versioned_synonyms(self):
        canonical = self.source.canonical
        self.assertEqual("exampleland", canonical("geography", "  Republic of  Exampleland "))
        self.assertEqual("exampleland", canonical("geography", "the Exampleland"))
        self.assertEqual("world", canonical("geography", "worldwide"))
        self.assertEqual("united states", canonical("geography", "U.S."))
        self.assertEqual("all", canonical("condition", "The disease"))
        self.assertEqual("all", canonical("condition", "all sites combined"))
        self.assertEqual("breast cancer", canonical("condition", "Breast  Cancer"))
        self.assertEqual("mortality-count", canonical("metric", "Mortality-count"))
        with self.assertRaisesRegex(ValidationError, "label kind"):
            canonical("unit", "deaths")

    def test_malformed_sources_are_refused(self):
        cases = {
            "format": (lambda d: d.update(format="other"), "ocsl-followup-tables-v1"),
            "licensing": (lambda d: d.update(licensing="cleared"), "licensing"),
            "version": (lambda d: d.pop("declaredVersion"), "declaredVersion"),
            "unknown": (lambda d: d.update(extra=1), "unknown fields"),
            "name": (lambda d: d.update(name="Has Spaces"), "name"),
            "value": (lambda d: d["reports"][0]["editions"]["second"]["cells"][0].update(value="12500"), "number"),
            "year": (lambda d: d["reports"][0]["editions"]["second"]["cells"][0].update(year="2019"), "year"),
            "duplicate": (lambda d: d["reports"].append(copy.deepcopy(d["reports"][0])), "declared twice"),
            "doi": (lambda d: d["reports"][0]["identifiers"].update(doi=["not-a-doi"]), "invalid identifier"),
            "synonyms": (lambda d: d["synonyms"].pop("version"), "synonyms"),
            "reports": (lambda d: d.update(reports=[]), "reports"),
        }
        for name, (change, message) in cases.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(ValidationError, message):
                    followup_tables.load(variant(self.root, f"{name}.json", change))
        broken = self.root / "broken.json"
        broken.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ValidationError, "UTF-8 JSON"):
            followup_tables.load(broken)
        with self.assertRaisesRegex(ValidationError, "missing"):
            followup_tables.load(self.root / "absent.json")

    def test_licensing_is_read_not_judged_by_the_source(self):
        for licensing in followup_tables.LICENSING:
            with self.subTest(licensing=licensing):
                source = followup_tables.load(variant(self.root, f"{licensing}.json",
                                                      lambda d, value=licensing: d.update(licensing=value)))
                self.assertEqual(licensing, source.licensing)


if __name__ == "__main__":
    unittest.main()
