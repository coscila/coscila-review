from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ocsl_review.config import Paths
from ocsl_review.repositories import validate_sources
from ocsl_review.standards import catalog

ROOT = Path(__file__).resolve().parents[1]


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.paths = Paths(home=ROOT, state=Path(tempfile.mkdtemp()), lab_repo=None)

    def tearDown(self):
        __import__("shutil").rmtree(self.paths.state)

    def test_catalog_and_exact_lock_reconcile(self):
        result = validate_sources(self.paths)
        self.assertEqual(result["repositories"], 3)

    def test_all_noassertion_sources_are_nonexecuting(self):
        lock = json.loads((ROOT / "repos" / "repositories.lock.json").read_text())
        for record in lock["repositories"]:
            if record["license"] == "NOASSERTION":
                self.assertFalse(record["executed"])

    def test_standards_are_official_https_locations(self):
        records = catalog(self.paths)
        self.assertGreaterEqual(len(records), 7)
        for record in records.values():
            self.assertTrue(record["url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
