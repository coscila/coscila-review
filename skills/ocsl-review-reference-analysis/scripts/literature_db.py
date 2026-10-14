#!/usr/bin/env python3
"""Initialize, verify, snapshot and branch a literature library (Python stdlib).

This manages storage, not ingestion, scientific judging, or a worker scheduler.
Paths are relative to an explicit workspace. Snapshots are portable, complete
copies; branches reuse their parent's sources and copy only the database.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid


TABLES = (
    "corpora", "runs", "analyses", "works", "work_identifiers", "source_artifacts",
    "retrievals", "source_sections", "reviews", "bibliography_entries", "claim_loci",
    "model_receipts", "assessments", "assessment_evidence", "work_observations",
    "work_observation_evidence", "processing_state", "facts",
)
SCHEMA = Path(__file__).resolve().parent.parent / "assets" / "literature-schema.sql"


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


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class Workspace:
    def __init__(self, path):
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

    def path(self, relative):
        candidate = Path(relative)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise ValueError("paths must be nonempty workspace-relative paths without '..'")
        current = self.root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"symlink is not allowed: {relative}")
        if not current.is_relative_to(self.root):
            raise ValueError("path leaves workspace")
        if any(current == p or p in current.parents for p in self.protected):
            raise ValueError("library path overlaps companion source or operator state")
        return current

    def relative(self, path):
        return path.relative_to(self.root).as_posix()

    @contextmanager
    def create(self, relative):
        destination = self.path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700)  # Never overwrite an existing bundle.
        try:
            (destination / "sources").mkdir()
            yield destination
        except BaseException:
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
        self.parent_root = None
        self.parent_sources = set()
        origin = workspace.path(f"{relative}/bundle.json")
        if origin.exists():
            metadata = json.loads(origin.read_text())
            parent = metadata.get("parent_snapshot")
            if parent:
                self.parent_root = workspace.path(parent)
                manifest = workspace.path(f"{parent}/manifest.json")
                if digest_file(manifest) != metadata["parent_manifest_sha256"]:
                    raise ValueError("parent snapshot manifest has changed")
                self.parent_sources = {item["sha256"] for item in
                                       json.loads(manifest.read_text())["sources"]}

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def source(self, item):
        expected = "sources/" + item["sha256"]
        if item["relative_path"] != expected:
            raise ValueError("artifact paths must be content-addressed sources/<sha256>")
        path = self.workspace.path(f"{self.relative}/{expected}")
        if not path.exists() and self.parent_root and item["sha256"] in self.parent_sources:
            path = self.workspace.path(self.workspace.relative(self.parent_root / expected))
        if not path.is_file():
            raise ValueError(f"missing source: {expected}")
        return path

    def inspect(self):
        with self.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("unsupported database schema version")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("database foreign key check failed")
            counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in TABLES}
            analyses = [dict(row) for row in connection.execute(
                "SELECT * FROM analyses ORDER BY analysis_id")]
            sources = [dict(row) for row in connection.execute(
                "SELECT sha256, relative_path, byte_count FROM source_artifacts ORDER BY sha256")]
            for row in connection.execute("SELECT extracted_text, text_sha256 FROM source_sections"):
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise ValueError("extracted source text hash mismatch")
            for row in connection.execute("SELECT protocol_json, protocol_sha256 FROM runs"):
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise ValueError("run protocol hash mismatch")
        for item in sources:
            path = self.source(item)
            if path.stat().st_size != item["byte_count"] or digest_file(path) != item["sha256"]:
                raise ValueError(f"source hash/size mismatch: {item['relative_path']}")
        return {"schema_version": 1, "counts": counts, "analyses": analyses, "sources": sources}

    def verify(self, require_snapshot=False):
        manifest_path = self.workspace.path(f"{self.relative}/manifest.json")
        is_snapshot = require_snapshot or manifest_path.exists()
        if is_snapshot:
            # A manifest hashes the complete checkpointed DB, never mutable sidecar pages.
            for suffix in ("-wal", "-journal"):
                sidecar = self.workspace.path(f"{self.relative}/corpus.sqlite3{suffix}")
                if sidecar.exists() and sidecar.stat().st_size:
                    raise ValueError("snapshot has an unexpected SQLite journal; use a fresh backup snapshot")
        result = self.inspect()
        if is_snapshot:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("format") != "ocsl-literature-snapshot" or manifest.get("version") != 1:
                raise ValueError("unsupported snapshot manifest")
            if self.parent_root:
                raise ValueError("portable snapshot must not depend on parent sources")
            database = manifest["database"]
            if (database["path"] != "corpus.sqlite3" or database["sha256"] != digest_file(self.db)
                    or database["byte_count"] != self.db.stat().st_size):
                raise ValueError("snapshot database hash/size mismatch")
            for key in result:
                if manifest[key] != result[key]:
                    raise ValueError(f"snapshot manifest {key} mismatch")
            result["manifest_sha256"] = digest_file(manifest_path)
        return result

    def backup(self, destination):
        # SQLite backup includes committed WAL pages; a raw .sqlite file copy does not.
        with self.connect() as source, database(destination) as target:
            source.backup(target, pages=256)
            target.execute("PRAGMA journal_mode=DELETE")

    def snapshot(self, destination):
        if self.workspace.path(f"{self.relative}/manifest.json").exists():
            self.verify(require_snapshot=True)
        with self.workspace.create(destination) as root:
            self.backup(root / "corpus.sqlite3")
            # Read the captured database, not a later state of the live writer.
            captured = Library(self.workspace, destination)
            with captured.connect() as connection:
                for item in connection.execute("SELECT * FROM source_artifacts ORDER BY sha256"):
                    original = self.source(item)
                    target = self.workspace.path(f"{destination}/{item['relative_path']}")
                    with original.open("rb") as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    target.chmod(0o444)
            result = captured.inspect()  # Missing or changed source bytes fail the entire snapshot.
            manifest = {"format": "ocsl-literature-snapshot", "version": 1,
                        "created_at": now(), **result,
                        "database": {"path": "corpus.sqlite3",
                                     "sha256": digest_file(captured.db),
                                     "byte_count": captured.db.stat().st_size}}
            write_json(root / "manifest.json", manifest)
            captured.db.chmod(0o444)
            (root / "manifest.json").chmod(0o444)
            return {"bundle": destination, "manifest_sha256": digest_file(root / "manifest.json"),
                    "counts": result["counts"], "portable": True}

    def branch(self, destination, question, analysis_id, protocol=None, code_revision=None):
        parent = self.verify(require_snapshot=True)
        with self.workspace.create(destination) as root:
            self.backup(root / "corpus.sqlite3")
            with database(root / "corpus.sqlite3") as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                corpus = connection.execute("SELECT corpus_id FROM corpora").fetchall()
                if len(corpus) != 1:
                    raise ValueError("branch expects one corpus per library")
                add_analysis(connection, corpus[0][0], question, analysis_id,
                             parent["manifest_sha256"], protocol, code_revision)
            write_json(root / "bundle.json", {
                "version": 1, "parent_snapshot": self.relative,
                "parent_manifest_sha256": parent["manifest_sha256"],
                "active_analysis_id": analysis_id,
            })
            Library(self.workspace, destination).verify()
            return {"bundle": destination, "analysis_id": analysis_id,
                    "parent_manifest_sha256": parent["manifest_sha256"],
                    "portable": False, "source_parent": self.relative}


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
    snapshot = commands.add_parser("snapshot", help="create a full portable snapshot including sources")
    snapshot.add_argument("library")
    snapshot.add_argument("destination")
    branch = commands.add_parser("branch", help="copy a snapshot DB; reuse immutable parent sources")
    branch.add_argument("library")
    branch.add_argument("destination")
    branch.add_argument("--question", required=True)
    branch.add_argument("--analysis-id", default=None)
    branch.add_argument("--protocol", help="workspace-relative new frozen protocol JSON")
    branch.add_argument("--code-revision", help="actual new analysis code revision, when known")
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
                checked = library.verify()
                result = {"bundle": args.library, "verified": True,
                          "counts": checked["counts"], "analyses": checked["analyses"],
                          "manifest_sha256": checked.get("manifest_sha256")}
            elif args.command == "snapshot":
                result = library.snapshot(args.destination)
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
