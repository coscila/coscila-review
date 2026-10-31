# SPDX-License-Identifier: Apache-2.0
"""A blind study (OCSL_REVIEW_BLIND) never imports earlier judgments or libraries and attaches only projections."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocsl_review import blind
from ocsl_review.cli import main
from ocsl_review.corpus_export import import_snapshot
from ocsl_review.corpus_import import import_legacy_census, import_legacy_scores
from ocsl_review.corpus_seed import import_legacy_evidence
from ocsl_review.errors import ValidationError


class BlindStudyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_marker_values(self):
        for value, expected in (("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("no", False)):
            self.assertEqual(expected, blind.blind_study({blind.BLIND_ENV: value}))
        self.assertFalse(blind.blind_study({}))

    def test_every_import_of_earlier_judgments_refuses_in_a_blind_study(self):
        with mock.patch.dict(os.environ, {blind.BLIND_ENV: "1"}):
            for call in (lambda: import_legacy_scores(None, self.root / "old.sqlite3"),
                         lambda: import_legacy_census(None, self.root / "old.jsonl"),
                         lambda: import_legacy_evidence(None, self.root / "old.sqlite3"),
                         lambda: import_snapshot(self.root, "work/seed", self.root / "seed")):
                with self.assertRaisesRegex(ValidationError, "blind study"):
                    call()
            for argv in (["corpus", "import-legacy", "--workspace", str(self.root), "--library", "work/x",
                          str(self.root / "old.sqlite3"), "--kind", "evidence"],
                         ["corpus", "import-snapshot", "--workspace", str(self.root), "--library", "work/x",
                          str(self.root / "seed")]):
                with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertNotEqual(0, main(argv))

    def test_a_blind_study_attaches_only_label_free_projections(self):
        store = self.root / "store"
        store.mkdir()
        (store / "manifest.json").write_text(json.dumps({"format": "ocsl-literature-snapshot", "version": 2}))
        with mock.patch.dict(os.environ, {blind.BLIND_ENV: "1"}):
            with self.assertRaisesRegex(ValidationError, "label-free"):
                blind.require_projection_in_blind_study(store)
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                self.assertNotEqual(0, main(["store", "attach", "--workspace", str(self.root), "--library", "work/x",
                                             "--store", str(store)]))
            (store / "manifest.json").write_text(json.dumps({"store": {"id": "literature-evidence@2"}}))
            blind.require_projection_in_blind_study(store)
        with mock.patch.dict(os.environ, {blind.BLIND_ENV: ""}):
            (store / "manifest.json").write_text("{}")
            blind.require_projection_in_blind_study(store)   # outside a blind study the guard is silent


if __name__ == "__main__":
    unittest.main()
