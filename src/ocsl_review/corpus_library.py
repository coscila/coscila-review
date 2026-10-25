# SPDX-License-Identifier: Apache-2.0
#!/usr/bin/env python3
"""Initialize, verify, snapshot and branch a literature library (Python stdlib).

This manages storage, not ingestion, scientific judging, or a worker scheduler.
Paths are relative to an explicit workspace, or absolute inside the study root
(OCSL_STUDY_ROOT) that the platform may provide outside the workspace; parent
snapshots may also be read from a mounted dataset root (OCSL_DATASET_ROOT).

Artifacts are stored in append-only content-addressed packs (corpus_packs);
loose sources/<sha256> files of older libraries stay readable. A snapshot is a
closed database, hard-linked sealed packs and a small manifest with a Merkle
root over those files. A branch is a delta database that reads its parent
snapshot through ATTACH (corpus_overlay); it never copies the parent database.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

from .corpus_overlay import PARENT, Overlay, prefix_shared_tables, quote
from .corpus_packs import INDEX as PACK_INDEX, PACK_NAME, SHA256, PackIntegrityError, PackStore, build_index


TABLES = (
    "corpora", "runs", "analyses", "works", "work_identifiers", "source_artifacts",
    "retrievals", "source_sections", "reviews", "bibliography_entries", "claim_loci",
    "model_receipts", "assessments", "assessment_evidence", "work_observations",
    "work_observation_evidence", "processing_state", "facts",
)
V2_TABLES = ("corpus_reviews", "corpus_documents", "corpus_tasks", "corpus_batches", "corpus_imports")
SCHEMA_VERSIONS = (1, 2, 3)
# Tables whose row totals status reports; schema 3 keeps them in progress_counters.
COUNTED_TABLES = ("reviews", "bibliography_entries", "works", "claim_loci", "source_artifacts", "source_sections")
SCHEMA = Path(__file__).resolve().parents[2] / "skills/ocsl-review-reference-analysis/assets/literature-schema.sql"
SNAPSHOT_FORMAT = "ocsl-literature-snapshot"
MERKLE_ALGORITHM = "sha256-files-v1"
STUDY_ROOT_ENV = "OCSL_STUDY_ROOT"
DATASET_ROOT_ENV = "OCSL_DATASET_ROOT"
LEVELS = ("light", "standard", "deep")
PATH_RULE = ("paths must be nonempty workspace-relative paths without '..' "
             f"(or absolute paths inside {STUDY_ROOT_ENV})")


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def digest_file(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    with path.open("xb") as handle:
        handle.write(encode(value))


def expected_progress_counters(connection, table=lambda name: name):
    """Recompute every schema-3 progress counter from the stored rows (nonzero values only).

    ``table`` maps a table name to the table whose rows the triggers count; in a
    branch these are the branch's own rows (Library.expected_counters).
    """
    expected = {}
    for name in COUNTED_TABLES:
        expected[("rows", "", name, "")] = connection.execute(f"SELECT count(*) FROM {table(name)}").fetchone()[0]
    queries = (
        f"SELECT 'tasks',analysis_id,tier,state,count(*) FROM {table('corpus_tasks')} GROUP BY analysis_id,tier,state",
        f"SELECT 'reviews','',phase,state,count(*) FROM {table('corpus_reviews')} GROUP BY phase,state",
        "SELECT 'assessment-loci',analysis_id,evidence_level,validation_state,count(DISTINCT locus_id) "
        f"FROM {table('assessments')} GROUP BY analysis_id,evidence_level,validation_state",
    )
    for query in queries:
        for row in connection.execute(query):
            expected[tuple(row[:4])] = row[4]
    return {key: value for key, value in expected.items() if value}


def stored_progress_counters(connection):
    return {tuple(row[:4]): row[4] for row in connection.execute(
        "SELECT counter,analysis_id,dimension,state,value FROM main.progress_counters WHERE value!=0")}


def rebuild_progress_counters(connection, table=lambda name: name):
    """Replace the stored counters with values recomputed from the rows (derived data only)."""
    expected = expected_progress_counters(connection, table)
    connection.execute("DELETE FROM main.progress_counters")
    connection.executemany("INSERT INTO main.progress_counters VALUES (?,?,?,?,?)",
                           [(*key, value) for key, value in sorted(expected.items())])
    return expected


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def merkle_root(files):
    """Binary SHA-256 Merkle tree over (path, sha256, size) leaves in path order."""
    level = [hashlib.sha256(b"\x00" + item["path"].encode() + b"\x00" + bytes.fromhex(item["sha256"])
                            + int(item["byte_count"]).to_bytes(8, "big")).digest()
             for item in sorted(files, key=lambda entry: entry["path"])]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        level = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() if i + 1 < len(level) else level[i]
                 for i in range(0, len(level), 2)]
    return level[0].hex()


def _external_root(value, name):
    raw = Path(value)
    if not raw.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if any(p.is_symlink() for p in (raw, *raw.parents)):
        raise ValueError(f"{name} must not use symlinks")
    root = raw.resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ValueError(f"{name} must be an existing directory")
    return root


def _overlaps(path, protected):
    return any(path == p or p in path.parents or path in p.parents for p in protected)


def link_or_copy(source, target):
    """Share an immutable file by hard link; copy only across filesystems."""
    try:
        os.link(source, target, follow_symlinks=False)
        return "linked"
    except OSError as error:
        if error.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            raise
    shutil.copyfile(source, target, follow_symlinks=False)
    target.chmod(0o444)
    return "copied"


class Workspace:
    def __init__(self, path, *, study_root=None, dataset_roots=None):
        raw = Path(path).absolute()
        if any(p.is_symlink() for p in (raw, *raw.parents)):
            raise ValueError("workspace must not use symlinks")
        self.root = raw.resolve(strict=True)
        if not self.root.is_dir() or self.root == Path(self.root.anchor):
            raise ValueError("select an existing workspace directory")
        protected = [Path(os.environ[k]).resolve() for k in
                     ("OCSL_REVIEW_HOME", "OCSL_REVIEW_STATE", "OCSL_REPO") if os.environ.get(k)]
        for parent in SCHEMA.parents:
            if (parent / "bin" / "ocsl-review").is_file():
                protected.append(parent)
        self.protected = protected
        if any(self.root == p or p in self.root.parents for p in protected):
            raise ValueError("workspace must be separate from companion source and operator state")
        study = os.environ.get(STUDY_ROOT_ENV) if study_root is None else study_root
        self.study_root = _external_root(study, STUDY_ROOT_ENV) if study else None
        datasets = (os.environ.get(DATASET_ROOT_ENV, "").split(os.pathsep) if dataset_roots is None
                    else [str(item) for item in dataset_roots])
        self.dataset_roots = [_external_root(item, DATASET_ROOT_ENV) for item in datasets if item]
        for root in ([self.study_root] if self.study_root else []) + self.dataset_roots:
            if _overlaps(root, protected):
                raise ValueError("study and dataset roots must be separate from companion source and operator state")

    def _base(self, candidate):
        for root in ([self.study_root] if self.study_root else []) + self.dataset_roots:
            if candidate.is_relative_to(root) and candidate != root:
                return root
        raise ValueError(PATH_RULE)

    def path(self, relative):
        candidate = Path(relative)
        if not candidate.parts or ".." in candidate.parts:
            raise ValueError(PATH_RULE)
        if candidate.is_absolute():
            base = self._base(candidate)
            parts = candidate.relative_to(base).parts
        else:
            base, parts = self.root, candidate.parts
        current = base
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"symlink is not allowed: {relative}")
        if not (current.is_relative_to(base) and current != base):
            raise ValueError("path leaves workspace")
        if any(current == p or p in current.parents for p in self.protected):
            raise ValueError("library path overlaps companion source or operator state")
        return current

    def writable(self, path):
        return not any(path.is_relative_to(root) for root in self.dataset_roots) or (
            self.study_root is not None and path.is_relative_to(self.study_root))

    def relative(self, path):
        path = Path(path)
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)

    @contextmanager
    def create(self, relative):
        destination = self.path(relative)
        if not self.writable(destination):
            raise ValueError("dataset roots are read-only; create libraries in the workspace or study root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700)  # Never overwrite an existing bundle.
        try:
            (destination / "sources").mkdir()
            yield destination
        except BaseException:
            # Never chmod here: hard-linked packs share their inode with the source library.
            shutil.rmtree(destination)
            raise


class Library:
    def __init__(self, workspace, relative):
        self.workspace = workspace
        self.relative = str(relative)
        self.root = workspace.path(relative)
        self.db = workspace.path(f"{relative}/corpus.sqlite3")
        if not self.db.is_file():
            raise ValueError("library database is missing")
        self.manifest_path = self.root / "manifest.json"
        self.is_snapshot = self.manifest_path.exists()
        self.parent_root = None
        self.parent_sources = set()
        self.parent = None
        self.overlay = None
        self.bundle = {}
        packs = self.root / "packs"
        self.packs = PackStore(packs, readonly=self.is_snapshot) if (packs / PACK_INDEX).is_file() else None
        origin = workspace.path(f"{relative}/bundle.json")
        if origin.exists() and not self.is_snapshot:
            self.bundle = metadata = json.loads(origin.read_text())
            parent = metadata.get("parent_snapshot")
            if parent:
                self.parent_root = workspace.path(parent)
                manifest = workspace.path(f"{parent}/manifest.json")
                if digest_file(manifest) != metadata["parent_manifest_sha256"]:
                    raise ValueError("parent snapshot manifest has changed")
                if metadata.get("kind") == "delta-branch":
                    self.parent = Library(workspace, parent)
                    if not self.parent.is_snapshot:
                        raise ValueError("branch parent is not a snapshot")
                    self.parent.check_sidecars()
                    self.overlay = Overlay(self.db, self.parent.db)
                else:
                    self.parent_sources = {item["sha256"] for item in
                                           json.loads(manifest.read_text())["sources"]}

    # ------------------------------------------------------------ connections
    def open(self, *, write=False, timeout=5.0):
        """A connection whose unqualified table names see the whole library.

        Sealed snapshots (no journal sidecars, checked by verification) are read
        immutable, which also works on read-only dataset mounts.
        """
        mode = "rw" if write else ("ro&immutable=1" if self.is_snapshot else "ro")
        connection = sqlite3.connect(self.db.as_uri() + "?mode=" + mode, uri=True, timeout=timeout)
        if self.overlay is not None:
            try:
                self.overlay.attach(connection)
            except BaseException:
                connection.close()
                raise
        return connection

    @contextmanager
    def connect(self):
        connection = self.open()
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def ordered(self, table, alias=None):
        """(FROM source, ORDER BY expression) for insertion order, also in branches."""
        if self.overlay is not None:
            return self.overlay.ordered(table, alias)
        return table, (f"{alias}." if alias else "") + "rowid"

    def keyset_sources(self, table, alias):
        """[(FROM source, rowid expression, extra condition)] that cover a table's rows once each.

        Keyset pages advance by rowid, which a branch's union view does not have: a
        branch pages its own rows first, then the parent snapshot's rows it does not
        override, each through its physical table's rowid.
        """
        spec = self.overlay.tables.get(table) if self.overlay is not None else None
        if spec is None or not spec.shared or not spec.rowid:
            return [(f"{table} AS {alias}", f"{alias}.rowid", "")]
        own = f"main.{quote(spec.physical)}"
        hidden = ""
        if spec.key:
            match = " AND ".join(f"d.{quote(c)}={alias}.{quote(c)}" for c in spec.key)
            hidden = f" AND NOT EXISTS (SELECT 1 FROM {own} AS d WHERE {match})"
        return [(f"{own} AS {alias}", f"{alias}.rowid", ""),
                (f"{PARENT}.{quote(table)} AS {alias}", f"{alias}.rowid", hidden)]

    # ------------------------------------------------------------------ blobs
    def _loose(self, digest):
        if not SHA256.fullmatch(digest):
            raise ValueError(f"invalid artifact digest: {digest}")
        path = self.workspace.path(f"{self.relative}/sources/{digest}")
        return path if path.is_file() else None

    def source(self, item):
        """Loose content-addressed file of a registered artifact (older libraries)."""
        expected = "sources/" + item["sha256"]
        if item["relative_path"] != expected:
            raise ValueError("artifact paths must be content-addressed sources/<sha256>")
        path = self._loose(item["sha256"])
        if path is None and self.parent_root and item["sha256"] in self.parent_sources:
            path = self.workspace.path(self.workspace.relative(self.parent_root / expected))
        if path is None or not path.is_file():
            raise ValueError(f"missing source: {expected}")
        return path

    def locate(self, digest):
        """('pack', store, location) | ('loose', path) | None, searching the parent chain."""
        if self.packs is not None:
            location = self.packs.locate(digest)
            if location is not None:
                return "pack", self.packs, location
        path = self._loose(digest)
        if path is not None:
            return "loose", path
        if self.parent is not None:
            return self.parent.locate(digest)
        if self.parent_root and digest in self.parent_sources:
            path = self.workspace.path(self.workspace.relative(self.parent_root / "sources" / digest))
            if path.is_file():
                return "loose", path
        return None

    def locate_many(self, digests):
        """locate() for many digests: one index query per pack store; unknown digests are left out."""
        wanted = list(dict.fromkeys(digests))
        found = {}
        if self.packs is not None:
            found.update((digest, ("pack", self.packs, location))
                         for digest, location in self.packs.locate_many(wanted).items())
        rest = [digest for digest in wanted if digest not in found]
        if rest:
            sources = self.workspace.path(f"{self.relative}/sources")  # validated once for the batch
            for digest in rest:
                if not SHA256.fullmatch(digest):
                    raise ValueError(f"invalid artifact digest: {digest}")
                try:
                    regular = stat.S_ISREG(os.lstat(sources / digest).st_mode)
                except FileNotFoundError:
                    continue
                # Anything but a regular local file takes the fully checked path (symlinks raise).
                path = sources / digest if regular else self._loose(digest)
                if path is not None:
                    found[digest] = ("loose", path)
            rest = [digest for digest in rest if digest not in found]
        if rest and self.parent is not None:
            found.update(self.parent.locate_many(rest))
        elif rest and self.parent_root:
            for digest in rest:
                if digest in self.parent_sources:
                    path = self.workspace.path(self.workspace.relative(self.parent_root / "sources" / digest))
                    if path.is_file():
                        found[digest] = ("loose", path)
        return found

    def has_blob(self, digest):
        return self.locate(digest) is not None

    def read_blob(self, digest, byte_count=None):
        """Verified artifact bytes; damage raises, it is never returned."""
        found = self.locate(digest)
        if found is None:
            raise ValueError(f"missing source: sources/{digest}")
        if found[0] == "pack":
            body = found[1].read(found[2])
        else:
            descriptor = os.open(found[1], os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                body = handle.read()
            if hashlib.sha256(body).hexdigest() != digest:
                raise PackIntegrityError(f"source hash/size mismatch: sources/{digest}")
        if byte_count is not None and len(body) != byte_count:
            raise PackIntegrityError(f"source hash/size mismatch: sources/{digest}")
        return body

    def store_blob(self, body, digest=None):
        """Store bytes durably (packs when enabled, else a loose file) before registration."""
        if self.is_snapshot:
            raise ValueError("immutable snapshots cannot be modified; branch first")
        digest = digest or hashlib.sha256(body).hexdigest()
        if self.packs is not None:
            if self.packs.contains(digest) or (self.parent is not None and self.parent.has_blob(digest)):
                return digest
            loose = self._loose(digest)
            if loose is not None:
                if digest_file(loose) != digest:
                    raise PackIntegrityError("stored immutable artifact was modified")
                return digest
            return self.packs.put(body, digest)
        path = self.workspace.path(f"{self.relative}/sources/{digest}")
        if path.exists():
            if digest_file(path) != digest:
                raise PackIntegrityError("stored immutable artifact was modified")
            return digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
                temporary = Path(target.name)
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
            temporary.chmod(0o444)
            os.link(temporary, path)
        except FileExistsError:
            if digest_file(path) != digest:
                raise PackIntegrityError("concurrent artifact write differs") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return digest

    def pack_stores(self):
        """Pack stores of this library and its parent chain, nearest first."""
        stores = [self.packs] if self.packs is not None else []
        return stores + (self.parent.pack_stores() if self.parent is not None else [])

    # ----------------------------------------------------------------- checks
    def manifest(self):
        # Always re-read: verification must see the manifest as it is on disk now.
        if not self.manifest_path.exists():
            raise ValueError("library is not a snapshot; take a snapshot first")
        return json.loads(self.manifest_path.read_text())

    def check_sidecars(self):
        # A manifest hashes the complete checkpointed DB, never mutable sidecar pages.
        for suffix in ("-wal", "-journal"):
            sidecar = self.workspace.path(f"{self.relative}/corpus.sqlite3{suffix}")
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError("snapshot has an unexpected SQLite journal; use a fresh backup snapshot")

    def _tables(self, connection):
        if self.overlay is not None:
            names = sorted(self.overlay.tables)  # Logical names: shared tables count parent and branch rows.
        else:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: connection.execute(f"SELECT count(*) FROM {quote(name)}").fetchone()[0] for name in names}

    def _own(self, table):
        """The table holding this library's own rows (a branch's prefixed table)."""
        return f"main.{quote(self.overlay.physical(table))}" if self.overlay is not None else table

    def expected_counters(self, connection):
        """Schema-3 progress counters recomputed from the rows their triggers count.

        A branch's triggers live on its own (prefixed) tables, so its stored counters
        cover the branch's own rows; the parent snapshot keeps its own counters.
        """
        return expected_progress_counters(connection, self._own)

    def rebuild_counters(self, connection):
        return rebuild_progress_counters(connection, self._own)

    def progress_counters(self, connection, analysis_id):
        """{(counter, dimension, state): value} of the library totals and one analysis (schema 3).

        A branch's triggers count only its own rows, so a branch adds the parent
        snapshot's counters and takes out the parent rows it has overridden
        (copy-on-write); task queues are branch-local and count only branch tasks.
        """
        def read(schema):
            return {tuple(row[:3]): row[3] for row in connection.execute(
                f"SELECT counter,dimension,state,value FROM {schema}.progress_counters "
                "WHERE value>0 AND analysis_id IN ('',?)", (analysis_id,))}

        counters = read("main")
        if self.overlay is None:
            return counters
        for key, value in read(PARENT).items():
            if key[0] != "tasks":
                counters[key] = counters.get(key, 0) + value
        tables = self.overlay.tables

        def subtract(key, value):
            counters[key] = counters.get(key, 0) - value

        for name in COUNTED_TABLES:
            table = tables.get(name)
            if table is not None and table.shared and table.key:
                match = " AND ".join(f"p.{quote(c)}=d.{quote(c)}" for c in table.key)
                subtract(("rows", name, ""), connection.execute(
                    f"SELECT count(*) FROM main.{quote(table.physical)} AS d WHERE EXISTS "
                    f"(SELECT 1 FROM {PARENT}.{quote(name)} AS p WHERE {match})").fetchone()[0])
        reviews = tables.get("corpus_reviews")
        if reviews is not None and reviews.shared:
            for phase, state, count in connection.execute(
                    f"SELECT p.phase,p.state,count(*) FROM main.{quote(reviews.physical)} AS d "
                    f"JOIN {PARENT}.corpus_reviews AS p ON p.review_id=d.review_id GROUP BY p.phase,p.state"):
                subtract(("reviews", phase, state), count)
        assessments = tables.get("assessments")
        if assessments is not None and assessments.shared:
            # Loci assessed at the same level and state in both count once in the union.
            for level, state, count in connection.execute(
                    "SELECT d.evidence_level,d.validation_state,count(DISTINCT d.locus_id) "
                    f"FROM main.{quote(assessments.physical)} AS d WHERE d.analysis_id=? AND EXISTS "
                    f"(SELECT 1 FROM {PARENT}.assessments AS p WHERE p.analysis_id=d.analysis_id "
                    "AND p.locus_id=d.locus_id AND p.evidence_level=d.evidence_level "
                    "AND p.validation_state=d.validation_state) GROUP BY d.evidence_level,d.validation_state",
                    (analysis_id,)):
                subtract(("assessment-loci", level, state), count)
        return {key: value for key, value in counters.items() if value > 0}

    def _logical(self, connection, *, deep):
        version = connection.execute("PRAGMA main.user_version").fetchone()[0]
        if version not in SCHEMA_VERSIONS:
            raise ValueError("unsupported database schema version")
        if self.overlay is None:
            if connection.execute("PRAGMA main.integrity_check" if deep or not self.is_snapshot
                                  else "PRAGMA main.quick_check").fetchone()[0] != "ok":
                raise ValueError("database integrity check failed")
            if (deep or not self.is_snapshot) and connection.execute("PRAGMA main.foreign_key_check").fetchone():
                raise ValueError("database foreign key check failed")
        else:
            if connection.execute("PRAGMA main.integrity_check" if deep else "PRAGMA main.quick_check"
                                  ).fetchone()[0] != "ok":
                raise ValueError("database integrity check failed")
            problems = self.overlay.foreign_key_problems(connection)
            if problems:
                raise ValueError("database foreign key check failed: " + "; ".join(problems))
        counts = self._tables(connection)
        if version >= 3 and stored_progress_counters(connection) != self.expected_counters(connection):
            raise ValueError("progress counters disagree with stored rows; "
                             "rebuild them with corpus migrate --rebuild-counters")
        analyses = [dict(row) for row in connection.execute("SELECT * FROM analyses ORDER BY analysis_id")]
        if deep or not self.is_snapshot:
            # Branches recheck only their own rows; the parent snapshot was verified when it was sealed.
            sections, runs = self._own("source_sections"), self._own("runs")
            for row in connection.execute(f"SELECT extracted_text, text_sha256 FROM {sections}"):
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise ValueError("extracted source text hash mismatch")
            for row in connection.execute(f"SELECT protocol_json, protocol_sha256 FROM {runs}"):
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise ValueError("run protocol hash mismatch")
        artifacts = connection.execute("SELECT count(*), coalesce(sum(byte_count),0) FROM source_artifacts").fetchone()
        return version, counts, analyses, {"count": artifacts[0], "bytes": artifacts[1]}

    def _artifact_problems(self, connection, *, deep, verify_packs=True):
        """Every registered artifact must be packed, loose, or in the parent snapshot."""
        table = self._own("source_artifacts")
        stores = self.pack_stores()
        for number, store in enumerate(stores):
            connection.execute(f"ATTACH DATABASE ? AS ocsl_packs{number}",
                               (store.index_path.as_uri() + ("?mode=ro&immutable=1" if store.readonly else "?mode=ro"),))
        packed = " OR ".join(f"EXISTS (SELECT 1 FROM ocsl_packs{n}.blobs b WHERE b.sha256=s.sha256)"
                             for n in range(len(stores))) or "0"
        sizes = " OR ".join(f"EXISTS (SELECT 1 FROM ocsl_packs{n}.blobs b WHERE b.sha256=s.sha256 AND "
                            f"b.raw_length!=s.byte_count)" for n in range(len(stores))) or "0"
        bad = connection.execute(f"SELECT relative_path FROM {table} s WHERE {sizes} LIMIT 1").fetchone()
        if bad:
            raise ValueError(f"source hash/size mismatch: {bad[0]}")
        loose = 0
        for item in connection.execute(
                f"SELECT sha256, relative_path, byte_count FROM {table} s WHERE NOT ({packed}) ORDER BY sha256"):
            if item[1] != "sources/" + item[0]:
                raise ValueError("artifact paths must be content-addressed sources/<sha256>")
            found = self.locate(item[0])
            if found is None:
                raise ValueError(f"missing source: {item[1]}")
            if found[0] == "loose":
                path = found[1]
                if path.stat().st_size != item[2] or digest_file(path) != item[0]:
                    raise ValueError(f"source hash/size mismatch: {item[1]}")
                loose += 1
        if verify_packs and self.packs is not None:
            self.packs.verify(deep=deep)
        for number in range(len(stores)):
            connection.execute(f"DETACH DATABASE ocsl_packs{number}")
        return loose

    def inspect(self, *, deep=False):
        connection = self.open()
        try:
            connection.row_factory = sqlite3.Row
            version, counts, analyses, artifacts = self._logical(connection, deep=deep)
            artifacts["loose"] = self._artifact_problems(connection, deep=deep)
        finally:
            connection.close()
        result = {"schema_version": version, "counts": counts, "analyses": analyses, "artifacts": artifacts}
        if self.packs is not None:
            result["packs"] = self.packs.stats()
        if self.parent is not None:
            parent = self.parent.verify(require_snapshot=True, level="light")
            result["parent"] = {"manifest_sha256": parent["manifest_sha256"], "merkle_root": parent.get("merkle_root")}
        return result

    def _sources_v1(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT sha256, relative_path, byte_count FROM source_artifacts ORDER BY sha256")]

    def _verify_v1(self, manifest):
        """Snapshots written before packs: every source is a loose file listed in the manifest."""
        if self.parent_root:
            raise ValueError("portable snapshot must not depend on parent sources")
        connection = self.open()
        try:
            connection.row_factory = sqlite3.Row
            version, counts, analyses, _ = self._logical(connection, deep=True)
        finally:
            connection.close()
        counts = {table: counts[table] for table in TABLES + (V2_TABLES if version >= 2 else ()) if table in counts}
        sources = self._sources_v1()
        for item in sources:
            path = self.source(item)
            if path.stat().st_size != item["byte_count"] or digest_file(path) != item["sha256"]:
                raise ValueError(f"source hash/size mismatch: {item['relative_path']}")
        result = {"schema_version": version, "counts": counts, "analyses": analyses, "sources": sources}
        database_entry = manifest["database"]
        if (database_entry["path"] != "corpus.sqlite3" or database_entry["sha256"] != digest_file(self.db)
                or database_entry["byte_count"] != self.db.stat().st_size):
            raise ValueError("snapshot database hash/size mismatch")
        for key in result:
            if manifest[key] != result[key]:
                raise ValueError(f"snapshot manifest {key} mismatch")
        result.pop("sources")
        result["artifacts"] = {"count": len(sources), "bytes": sum(item["byte_count"] for item in sources), "loose": len(sources)}
        result["format_version"] = 1
        return result

    def _manifest_files(self, manifest):
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("snapshot manifest lists no files")
        seen = set()
        for item in files:
            path = item.get("path") if isinstance(item, dict) else None
            if (not isinstance(path, str) or path in seen or not isinstance(item.get("sha256"), str)
                    or not SHA256.fullmatch(item["sha256"]) or not isinstance(item.get("byte_count"), int)
                    or item["byte_count"] < 0):
                raise ValueError("snapshot manifest file entry is malformed")
            if path not in ("corpus.sqlite3", "packs/" + PACK_INDEX) and not (
                    path.startswith("packs/") and PACK_NAME.fullmatch(path[6:])):
                raise ValueError(f"snapshot manifest lists an unexpected file: {path}")
            seen.add(path)
        if {"corpus.sqlite3", "packs/" + PACK_INDEX} - seen:
            raise ValueError("snapshot manifest must list the database and the pack index")
        return files

    def _verify_v2(self, manifest, level):
        deep = level == "deep"
        files = self._manifest_files(manifest)
        by_path = {item["path"]: item for item in files}
        database_entry = manifest.get("database") or {}
        if database_entry != {key: by_path["corpus.sqlite3"][key] for key in ("path", "sha256", "byte_count")}:
            raise ValueError("snapshot database hash/size mismatch")
        listed = {item["path"][6:] for item in files if item["path"].startswith("packs/pack-")}
        present = {path.name for path in (self.root / "packs").iterdir() if path.name.startswith("pack-")}
        if present != listed:
            raise ValueError("snapshot packs differ from its manifest")
        for item in files:
            path = self.root / item["path"]
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"snapshot file is missing: {item['path']}")
            if path.stat().st_size != item["byte_count"]:
                if item["path"] == "corpus.sqlite3":
                    raise ValueError("snapshot database hash/size mismatch")
                raise ValueError(f"snapshot file size differs from its manifest: {item['path']}")
            if item["path"] == "corpus.sqlite3" or item["path"].endswith(PACK_INDEX) or level != "light":
                if digest_file(path) != item["sha256"]:
                    if item["path"] == "corpus.sqlite3":
                        raise ValueError("snapshot database hash/size mismatch")
                    raise ValueError(f"snapshot file hash differs from its manifest: {item['path']}")
        merkle = manifest.get("merkle") or {}
        if merkle.get("algorithm") != MERKLE_ALGORITHM or merkle.get("root") != merkle_root(files):
            raise ValueError("snapshot Merkle root mismatch")
        result = {"schema_version": manifest.get("schema_version"), "counts": manifest.get("counts"),
                  "analyses": manifest.get("analyses"), "artifacts": manifest.get("artifacts"),
                  "merkle_root": merkle["root"], "format_version": 2, "level": level}
        if level != "light":
            index_packs = {p["name"]: p for p in self.packs.packs()} if self.packs is not None else {}
            if set(index_packs) != listed or any(
                    index_packs[name]["sha256"] != by_path["packs/" + name]["sha256"]
                    or index_packs[name]["byte_count"] != by_path["packs/" + name]["byte_count"] for name in listed):
                raise ValueError("snapshot pack index differs from its manifest")
            connection = self.open()
            try:
                connection.row_factory = sqlite3.Row
                version, counts, analyses, artifacts = self._logical(connection, deep=deep)
                artifacts["loose"] = self._artifact_problems(connection, deep=deep, verify_packs=False)
            finally:
                connection.close()
            if deep and self.packs is not None:
                self.packs.verify(deep=True)
            if artifacts["loose"]:
                raise ValueError("snapshot artifacts must be packed")
            for key, value in (("schema_version", version), ("counts", counts), ("analyses", analyses)):
                if manifest.get(key) != value:
                    raise ValueError(f"snapshot manifest {key} mismatch")
            if {k: v for k, v in artifacts.items() if k != "loose"} != manifest.get("artifacts"):
                raise ValueError("snapshot manifest artifacts mismatch")
        return result

    def verify(self, require_snapshot=False, *, level="standard"):
        if level not in LEVELS:
            raise ValueError(f"verification level must be one of {', '.join(LEVELS)}")
        is_snapshot = require_snapshot or self.manifest_path.exists()
        if not is_snapshot:
            return self.inspect(deep=level == "deep")
        self.check_sidecars()
        manifest = self.manifest()
        if manifest.get("format") != SNAPSHOT_FORMAT or manifest.get("version") not in (1, 2):
            raise ValueError("unsupported snapshot manifest")
        result = self._verify_v1(manifest) if manifest["version"] == 1 else self._verify_v2(manifest, level)
        if "active_analysis_id" in manifest:
            active = manifest["active_analysis_id"]
            if not isinstance(active, str) or active not in {row["analysis_id"] for row in result["analyses"]}:
                raise ValueError("snapshot active analysis is not present in its database")
            result["active_analysis_id"] = active
        result["manifest_sha256"] = digest_file(self.manifest_path)
        return result

    def active_analysis_id(self):
        """Resolve snapshot defaults from verified, portable metadata only."""
        if self.manifest_path.exists():
            manifest = self.manifest()
            if manifest.get("version") == 1:
                result = self.verify(require_snapshot=True)
                analyses = result["analyses"]
            else:
                self.check_sidecars()
                with self.connect() as connection:
                    analyses = [dict(row) for row in connection.execute("SELECT analysis_id FROM analyses")]
                if "active_analysis_id" in manifest:
                    if manifest["active_analysis_id"] not in {row["analysis_id"] for row in analyses}:
                        raise ValueError("snapshot active analysis is not present in its database")
                    return manifest["active_analysis_id"]
            if "active_analysis_id" in manifest:
                return manifest["active_analysis_id"]
            if len(analyses) == 1:
                return analyses[0]["analysis_id"]
            raise ValueError("snapshot has no active analysis; specify an explicit analysis ID")
        return json.loads((self.root / "bundle.json").read_text())["active_analysis_id"]

    # -------------------------------------------------------------- snapshots
    def backup(self, destination):
        # SQLite backup includes committed WAL pages; a raw .sqlite file copy does not.
        with self.connect() as source, database(destination) as target:
            source.backup(target, pages=16384)
            target.execute("PRAGMA journal_mode=DELETE")

    def _flatten(self, destination):
        """Parent rows plus branch rows in one closed database (a portable branch snapshot)."""
        self.parent.verify(require_snapshot=True, level="light")
        shutil.copyfile(self.parent.db, destination)
        destination.chmod(0o600)
        connection = sqlite3.connect(destination)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA recursive_triggers=OFF")
            connection.execute("ATTACH DATABASE ? AS ocsl_delta", (self.db.as_uri() + "?mode=ro",))
            with connection:
                for name, table in self.overlay.tables.items():
                    if name == "progress_counters":
                        continue
                    columns = ", ".join(quote(c) for c in table.columns)
                    connection.execute(f"INSERT OR REPLACE INTO main.{quote(name)}({columns}) "
                                       f"SELECT {columns} FROM ocsl_delta.{quote(table.physical)}")
                if "progress_counters" in self.overlay.tables:
                    # The branch counted only its own rows and the copies above fired the parent's
                    # triggers once more for replaced rows: recompute the derived counters instead.
                    rebuild_progress_counters(connection, lambda name: f"main.{quote(name)}")
            connection.execute("DETACH DATABASE ocsl_delta")
            connection.execute("PRAGMA journal_mode=DELETE")
        finally:
            connection.close()

    def snapshot(self, destination):
        """Closed database copy, hard-linked sealed packs, a small manifest and a Merkle root.

        Sealed packs are checked by their stat signature (rehashed only when it changed)
        and shared by hard link; loose artifacts are packed into the snapshot. The result
        reports the wall time of each phase.
        """
        clock = _Phases()
        if self.manifest_path.exists():
            # Never bless tampering: a snapshot source must still match its own manifest.
            with clock("verify_source"):
                self.verify(require_snapshot=True, level="light")
        active = self.active_analysis_id()
        with self.workspace.create(destination) as root:
            db = root / "corpus.sqlite3"
            with clock("database"):
                if self.overlay is not None:
                    self._flatten(db)
                else:
                    self.backup(db)
            stores = self.pack_stores()
            with clock("seal"):
                for store in stores:
                    if not store.readonly:
                        store.seal_open()
            packs_dir = root / "packs"
            packs_dir.mkdir(mode=0o700)
            listed, linked = {}, 0
            with clock("link"):
                for store in stores:
                    shared = []
                    for pack in store.packs("sealed"):
                        if pack["name"] in listed:
                            continue
                        source = store.pack_path(pack["name"])
                        if source.is_symlink() or not source.is_file():
                            raise PackIntegrityError(f"pack is missing: {pack['name']}")
                        if store.readonly:
                            if source.stat().st_size != pack["byte_count"]:
                                raise PackIntegrityError(f"sealed pack size changed: {pack['name']}")
                        else:
                            store.sealed_digest(pack["name"], pack["sha256"], remember=True)
                        if link_or_copy(source, packs_dir / pack["name"]) == "copied":
                            if digest_file(packs_dir / pack["name"]) != pack["sha256"]:
                                raise PackIntegrityError(f"sealed pack changed after sealing: {pack['name']}")
                        else:
                            linked += 1
                        shared.append((pack["name"], pack["sha256"]))
                        listed[pack["name"]] = pack
                    # Linking changes the inode's ctime: refresh the stat cache in one transaction.
                    store.remember_many(shared)
            index = packs_dir / PACK_INDEX
            sources = [store.index_path for store in stores]
            with clock("index"):
                build_index(index, list(listed.values()), sources)
            with clock("loose"):
                found = self._unpacked(db, index)
                missing = [(digest, size) for digest, size, packed in found if packed is None]
                if missing:
                    # Loose artifacts (older libraries, legacy archives) are packed into the snapshot itself.
                    staging = PackStore.create(root / ".packing")
                    with staging.batch():
                        for digest, byte_count in missing:
                            staging.put(self.read_blob(digest, byte_count), digest)
                    staging.seal_open()
                    for item in staging.packs("sealed"):
                        os.rename(staging.pack_path(item["name"]), packs_dir / item["name"])
                        listed[item["name"]] = item
                    index.unlink()
                    build_index(index, list(listed.values()), sources + [staging.index_path])
                    shutil.rmtree(root / ".packing")
                    found = self._unpacked(db, index)
            with clock("check"):
                bad = found
                if bad:
                    if bad[0][2] is None:
                        raise ValueError("snapshot could not pack every registered artifact")
                    raise ValueError(f"source hash/size mismatch: sources/{bad[0][0]}")
                connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
                try:
                    connection.row_factory = sqlite3.Row
                    version = connection.execute("PRAGMA main.user_version").fetchone()[0]
                    if version not in SCHEMA_VERSIONS:
                        raise ValueError("unsupported database schema version")
                    if connection.execute("PRAGMA main.quick_check").fetchone()[0] != "ok":
                        raise ValueError("database integrity check failed")
                    counts = self._tables(connection)
                    analyses = [dict(row) for row in connection.execute("SELECT * FROM analyses ORDER BY analysis_id")]
                    artifacts = connection.execute(
                        "SELECT count(*), coalesce(sum(byte_count),0) FROM source_artifacts").fetchone()
                finally:
                    connection.close()
            if active not in {row["analysis_id"] for row in analyses}:
                raise ValueError("active analysis is not present in the captured database")
            with clock("manifest"):
                files = [{"path": "corpus.sqlite3", "sha256": digest_file(db), "byte_count": db.stat().st_size},
                         {"path": "packs/" + PACK_INDEX, "sha256": digest_file(index),
                          "byte_count": index.stat().st_size}]
                files += [{"path": "packs/" + name, "sha256": pack["sha256"], "byte_count": pack["byte_count"]}
                          for name, pack in sorted(listed.items())]
                manifest = snapshot_manifest(files, version=version, counts=counts, analyses=analyses,
                                             artifacts={"count": artifacts[0], "bytes": artifacts[1]}, active=active)
                write_json(root / "manifest.json", manifest)
                for path in (db, index, root / "manifest.json"):
                    path.chmod(0o444)
            return {"bundle": destination, "manifest_sha256": digest_file(root / "manifest.json"),
                    "merkle_root": manifest["merkle"]["root"], "counts": counts, "portable": True,
                    "packs": len(listed), "linked_packs": linked, "files": len(files) + 1,
                    "manifest_bytes": (root / "manifest.json").stat().st_size,
                    "seconds": clock.total(), "phases": clock.phases}

    @staticmethod
    def _unpacked(db, index):
        """Registered artifacts that the index lacks (packed is None) or holds with another size."""
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("ATTACH DATABASE ? AS ocsl_index", (index.as_uri() + "?mode=ro",))
            return connection.execute(
                "SELECT s.sha256, s.byte_count, b.raw_length FROM source_artifacts s LEFT JOIN ocsl_index.blobs b "
                "ON b.sha256=s.sha256 WHERE b.sha256 IS NULL OR b.raw_length != s.byte_count "
                "OR s.relative_path != 'sources/' || s.sha256 ORDER BY s.sha256").fetchall()
        finally:
            connection.close()

    # --------------------------------------------------------------- branches
    def _create_delta(self, destination):
        source = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            statements = [row[0] for row in source.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
            version = source.execute("PRAGMA user_version").fetchone()[0]
        finally:
            source.close()
        target = sqlite3.connect(destination)
        try:
            target.executescript(";\n".join(statements) + ";")
            # Shared tables carry a prefix; connections show the parent's rows under the plain names.
            prefix_shared_tables(target)
            target.execute(f"PRAGMA user_version={int(version)}")
            target.commit()
        finally:
            target.close()

    def branch(self, destination, question, analysis_id, protocol=None, code_revision=None):
        """A delta database over this snapshot; the parent database is never copied."""
        if not self.manifest_path.exists():
            raise ValueError("branch requires a snapshot; take a snapshot first")
        clock = _Phases()
        with clock("verify_parent"):
            parent = self.verify(require_snapshot=True, level="light")
        with self.workspace.create(destination) as root:
            with clock("delta"):
                delta = root / "corpus.sqlite3"
                self._create_delta(delta)
                with self.connect() as connection:
                    corpus = connection.execute("SELECT corpus_id FROM corpora").fetchall()
                    clash = connection.execute("SELECT 1 FROM analyses WHERE analysis_id=?", (analysis_id,)).fetchone()
                if len(corpus) != 1:
                    raise ValueError("branch expects one corpus per library")
                if clash:
                    raise ValueError("analysis ID already exists in the parent snapshot")
                PackStore.create(root / "packs")
                write_json(root / "bundle.json", {
                    "version": 2, "kind": "delta-branch", "parent_snapshot": self.relative,
                    "parent_manifest_sha256": parent["manifest_sha256"],
                    "parent_merkle_root": parent.get("merkle_root"), "active_analysis_id": analysis_id,
                })
                created = Library(self.workspace, destination)
                connection = created.open(write=True)
                try:
                    with connection:
                        add_analysis(connection, corpus[0][0], question, analysis_id,
                                     parent["manifest_sha256"], protocol, code_revision)
                finally:
                    connection.close()
            with clock("check"):
                connection = created.open()
                try:
                    connection.row_factory = sqlite3.Row
                    created._logical(connection, deep=False)
                finally:
                    connection.close()
            return {"bundle": destination, "analysis_id": analysis_id,
                    "parent_manifest_sha256": parent["manifest_sha256"],
                    "parent_merkle_root": parent.get("merkle_root"),
                    "portable": False, "delta": True, "source_parent": self.relative,
                    "seconds": clock.total(), "phases": clock.phases}

    # -------------------------------------------------------------- migration
    def pack_loose(self):
        """Offline migration: copy loose registered artifacts into packs (loose files stay)."""
        if self.is_snapshot:
            raise ValueError("immutable snapshots cannot be modified; branch first")
        if self.packs is None:
            self.packs = PackStore.create(self.root / "packs")
        packed = present = byte_total = 0
        connection = self.open()
        try:
            rows = connection.execute(f"SELECT sha256, byte_count FROM {self._own('source_artifacts')} ORDER BY sha256")
            with self.packs.batch():
                for digest, byte_count in rows:
                    if self.packs.contains(digest):
                        present += 1
                        continue
                    path = self._loose(digest)
                    if path is None:
                        if self.parent is not None and self.parent.has_blob(digest):
                            present += 1
                            continue
                        raise ValueError(f"missing source: sources/{digest}")
                    _, size = self.packs.put_file(path, digest)
                    if size != byte_count:
                        raise ValueError(f"source hash/size mismatch: sources/{digest}")
                    packed += 1
                    byte_total += size
        finally:
            connection.close()
        return {"packed": packed, "packedBytes": byte_total, "alreadyPresent": present, **self.packs.stats()}


class _Phases:
    """Wall time per named phase of a storage operation."""

    def __init__(self):
        self.started = time.monotonic()
        self.phases = {}

    @contextmanager
    def __call__(self, name):
        began = time.monotonic()
        try:
            yield
        finally:
            self.phases[name] = round(self.phases.get(name, 0) + time.monotonic() - began, 3)

    def total(self):
        return round(time.monotonic() - self.started, 3)


def snapshot_manifest(files, *, version, counts, analyses, artifacts, active=None, extra=None):
    database_entry = next(item for item in files if item["path"] == "corpus.sqlite3")
    manifest = {"format": SNAPSHOT_FORMAT, "version": 2, "created_at": now(), "schema_version": version,
                "counts": counts, "analyses": analyses, "artifacts": artifacts,
                "database": dict(database_entry), "files": sorted(files, key=lambda item: item["path"]),
                "merkle": {"algorithm": MERKLE_ALGORITHM, "leaves": len(files), "root": merkle_root(files)}}
    if active is not None:
        manifest["active_analysis_id"] = active
    if extra:
        manifest.update(extra)
    return manifest


def add_analysis(connection, corpus_id, question, analysis_id, parent_digest=None,
                 protocol=None, code_revision=None):
    if not question.strip() or not analysis_id.strip():
        raise ValueError("question and analysis ID must be nonempty")
    if protocol is None:
        protocol = {"status": "unconfigured", "question": question}
    protocol = json.dumps(protocol, sort_keys=True)
    run_id = str(uuid.uuid4())
    connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                       (run_id, corpus_id, protocol, hashlib.sha256(protocol.encode()).hexdigest(),
                        code_revision, now()))
    connection.execute("INSERT INTO analyses VALUES (?, ?, ?, ?, ?)",
                       (analysis_id, run_id, question, parent_digest, now()))


def initialize(workspace, destination, question, analysis_id, protocol=None, code_revision=None):
    with workspace.create(destination) as root:
        with database(root / "corpus.sqlite3") as connection:
            connection.executescript(SCHEMA.read_text())
            corpus_id = str(uuid.uuid4())
            connection.execute("INSERT INTO corpora VALUES (?, ?, ?)",
                               (corpus_id, "Unpopulated literature corpus", now()))
            add_analysis(connection, corpus_id, question, analysis_id,
                         protocol=protocol, code_revision=code_revision)
        PackStore.create(root / "packs")
        write_json(root / "bundle.json", {"version": 1, "active_analysis_id": analysis_id})
        return {"bundle": destination, "analysis_id": analysis_id, "corpus_id": corpus_id,
                "status": "empty; no corpus evidence has been imported",
                "protocol_supplied": protocol is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="existing selected chat workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create an empty library; never overwrite")
    init.add_argument("destination")
    init.add_argument("--question", required=True)
    init.add_argument("--analysis-id", default=None)
    init.add_argument("--protocol", help="workspace-relative frozen protocol JSON")
    init.add_argument("--code-revision", help="actual importer/analysis code revision, when known")
    verify = commands.add_parser("verify", help="check DB, all registered sources, and any manifest")
    verify.add_argument("library")
    verify.add_argument("--level", choices=LEVELS, default="standard")
    snapshot = commands.add_parser("snapshot", help="create a portable snapshot: closed DB, packs, Merkle root")
    snapshot.add_argument("library")
    snapshot.add_argument("destination")
    branch = commands.add_parser("branch", help="delta database over a snapshot; the parent is never copied")
    branch.add_argument("library")
    branch.add_argument("destination")
    branch.add_argument("--question", required=True)
    branch.add_argument("--analysis-id", default=None)
    branch.add_argument("--protocol", help="workspace-relative new frozen protocol JSON")
    branch.add_argument("--code-revision", help="actual new analysis code revision, when known")
    pack = commands.add_parser("pack", help="copy loose registered artifacts into packs")
    pack.add_argument("library")
    args = parser.parse_args(argv)
    try:
        workspace = Workspace(args.workspace)
        protocol = None
        if getattr(args, "protocol", None):
            protocol = json.loads(workspace.path(args.protocol).read_text())
            if not isinstance(protocol, dict):
                raise ValueError("protocol must be a JSON object")
        if args.command == "init":
            result = initialize(workspace, args.destination, args.question,
                                args.analysis_id or str(uuid.uuid4()), protocol, args.code_revision)
        else:
            library = Library(workspace, args.library)
            if args.command == "verify":
                checked = library.verify(level=args.level)
                result = {"bundle": args.library, "verified": True,
                          "counts": checked["counts"], "analyses": checked["analyses"],
                          "manifest_sha256": checked.get("manifest_sha256"),
                          "merkle_root": checked.get("merkle_root")}
            elif args.command == "snapshot":
                result = library.snapshot(args.destination)
            elif args.command == "pack":
                result = library.pack_loose()
            else:
                result = library.branch(args.destination, args.question,
                                        args.analysis_id or str(uuid.uuid4()), protocol, args.code_revision)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
