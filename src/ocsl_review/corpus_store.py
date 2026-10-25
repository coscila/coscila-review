# SPDX-License-Identifier: Apache-2.0
"""Transactional corpus storage shared by import, acquisition and execution.

Immutable evidence is content addressed; queue state is separate from scientific
observations. No provider access or worker dispatch happens in this module.
Schema changes happen only through the explicit, backed-up migrate(); bulk writers
stage artifacts behind one durability barrier per batch and insert rows in
transactions of at most BATCH_ROWS.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Iterator

from . import corpus_library as library
from .corpus_packs import PackIntegrityError
from .errors import ValidationError

HOME = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 3
MIGRATIONS = {1: HOME / "schemas/corpus-v2.sql", 2: HOME / "schemas/corpus-v3.sql"}
BATCH_ROWS = 5000
# Writers checkpoint the WAL after this many pages instead of SQLite's 1,000, so bulk
# imports pay the checkpoint fsyncs ten times less often.
WAL_AUTOCHECKPOINT_PAGES = 10000
# Below this many new files a per-file fsync is cheaper than a filesystem-wide barrier.
SYNCFS_MIN_FILES = 8
VERIFIED_CACHE_LIMIT = 200_000
_SYNCFS = None


def _syncfs_function():
    """Linux syncfs(2) as one barrier for a batch; OCSL_REVIEW_ARTIFACT_SYNC=fsync disables it."""
    global _SYNCFS
    if _SYNCFS is None:
        _SYNCFS = False
        if sys.platform.startswith("linux") and os.environ.get("OCSL_REVIEW_ARTIFACT_SYNC", "syncfs") == "syncfs":
            try:
                function = ctypes.CDLL(None, use_errno=True).syncfs
                function.argtypes, function.restype = [ctypes.c_int], ctypes.c_int
                _SYNCFS = function
            except (OSError, AttributeError):
                pass
    return _SYNCFS


def _fsync_path(path: Path, flags: int = 0) -> None:
    descriptor = os.open(path, os.O_RDONLY | flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_batch(directory: Path, paths: list[Path]) -> None:
    """Make files written without per-file fsync durable before rows refer to them."""
    if not paths:
        return
    function = _syncfs_function() if len(paths) >= SYNCFS_MIN_FILES else False
    if function:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if function(descriptor) == 0:
                return
        finally:
            os.close(descriptor)
    with ThreadPoolExecutor(max_workers=min(16, len(paths))) as pool:
        list(pool.map(lambda path: _fsync_path(path, os.O_NOFOLLOW), paths))
    _fsync_path(directory, os.O_DIRECTORY)


@contextmanager
def controller_lock(repo):
    """Exclusive ownership of one library by a controller: dispatch, calibration or migration."""
    with (repo.root / "controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationError("another corpus controller owns this library") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(*values: Any) -> str:
    return hashlib.sha256(canonical(values).encode()).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class CorpusRepository:
    """Short-lived connections avoid shared SQLite cursors across workers."""

    def __init__(self, workspace: Path, relative: str = "work/literature"):
        self.workspace = library.Workspace(workspace)
        self.library = library.Library(self.workspace, relative)
        self.root, self.db, self.relative = self.library.root, self.library.db, relative
        with self.library.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in library.SCHEMA_VERSIONS:
                raise ValidationError("unsupported corpus database version")
        # An older library stays at its version until an operator runs migrate().
        self.version = version
        self._verified_artifacts = {}
        self._held = None
        self._store_client = None

    @property
    def store_client(self):
        """Read-only stores pinned in bundle.json (study-first lookups), or None."""
        if self._store_client is None and self.library.bundle.get("stores"):
            from .store_client import StoreClient
            self._store_client = StoreClient.from_bundle(self)
        return self._store_client

    @classmethod
    def create(cls, workspace: Path, relative: str, question: str, analysis_id: str,
               protocol: dict[str, Any], code_revision: str | None = None):
        library.initialize(library.Workspace(workspace), relative, question, analysis_id,
                           protocol, code_revision)
        result = cls(workspace, relative)
        result.migrate()
        return result

    def _backup_path(self, version: int) -> Path:
        candidate = self.root / f"corpus-v{version}-backup.sqlite3"
        if not os.path.lexists(candidate):
            return candidate
        # Never overwrite an earlier backup; it may be the only pre-migration copy.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = self.root / f"corpus-v{version}-backup-{stamp}.sqlite3"
        if os.path.lexists(candidate):
            raise ValidationError("schema backup destination already exists")
        return candidate

    @staticmethod
    def _check_backup(path: Path, version: int) -> None:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            if (connection.execute("PRAGMA user_version").fetchone()[0] != version
                    or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
                raise ValidationError("schema backup failed verification; nothing was migrated")
        finally:
            connection.close()

    def migrate(self, *, rebuild_counters: bool = False) -> dict[str, Any]:
        """Upgrade the schema explicitly, one verified backup per step; never implicit.

        Opening a library never migrates it, so retained older databases keep their
        exact schema until an operator runs ``corpus migrate``.
        """
        if (self.root / "manifest.json").exists():
            raise ValidationError("branch or copy an immutable snapshot before migrating")
        if self.library.overlay is not None and self.version < SCHEMA_VERSION:
            # A delta branch must keep its parent snapshot's schema version.
            raise ValidationError("migrate the parent library, snapshot it and branch again; "
                                  "a branch keeps its parent snapshot's schema")
        steps = []
        # No controller may write between the backup and the migration it protects.
        with controller_lock(self):
            while self.version < SCHEMA_VERSION:
                backup = self._backup_path(self.version)
                self.library.backup(backup)
                self._check_backup(backup, self.version)
                with self.connect(write=True) as connection:
                    connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATIONS[self.version].read_text() + "\nCOMMIT;")
                    reached = connection.execute("PRAGMA user_version").fetchone()[0]
                if reached != self.version + 1:
                    raise ValidationError("schema migration did not reach its declared version")
                steps.append({"from": self.version, "to": reached, "backup": backup.name})
                self.version = reached
            if rebuild_counters:
                # Counters are derived data; recomputing them needs no evidence backup.
                with self.connect(write=True) as connection:
                    self.library.rebuild_counters(connection)
        return {"schemaVersion": self.version, "migrations": steps, "countersRebuilt": rebuild_counters}

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write and (self.root / "manifest.json").exists():
            raise ValidationError("immutable snapshots cannot be modified; branch first")
        if self.db.is_symlink() or (write and self.db.stat().st_nlink != 1):
            raise ValidationError("corpus database must not share mutable inodes")
        # A branch connection also sees its parent snapshot (corpus_overlay); SQLite cannot
        # enforce foreign keys across the two databases, so verification checks them.
        connection = self.library.open(write=write, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF" if self.library.overlay else "PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if write:
            connection.execute("PRAGMA main.journal_mode=WAL")
            connection.execute("PRAGMA main.synchronous=NORMAL")
            connection.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
        try:
            if not write:
                connection.execute("PRAGMA query_only=ON")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def held_open(self) -> Iterator[None]:
        """Keep one idle writable connection open around a bulk operation.

        Short-lived connections then neither checkpoint the WAL when they close nor
        rebuild its index when they open; automatic checkpoints still bound the WAL.
        The held connection closes last and, being writable, checkpoints and removes
        the WAL as any last writer does, so later readers open it cheaply.
        """
        if self._held is not None or (self.root / "manifest.json").exists():
            # Re-entrant; an immutable snapshot is only read, so nothing is held.
            yield
            return
        with self.connect(write=True) as connection:
            connection.execute("PRAGMA user_version").fetchone()
            self._held = connection
            try:
                yield
            finally:
                self._held = None

    def analysis(self, analysis_id: str | None = None, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        if analysis_id is None:
            analysis_id = self.library.active_analysis_id()
        if connection is None:
            with self.connect() as own:
                return self.analysis(analysis_id, connection=own)
        row = connection.execute("SELECT a.*,r.protocol_json,r.protocol_sha256,r.corpus_id,r.code_revision FROM analyses a JOIN runs r USING(run_id) WHERE a.analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            raise ValidationError("unknown analysis")
        return {**dict(row), "protocol": json.loads(row["protocol_json"])}

    def _sources(self) -> Path:
        if (self.root / "manifest.json").exists():
            raise ValidationError("immutable snapshots cannot be modified; branch first")
        return self.workspace.path(f"{self.relative}/sources")

    def _remember(self, digest: str, signature: tuple) -> None:
        if len(self._verified_artifacts) >= VERIFIED_CACHE_LIMIT:
            self._verified_artifacts.clear()
        self._verified_artifacts[digest] = signature

    def _existing_matches(self, path: Path, digest: str) -> bool:
        stat = os.stat(path, follow_symlinks=False)
        signature = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if self._verified_artifacts.get(digest) == signature:
            return True
        if library.digest_file(path) != digest:
            return False
        self._remember(digest, signature)
        return True

    def _write_source(self, sources: Path, digest: str, body: bytes, *, durable: bool) -> bool:
        """Create a loose sources/<digest> exactly once (libraries without packs); True when created."""
        path = sources / digest
        if os.path.lexists(path):
            if path.is_symlink() or not self._existing_matches(path, digest):
                raise ValidationError("stored immutable artifact was modified")
            return False
        temporary = sources / f".{digest[:16]}.{os.urandom(8).hex()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(descriptor, view):]
                if durable:
                    os.fsync(descriptor)
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if library.digest_file(path) != digest:
                    raise ValidationError("concurrent artifact write differs")
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def artifact(self, body: bytes, kind: str, media_type: str = "application/json") -> str:
        sources = self._sources()
        digest = hashlib.sha256(body).hexdigest()
        # Bytes are durable (pack fsync or loose file fsync) before the registry row exists.
        if self.library.packs is None:
            self._write_source(sources, digest, body, durable=True)
        else:
            try:
                self.library.store_blob(body, digest)
            except PackIntegrityError as error:
                raise ValidationError(str(error)) from None
        with self.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO source_artifacts VALUES (?,?,?,?,?,?)",
                               (digest, "sources/" + digest, len(body), media_type, kind, library.now()))
        return digest

    def json_artifact(self, value: Any, kind: str) -> str:
        return self.artifact(canonical(json_safe(value)).encode(), kind)

    def stage_artifacts(self, entries: Iterable[tuple[bytes, str, str]], *,
                        pending: list | None = None) -> list[tuple]:
        """Store content-addressed bytes behind one durability barrier for the batch.

        Returns the source_artifacts rows (digest first, in input order) for the
        caller to register in the same transaction as the rows that refer to them,
        so no committed row points at bytes that are not yet durable. Libraries with
        packs append the new bytes to a pack and fsync it once; older libraries write
        loose files and issue one barrier. With ``pending``, the new bytes are
        collected there instead and the caller issues one shared barrier
        (sync_staged) before committing any row that refers to them.
        """
        sources = self._sources()
        packed = self.library.packs is not None
        created_at = library.now()
        rows, new, seen = [], [], set()
        for body, kind, media_type in entries:
            digest = hashlib.sha256(body).hexdigest()
            rows.append((digest, "sources/" + digest, len(body), media_type, kind, created_at))
            if digest not in seen:
                seen.add(digest)
                if packed:
                    new.append((digest, body))
                elif self._write_source(sources, digest, body, durable=False):
                    new.append(sources / digest)
        if pending is None:
            self.sync_staged(new)
        else:
            pending.extend(new)
        return rows

    def sync_staged(self, staged: list) -> None:
        """One durability barrier for the bytes collected by stage_artifacts(pending=...).

        Pack blobs are appended in one pack batch (one fsync before their index rows
        commit); loose files of older libraries share one filesystem barrier.
        """
        blobs = [item for item in staged if isinstance(item, tuple)]
        files = [item for item in staged if not isinstance(item, tuple)]
        if blobs:
            packs, parent = self.library.packs, self.library.parent
            present = parent.locate_many([digest for digest, _ in blobs]) if parent is not None else {}
            try:
                with packs.batch():
                    for digest, body in blobs:
                        if digest not in present:  # a branch reads its parent snapshot's blobs
                            packs.put(body, digest)
            except PackIntegrityError as error:
                raise ValidationError(str(error)) from None
        if files:
            sync_batch(self._sources(), files)

    @staticmethod
    def register_artifacts(connection: sqlite3.Connection, rows: list[tuple]) -> None:
        connection.executemany("INSERT OR IGNORE INTO source_artifacts VALUES (?,?,?,?,?,?)", rows)

    def _artifact_rows(self, connection: sqlite3.Connection, digests: list[str]) -> dict[str, sqlite3.Row]:
        wanted = list(dict.fromkeys(digests))
        rows = {}
        for start in range(0, len(wanted), BATCH_ROWS):
            rows.update((row["sha256"], row) for row in connection.execute(
                "SELECT * FROM source_artifacts WHERE sha256 IN (SELECT value FROM json_each(?))",
                (json.dumps(wanted[start:start + BATCH_ROWS]),)))
        missing = [digest for digest in wanted if digest not in rows]
        if missing:
            raise ValidationError("artifact is not registered")
        if any(item["relative_path"] != "sources/" + digest for digest, item in rows.items()):
            raise ValidationError("artifact paths must be content-addressed sources/<sha256>")
        return rows

    def _located(self, rows: dict[str, sqlite3.Row]) -> dict[str, tuple]:
        """Storage of many registered artifacts: packs, loose files or the parent chain."""
        found = self.library.locate_many(list(rows))
        for digest, item in rows.items():
            if digest not in found:
                raise ValidationError(f"missing source: {item['relative_path']}")
        return found

    def _checked(self, digest: str, item, location, *, keep: bool) -> bytes | None:
        """Verified bytes of one located artifact; unchanged storage verified earlier is not rehashed."""
        if location[0] == "pack":
            path = location[1].pack_path(location[2].pack)
        else:
            path = location[1]
        stat = os.stat(path, follow_symlinks=False)
        signature = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._verified_artifacts.get(digest) == signature
        if cached and not keep:
            return None
        try:
            if location[0] == "pack":
                body = location[1].read(location[2])  # decoding checks length and SHA-256
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as handle:
                    body = handle.read()
                if not cached and hashlib.sha256(body).hexdigest() != digest:
                    raise PackIntegrityError(digest)
        except PackIntegrityError:
            raise ValidationError("registered artifact hash mismatch") from None
        if len(body) != item["byte_count"]:
            raise ValidationError("registered artifact hash mismatch")
        self._remember(digest, signature)
        return body if keep else None

    def read_artifacts(self, digests: Iterable[str], *, connection: sqlite3.Connection | None = None) -> dict[str, bytes]:
        """Read and verify many registered artifacts through one connection."""
        digests = list(digests)
        if connection is None:
            with self.connect() as own:
                return self.read_artifacts(digests, connection=own)
        rows = self._artifact_rows(connection, digests)
        found = self._located(rows)
        return {digest: self._checked(digest, item, found[digest], keep=True) for digest, item in rows.items()}

    def read_artifact(self, digest: str) -> bytes:
        return self.read_artifacts([digest])[digest]

    def verify_artifacts(self, digests: Iterable[str], *, connection: sqlite3.Connection | None = None) -> None:
        digests = list(digests)
        if connection is None:
            with self.connect() as own:
                return self.verify_artifacts(digests, connection=own)
        rows = self._artifact_rows(connection, digests)
        found = self._located(rows)
        for digest, item in rows.items():
            self._checked(digest, item, found[digest], keep=False)

    def verify_artifact(self, digest: str):
        """Check a registered artifact's bytes; repeated checks of unchanged storage are cached."""
        with self.connect() as connection:
            rows = self._artifact_rows(connection, [digest])
        found = self._located(rows)
        self._checked(digest, rows[digest], found[digest], keep=False)
        return found[digest]

    def work(self, metadata: dict[str, Any], *, key: str | None = None) -> str:
        identifiers = {k: str(metadata[k]).strip().lower() for k in ("doi", "pmid", "pmcid") if metadata.get(k)}
        canonical_key = key or next((f"{k}:{identifiers[k]}" for k in ("doi", "pmid", "pmcid") if k in identifiers), None)
        work_id = "work-" + identity(canonical_key or metadata)
        with self.connect(write=True) as connection:
            for scheme, value in identifiers.items():
                existing = connection.execute("SELECT work_id FROM work_identifiers WHERE scheme=? AND value=?", (scheme, value)).fetchone()
                if existing:
                    work_id = existing[0]
                    break
            connection.execute("INSERT OR IGNORE INTO works VALUES (?,?,?,?)", (work_id,
                               metadata.get("title") or "Unresolved work", metadata.get("year"), canonical(metadata)))
            old = connection.execute("SELECT metadata_json,title FROM works WHERE work_id=?", (work_id,)).fetchone()
            if metadata.get("title") and metadata["title"] != "Unresolved work":
                enriched = {**json.loads(old[0]), **{k:v for k,v in metadata.items() if v not in (None, "")}}
                connection.execute("UPDATE works SET title=?,publication_year=?,metadata_json=? WHERE work_id=?", (enriched["title"], enriched.get("year"), canonical(enriched), work_id))
        return work_id

    def review(self, metadata: dict[str, Any], *, inclusion: str = "included", reason: str = "manifest",
               analysis_id: str | None = None) -> str:
        analysis = self.analysis(analysis_id)
        key = metadata.get("doi") or metadata.get("pmid") or metadata.get("pmcid") or metadata.get("id")
        if not key:
            raise ValidationError("review requires a stable identifier")
        work_id = self.work(metadata, key=None if any(metadata.get(k) for k in ("doi", "pmid", "pmcid")) else str(key).lower())
        review_id = "review-" + identity(analysis["corpus_id"], work_id)
        with self.connect(write=True) as connection:
            prior = connection.execute("SELECT inclusion_state FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if prior and prior[0] not in ("unresolved", inclusion):
                raise ValidationError("review eligibility is already resolved differently; retain this corpus and start a distinct selection")
            connection.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,?)", (review_id, analysis["corpus_id"], work_id, inclusion,
                               canonical({k: metadata[k] for k in ("journal", "label", "stratum", "date") if k in metadata}), reason))
            if prior and prior[0] == "unresolved" and inclusion != "unresolved":
                connection.execute("UPDATE reviews SET inclusion_state=?,selection_reason=?,strata_json=? WHERE review_id=?", (
                    inclusion, reason, canonical({k: metadata[k] for k in ("journal", "label", "stratum", "date") if k in metadata}), review_id))
            connection.execute("INSERT OR IGNORE INTO corpus_reviews(review_id,updated_at) VALUES (?,?)", (review_id, library.now()))
        return review_id

    def review_state(self, review_id: str, phase: str, state: str, reason: str | None = None,
                     checkpoint: dict | None = None, document_sha: str | None = None):
        with self.connect(write=True) as connection:
            connection.execute("UPDATE corpus_reviews SET phase=?,state=?,reason=?,checkpoint_json=?,document_sha256=coalesce(?,document_sha256),updated_at=? WHERE review_id=?",
                               (phase, state, reason, canonical(checkpoint or {}), document_sha, library.now(), review_id))

    def document(self, work_id: str, document: dict[str, Any], tier: str, provider: str) -> str:
        """Store complete acquisition and extracted units; never just the score."""
        original = document.get("body")
        if isinstance(original, bytes):
            raw_sha = self.artifact(original, "raw-source", document.get("mediaType", "application/octet-stream"))
        else:
            raw_sha = self.json_artifact(document, "provider-document")
        stored = {k: v for k, v in document.items() if k != "body"}
        stored["rawArtifactSha256"] = raw_sha
        blocks = stored.get("blocks") or [{"id": "abstract" if tier == "abstract" else "body", "text": stored.get("text", ""), "kind": tier}]
        doc_sha = self.json_artifact({**stored, "blocks": blocks}, "source-document")
        document_id = "document-" + identity(work_id, doc_sha, tier)
        with self.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO corpus_documents VALUES (?,?,?,?,?,?,?,?,?)",
                               (document_id, work_id, doc_sha, tier, str(stored.get("sourceUrl", "")), provider, int(bool(stored.get("complete"))), canonical({"rawArtifactSha256": raw_sha}), library.now()))
            for i, block in enumerate(blocks):
                text = str(block.get("text", ""))
                section_id = "section-" + identity(doc_sha, block.get("id", i))
                connection.execute("INSERT OR IGNORE INTO source_sections VALUES (?,?,?,?,?,?,?,?,?)", (section_id, work_id, doc_sha,
                                   str(block.get("locator") or block.get("id", i)), str(block.get("kind", tier)), text, hashlib.sha256(text.encode()).hexdigest(), "corpus-v2", int(bool(stored.get("complete")))))
        return document_id

    def evidence(self, work_id: str, tier: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM corpus_documents WHERE work_id=? AND evidence_tier=? ORDER BY complete DESC,created_at DESC,document_id DESC LIMIT 1", (work_id, tier)).fetchone()
            if row is None:
                return None
            sections, order = self.library.ordered("source_sections")
            units = [dict(r) for r in connection.execute(f"SELECT section_id AS id,extracted_text AS text,locator,section_kind AS kind FROM {sections} WHERE artifact_sha256=? AND work_id=? ORDER BY {order}", (row["artifact_sha256"], work_id))]
        self.verify_artifact(row["artifact_sha256"])
        return {"documentId": row["document_id"], "sourceSha256": row["artifact_sha256"], "evidenceTier": tier,
                "evidenceUnits": units, "complete": bool(row["complete"]), "provider": row["provider"], "sourceUrl": row["source_url"]}

    def evidence_many(self, work_ids: Iterable[str], tier: str, *,
                      connection: sqlite3.Connection | None = None) -> dict[str, dict[str, Any] | None]:
        """The selection of evidence() for many works, in two indexed queries."""
        wanted = list(dict.fromkeys(work for work in work_ids if work))
        if connection is None:
            with self.connect() as own:
                return self.evidence_many(wanted, tier, connection=own)
        result: dict[str, dict[str, Any] | None] = dict.fromkeys(wanted)
        for start in range(0, len(wanted), BATCH_ROWS):
            chunk = json.dumps(wanted[start:start + BATCH_ROWS])
            best = {}
            for row in connection.execute(
                    "SELECT * FROM corpus_documents WHERE evidence_tier=? AND work_id IN (SELECT value FROM json_each(?)) "
                    "ORDER BY work_id,complete DESC,created_at DESC,document_id DESC", (tier, chunk)):
                best.setdefault(row["work_id"], row)
            if not best:
                continue
            documents = {(row["artifact_sha256"], work) for work, row in best.items()}
            units: dict[tuple[str, str], list[dict[str, Any]]] = {}
            sections, order = self.library.ordered("source_sections")
            for row in connection.execute(
                    "SELECT section_id AS id,extracted_text AS text,locator,section_kind AS kind,artifact_sha256,work_id "
                    f"FROM {sections} WHERE work_id IN (SELECT value FROM json_each(?)) ORDER BY {order}",
                    (json.dumps(list(best)),)):
                key = (row["artifact_sha256"], row["work_id"])
                if key in documents:
                    units.setdefault(key, []).append(
                        {"id": row["id"], "text": row["text"], "locator": row["locator"], "kind": row["kind"]})
            self.verify_artifacts([row["artifact_sha256"] for row in best.values()], connection=connection)
            for work, row in best.items():
                result[work] = {"documentId": row["document_id"], "sourceSha256": row["artifact_sha256"],
                                "evidenceTier": tier, "evidenceUnits": units.get((row["artifact_sha256"], work), []),
                                "complete": bool(row["complete"]), "provider": row["provider"],
                                "sourceUrl": row["source_url"]}
        return result

    def add_task(self, analysis_id: str, locus_id: str, tier: str, spec: dict[str, Any], reason: str | None = None) -> str:
        return self.add_tasks(analysis_id, [(locus_id, tier, spec, reason)])[0]

    def add_tasks(self, analysis_id: str, items: Iterable[tuple[str, str, dict[str, Any], str | None]], *,
                  supersede_blocked: bool = False) -> list[str]:
        """Store (locus, tier, spec, reason) tasks: one barrier and one transaction per BATCH_ROWS.

        With supersede_blocked, a task that can run cancels earlier blocked tasks of
        the same locus and tier in the same transaction.
        """
        ids, chunk = [], []
        for item in items:
            chunk.append(item)
            if len(chunk) == BATCH_ROWS:
                ids += self._add_task_chunk(analysis_id, chunk, supersede_blocked)
                chunk = []
        if chunk:
            ids += self._add_task_chunk(analysis_id, chunk, supersede_blocked)
        return ids

    def _add_task_chunk(self, analysis_id, chunk, supersede_blocked):
        prepared = []
        for locus_id, tier, spec, reason in chunk:
            task_id = "task-" + identity(analysis_id, locus_id, tier, spec)
            prepared.append((task_id, locus_id, tier, reason, canonical(json_safe({**spec, "id": task_id})).encode()))
        rows = self.stage_artifacts((body, "task-input", "application/json") for *_, body in prepared)
        updated = library.now()
        with self.connect(write=True) as connection:
            self.register_artifacts(connection, rows)
            connection.executemany("INSERT OR IGNORE INTO corpus_tasks VALUES (?,?,?,?,?,?,?,?,?)", [
                (task_id, analysis_id, locus_id, tier, row[0], "blocked" if reason else "pending", reason, 0, updated)
                for (task_id, locus_id, tier, reason, _), row in zip(prepared, rows)])
            if supersede_blocked:
                connection.executemany(
                    "UPDATE corpus_tasks SET state='cancelled',reason='superseded by acquired evidence',updated_at=? "
                    "WHERE analysis_id=? AND locus_id=? AND tier=? AND task_id!=? AND state='blocked'",
                    [(updated, analysis_id, locus_id, tier, task_id)
                     for task_id, locus_id, tier, reason, _ in prepared if not reason])
        return [item[0] for item in prepared]

    def tasks(self, analysis_id: str, *, state: str | None = None, tier: str | None = None, limit: int | None = None,
              after: str | None = None):
        query, params = "SELECT * FROM corpus_tasks WHERE analysis_id=?", [analysis_id]
        if state:
            query += " AND state=?"; params.append(state)
        if tier:
            query += " AND tier=?"; params.append(tier)
        if after is not None:
            query += " AND task_id>?"; params.append(after)
        query += " ORDER BY task_id"
        if limit is not None:
            query += " LIMIT ?"; params.append(limit)
        with self.connect() as connection:
            rows = [dict(r) for r in connection.execute(query, params)]
            bodies = self.read_artifacts([row["spec_sha256"] for row in rows], connection=connection)
        return [{**row, "spec": json.loads(bodies[row["spec_sha256"]])} for row in rows]

    def task_counts(self, analysis_id: str, tier: str | None = None) -> dict[str, int]:
        """Task totals by state; schema 3 reads the progress counters, never the queue."""
        with self.connect() as connection:
            if self.version >= 3:
                rows = connection.execute(
                    "SELECT state,sum(value) FROM progress_counters WHERE counter='tasks' AND analysis_id=? "
                    "AND (? IS NULL OR dimension=?) GROUP BY state", (analysis_id, tier, tier))
            else:
                rows = connection.execute("SELECT state,count(*) FROM corpus_tasks WHERE analysis_id=? AND (? IS NULL OR tier=?) "
                                          "GROUP BY state", (analysis_id, tier, tier))
            return {state: count for state, count in rows if count}

    def status(self, analysis_id: str | None = None) -> dict[str, Any]:
        if self.version < 3:
            return self._scanned_status(self.analysis(analysis_id))
        counts = dict.fromkeys(library.COUNTED_TABLES, 0)
        tasks, assessments, reviews = [], [], []
        with self.connect() as connection:   # one connection: a branch builds its overlay on every connect
            analysis = self.analysis(analysis_id, connection=connection)
            counters = self.library.progress_counters(connection, analysis["analysis_id"])
        for (counter, dimension, state), value in counters.items():
            if counter == "rows" and dimension in counts:
                counts[dimension] = value
            elif counter == "tasks":
                tasks.append({"tier": dimension, "state": state, "count": value})
            elif counter == "assessment-loci":
                assessments.append({"evidence_level": dimension, "validation_state": state, "count": value})
            elif counter == "reviews":
                reviews.append({"phase": dimension, "state": state, "count": value})
        return {"analysisId": analysis["analysis_id"], "schemaVersion": self.version, "counts": counts,
                "reviewStates": reviews, "taskStates": tasks, "assessments": assessments}

    def _scanned_status(self, analysis: dict[str, Any]) -> dict[str, Any]:
        """Status for libraries not yet migrated to counters (table scans)."""
        with self.connect() as connection:
            counts = {t: connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in library.COUNTED_TABLES}
            tasks = [dict(r) for r in connection.execute("SELECT tier,state,count(*) AS count FROM corpus_tasks WHERE analysis_id=? GROUP BY tier,state", (analysis["analysis_id"],))]
            assessments = [dict(r) for r in connection.execute("SELECT evidence_level,validation_state,count(DISTINCT locus_id) AS count FROM assessments WHERE analysis_id=? GROUP BY evidence_level,validation_state", (analysis["analysis_id"],))]
            reviews = [dict(r) for r in connection.execute("SELECT phase,state,count(*) AS count FROM corpus_reviews GROUP BY phase,state")]
        return {"analysisId": analysis["analysis_id"], "schemaVersion": self.version, "counts": counts,
                "reviewStates": reviews, "taskStates": tasks, "assessments": assessments,
                "schemaUpgrade": "run corpus migrate to keep progress counters (a verified backup is written first)"}
