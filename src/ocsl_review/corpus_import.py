# SPDX-License-Identifier: Apache-2.0
"""Explicit, read-only adapters for legacy scores and per-review census state.

Legacy results remain observations under their original instrument, never v2
per-locus assessments. Inputs are copied before SQLite opens them: even WAL
recovery and shared-memory creation take place only in the new corpus cache.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterator

from . import corpus_library as library
from .corpus_store import CorpusRepository, canonical, identity
from .errors import ValidationError


IMPORT_VERSION = "legacy-import-v1"


def _regular(path: Path) -> Path:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValidationError("legacy input must be a regular file without symlinks")
    return path


def _stat(path: Path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def _read_stable(path: Path) -> bytes:
    _regular(path)
    before = _stat(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        body = stream.read()
    if before != _stat(path):
        raise ValidationError("legacy input changed during capture; pause its writer and retry")
    return body


@contextmanager
def _snapshot(repo: CorpusRepository, source: Path) -> Iterator[tuple[sqlite3.Connection, dict]]:
    source = _regular(Path(source))
    wal = Path(str(source) + "-wal")
    before = (_stat(source), _stat(wal))
    raw = _read_stable(source)
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValidationError("legacy source is not a SQLite database")
    wal_raw = _read_stable(wal) if wal.exists() else None
    if before != (_stat(source), _stat(wal)):
        raise ValidationError("legacy database changed during capture; pause its writer and retry")
    cache = repo.root / "cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="legacy-", dir=cache) as directory:
        copied = Path(directory) / "source.sqlite3"
        copied.write_bytes(raw)
        if wal_raw is not None:
            Path(str(copied) + "-wal").write_bytes(wal_raw)
        stable = Path(directory) / "snapshot.sqlite3"
        incoming = sqlite3.connect(copied)
        output = sqlite3.connect(stable)
        try:
            incoming.execute("PRAGMA trusted_schema=OFF")
            if incoming.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValidationError("legacy source failed SQLite integrity checking")
            incoming.backup(output)
        finally:
            output.close()
            incoming.close()
        raw_sha = repo.artifact(raw, "legacy-original-database", "application/vnd.sqlite3")
        wal_sha = repo.artifact(wal_raw, "legacy-original-wal", "application/octet-stream") if wal_raw is not None else None
        snapshot_sha = repo.artifact(stable.read_bytes(), "legacy-database-snapshot", "application/vnd.sqlite3")
        connection = sqlite3.connect(stable.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        try:
            yield connection, {"sourcePath": str(source), "originalDatabaseSha256": raw_sha,
                               "originalWalSha256": wal_sha, "snapshotSha256": snapshot_sha}
        finally:
            connection.close()


def _require(connection: sqlite3.Connection, tables: dict[str, set[str]]) -> None:
    actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in tables.items():
        if table not in actual or not columns <= {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}:
            raise ValidationError(f"unsupported legacy database: missing {table} columns")


def _namespace(repo: CorpusRepository, prefix: str, kind: str, manifest: dict) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", prefix):
        raise ValidationError("legacy namespace must be a short alphanumeric name")
    digest = repo.json_artifact(manifest, "legacy-import-manifest")
    capture_id = identity(manifest["snapshotSha256"], manifest.get("companions", []))
    analysis_id = f"{prefix}:{capture_id}"
    protocol = {"version": IMPORT_VERSION, "sourceKind": kind,
                "sourceSnapshotSha256": manifest["snapshotSha256"],
                "legacyOnly": True, "eligibleForV2Assessment": False}
    current = repo.analysis()
    with repo.connect(write=True) as connection:
        existing = connection.execute("SELECT protocol_json FROM analyses JOIN runs USING(run_id) WHERE analysis_id=?", (analysis_id,)).fetchone()
        if existing:
            if json.loads(existing[0]) != protocol:
                raise ValidationError("legacy analysis namespace already has a different protocol")
        else:
            library.add_analysis(connection, current["corpus_id"], "Preserved legacy " + kind + " observations",
                                 analysis_id, protocol=protocol)
    return analysis_id, digest


def _completed(repo: CorpusRepository, analysis: str):
    with repo.connect() as connection:
        row = connection.execute("SELECT summary_json FROM corpus_imports WHERE import_id=?", (analysis,)).fetchone()
    return json.loads(row[0]) if row else None


def _finish(repo: CorpusRepository, analysis: str, manifest: dict, kind: str, summary: dict) -> dict:
    result = {"analysisId": analysis, "sourceKind": kind, "snapshotSha256": manifest["snapshotSha256"],
              "eligibleForV2Assessment": False, **summary}
    with repo.connect(write=True) as connection:
        connection.execute("INSERT OR IGNORE INTO corpus_imports VALUES (?,?,?,?,?)",
                           (analysis, manifest["snapshotSha256"], kind, canonical(result), library.now()))
    return result


def _observations(repo: CorpusRepository, records) -> None:
    """Bound transaction size for historical corpora with hundreds of thousands of rows."""
    batch = []
    for record in records:
        batch.append(record)
        if len(batch) == 1000:
            with repo.connect(write=True) as connection:
                connection.executemany("INSERT OR IGNORE INTO work_observations VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        with repo.connect(write=True) as connection:
            connection.executemany("INSERT OR IGNORE INTO work_observations VALUES (?,?,?,?,?,?,?,?,?,?)", batch)


def _observation(analysis: str, work: str, property_name: str, key: str, value: dict,
                 method_sha: str, receipt: str | None = None, timestamp: str | None = None):
    return ("legacy-observation-" + identity(analysis, property_name, key), analysis, work,
            property_name, canonical(value), IMPORT_VERSION, method_sha, receipt,
            "unresolved", timestamp or library.now())


def import_legacy_scores(repo: CorpusRepository, source_db: Path, *, namespace: str = "legacy-scores") -> dict:
    """Preserve scores as scores: the old table does not contain exact quote/locus evidence."""
    with _snapshot(repo, source_db) as (source, manifest):
        _require(source, {"reviews": {"doi", "title"}, "citations": {"doi", "label"}})
        analysis, method = _namespace(repo, namespace, "scores", manifest)
        previous = _completed(repo, analysis)
        if previous:
            return previous
        works = {}
        count = {"reviews": 0, "citations": 0}
        for row in source.execute("SELECT rowid AS legacy_rowid,* FROM reviews"):
            record = dict(row)
            key = (record.get("journal"), record["doi"])
            works[key] = repo.work({"doi": record["doi"], "title": record["title"], "year": record.get("year")},
                                   key=None if record["doi"] else "legacy-review-" + identity(analysis, key))
            _observations(repo, [_observation(analysis, works[key], "legacy.review_summary", str(record["legacy_rowid"]),
                {"row": record, "sourceTable": "reviews", "evidenceLimitation": "Original aggregate; no per-locus evidence."}, method)])
            count["reviews"] += 1
        def citations():
            for row in source.execute("SELECT rowid AS legacy_rowid,* FROM citations"):
                record = dict(row)
                key = (record.get("journal"), record["doi"])
                if key not in works:
                    works[key] = repo.work({"doi": record["doi"]}, key=None if record["doi"] else "legacy-review-" + identity(analysis, key))
                count["citations"] += 1
                yield _observation(analysis, works[key], "legacy.citation_score", str(record["legacy_rowid"]),
                    {"row": record, "sourceTable": "citations", "evidenceLimitation": "No original claim locus, model receipt or evidence quotation in scores database."}, method)
        _observations(repo, citations())
        return _finish(repo, analysis, manifest, "scores", count)


def _companions(repo: CorpusRepository, db_path: Path) -> tuple[list[dict], dict]:
    root = db_path.parent.parent
    if root.name != ".ocsl-review":
        return [], {}
    artifacts, manuscript = [], {}
    paths = [root / name for name in ("manuscript.json", "audit-graph.json", "literature-audit.json", "review-plan.json")]
    literature = root / "literature"
    if literature.is_symlink():
        raise ValidationError("legacy ledger directory must not be a symlink")
    if literature.is_dir():
        paths += sorted(p for p in literature.rglob("*") if p.is_file() or p.is_symlink())
    for path in paths:
        if not path.exists():
            continue
        body = _read_stable(path)
        sha = repo.artifact(body, "legacy-ledger", "application/json" if path.suffix == ".json" else "application/octet-stream")
        artifacts.append({"relativePath": path.relative_to(root).as_posix(), "sha256": sha})
        if path.name == "manuscript.json" and path.parent == root:
            try:
                manuscript = json.loads(body)
            except (ValueError, UnicodeError):
                raise ValidationError("legacy manuscript is not JSON") from None
    return artifacts, manuscript


def _import_census_database(repo: CorpusRepository, db_path: Path, namespace: str) -> dict:
    with _snapshot(repo, db_path) as (source, manifest):
        _require(source, {"runs": {"id", "spec"}, "tasks": {"id", "run_id", "spec"},
                          "assessments": {"id", "run_id", "task_id", "accepted", "payload", "recorded_at"}})
        manifest["companions"], manuscript = _companions(repo, db_path)
        analysis, method = _namespace(repo, namespace, "census", manifest)
        previous = _completed(repo, analysis)
        if previous:
            return previous
        publication = manuscript.get("structuredSource", {}).get("publication", {})
        metadata = {k: publication[k] for k in ("doi", "pmid", "pmcid") if publication.get(k)}
        metadata["title"] = publication.get("article-title", "Legacy review")
        work = repo.work(metadata, key=None if any(metadata.get(k) for k in ("doi", "pmid", "pmcid")) else "legacy-census-" + manifest["snapshotSha256"])
        tasks = {}
        runs = {row["id"]: json.loads(row["spec"]) for row in source.execute("SELECT * FROM runs")}
        for row in source.execute("SELECT * FROM tasks"):
            request_sha = repo.artifact(row["spec"].encode(), "legacy-task-record")
            tasks[(row["run_id"], row["id"])] = request_sha
        accepted = set()
        counts = {"runs": len(runs), "tasks": len(tasks), "attempts": 0, "legacyAcceptedAttempts": 0,
                  "legacyRejectedAttempts": 0, "receipts": 0}
        def observations():
            for row in source.execute("SELECT * FROM assessments"):
                payload = json.loads(row["payload"])
                raw_sha = repo.artifact(row["payload"].encode(), "legacy-assessment-record")
                result = payload.get("result") or {}
                request_sha = tasks.get((row["run_id"], row["task_id"]))
                receipt = None
                if request_sha:
                    receipt = "legacy-receipt-" + identity(analysis, row["id"])
                    outcome = "completed" if result.get("state") == "completed" and result.get("output") is not None else "failed" if result.get("state") == "failed" else "uncertain"
                    with repo.connect(write=True) as connection:
                        connection.execute("INSERT OR IGNORE INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (receipt, analysis, row["task_id"], request_sha, raw_sha,
                             str(result.get("providerId") or "unknown"), str(result.get("model") or "unknown"),
                             canonical({"format": "legacy-task-and-assessment-records", "wireRequestAvailable": False,
                                        "legacyRunId": row["run_id"], "runSpec": runs.get(row["run_id"]),
                                        "promptSha256": result.get("promptSha256"), "inputSha256": result.get("inputSha256")}),
                             canonical(result.get("usage") or {}), outcome, row["recorded_at"], row["recorded_at"]))
                    counts["receipts"] += 1
                counts["attempts"] += 1
                if row["accepted"]:
                    accepted.add((row["run_id"], row["task_id"]))
                    counts["legacyAcceptedAttempts"] += 1
                else:
                    counts["legacyRejectedAttempts"] += 1
                yield _observation(analysis, work, "legacy.census_attempt", row["id"],
                    {"legacyRunId": row["run_id"], "legacyTaskId": row["task_id"],
                     "legacyAccepted": bool(row["accepted"]), "rawPayloadSha256": raw_sha,
                     "assessment": payload.get("assessment"), "validationError": payload.get("validationError"),
                     "evidenceLimitation": "Preserved under original census protocol; not revalidated as v2."},
                    method, receipt, row["recorded_at"])
        _observations(repo, observations())
        counts["legacyAcceptedDistinctTasks"] = len(accepted)
        return _finish(repo, analysis, manifest, "census", counts)


def import_legacy_census(repo: CorpusRepository, source_path: Path, *, namespace: str = "legacy-census") -> dict:
    source = Path(source_path).absolute()
    if source.is_file():
        databases = [_regular(source)]
    elif source.is_dir() and not any(p.is_symlink() for p in (source, *source.parents)):
        # Deliberately excludes the root pilot and smoke databases.
        databases = sorted(source.glob("work/corpus/ws/*/.ocsl-review/census/census.sqlite3"))
    else:
        raise ValidationError("legacy census input must be a database or paused workspace")
    if not databases:
        raise ValidationError("no production per-review census databases found")
    results = [_import_census_database(repo, path, namespace) for path in databases]
    return {"databases": len(results), "imports": results,
            "legacyAcceptedDistinctTasks": sum(item["legacyAcceptedDistinctTasks"] for item in results),
            "eligibleForV2Assessment": False}
