# SPDX-License-Identifier: Apache-2.0
import concurrent.futures
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ocsl_review import corpus_library, corpus_store
from ocsl_review.corpus_store import CorpusRepository, canonical, identity
from ocsl_review.corpus_ingest import import_manifest, ingest_file
from ocsl_review.errors import ValidationError
from ocsl_review.corpus_library import add_analysis


def legacy_v2_library(root, relative="work/legacy"):
    """An existing schema-2 library, as written before progress counters existed."""
    corpus_library.initialize(corpus_library.Workspace(root), relative, "Legacy question", "legacy", {"version": 2})
    connection = sqlite3.connect(root / relative / "corpus.sqlite3")
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + corpus_store.MIGRATIONS[1].read_text() + "\nCOMMIT;")
    finally:
        connection.close()
    return CorpusRepository(root, relative)


@contextmanager
def traced_statements(repo):
    """Record every SQL statement the repository's connections execute."""
    statements, original = [], CorpusRepository.connect

    @contextmanager
    def connect(self, *, write=False):
        with original(self, write=write) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    with patch.object(CorpusRepository, "connect", connect):
        yield statements


class CorpusStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/corpus", "Fixture question", "v2", {"version": 2})

    def test_upgrade_preserves_original_and_checks_foreign_keys(self):
        self.assertEqual(self.repo.version, corpus_store.SCHEMA_VERSION)
        self.assertTrue((self.repo.root / "corpus-v1-backup.sqlite3").exists())
        self.assertTrue((self.repo.root / "corpus-v2-backup.sqlite3").exists())
        self.assertEqual(self.repo.library.verify()["schema_version"], corpus_store.SCHEMA_VERSION)

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


class CorpusSchemaAndCounterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def populate(self, repo, analysis, *, loci=3):
        review = repo.review({"doi": "10.1234/review", "title": "Review"})
        work = repo.work({"doi": "10.1234/source", "title": "Source"})
        repo.document(work, {"text": "Sixty patients were included.", "complete": True,
                             "sourceUrl": "https://example.org/a"}, "abstract", "europe-pmc")
        manuscript = repo.json_artifact({"confidential": False}, "review-manuscript")
        with repo.connect(write=True) as connection:
            connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", (
                "entry-1", review, "1", "Source", "{}", work, "verified", manuscript))
            for index in range(loci):
                connection.execute("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", (
                    f"locus-{index}", "entry-1", manuscript, f"p{index}", "Sixty patients.", "[]", "fixture"))
        repo.review_state(review, "binding", "completed")
        ids = repo.add_tasks(analysis, [(f"locus-{i}", "abstract", {"claim": f"claim {i}"}, "fixture block" if i == 2 else None)
                                        for i in range(loci)])
        with repo.connect(write=True) as connection:
            connection.execute("UPDATE corpus_tasks SET state='completed' WHERE task_id=?", (ids[0],))
            connection.execute("INSERT INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                "receipt-1", analysis, "item-1", manuscript, manuscript, "fixture", "model", "{}", "{}",
                "completed", corpus_library.now(), corpus_library.now()))
            for name, locus, state in (("a1", "locus-0", "accepted"), ("a2", "locus-0", "accepted"),
                                       ("a3", "locus-1", "rejected")):
                connection.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", (
                    name, analysis, locus, "receipt-1", "fixture", "abstract", "{}", state, corpus_library.now()))
        return ids

    @staticmethod
    def comparable(status):
        keys = ("counts", "reviewStates", "taskStates", "assessments")
        order = lambda rows: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return {key: status[key] if key == "counts" else order(status[key]) for key in keys}

    def test_existing_library_is_never_migrated_implicitly_and_migrate_backs_up_first(self):
        legacy = legacy_v2_library(self.root)
        self.populate(legacy, "legacy")
        scanned = legacy.status()
        reopened = CorpusRepository(self.root, "work/legacy")
        self.assertEqual(reopened.version, 2)
        self.assertIn("schemaUpgrade", reopened.status())
        with closing(sqlite3.connect(legacy.db.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(list(legacy.root.glob("corpus-v*-backup*")), [])
        result = reopened.migrate()
        self.assertEqual(result["migrations"], [{"from": 2, "to": 3, "backup": "corpus-v2-backup.sqlite3"}])
        with closing(sqlite3.connect((legacy.root / "corpus-v2-backup.sqlite3").as_uri() + "?mode=ro", uri=True)) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(backup.execute("SELECT count(*) FROM corpus_tasks").fetchone()[0], 3)
        self.assertEqual(self.comparable(reopened.status()), self.comparable(scanned))
        self.assertEqual(reopened.library.verify()["schema_version"], 3)
        self.assertEqual(reopened.migrate()["migrations"], [])
        self.assertEqual(len(list(legacy.root.glob("corpus-v2-backup*"))), 1)

    def test_migration_refuses_while_another_controller_owns_the_library(self):
        legacy = legacy_v2_library(self.root)
        with corpus_store.controller_lock(legacy):
            with self.assertRaisesRegex(ValidationError, "another corpus controller"):
                CorpusRepository(self.root, "work/legacy").migrate()
        self.assertEqual(CorpusRepository(self.root, "work/legacy").version, 2)
        self.assertEqual(list(legacy.root.glob("corpus-v*-backup*")), [])

    def test_migration_never_overwrites_a_backup_and_refuses_snapshots(self):
        legacy = legacy_v2_library(self.root)
        occupied = legacy.root / "corpus-v2-backup.sqlite3"
        occupied.write_bytes(b"an operator copy that must survive")
        step = legacy.migrate()["migrations"][0]
        self.assertNotEqual(step["backup"], occupied.name)
        self.assertTrue(step["backup"].startswith("corpus-v2-backup-"))
        self.assertEqual(occupied.read_bytes(), b"an operator copy that must survive")
        other = legacy_v2_library(self.root, "work/other")
        other.library.snapshot("results/legacy-snapshot")
        snapshot = CorpusRepository(self.root, "results/legacy-snapshot")
        self.assertEqual(snapshot.version, 2)
        with self.assertRaisesRegex(ValidationError, "immutable snapshot"):
            snapshot.migrate()

    def test_progress_counters_follow_every_writer_and_status_scans_no_table(self):
        repo = CorpusRepository.create(self.root, "work/corpus", "Question", "main", {"version": 2})
        self.populate(repo, "main")
        status = repo.status()
        self.assertEqual(self.comparable(status), self.comparable(repo._scanned_status(repo.analysis())))
        self.assertEqual(status["assessments"], [{"evidence_level": "abstract", "validation_state": "accepted", "count": 1},
                                                 {"evidence_level": "abstract", "validation_state": "rejected", "count": 1}])
        with traced_statements(repo) as statements:
            repo.status()
            repo.task_counts("main", "abstract")
        scans = [s for s in statements if "count(" in s.lower() or any(
            f" {table}" in s for table in ("corpus_tasks", "claim_loci", "source_sections", "assessments"))]
        self.assertEqual(scans, [])
        self.assertEqual(repo.task_counts("main", "abstract"), {"blocked": 1, "completed": 1, "pending": 1})
        repo.library.verify()
        with repo.connect(write=True) as connection:
            connection.execute("UPDATE progress_counters SET value=value+5 WHERE counter='tasks' AND state='pending'")
        with self.assertRaisesRegex(ValueError, "progress counters disagree"):
            repo.library.verify()
        self.assertTrue(repo.migrate(rebuild_counters=True)["countersRebuilt"])
        repo.library.verify()

    def test_bulk_tasks_use_bounded_transactions_one_barrier_each_and_stable_ids(self):
        repo = CorpusRepository.create(self.root, "work/corpus", "Question", "main", {"version": 2})
        manuscript = repo.json_artifact({"confidential": False}, "review-manuscript")
        review = repo.review({"doi": "10.1234/review"})
        work = repo.work({"doi": "10.1234/source", "title": "Source"})
        with repo.connect(write=True) as connection:
            connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", (
                "entry-1", review, "1", "Source", "{}", work, "verified", manuscript))
            connection.executemany("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", [
                (f"locus-{i}", "entry-1", manuscript, f"p{i}", "Claim.", "[]", "fixture") for i in range(7)])
        items = [(f"locus-{i}", "abstract", {"claim": f"claim {i}", "evidenceUnits": [{"id": "u", "text": "x" * i}]},
                  None) for i in range(7)]
        writes, barriers = [], []
        original_connect, original_sync = CorpusRepository.connect, corpus_store.sync_batch

        @contextmanager
        def counting_connect(self, *, write=False):
            if write:
                writes.append(1)
            with original_connect(self, write=write) as connection:
                yield connection

        def counting_sync(directory, paths):
            barriers.append(len(paths))
            return original_sync(directory, paths)

        with patch.object(corpus_store, "BATCH_ROWS", 3), patch.object(CorpusRepository, "connect", counting_connect), \
                patch.object(corpus_store, "sync_batch", counting_sync):
            ids = repo.add_tasks("main", items)
        self.assertEqual((len(writes), barriers), (3, [3, 3, 1]))
        self.assertEqual(ids, ["task-" + identity("main", locus, tier, spec) for locus, tier, spec, _ in items])
        stored = {task["task_id"]: task for task in repo.tasks("main")}
        for task_id, (_, _, spec, _) in zip(ids, items):
            self.assertEqual(stored[task_id]["spec"], {**spec, "id": task_id})
            self.assertEqual(repo.read_artifact(stored[task_id]["spec_sha256"]),
                             canonical({**spec, "id": task_id}).encode())
        self.assertEqual(repo.add_tasks("main", items), ids)
        self.assertEqual(repo.task_counts("main"), {"pending": 7})
        blocked = repo.add_task("main", "locus-0", "abstract", {"claim": "missing evidence"}, "evidence unavailable")
        repo.add_tasks("main", [("locus-0", "abstract", {"claim": "acquired evidence"}, None)], supersede_blocked=True)
        self.assertEqual(stored and next(t for t in repo.tasks("main", state="cancelled"))["task_id"], blocked)
        repo.library.verify()

    def test_held_connection_spans_bulk_work_and_is_released(self):
        repo = CorpusRepository.create(self.root, "work/corpus", "Question", "main", {"version": 2})
        wal = repo.db.with_name(repo.db.name + "-wal")
        with repo.held_open():
            held = repo._held
            with repo.held_open():
                self.assertIs(repo._held, held)
            repo.add_tasks("main", [])
            repo.json_artifact({"value": 1}, "test")
            # The short writer was not the last connection, so the WAL was not checkpointed away.
            self.assertTrue(wal.exists())
        self.assertIsNone(repo._held)
        # The held connection closed last and checkpointed the WAL away.
        self.assertFalse(wal.exists())
        with self.assertRaises(sqlite3.ProgrammingError):
            held.execute("SELECT 1")
        self.assertEqual(repo.status()["counts"]["source_artifacts"], len(list((repo.root / "sources").iterdir())))

    def test_reads_resolve_local_sources_quickly_and_refuse_swapped_symlinks(self):
        repo = CorpusRepository.create(self.root, "work/corpus", "Question", "main", {"version": 2})
        digest = repo.artifact(b"original evidence", "test")
        self.assertEqual(repo.read_artifacts([digest]), {digest: b"original evidence"})
        path = repo.root / "sources" / digest
        outside = self.root / "outside"
        outside.write_bytes(b"original evidence")
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises((ValueError, ValidationError)):
            repo.read_artifacts([digest])
        with self.assertRaises((ValueError, ValidationError)):
            repo.verify_artifacts([digest])

    def test_batch_barrier_uses_syncfs_once_or_fsyncs_every_new_file(self):
        repo = CorpusRepository.create(self.root, "work/corpus", "Question", "main", {"version": 2})
        calls = []
        with patch.object(corpus_store, "_syncfs_function", return_value=lambda descriptor: calls.append(descriptor) or 0):
            repo.stage_artifacts([(f"body {i}".encode(), "test", "text/plain") for i in range(10)])
        self.assertEqual(len(calls), 1)
        synced = []
        real_fsync = corpus_store.os.fsync
        with patch.object(corpus_store, "_syncfs_function", return_value=False), \
                patch.object(corpus_store.os, "fsync", side_effect=lambda fd: synced.append(fd) or real_fsync(fd)):
            rows = repo.stage_artifacts([(f"other {i}".encode(), "test", "text/plain") for i in range(10)]
                                        + [(b"other 0", "test", "text/plain")])
        self.assertEqual(len(synced), 11)  # ten new files and their directory
        self.assertEqual(len(rows), 11)
        with repo.connect(write=True) as connection:
            repo.register_artifacts(connection, rows)
        self.assertEqual(repo.read_artifacts([rows[0][0], rows[-1][0]]), {rows[0][0]: b"other 0"})


if __name__ == "__main__":
    unittest.main()
