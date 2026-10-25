# SPDX-License-Identifier: Apache-2.0
"""Snapshots (closed DB, hard-linked packs, Merkle root), delta branches and the study root."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from ocsl_review import corpus_library as library
from ocsl_review.cli import main
from ocsl_review.corpus_library import Library, Workspace, merkle_root
from ocsl_review.corpus_store import CorpusRepository
from ocsl_review.errors import ValidationError


def populate(repo, prefix="w", count=3):
    works = []
    for number in range(count):
        work = repo.work({"doi": f"10.1234/{prefix}{number}", "title": f"Fixture work {prefix}{number}"})
        blocks = [{"id": f"b{i}", "text": f"Block {i} of {prefix}{number}: measured evidence.", "kind": "abstract"}
                  for i in (3, 1, 2)]
        repo.document(work, {"text": " ".join(b["text"] for b in blocks), "complete": True, "blocks": blocks},
                      "abstract", "fixture")
        works.append(work)
    return works


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/literature", "Fixture question", "first", {"v": 1})
        self.works = populate(self.repo)

    def test_snapshot_is_a_closed_database_hard_linked_packs_and_a_merkle_root(self):
        result = self.repo.library.snapshot("results/snapshot")
        snapshot = Library(self.repo.workspace, "results/snapshot")
        manifest = json.loads(snapshot.manifest_path.read_text())
        self.assertEqual((manifest["format"], manifest["version"]), ("ocsl-literature-snapshot", 2))
        self.assertEqual(manifest["merkle"]["root"], merkle_root(manifest["files"]))
        self.assertEqual(result["merkle_root"], manifest["merkle"]["root"])
        self.assertEqual(manifest["database"], next(f for f in manifest["files"] if f["path"] == "corpus.sqlite3"))
        self.assertNotIn("sources", manifest)
        self.assertEqual(list((snapshot.root / "sources").iterdir()), [])
        for item in manifest["files"]:
            path = snapshot.root / item["path"]
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            self.assertEqual(library.digest_file(path), item["sha256"])
        live = {p["name"]: p for p in self.repo.library.packs.packs("sealed")}
        packs = [f for f in manifest["files"] if f["path"].startswith("packs/pack-")]
        self.assertTrue(packs)
        for item in packs:
            name = item["path"][6:]
            self.assertEqual((snapshot.root / item["path"]).stat().st_ino,
                             self.repo.library.packs.pack_path(name).stat().st_ino)
            self.assertEqual(live[name]["sha256"], item["sha256"])
        for level in ("light", "standard", "deep"):
            checked = snapshot.verify(level=level)
            self.assertEqual(checked["merkle_root"], manifest["merkle"]["root"])
            self.assertEqual(checked["active_analysis_id"], "first")
        self.assertEqual(checked["counts"]["source_sections"], 9)
        self.assertLess(result["manifest_bytes"], 10 * 1024 * 1024)
        self.assertLess(sum(1 for _ in snapshot.root.rglob("*")), 12)
        evidence = CorpusRepository(self.root, "results/snapshot").evidence(self.works[0], "abstract")
        self.assertEqual([u["text"][:7] for u in evidence["evidenceUnits"]], ["Block 3", "Block 1", "Block 2"])

    def test_snapshot_verification_levels_detect_damage(self):
        self.repo.library.snapshot("results/snapshot")
        snapshot = Library(self.repo.workspace, "results/snapshot")
        manifest = json.loads(snapshot.manifest_path.read_text())
        pack = snapshot.root / next(f["path"] for f in manifest["files"] if f["path"].startswith("packs/pack-"))
        stray = snapshot.root / "packs" / ("pack-" + "0" * 32 + ".pack")
        stray.write_bytes(b"unlisted")
        with self.assertRaisesRegex(ValueError, "packs differ"):
            snapshot.verify(level="light")
        stray.unlink()
        pack.chmod(0o644)  # The snapshot pack is the library's pack (hard link); damage shows in both.
        with pack.open("r+b") as handle:
            handle.seek(80)
            value = handle.read(1)
            handle.seek(80)
            handle.write(bytes([value[0] ^ 0xFF]))
        snapshot.verify(level="light")  # Light checks sizes and the Merkle root, not pack contents.
        with self.assertRaisesRegex(ValueError, "hash differs"):
            snapshot.verify()
        with self.assertRaises(ValueError):
            self.repo.library.verify()
        tampered = dict(manifest, merkle={**manifest["merkle"], "root": "0" * 64})
        snapshot.manifest_path.chmod(0o644)
        snapshot.manifest_path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "Merkle root"):
            snapshot.verify(level="light")

    def test_loose_artifacts_of_older_libraries_are_packed_into_the_snapshot(self):
        body = b"retained loose source bytes"
        digest = hashlib.sha256(body).hexdigest()
        (self.repo.root / "sources" / digest).write_bytes(body)
        with self.repo.connect(write=True) as connection:
            connection.execute("INSERT INTO source_artifacts VALUES (?,?,?,?,?,?)",
                               (digest, "sources/" + digest, len(body), "text/plain", "fixture", library.now()))
        self.repo.library.snapshot("results/snapshot")
        snapshot = Library(self.repo.workspace, "results/snapshot")
        self.assertEqual(snapshot.locate(digest)[0], "pack")
        self.assertEqual(snapshot.read_blob(digest), body)
        snapshot.verify(level="deep")
        (self.repo.root / "sources" / digest).chmod(0o600)
        (self.repo.root / "sources" / digest).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
            self.repo.library.snapshot("results/second")
        self.assertFalse((self.root / "results/second").exists())

    def test_resnapshot_and_moved_snapshots_stay_valid(self):
        self.repo.library.snapshot("results/first")
        first = CorpusRepository(self.root, "results/first")
        second = first.library.snapshot("results/second")
        self.assertEqual(second["counts"], Library(self.repo.workspace, "results/first").verify()["counts"])
        other = self.root / "other"
        other.mkdir()
        (self.root / "results/second").rename(other / "moved")
        self.assertEqual(Library(Workspace(other), "moved").verify(level="deep")["counts"]["works"], 3)


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/literature", "Fixture question", "first", {"v": 1})
        self.works = populate(self.repo)
        self.repo.library.snapshot("results/snapshot")
        self.parent = Library(self.repo.workspace, "results/snapshot")
        self.parent_digest = library.digest_file(self.parent.manifest_path)
        self.parent_db = library.digest_file(self.parent.db)
        result = self.parent.branch("work/branch", "A different question", "second", {"v": 2})
        self.assertTrue(result["delta"])
        self.branch = CorpusRepository(self.root, "work/branch")

    def assertParentUnchanged(self):
        self.assertEqual(library.digest_file(self.parent.manifest_path), self.parent_digest)
        self.assertEqual(library.digest_file(self.parent.db), self.parent_db)
        self.parent.verify(level="deep")

    def test_branch_reads_the_parent_through_attach_without_copying_it(self):
        with contextlib.closing(sqlite3.connect(self.branch.db.as_uri() + "?mode=ro", uri=True)) as raw:
            # The branch database holds only its own rows, in prefixed tables.
            self.assertEqual(raw.execute("SELECT count(*) FROM ocsl_delta_works").fetchone()[0], 0)
            self.assertEqual(raw.execute("SELECT count(*) FROM ocsl_delta_analyses").fetchone()[0], 1)
        self.assertEqual(self.branch.status()["counts"]["works"], 3)
        self.assertEqual({row["analysis_id"] for row in self.branch.library.verify()["analyses"]}, {"first", "second"})
        self.assertEqual(self.branch.analysis()["analysis_id"], "second")
        units = self.branch.evidence(self.works[1], "abstract")["evidenceUnits"]
        self.assertEqual([u["text"][:7] for u in units], ["Block 3", "Block 1", "Block 2"])
        self.assertEqual(list((self.branch.root / "sources").iterdir()), [])
        self.assertParentUnchanged()

    def test_writes_stay_in_the_branch_and_parent_rows_copy_on_write(self):
        self.branch.work({"doi": "10.1234/w0", "title": "Corrected title in the branch"})
        new = populate(self.branch, prefix="b", count=1)[0]
        digest = self.branch.artifact(b"branch-only artifact", "fixture", "text/plain")
        self.assertEqual(self.branch.read_artifact(digest), b"branch-only artifact")
        with self.branch.connect() as connection:
            titles = dict(connection.execute("SELECT work_id,title FROM works").fetchall())
        self.assertEqual(titles[self.works[0]], "Corrected title in the branch")
        self.assertIn(new, titles)
        with self.parent.connect() as connection:
            self.assertEqual(connection.execute("SELECT title FROM works WHERE work_id=?", (self.works[0],)).fetchone()[0],
                             "Fixture work w0")
            self.assertIsNone(connection.execute("SELECT 1 FROM works WHERE work_id=?", (new,)).fetchone())
        with contextlib.closing(sqlite3.connect(self.branch.db.as_uri() + "?mode=ro", uri=True)) as raw:
            self.assertEqual(raw.execute("SELECT count(*) FROM ocsl_delta_works").fetchone()[0], 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "parent snapshot are immutable"):
            with self.branch.connect(write=True) as connection:
                connection.execute("DELETE FROM works WHERE work_id=?", (self.works[1],))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.branch.connect(write=True) as connection:
                connection.execute("UPDATE source_sections SET extracted_text='changed'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            with self.branch.connect(write=True) as connection:
                connection.execute("INSERT INTO source_sections VALUES ('s','missing-work',?,'l','k','t',?,'v',1)",
                                   (digest, hashlib.sha256(b"t").hexdigest()))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "keys of shared rows cannot change"):
            with self.branch.connect(write=True) as connection:
                connection.execute("UPDATE works SET work_id='renamed' WHERE work_id=?", (self.works[2],))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "exists in the parent snapshot"):
            with self.branch.connect(write=True) as connection:
                library.add_analysis(connection, self.branch.analysis()["corpus_id"], "Q", "first")
        self.branch.library.verify()
        self.assertParentUnchanged()

    def test_verification_resolves_references_over_parent_and_branch(self):
        with contextlib.closing(sqlite3.connect(self.branch.db)) as raw, raw:
            # Bypass the branch triggers: verification must still find the dangling reference.
            raw.execute("INSERT INTO ocsl_delta_work_identifiers VALUES ('nowhere','doi','10.1/x',?)", ("0" * 64,))
        with self.assertRaisesRegex(ValueError, "foreign key"):
            self.branch.library.verify()

    def test_branch_ids_cannot_reuse_a_parent_analysis(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.parent.branch("work/clash", "Q", "first")
        self.assertFalse((self.root / "work/clash").exists())

    def test_branch_snapshot_flattens_parent_and_branch_rows(self):
        populate(self.branch, prefix="b", count=2)
        self.branch.work({"doi": "10.1234/w0", "title": "Corrected title in the branch"})
        result = self.branch.library.snapshot("results/branch-snapshot")
        flat = Library(self.repo.workspace, "results/branch-snapshot")
        checked = flat.verify(level="deep")
        self.assertEqual(checked["counts"]["works"], 5)
        self.assertEqual(checked["active_analysis_id"], "second")
        self.assertIsNone(flat.parent)
        with flat.connect() as connection:
            self.assertEqual(connection.execute("SELECT title FROM works WHERE work_id=?", (self.works[0],)).fetchone()[0],
                             "Corrected title in the branch")
        self.assertTrue(result["portable"])
        other = self.root / "elsewhere"
        other.mkdir()
        (self.root / "results/branch-snapshot").rename(other / "moved")
        self.assertEqual(Library(Workspace(other), "moved").verify()["counts"]["source_sections"], 15)
        self.assertParentUnchanged()

    def test_branch_requires_an_unchanged_parent(self):
        self.parent.manifest_path.chmod(0o644)
        self.parent.manifest_path.write_text(self.parent.manifest_path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "parent snapshot manifest has changed"):
            CorpusRepository(self.root, "work/branch")


class RootTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.workspace = base / "workspace"
        self.study = base / "study-volume"
        self.datasets = base / "datasets"
        for path in (self.workspace, self.study, self.datasets):
            path.mkdir()

    def test_study_root_may_sit_outside_the_workspace(self):
        with mock.patch.dict(os.environ, {library.STUDY_ROOT_ENV: str(self.study)}):
            repo = CorpusRepository.create(self.workspace, str(self.study / "literature"), "Q", "study", {})
            populate(repo, count=1)
            self.assertTrue((self.study / "literature/corpus.sqlite3").is_file())
            self.assertEqual(list(self.workspace.iterdir()), [])
            repo.library.snapshot(str(self.study / "frozen"))
            Library(Workspace(self.workspace), str(self.study / "frozen")).verify(level="deep")
            outside = self.study.parent / "elsewhere"
            outside.mkdir()
            with self.assertRaisesRegex(ValueError, "workspace-relative"):
                Workspace(self.workspace).path(str(outside / "library"))
            with self.assertRaisesRegex(ValueError, "workspace-relative"):
                Workspace(self.workspace).path(str(self.study))
            (self.study / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                Workspace(self.workspace).path(str(self.study / "link" / "library"))
        with self.assertRaisesRegex(ValueError, "workspace-relative"):
            Workspace(self.workspace).path(str(self.study / "literature"))
        with mock.patch.dict(os.environ, {library.STUDY_ROOT_ENV: "relative/path"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                Workspace(self.workspace)

    def test_parents_on_a_dataset_root_are_read_only(self):
        seed = CorpusRepository.create(self.workspace, "seed-source", "Q", "seed", {})
        populate(seed, count=2)
        seed.library.snapshot("seed")
        os.rename(self.workspace / "seed", self.datasets / "seed")
        with mock.patch.dict(os.environ, {library.DATASET_ROOT_ENV: str(self.datasets)}):
            workspace = Workspace(self.workspace)
            parent = Library(workspace, str(self.datasets / "seed"))
            parent.branch("work/followup", "Follow-up question", "followup", {})
            branch = CorpusRepository(self.workspace, "work/followup")
            self.assertEqual(branch.status()["counts"]["works"], 2)
            with self.assertRaisesRegex(ValueError, "read-only"):
                library.initialize(workspace, str(self.datasets / "new"), "Q", "x")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_loose_library_packs_offline_and_keeps_its_loose_files(self):
        workspace = Workspace(self.root)
        library.initialize(workspace, "work/old", "Q", "old")
        packs = self.root / "work/old/packs"
        for item in packs.iterdir():
            item.unlink()
        packs.rmdir()  # A library written before packs existed.
        repo = CorpusRepository(self.root, "work/old")
        repo.migrate()
        populate(repo, count=2)
        loose = sorted((self.root / "work/old/sources").iterdir())
        self.assertGreater(len(loose), 2)
        code = main(["corpus", "pack", "--workspace", str(self.root), "--library", "work/old"])
        self.assertEqual(code, 0)
        repo = CorpusRepository(self.root, "work/old")
        self.assertEqual(sorted((self.root / "work/old/sources").iterdir()), loose)
        with repo.connect() as connection:
            digests = [row[0] for row in connection.execute("SELECT sha256 FROM source_artifacts")]
        self.assertTrue(all(repo.library.locate(d)[0] == "pack" for d in digests))
        self.assertEqual(main(["corpus", "verify", "--workspace", str(self.root), "--library", "work/old",
                               "--level", "deep"]), 0)
        repo.library.snapshot("results/snapshot")
        self.assertEqual(list((self.root / "results/snapshot/sources").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
