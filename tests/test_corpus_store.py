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
