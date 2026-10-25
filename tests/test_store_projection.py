# SPDX-License-Identifier: Apache-2.0
"""The label-free projection literature-evidence@2: deny-by-default rules and a fail-closed gate."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from ocsl_review import store_projection
from ocsl_review.corpus_library import Library, Workspace, digest_file
from ocsl_review.corpus_store import CorpusRepository, canonical
from ocsl_review.errors import ValidationError
from ocsl_review.store_projection import FORMAT, build_projection, removed_rows, verify_projection, vocabulary_hits

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "storage_benchmark.py"
spec = importlib.util.spec_from_file_location("storage_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def tree_state(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size, digest_file(p))
            for p in sorted(root.rglob("*")) if p.is_file()}


class ProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.stats = benchmark.generate(cls.root, "store", scale=0.0005, progress=lambda event: None)
        CorpusRepository(cls.root, "store").library.snapshot("snapshot")
        cls.source = cls.root / "snapshot"
        cls.before = tree_state(cls.source)
        cls.receipt = build_projection(cls.source, cls.root / "projection", workers=2)
        cls.projection = Library(Workspace(cls.root), "projection")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def query(self, sql, params=()):
        with self.projection.connect() as connection:
            return [tuple(row) for row in connection.execute(sql, params).fetchall()]

    def test_gate_returns_zero_rows_and_the_source_is_untouched(self):
        self.assertEqual(self.receipt["gate"]["removedRows"], 0)
        self.assertEqual(self.receipt["gate"]["vocabularyHits"], 0)
        self.assertEqual(tree_state(self.source), self.before)
        with self.projection.connect() as connection:
            self.assertEqual(removed_rows(connection), {})
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM work_observations WHERE property LIKE 'legacy.%'").fetchone()[0], 0)
        deep = verify_projection(self.root / "projection", level="deep")
        self.assertTrue(deep["gate"]["blobsScanned"])
        self.assertEqual(deep["gate"]["vocabularyHits"], 0)
        dropped = self.receipt["droppedObservationProperties"]
        for prop in ("legacy.citation_score", "legacy.citation_locus", "legacy.corrected_locus", "legacy.census_attempt",
                     "legacy.review_summary", "legacy.bibliography", "legacy.corrected_bibliography",
                     "legacy.review_manifest"):
            self.assertGreater(dropped[prop], 0, prop)
        for table in ("assessments", "model_receipts", "claim_loci", "bibliography_entries", "reviews", "corpus_tasks"):
            self.assertGreater(self.receipt["dropped"][table], 0, table)

    def test_evidence_is_kept_and_only_a_neutral_retained_source_index_is_derived(self):
        reviews = self.stats["sizes"]["reviews"]
        rows = self.query("SELECT doi, capture_sha256, media_type, route, captured_at FROM retained_sources ORDER BY doi")
        self.assertEqual(len(rows), reviews)
        columns = [r[1] for r in self.query("PRAGMA table_info(retained_sources)")]
        self.assertEqual(columns, ["doi", "capture_sha256", "media_type", "route", "captured_at"])
        for doi, capture, media, route, _ in rows:
            body = self.projection.read_blob(capture)
            self.assertIn(doi.encode(), body)
            self.assertEqual((media, route), ("application/xml", "jats"))
        kept = self.receipt["kept"]
        self.assertEqual(kept["works"], self.stats["sizes"]["reviews"] + self.stats["sizes"]["cited_works"])
        self.assertEqual(kept["work_identifiers"], self.stats["sizes"]["cited_works"])
        self.assertEqual(kept["retrievals"], self.stats["sizes"]["cited_works"])
        self.assertEqual(self.receipt["dropped"]["corpus_documents"], self.stats["sizes"]["audit_excerpts"])
        self.assertEqual(self.query("SELECT count(*) FROM corpus_documents WHERE provider='legacy-audit-excerpt'"), [(0,)])
        for (metadata,) in self.query("SELECT metadata_json FROM works"):
            self.assertFalse({"stratum", "label"} & set(json.loads(metadata)))
        for (metadata,) in self.query("SELECT metadata_json FROM corpus_documents WHERE provider='legacy:epmc'"):
            value = json.loads(metadata)
            self.assertNotIn("sourceDatabaseSha256", value)
            self.assertEqual(value["completenessBasis"], "legacy-field-only-unverified")
        kinds = {kind for (kind,) in self.query("SELECT DISTINCT artifact_kind FROM source_artifacts")}
        self.assertTrue(kinds <= set(store_projection.ALWAYS_KINDS + store_projection.REFERENCED_KINDS))
        for withheld in ("legacy-original-database", "legacy-fulltext-selection-manifest", "worker-response",
                         "task-input", "legacy-assessment-record"):
            self.assertIn(withheld, self.receipt["withheldArtifactKinds"])
        self.assertEqual(self.query("SELECT count(*) FROM source_sections s WHERE NOT EXISTS "
                                    "(SELECT 1 FROM source_artifacts a WHERE a.sha256=s.artifact_sha256)"), [(0,)])

    def test_projection_is_a_sealed_store_that_points_nowhere_else(self):
        manifest = json.loads(self.projection.manifest_path.read_text())
        self.assertEqual(manifest["store"], {"id": FORMAT, "rules_sha256": store_projection.RULES_SHA256})
        self.assertEqual(self.projection.verify(level="deep")["merkle_root"], self.receipt["merkleRoot"])
        self.assertEqual(dict(self.query("SELECT key, value FROM store_meta"))["format"], FORMAT)
        text = self.projection.manifest_path.read_bytes() + b"".join(
            value.encode() for (value,) in self.query("SELECT value FROM store_meta"))
        for digest in (self.receipt["source"]["manifestSha256"], self.receipt["source"]["databaseSha256"]):
            self.assertNotIn(digest.encode(), text)
        self.assertLess(sum(1 for p in (self.root / "projection").rglob("*") if p.is_file()), 50)

    def test_tampered_projections_fail_verification(self):
        copy = self.root / "tampered"
        shutil.copytree(self.root / "projection", copy)
        os.chmod(copy / "corpus.sqlite3", 0o644)
        connection = sqlite3.connect(copy / "corpus.sqlite3")
        with connection:
            connection.execute("INSERT INTO work_observations SELECT 'x','none',work_id,'legacy.citation_score','{}','v',"
                               "(SELECT sha256 FROM source_artifacts LIMIT 1),NULL,'unresolved','t' FROM works LIMIT 1")
        self.assertEqual(removed_rows(connection), {"table:work_observations": 1, "legacy-property": 1})
        connection.close()
        with self.assertRaises(ValidationError):
            verify_projection(copy)
        shutil.rmtree(copy)
        shutil.copytree(self.root / "projection", copy)
        manifest = json.loads((copy / "manifest.json").read_text())
        manifest["store"]["rules_sha256"] = "0" * 64
        os.chmod(copy / "manifest.json", 0o644)
        (copy / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValidationError, "different rules"):
            verify_projection(copy)
        shutil.rmtree(copy)


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "store", "Q", "seed", {})

    def test_label_vocabulary_in_a_kept_artifact_fails_the_build(self):
        self.repo.artifact(canonical({"resultList": {"result": [{"abs_class": "sure_not_supported"}]}}).encode(),
                           "raw-provider-response")
        self.repo.library.snapshot("snapshot")
        with self.assertRaisesRegex(ValidationError, "vocabulary"):
            build_projection(self.root / "snapshot", self.root / "projection", workers=1)
        self.assertFalse((self.root / "projection").exists())

    def test_vocabulary_patterns(self):
        self.assertEqual(vocabulary_hits(b"cell proliferation increased in the treated cohort"), [])
        self.assertEqual(vocabulary_hits(b'{"c":"F","v":"contra"}'), ["compact-verdict"])
        self.assertIn("three-class-label", vocabulary_hits(b"class sure_not_supported"))
        self.assertIn("legacy-property", vocabulary_hits(b'"property":"legacy.citation_score"'))
        self.assertIn("verdict-field", vocabulary_hits(b'{"support_verdict": "x"}'))

    def test_older_snapshot_formats_with_large_manifests_are_read_without_loading_them(self):
        work = self.repo.work({"doi": "10.1234/one", "title": "One"})
        self.repo.document(work, {"text": "Kept abstract.", "complete": True}, "abstract", "europe-pmc")
        source = self.root / "old-format"
        source.mkdir()
        shutil.copyfile(self.repo.db, source / "corpus.sqlite3")
        (source / "sources").mkdir()
        with sqlite3.connect(source / "corpus.sqlite3") as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            rows = connection.execute("SELECT sha256, relative_path, byte_count FROM source_artifacts ORDER BY sha256").fetchall()
        connection.close()
        for digest, relative, size in rows:
            (source / relative).write_bytes(self.repo.read_artifact(digest))
        manifest = {"format": "ocsl-literature-snapshot", "version": 1,
                    "database": {"path": "corpus.sqlite3", "sha256": digest_file(source / "corpus.sqlite3"),
                                 "byte_count": (source / "corpus.sqlite3").stat().st_size},
                    "sources": [{"sha256": d, "relative_path": r, "byte_count": s} for d, r, s in rows]}
        (source / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        with mock.patch.object(store_projection, "MANIFEST_PARSE_LIMIT", 0):
            head = store_projection._manifest_head(source / "manifest.json")
            self.assertEqual((head["format"], head["version"]), ("ocsl-literature-snapshot", 1))
            self.assertEqual(head["database"]["sha256"], manifest["database"]["sha256"])
            receipt = build_projection(source, self.root / "projection", workers=1)
        self.assertEqual(receipt["source"]["formatVersion"], 1)
        self.assertEqual(receipt["kept"]["corpus_documents"], 1)
        verify_projection(self.root / "projection", level="deep")


if __name__ == "__main__":
    unittest.main()
