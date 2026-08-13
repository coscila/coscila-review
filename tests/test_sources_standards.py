from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ocsl_review.config import Paths
from ocsl_review.errors import ProviderError
from ocsl_review.repositories import validate_sources
from ocsl_review.standards import catalog, sync_standard, verify_standard

ROOT = Path(__file__).resolve().parents[1]


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.paths = Paths(home=ROOT, state=Path(tempfile.mkdtemp()), lab_repo=None)

    def tearDown(self):
        for path in sorted(self.paths.state.rglob("*"), reverse=True):
            path.chmod(0o700 if path.is_dir() else 0o600)
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

    def test_digest_pinned_archive_fallback_is_recorded(self):
        body = b"%PDF-1.7\nverified standard\n"
        digest = hashlib.sha256(body).hexdigest()
        record = self._archive_record(digest)

        denied = urllib.error.HTTPError(record["url"], 403, "Forbidden", {}, None)
        with (
            mock.patch(
                "ocsl_review.standards.catalog", return_value={record["id"]: record}
            ),
            mock.patch(
                "ocsl_review.standards.urllib.request.urlopen",
                side_effect=[denied, self._response(record["archive_url"], body)],
            ),
        ):
            result = sync_standard(self.paths, record["id"])

        self.assertEqual(result["sha256"], digest)
        snapshot = json.loads(
            (
                self.paths.standards / record["id"] / digest / "snapshot.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(snapshot["archiveFallback"])
        self.assertEqual(snapshot["requestedUrl"], record["url"])
        self.assertEqual(snapshot["retrievalUrl"], record["archive_url"])

    def test_archive_digest_mismatch_does_not_promote(self):
        body = b"%PDF-1.7\nsubstituted\n"
        record = self._archive_record("0" * 64)
        denied = urllib.error.HTTPError(record["url"], 403, "Forbidden", {}, None)
        with (
            mock.patch(
                "ocsl_review.standards.catalog", return_value={record["id"]: record}
            ),
            mock.patch(
                "ocsl_review.standards.urllib.request.urlopen",
                side_effect=[denied, self._response(record["archive_url"], body)],
            ),
            self.assertRaisesRegex(ProviderError, "differs from its catalogued SHA-256"),
        ):
            sync_standard(self.paths, record["id"])

        self.assertFalse((self.paths.standards / record["id"] / "current.json").exists())

    def test_matching_official_document_does_not_use_archive(self):
        body = b"%PDF-1.7\nofficial\n"
        record = self._archive_record(hashlib.sha256(body).hexdigest())
        opener = mock.Mock(return_value=self._response(record["url"], body))
        with (
            mock.patch(
                "ocsl_review.standards.catalog", return_value={record["id"]: record}
            ),
            mock.patch("ocsl_review.standards.urllib.request.urlopen", opener),
        ):
            result = sync_standard(self.paths, record["id"])

        self.assertEqual(result["source"], "official")
        self.assertEqual(opener.call_count, 1)

    def test_pdf_signature_mismatch_does_not_promote(self):
        body = b"an HTML challenge page"
        record = self._archive_record(hashlib.sha256(body).hexdigest())
        with (
            mock.patch(
                "ocsl_review.standards.catalog", return_value={record["id"]: record}
            ),
            mock.patch(
                "ocsl_review.standards.urllib.request.urlopen",
                side_effect=[
                    self._response(record["url"], body),
                    self._response(record["archive_url"], body),
                ],
            ),
            self.assertRaisesRegex(ProviderError, "did not return a PDF document"),
        ):
            sync_standard(self.paths, record["id"])

        self.assertFalse((self.paths.standards / record["id"] / "current.json").exists())

    def test_verify_rejects_a_snapshot_outside_the_catalog_pin(self):
        body = b"%PDF-1.7\nofficial\n"
        record = self._archive_record(hashlib.sha256(body).hexdigest())
        with (
            mock.patch(
                "ocsl_review.standards.catalog", return_value={record["id"]: record}
            ),
            mock.patch(
                "ocsl_review.standards.urllib.request.urlopen",
                return_value=self._response(record["url"], body),
            ),
        ):
            sync_standard(self.paths, record["id"])

        mismatched = {**record, "sha256": "0" * 64}
        with mock.patch(
            "ocsl_review.standards.catalog",
            return_value={mismatched["id"]: mismatched},
        ):
            report = verify_standard(self.paths, mismatched["id"])
        self.assertFalse(report["ok"])
        self.assertIn(
            "current snapshot differs from the catalogued SHA-256", report["problems"]
        )

    @staticmethod
    def _archive_record(digest):
        return {
            "id": "blocked-standard",
            "title": "Blocked official standard",
            "url": "https://authority.example/standard.pdf",
            "archive_url": (
                "https://web.archive.org/web/20200102030405id_/"
                "https://authority.example/standard.pdf"
            ),
            "sha256": digest,
            "authority": "Example authority",
            "purpose": "Exercise a digest-pinned archival fallback",
            "media_type": "application/pdf",
            "version_policy": "publisher-hosted-edition",
        }

    @staticmethod
    def _response(url, body):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        response.geturl.return_value = url
        response.headers.get_content_type.return_value = "application/pdf"
        return response


if __name__ == "__main__":
    unittest.main()
