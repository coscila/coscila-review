# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import tempfile
import unittest

from ocsl_review.corpus_review_reuse import retained_review_sources, reuse_review
from ocsl_review.corpus_store import CorpusRepository, canonical
from ocsl_review.corpus_library import now
from ocsl_review.errors import ValidationError
from test_structured import article


class ReviewReuseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = CorpusRepository.create(Path(self.directory.name), "library", "Question", "v2", {})
        self.review = self.repo.review({"doi": "10.1234/review", "title": "Review"})
        with self.repo.connect() as connection:
            self.row = dict(connection.execute("SELECT r.review_id,w.work_id,w.metadata_json FROM reviews r JOIN works w USING(work_id)").fetchone())

    def retain(self, doi="10.1234/review"):
        raw = article('<p>A finding <xref ref-type="bibr" rid="R1">1</xref>.</p>').replace("10.1234/review", doi).encode()
        digest = self.repo.artifact(raw, "legacy-original-source", "application/xml")
        value = {"sources": [{"sha256": digest, "state": "retained", "mediaType": "application/xml"}]}
        with self.repo.connect(write=True) as connection:
            connection.execute("INSERT INTO work_observations VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "old-review", "v2", self.row["work_id"], "legacy.review_manifest", canonical(value),
                "test-import", digest, None, "unresolved", now()))
        return digest

    def test_retained_review_reparsed_with_verified_article_doi(self):
        digest = self.retain()
        sources = retained_review_sources(self.repo, [self.row])
        self.assertTrue(reuse_review(self.repo, self.row, sources[self.row["work_id"]]))
        with self.repo.connect() as connection:
            result = connection.execute("SELECT * FROM corpus_reviews").fetchone()
        proof = json.loads(result["checkpoint_json"])
        self.assertEqual(digest, proof["rawSourceSha256"])
        self.assertTrue(proof["public"])
        self.assertEqual("retained-structured-source", proof["provider"])
        self.assertEqual("completed", result["state"])

    def test_mismatched_article_identity_is_not_reused(self):
        self.retain("10.1234/different")
        sources = retained_review_sources(self.repo, [self.row])
        self.assertFalse(reuse_review(self.repo, self.row, sources[self.row["work_id"]]))
        with self.repo.connect() as connection:
            self.assertEqual("pending", connection.execute("SELECT phase FROM corpus_reviews").fetchone()[0])

    def test_modified_retained_source_fails_before_ingestion(self):
        digest = self.retain()
        path = self.repo.root / "sources" / digest
        path.chmod(0o600)
        path.write_bytes(b"changed")
        sources = retained_review_sources(self.repo, [self.row])
        with self.assertRaisesRegex(ValidationError, "hash mismatch"):
            reuse_review(self.repo, self.row, sources[self.row["work_id"]])


if __name__ == "__main__":
    unittest.main()
