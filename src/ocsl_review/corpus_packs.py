# SPDX-License-Identifier: Apache-2.0
"""Append-only content-addressed packs for corpus artifacts (Python stdlib).

A pack is an append-only file of self-describing records (tag, SHA-256, raw and
stored length, codec, then the stored bytes). A small SQLite index maps each
SHA-256 to its pack, offset and codec. Writers append under an exclusive lock
and fsync before the index commit, so an indexed blob is always durable; a
batch defers that fsync to one per pack flush. Packs are sealed (fsynced,
hashed, mode 0444) when they reach the target size or when a snapshot is taken,
and sealed packs are shared between snapshots by hard link. Every read checks
the blob's SHA-256, so damage is reported, never returned.

Loose ``sources/<sha256>`` files of older libraries remain readable through the
library (``corpus_library.Library``); this module only manages packs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import struct
import threading
import uuid
import zlib


FILE_MAGIC = b"OCSLPACK"
FORMAT_VERSION = 1
FILE_HEADER = struct.Struct(">8sII")      # magic, format version, reserved
RECORD = struct.Struct(">4s32sQQB3x")     # tag, sha256, raw length, stored length, codec
RECORD_TAG = b"BLOB"
RAW, ZLIB = 0, 1
TARGET_PACK_BYTES = 256 * 1024 * 1024
INDEX = "index.sqlite3"
LOCK = ".lock"
PACK_NAME = re.compile(r"pack-[0-9a-f]{32}\.pack")
SHA256 = re.compile(r"[0-9a-f]{64}")
CHUNK = 1024 * 1024
STREAM_THRESHOLD = 64 * CHUNK  # larger files are streamed into a pack instead of read whole

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('open','sealed')),
  byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
  blob_count INTEGER NOT NULL CHECK(blob_count >= 0),
  sha256 TEXT CHECK(sha256 IS NULL OR length(sha256) = 64),
  created_at TEXT NOT NULL,
  sealed_at TEXT,
  CHECK(state = 'open' OR sha256 IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY CHECK(length(sha256) = 64),
  pack_id INTEGER NOT NULL REFERENCES packs(id),
  offset INTEGER NOT NULL CHECK(offset >= 0),
  stored_length INTEGER NOT NULL CHECK(stored_length >= 0),
  raw_length INTEGER NOT NULL CHECK(raw_length >= 0),
  codec INTEGER NOT NULL CHECK(codec IN (0, 1))
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS blobs_by_pack ON blobs(pack_id, offset);
CREATE TABLE IF NOT EXISTS stat_cache (
  name TEXT PRIMARY KEY, dev INTEGER NOT NULL, ino INTEGER NOT NULL, size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


class PackIntegrityError(ValueError):
    """Stored bytes no longer match their recorded identity."""


@dataclass(frozen=True)
class Location:
    sha256: str
    pack: str
    offset: int
    stored_length: int
    raw_length: int
    codec: int


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_path(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while block := os.read(descriptor, CHUNK):
            digest.update(block)
            size += len(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = os.stat(path, follow_symlinks=False)
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def encode(body: bytes, level: int) -> tuple[int, bytes]:
    """Compress when it pays; already compressed media are stored as they are."""
    if level <= 0 or len(body) < 64:
        return RAW, body
    probe = body[:65536]
    if len(probe) == 65536 and len(zlib.compress(probe, 1)) > 0.9 * len(probe):
        return RAW, body
    stored = zlib.compress(body, level)
    return (ZLIB, stored) if len(stored) < len(body) else (RAW, body)


def decode(sha256: str, codec: int, stored: bytes, raw_length: int) -> bytes:
    try:
        body = zlib.decompress(stored) if codec == ZLIB else stored
    except zlib.error:
        raise PackIntegrityError(f"stored blob cannot be decoded: {sha256}") from None
    if len(body) != raw_length or hashlib.sha256(body).hexdigest() != sha256:
        raise PackIntegrityError(f"registered artifact hash mismatch: {sha256}")
    return body


def _pread(descriptor: int, length: int, offset: int) -> bytes:
    parts, remaining = [], length
    while remaining:
        block = os.pread(descriptor, min(remaining, 64 * CHUNK), offset)
        if not block:
            break
        parts.append(block)
        offset += len(block)
        remaining -= len(block)
    return b"".join(parts)


def _write_all(descriptor: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(descriptor, view, offset)
        if written <= 0:
            raise OSError(errno.EIO, "short pack write")
        view, offset = view[written:], offset + written


def scan(path: Path, end: int | None = None, *, payload: bool = False):
    """Yield (Location, stored bytes or None) for every record before ``end``."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(descriptor).st_size if end is None else end
        header = _pread(descriptor, FILE_HEADER.size, 0)
        if len(header) != FILE_HEADER.size or FILE_HEADER.unpack(header)[:2] != (FILE_MAGIC, FORMAT_VERSION):
            raise PackIntegrityError(f"not a pack file: {path.name}")
        offset = FILE_HEADER.size
        while offset < size:
            raw = _pread(descriptor, RECORD.size, offset)
            if len(raw) != RECORD.size:
                raise PackIntegrityError(f"truncated record header in {path.name} at {offset}")
            tag, digest, raw_length, stored_length, codec = RECORD.unpack(raw)
            if tag != RECORD_TAG or codec not in (RAW, ZLIB):
                raise PackIntegrityError(f"invalid record in {path.name} at {offset}")
            start = offset + RECORD.size
            if start + stored_length > size:
                raise PackIntegrityError(f"truncated record payload in {path.name} at {offset}")
            stored = _pread(descriptor, stored_length, start) if payload else None
            yield Location(digest.hex(), path.name, start, stored_length, raw_length, codec), stored
            offset = start + stored_length
    finally:
        os.close(descriptor)


