# SPDX-License-Identifier: Apache-2.0
"""Append-only content-addressed packs: durability order, sealing, sharing and damage detection."""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from ocsl_review import corpus_packs
from ocsl_review.corpus_packs import PackIntegrityError, PackStore, build_index, scan


def flip(path: Path, offset: int) -> None:
    path.chmod(0o600)
    with path.open("r+b") as handle:
        handle.seek(offset)
        value = handle.read(1)
        handle.seek(offset)
        handle.write(bytes([value[0] ^ 0xFF]))


class PackStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = PackStore.create(self.root / "packs", target_bytes=64 * 1024)

    def test_round_trip_dedupe_and_codec_choice(self):
        text = b"cited evidence sentence. " * 400
        noise = os.urandom(4096)
        first, second = self.store.put(text), self.store.put(noise)
        self.assertEqual(first, hashlib.sha256(text).hexdigest())
        self.assertEqual(first, self.store.put(text))
        self.assertEqual(self.store.get(first), text)
        self.assertEqual(self.store.get(second), noise)
        self.assertEqual(self.store.locate(first).codec, corpus_packs.ZLIB)
        self.assertEqual(self.store.locate(second).codec, corpus_packs.RAW)
        self.assertIsNone(self.store.get("0" * 64))
        self.assertEqual(self.store.stats()["blobs"], 2)
        with self.assertRaises(PackIntegrityError):
            self.store.put(text, "f" * 64)

    def test_concurrent_writers_store_each_blob_once(self):
        bodies = [os.urandom(700) for _ in range(120)] * 3
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            digests = list(pool.map(self.store.put, bodies))
        self.assertEqual(len(set(digests)), 120)
        self.assertEqual(self.store.stats()["blobs"], 120)
        self.assertTrue(all(self.store.get(d) == b for d, b in zip(digests, bodies)))
        self.store.verify(deep=True)

    def test_single_puts_fsync_before_commit_and_batches_fsync_once_per_flush(self):
        calls = []
        real = os.fsync
        with mock.patch.object(corpus_packs.os, "fsync", side_effect=lambda fd: (calls.append(fd), real(fd))):
            for number in range(5):
                self.store.put(b"single %d " % number * 20)
            single = len(calls)
            calls.clear()
            with self.store.batch():
                for number in range(200):
                    self.store.put(b"batched %d " % number * 3)
                # Readable inside the batch before the index commit.
                self.assertEqual(self.store.get(hashlib.sha256(b"batched 7 " * 3).hexdigest()), b"batched 7 " * 3)
            batched = len(calls)
        self.assertGreaterEqual(single, 5)
        self.assertLessEqual(batched, 3)
        self.assertEqual(self.store.stats()["blobs"], 205)

    def test_packs_seal_at_target_size_and_become_read_only(self):
        for number in range(40):
            self.store.put(os.urandom(4000) + bytes([number]))
        sealed = self.store.packs("sealed")
        self.assertGreater(len(sealed), 1)
        for pack in sealed:
            path = self.store.pack_path(pack["name"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            self.assertEqual(corpus_packs.digest_path(path)[0], pack["sha256"])
        names = self.store.seal_open()
        self.assertEqual(self.store.packs("open"), [])
        self.assertTrue(all(corpus_packs.PACK_NAME.fullmatch(name) for name in names))
        self.store.verify(deep=True)

    def test_sealed_digest_uses_the_stat_cache_until_the_file_changes(self):
        digest = self.store.put(b"sealed evidence " * 100)
        (name,) = self.store.seal_open()
        expected = self.store.packs("sealed")[0]["sha256"]
        with mock.patch.object(corpus_packs, "digest_path", wraps=corpus_packs.digest_path) as hashing:
            self.store.sealed_digest(name, expected)
            self.assertEqual(hashing.call_count, 0)
            location = self.store.locate(digest)
            flip(self.store.pack_path(name), location.offset)
            with self.assertRaisesRegex(PackIntegrityError, "changed after sealing"):
                self.store.sealed_digest(name, expected)
            self.assertEqual(hashing.call_count, 1)
        with self.assertRaises(PackIntegrityError):
            self.store.get(digest)
        with self.assertRaises(PackIntegrityError):
            self.store.verify()

    def test_uncommitted_tail_of_an_interrupted_writer_is_dropped(self):
        kept = self.store.put(b"committed " * 50)
        (pack,) = self.store.packs("open")
        path = self.store.pack_path(pack["name"])
        with path.open("ab") as handle:
            handle.write(corpus_packs.RECORD.pack(corpus_packs.RECORD_TAG, b"\x01" * 32, 999, 999, 0) + b"partial")
        later = self.store.put(b"after the crash " * 30)
        self.assertEqual(self.store.get(kept), b"committed " * 50)
        self.assertEqual(self.store.get(later), b"after the crash " * 30)
        self.assertEqual([loc.sha256 for loc, _ in scan(path)], [kept, later])
        self.store.verify(deep=True)

    def test_large_files_stream_into_packs(self):
        source = self.root / "large.bin"
        source.write_bytes(b"structured full text " * 6000)
        with mock.patch.object(corpus_packs, "STREAM_THRESHOLD", 1024):
            digest, size = self.store.put_file(source)
        self.assertEqual((digest, size), (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size))
        self.assertEqual(self.store.get(digest), source.read_bytes())
        self.assertEqual(self.store.locate(digest).codec, corpus_packs.ZLIB)
        with self.assertRaises(PackIntegrityError):
            self.store.put_file(source, "0" * 64)

    def test_hard_linked_sealed_packs_and_a_closed_index_serve_a_snapshot(self):
        digests = [self.store.put(os.urandom(3000)) for _ in range(30)]
        self.store.seal_open()
        packs = self.store.packs("sealed")
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        for pack in packs:
            os.link(self.store.pack_path(pack["name"]), snapshot / pack["name"])
        build_index(snapshot / corpus_packs.INDEX, packs, [self.store.index_path])
        closed = PackStore(snapshot, readonly=True)
        self.assertEqual([closed.get(d) for d in digests], [self.store.get(d) for d in digests])
        self.assertEqual(closed.stats()["blobs"], 30)
        closed.verify(deep=True)
        with self.assertRaisesRegex(ValueError, "read-only"):
            closed.put(b"new bytes")
        self.assertEqual((snapshot / packs[0]["name"]).stat().st_ino,
                         self.store.pack_path(packs[0]["name"]).stat().st_ino)

    def test_index_built_from_two_sources_indexes_a_shared_blob_once(self):
        other = PackStore.create(self.root / "other")
        shared = b"shared evidence " * 40
        self.store.put(shared)
        other.put(shared)
        other.put(b"only in the second store")
        self.store.seal_open()
        other.seal_open()
        packs = self.store.packs("sealed") + other.packs("sealed")
        target = self.root / "merged"
        target.mkdir()
        for store in (self.store, other):
            for pack in store.packs("sealed"):
                os.link(store.pack_path(pack["name"]), target / pack["name"])
        build_index(target / corpus_packs.INDEX, packs, [self.store.index_path, other.index_path])
        merged = PackStore(target, readonly=True)
        self.assertEqual(merged.stats()["blobs"], 2)
        merged.verify(deep=True)


if __name__ == "__main__":
    unittest.main()
