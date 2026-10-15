# SPDX-License-Identifier: Apache-2.0
import concurrent.futures
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ocsl_review.corpus_store import CorpusRepository
from ocsl_review.corpus_ingest import import_manifest, ingest_file
from ocsl_review.errors import ValidationError
from ocsl_review.corpus_library import add_analysis


class CorpusStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/corpus", "Fixture question", "v2", {"version": 2})

    def test_upgrade_preserves_original_and_checks_foreign_keys(self):
        self.assertEqual(self.repo.version, 2)
        self.assertTrue((self.repo.root / "corpus-v1-backup.sqlite3").exists())
        self.assertEqual(self.repo.library.verify()["schema_version"], 2)

    def test_content_addressed_atomic_concurrent_source_writes(self):
        body = b"evidence\n" * 100000
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            hashes = list(executor.map(lambda _: self.repo.artifact(body, "test"), range(24)))
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual(self.repo.read_artifact(hashes[0]), body)
        self.assertEqual(self.repo.library.verify()["counts"]["source_artifacts"], 1)

    def test_identifier_stability_and_independent_branch(self):
        work = self.repo.work({"doi": "10.1/example", "title": "Example"})
        self.assertEqual(work, self.repo.work({"doi": "10.1/example", "pmid": "123"}))
        self.repo.document(work, {"text": "Exact reusable abstract", "complete": True}, "abstract", "fixture")
        snapshot = self.repo.library.snapshot("results/snapshot")
        parent = CorpusRepository(self.root, "results/snapshot")
        before = hashlib.sha256(parent.db.read_bytes()).hexdigest()
        with self.assertRaises(ValidationError):
            parent.artifact(b"must not write", "test")
        parent.library.branch("work/followup", "Another scientific question", "followup", {"version": 2})
        branch = CorpusRepository(self.root, "work/followup")
        self.assertIn("Exact reusable abstract", branch.evidence(work, "abstract")["evidenceUnits"][0]["text"])
        self.assertEqual(branch.analysis()["analysis_id"], "followup")
        branch.library.snapshot("results/followup")
        self.assertEqual(before, hashlib.sha256(parent.db.read_bytes()).hexdigest())
        self.assertTrue(snapshot["portable"])

    def test_snapshot_default_is_portable_verified_and_preserved_on_resnapshot(self):
        self.repo.library.snapshot("results/first")
        snapshot = CorpusRepository(self.root, "results/first")
        self.assertEqual(snapshot.analysis()["analysis_id"], "v2")
        self.assertEqual(snapshot.status()["analysisId"], "v2")
        # An extra mutable pointer cannot override the verified manifest.
        (snapshot.root / "bundle.json").write_text('{"active_analysis_id":"wrong"}')
        self.assertEqual(snapshot.analysis()["analysis_id"], "v2")
        snapshot.library.snapshot("results/second")
        self.assertEqual(CorpusRepository(self.root, "results/second").analysis()["analysis_id"], "v2")
        manifest = snapshot.root / "manifest.json"
        value = json.loads(manifest.read_text())
        value["active_analysis_id"] = "unknown"
        manifest.chmod(0o600)
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "active analysis"):
            snapshot.library.verify()

    def test_legacy_snapshot_single_analysis_fallback_but_multiple_require_explicit_id(self):
        for name, multiple in (("single", False), ("multiple", True)):
            if multiple:
                with self.repo.connect(write=True) as connection:
                    add_analysis(connection, self.repo.analysis()["corpus_id"], "Q2", "second")
            self.repo.library.snapshot("results/" + name)
            snapshot = CorpusRepository(self.root, "results/" + name)
            manifest = snapshot.root / "manifest.json"
            value = json.loads(manifest.read_text())
            value.pop("active_analysis_id")
            manifest.chmod(0o600)
            manifest.write_text(json.dumps(value))
            snapshot.library.verify()
            if multiple:
                with self.assertRaisesRegex(ValueError, "explicit analysis ID"):
                    snapshot.analysis()
                self.assertEqual(snapshot.analysis("second")["analysis_id"], "second")
            else:
                self.assertEqual(snapshot.analysis()["analysis_id"], "v2")

    def test_seed_candidates_can_be_resolved_by_a_frozen_selection(self):
        metadata = {"doi": "10.1234/seed", "title": "Seed candidate"}
        review = self.repo.review(metadata, inclusion="unresolved", reason="historical candidate")
        self.assertEqual(review, self.repo.review(metadata, inclusion="included", reason="frozen manifest"))
        with self.repo.connect() as connection:
            row = connection.execute("SELECT inclusion_state,selection_reason FROM reviews WHERE review_id=?", (review,)).fetchone()
        self.assertEqual(tuple(row), ("included", "frozen manifest"))
        with self.assertRaisesRegex(ValidationError, "eligibility"):
            self.repo.review(metadata, inclusion="excluded")

    def test_manifest_and_all_occurrences_survive_idempotent_ingestion(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps([{"doi": "10.1234/fixture", "title": "Fixture"}] * 2))
        self.assertEqual(import_manifest(self.repo, manifest)["distinctReviews"], 1)
        with self.repo.connect() as connection:
            review_id = connection.execute("SELECT review_id FROM reviews").fetchone()[0]
        fixture = Path(__file__).parent / "fixtures/citation_census_canary.xml"
        first = ingest_file(self.repo, review_id, fixture, public=True)
        second = ingest_file(self.repo, review_id, fixture, public=True)
        self.assertEqual(first, second)
        self.assertGreater(first["bibliographyEntries"], 0)
        self.assertGreater(first["citationLoci"], 0)
        self.assertEqual(self.repo.status()["counts"]["claim_loci"], first["citationLoci"])
        self.repo.library.verify()


if __name__ == "__main__":
    unittest.main()