class PackStore:
    """One directory of packs and its index.

    ``readonly`` stores (closed snapshot indexes) are opened immutable. Writable
    stores serialize appends with ``flock`` on ``.lock``; inside ``batch()`` the
    calling thread keeps the lock until the batch is flushed.
    """

    def __init__(self, root: Path, *, readonly: bool = False, target_bytes: int = TARGET_PACK_BYTES,
                 level: int = 6):
        self.root = Path(root).absolute()
        self.index_path = self.root / INDEX
        if self.root.is_symlink() or not self.root.is_dir() or self.index_path.is_symlink() \
                or not self.index_path.is_file():
            raise ValueError(f"pack index is missing: {self.root}")
        self.readonly = readonly
        self.target_bytes = max(int(target_bytes), FILE_HEADER.size + RECORD.size + 1)
        self.level = level
        self._state = threading.Lock()
        self._owner = None
        self._writer = None
        self._pending: dict[str, Location] = {}

    @classmethod
    def create(cls, root: Path, **options) -> "PackStore":
        root = Path(root).absolute()
        root.mkdir(mode=0o700)
        connection = sqlite3.connect(root / INDEX)
        try:
            connection.executescript(INDEX_SCHEMA)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.commit()
        finally:
            connection.close()
        return cls(root, **options)

    # ------------------------------------------------------------------ index
    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        # Only a closed index may be opened immutable: a live WAL index keeps
        # recent commits in its -wal file, which immutable readers would ignore.
        if write:
            uri = self.index_path.as_uri() + "?mode=rw"
        elif self.readonly:
            uri = self.index_path.as_uri() + "?mode=ro&immutable=1"
        else:
            uri = self.index_path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=60)
        connection.execute("PRAGMA busy_timeout=60000")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        return connection

    @contextmanager
    def _index(self):
        # Short-lived connections: no handle outlives the call or the calling thread.
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def close(self) -> None:
        """Kept for callers that release stores explicitly; nothing stays open between calls."""

    # ------------------------------------------------------------------ reads
    def locate(self, sha256: str) -> Location | None:
        with self._state:
            pending = self._pending.get(sha256)
        if pending is not None:
            return pending
        with self._index() as connection:
            row = connection.execute(
                "SELECT p.name,b.offset,b.stored_length,b.raw_length,b.codec FROM blobs b JOIN packs p ON p.id=b.pack_id "
                "WHERE b.sha256=?", (sha256,)).fetchone()
        return Location(sha256, *row) if row else None

    def contains(self, sha256: str) -> bool:
        return self.locate(sha256) is not None

    def pack_path(self, name: str) -> Path:
        if not PACK_NAME.fullmatch(name):
            raise PackIntegrityError(f"invalid pack name: {name}")
        return self.root / name

    def read(self, location: Location) -> bytes:
        try:
            descriptor = os.open(self.pack_path(location.pack), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise PackIntegrityError(f"pack is missing: {location.pack}") from None
        try:
            stored = _pread(descriptor, location.stored_length, location.offset)
        finally:
            os.close(descriptor)
        if len(stored) != location.stored_length:
            raise PackIntegrityError(f"pack is truncated: {location.pack}")
        return decode(location.sha256, location.codec, stored, location.raw_length)

    def get(self, sha256: str) -> bytes | None:
        location = self.locate(sha256)
        return None if location is None else self.read(location)

    def stats(self) -> dict:
        with self._index() as connection:
            blobs, stored, raw = connection.execute(
                "SELECT count(*),coalesce(sum(stored_length),0),coalesce(sum(raw_length),0) FROM blobs").fetchone()
            packs = dict(connection.execute("SELECT state,count(*) FROM packs GROUP BY state").fetchall())
        return {"blobs": blobs, "storedBytes": stored, "rawBytes": raw,
                "openPacks": packs.get("open", 0), "sealedPacks": packs.get("sealed", 0)}

    def packs(self, state: str | None = None) -> list[dict]:
        query = "SELECT id,name,state,byte_count,blob_count,sha256 FROM packs"
        params: tuple = ()
        if state:
            query, params = query + " WHERE state=?", (state,)
        keys = ("id", "name", "state", "byte_count", "blob_count", "sha256")
        with self._index() as connection:
            return [dict(zip(keys, row)) for row in connection.execute(query + " ORDER BY name", params)]

    # ----------------------------------------------------------------- writes
    @contextmanager
    def _locked(self):
        if self.readonly:
            raise ValueError("pack store is read-only")
        if self._owner == threading.get_ident():
            yield self._writer
            return
        descriptor = os.open(self.root / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            writer = _Writer(self)
            self._owner, self._writer = threading.get_ident(), writer
            try:
                yield writer
                writer.flush()
            finally:
                writer.release()
                self._owner, self._writer = None, None
                with self._state:
                    self._pending.clear()
        finally:
            os.close(descriptor)

    @contextmanager
    def batch(self):
        """Hold the writer lock; fsync and commit once per pack flush."""
        if self._owner == threading.get_ident():
            yield self
            return
        with self._locked() as writer:
            writer.deferred = True
            yield self

    def put(self, body: bytes, sha256: str | None = None) -> str:
        digest = hashlib.sha256(body).hexdigest()
        if sha256 is not None and sha256 != digest:
            raise PackIntegrityError("artifact bytes do not match their declared SHA-256")
        # Outside a batch, known blobs return without the lock; a batch owner asks its writer.
        if self._owner != threading.get_ident() and self.contains(digest):
            return digest
        codec, stored = encode(body, self.level)
        with self._locked() as writer:
            if not writer.known(digest):
                writer.append(digest, len(body), codec, stored)
        return digest

    def put_encoded(self, sha256: str, raw_length: int, codec: int, stored: bytes, *, verified: bool = False,
                    new: bool = False) -> bool:
        """Append bytes encoded by the caller (e.g. in worker threads).

        The encoding is decoded and hashed first unless the caller encoded bytes it
        had just verified against ``sha256`` itself (``verified=True``). ``new`` skips
        the duplicate lookup when the caller guarantees a blob is not stored yet
        (a fresh store filled from distinct digests); the index still rejects duplicates.
        """
        if not verified:
            decode(sha256, codec, stored, raw_length)
        if not new and self._owner != threading.get_ident() and self.contains(sha256):
            return False
        with self._locked() as writer:
            if not new and writer.known(sha256):
                return False
            writer.append(sha256, raw_length, codec, stored)
        return True

    def put_file(self, path: Path, sha256: str | None = None) -> tuple[str, int]:
        """Stream a regular file into a pack without holding it in memory."""
        digest, size = digest_path(path)
        if sha256 is not None and sha256 != digest:
            raise PackIntegrityError(f"registered artifact hash mismatch: {sha256}")
        if self.contains(digest):
            return digest, size
        if size <= STREAM_THRESHOLD:
            body = Path(path).read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise PackIntegrityError("source changed while it was packed")
            self.put(body, digest)
            return digest, size
        with self._locked() as writer:
            if not writer.known(digest):
                writer.append_file(digest, size, Path(path), self.level)
        return digest, size

    def seal_open(self) -> list[str]:
        """Seal every open pack (snapshot boundary)."""
        with self._locked() as writer:
            return writer.seal_all()

    def sealed_digest(self, name: str, expected: str, *, remember: bool = False) -> str:
        """Check a sealed pack by its stat signature, rehashing only when it changed.

        Only writers (``remember=True``, e.g. a snapshot) refresh the stat cache;
        verification itself never writes.
        """
        path = self.pack_path(name)
        current = signature(path)
        row = None
        if not self.readonly:
            with self._index() as connection:
                row = connection.execute("SELECT dev,ino,size,mtime_ns,ctime_ns,sha256 FROM stat_cache WHERE name=?",
                                         (name,)).fetchone()
        if row and tuple(row[:5]) == current and row[5] == expected:
            return expected
        digest, size = digest_path(path)
        if digest != expected or size != current[2]:
            raise PackIntegrityError(f"sealed pack changed after sealing: {name}")
        if remember and not self.readonly:
            self.remember(name, expected)
        return digest

    def remember(self, name: str, sha256: str) -> None:
        """Record the current stat signature of a verified sealed pack."""
        if self.readonly:
            return
        current = signature(self.pack_path(name))
        connection = self._connect(write=True)
        try:
            connection.execute("INSERT OR REPLACE INTO stat_cache VALUES (?,?,?,?,?,?,?)", (name, *current, sha256))
            connection.commit()
        finally:
            connection.close()

    # ----------------------------------------------------------------- checks
    def verify(self, *, deep: bool = False) -> dict:
        """Sealed packs by digest (stat-cached unless deep); open packs blob by blob.

        Deep verification also parses every committed record sequentially and
        decodes and hashes each blob.
        """
        with self._index() as connection:
            return self._verify(connection, deep)

    def _verify(self, connection, deep):
        checked = 0
        for pack in self.packs():
            path = self.pack_path(pack["name"])
            if path.is_symlink() or not path.is_file():
                raise PackIntegrityError(f"pack is missing: {pack['name']}")
            size = path.stat().st_size
            if pack["state"] == "sealed":
                if size != pack["byte_count"]:
                    raise PackIntegrityError(f"sealed pack size changed: {pack['name']}")
                if deep:
                    if digest_path(path)[0] != pack["sha256"]:
                        raise PackIntegrityError(f"sealed pack changed after sealing: {pack['name']}")
                else:
                    self.sealed_digest(pack["name"], pack["sha256"])
            elif size < pack["byte_count"]:
                raise PackIntegrityError(f"open pack is shorter than its index: {pack['name']}")
            rows = {offset: (sha, stored, raw, codec) for sha, offset, stored, raw, codec in connection.execute(
                "SELECT sha256,offset,stored_length,raw_length,codec FROM blobs WHERE pack_id=?", (pack["id"],))}
            if len(rows) != pack["blob_count"]:
                raise PackIntegrityError(f"pack blob count differs from its index: {pack['name']}")
            if deep:
                seen = 0
                for location, stored in scan(path, pack["byte_count"], payload=True):
                    decode(location.sha256, location.codec, stored, location.raw_length)
                    row = rows.get(location.offset)
                    if row is not None:
                        if row != (location.sha256, location.stored_length, location.raw_length, location.codec):
                            raise PackIntegrityError(f"index row differs from its pack record: {row[0]}")
                        seen += 1
                    checked += 1
                if seen != len(rows):
                    raise PackIntegrityError(f"index lists blobs that are not records of {pack['name']}")
            elif pack["state"] == "open":
                for offset, (sha, stored, raw, codec) in rows.items():
                    if offset + stored > pack["byte_count"]:
                        raise PackIntegrityError(f"blob lies beyond the committed pack end: {sha}")
                    self.read(Location(sha, pack["name"], offset, stored, raw, codec))
                    checked += 1
        orphans = connection.execute(
            "SELECT count(*) FROM blobs WHERE pack_id NOT IN (SELECT id FROM packs)").fetchone()[0]
        if orphans:
            raise PackIntegrityError("pack index lists blobs of unknown packs")
        return {**self.stats(), "blobsChecked": checked, "deep": deep}


class _Writer:
    """State of one lock holder: the open pack, deferred index rows, fsync set."""

    def __init__(self, store: PackStore):
        self.store = store
        self.connection = store._connect(write=True)
        self.deferred = False
        self.pack = None          # (id, name, descriptor, end offset, blob count)
        self.rows: list[tuple] = []
        self.dirty: set[int] = set()

    def known(self, sha256: str) -> bool:
        with self.store._state:
            if sha256 in self.store._pending:
                return True
        return self.connection.execute("SELECT 1 FROM blobs WHERE sha256=?", (sha256,)).fetchone() is not None

    def _open(self):
        if self.pack is not None:
            return self.pack
        row = self.connection.execute(
            "SELECT id,name,byte_count,blob_count FROM packs WHERE state='open' ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            pack_id, name, committed, count = row
            path = self.store.pack_path(name)
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            # Bytes past the committed end belong to an interrupted writer: drop them.
            if os.fstat(descriptor).st_size != committed:
                os.ftruncate(descriptor, committed)
        else:
            name = f"pack-{uuid.uuid4().hex}.pack"
            path = self.store.pack_path(name)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            _write_all(descriptor, FILE_HEADER.pack(FILE_MAGIC, FORMAT_VERSION, 0), 0)
            os.fsync(descriptor)
            cursor = self.connection.execute(
                "INSERT INTO packs(name,state,byte_count,blob_count,created_at) VALUES (?,?,?,?,?)",
                (name, "open", FILE_HEADER.size, 0, now()))
            self.connection.commit()
            pack_id, committed, count = cursor.lastrowid, FILE_HEADER.size, 0
        self.pack = [pack_id, name, descriptor, committed, count]
        return self.pack

    def append(self, sha256: str, raw_length: int, codec: int, stored: bytes) -> None:
        pack = self._open()
        pack_id, name, descriptor, end, _ = pack
        header = RECORD.pack(RECORD_TAG, bytes.fromhex(sha256), raw_length, len(stored), codec)
        _write_all(descriptor, header + stored, end)
        self._added(sha256, raw_length, codec, len(stored), RECORD.size + len(stored))

    def append_file(self, sha256: str, size: int, path: Path, level: int) -> None:
        pack = self._open()
        pack_id, name, descriptor, end, _ = pack
        source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            first = os.read(source, CHUNK)
            codec, _ = encode(first, level) if len(first) >= 64 else (RAW, first)
            compressor = zlib.compressobj(level) if codec == ZLIB else None
            digest, offset, total = hashlib.sha256(), end + RECORD.size, 0
            block = first
            while block:
                digest.update(block)
                total += len(block)
                data = compressor.compress(block) if compressor else block
                if data:
                    _write_all(descriptor, data, offset)
                    offset += len(data)
                block = os.read(source, CHUNK)
            if compressor:
                tail = compressor.flush()
                _write_all(descriptor, tail, offset)
                offset += len(tail)
        finally:
            os.close(source)
        if digest.hexdigest() != sha256 or total != size:
            raise PackIntegrityError("source changed while it was packed")
        stored = offset - end - RECORD.size
        _write_all(descriptor, RECORD.pack(RECORD_TAG, bytes.fromhex(sha256), size, stored, codec), end)
        self._added(sha256, size, codec, stored, RECORD.size + stored)

    def _added(self, sha256, raw_length, codec, stored_length, record_length):
        pack_id, name, descriptor, end, count = self.pack
        location = Location(sha256, name, end + RECORD.size, stored_length, raw_length, codec)
        self.rows.append((sha256, pack_id, location.offset, stored_length, raw_length, codec))
        with self.store._state:
            self.store._pending[sha256] = location
        self.pack[3], self.pack[4] = end + record_length, count + 1
        self.dirty.add(pack_id)
        if not self.deferred:
            self.flush()
        if self.pack[3] >= self.store.target_bytes:
            self.flush()
            self._seal_current()

    def flush(self) -> None:
        """fsync the touched pack, then commit its index rows (durable before visible)."""
        if self.pack is not None and self.dirty:
            os.fsync(self.pack[2])
        if self.rows or self.dirty:
            if self.pack is not None:
                pack_id, _, _, end, count = self.pack
                self.connection.execute("UPDATE packs SET byte_count=?,blob_count=? WHERE id=?", (end, count, pack_id))
            self.connection.executemany("INSERT OR IGNORE INTO blobs VALUES (?,?,?,?,?,?)", self.rows)
            self.connection.commit()
            with self.store._state:
                for row in self.rows:
                    self.store._pending.pop(row[0], None)
            self.rows.clear()
            self.dirty.clear()

    def _seal_current(self) -> str | None:
        if self.pack is None:
            return None
        self.flush()
        pack_id, name, descriptor, end, count = self.pack
        os.fsync(descriptor)
        digest = hashlib.sha256()
        offset = 0
        while offset < end:
            block = _pread(descriptor, min(64 * CHUNK, end - offset), offset)
            if not block:
                raise PackIntegrityError(f"pack is shorter than its committed end: {name}")
            digest.update(block)
            offset += len(block)
        os.fchmod(descriptor, 0o444)
        os.close(descriptor)
        self.pack = None
        sha = digest.hexdigest()
        self.connection.execute("UPDATE packs SET state='sealed',sha256=?,sealed_at=?,byte_count=?,blob_count=? WHERE id=?",
                                (sha, now(), end, count, pack_id))
        current = signature(self.store.pack_path(name))
        self.connection.execute("INSERT OR REPLACE INTO stat_cache VALUES (?,?,?,?,?,?,?)", (name, *current, sha))
        self.connection.commit()
        return name

    def seal_all(self) -> list[str]:
        sealed = []
        self.flush()
        if self.pack is not None:
            sealed.append(self._seal_current())
        for (pack_id,) in self.connection.execute("SELECT id FROM packs WHERE state='open'").fetchall():
            name, committed, count = self.connection.execute(
                "SELECT name,byte_count,blob_count FROM packs WHERE id=?", (pack_id,)).fetchone()
            descriptor = os.open(self.store.pack_path(name), os.O_RDWR | os.O_NOFOLLOW)
            if os.fstat(descriptor).st_size != committed:
                os.ftruncate(descriptor, committed)
            self.pack = [pack_id, name, descriptor, committed, count]
            sealed.append(self._seal_current())
        return [name for name in sealed if name]

    def release(self) -> None:
        try:
            if self.pack is not None:
                os.close(self.pack[2])
                self.pack = None
        finally:
            self.connection.close()


def build_index(destination: Path, packs: list[dict], sources: list[Path]) -> Path:
    """Write a closed index for the listed sealed packs from one or more source indexes.

    A blob stored in two packs is indexed once (first source wins); every blob of
    every listed pack must be resolvable in the result.
    """
    destination = Path(destination)
    connection = sqlite3.connect(destination)
    try:
        connection.executescript(INDEX_SCHEMA)
        connection.execute("DROP TABLE stat_cache")
        connection.executemany(
            "INSERT INTO packs(name,state,byte_count,blob_count,sha256,created_at,sealed_at) VALUES (?,?,?,?,?,?,?)",
            [(p["name"], "sealed", p["byte_count"], 0, p["sha256"], p.get("created_at") or now(),
              p.get("sealed_at") or now()) for p in sorted(packs, key=lambda item: item["name"])])
        connection.commit()
        for number, source in enumerate(sources):
            alias = f"source{number}"
            # mode=ro, never immutable: a source may be a live WAL index.
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (Path(source).absolute().as_uri() + "?mode=ro",))
            connection.execute(
                f"INSERT OR IGNORE INTO main.blobs SELECT b.sha256,d.id,b.offset,b.stored_length,b.raw_length,b.codec "
                f"FROM {alias}.blobs b JOIN {alias}.packs s ON s.id=b.pack_id JOIN main.packs d ON d.name=s.name")
            missing = connection.execute(
                f"SELECT count(*) FROM {alias}.blobs b JOIN {alias}.packs s ON s.id=b.pack_id "
                f"JOIN main.packs d ON d.name=s.name WHERE NOT EXISTS (SELECT 1 FROM main.blobs m WHERE m.sha256=b.sha256)"
            ).fetchone()[0]
            connection.commit()
            connection.execute(f"DETACH DATABASE {alias}")
            if missing:
                raise PackIntegrityError("index rows of a listed pack were not copied")
        connection.execute("UPDATE packs SET blob_count=(SELECT count(*) FROM blobs WHERE pack_id=packs.id)")
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    return destination
