# SPDX-License-Identifier: Apache-2.0
"""The follow-up branch: a delta database over a frozen census scores database.

``FollowupBranch.create`` makes a new SQLite file with the follow-up schema and records the parent census it branches
from: its path, sha256, size and modification time, the metadata that pins the census (instrument digest, study
bundle, completeness) and a digest of its review population. Every connection ATTACHes the parent read-only and
immutable (``mode=ro&immutable=1``) as ``census``, and, when the study supplies one, the private frozen copy that keeps
entitled citing sentences as ``census_text``. The parent is never copied and never written; no foreign key crosses
databases. ``verify`` recomputes the parent's sha256 (the gate: the parent digest is unchanged after branch and run),
checks the schema and checks every reference a follow-up row makes to the parent.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import zlib
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import followup_schema, scores_db
from .errors import ValidationError
from .followup_model import REDACTED_PREFIX, canonical, dataset_id

PARENT = "census"
PARENT_TEXT = "census_text"
PARENT_TABLES = ("meta", "reviews", "citations", "loci", "works", "identity")
PROTOCOL_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "followup-protocol.schema.json"
PROTOCOL_FORMAT = "ocsl-followup-protocol-v1"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uri(path: Path, query: str) -> str:
    return path.resolve().as_uri() + query


def population_digest(connection: sqlite3.Connection, schema: str = "main") -> str:
    """Digest of a census population: every review with its provenance class, in key order."""
    value = hashlib.sha256()
    for row in connection.execute(f"SELECT journal, doi, provenance_class FROM {schema}.reviews ORDER BY journal, doi"):
        value.update(canonical(list(row)).encode("utf-8") + b"\n")
    return value.hexdigest()


def inspect_parent(path: Path, *, allow_incomplete: bool = False, require_text: bool = False) -> dict[str, Any]:
    """Check that ``path`` is a frozen census scores database and describe it (digest, pins, population)."""
    path = Path(path)
    if not path.is_absolute():
        raise ValidationError("the parent census path must be absolute")
    if not path.is_file():
        raise ValidationError(f"the parent census is missing: {path}")
    with closing(sqlite3.connect(_uri(path, "?mode=ro&immutable=1"), uri=True)) as connection:
        try:
            present = followup_schema.tables(connection)
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall()) if "meta" in present else {}
        except sqlite3.DatabaseError as error:
            raise ValidationError("the parent is not a census scores database") from error
        missing = [table for table in PARENT_TABLES if table not in present]
        if missing:
            raise ValidationError("the parent is not a census scores database (missing " + ", ".join(missing) + ")")
        if meta.get("schemaVersion") != str(scores_db.SCHEMA_VERSION):
            raise ValidationError("unsupported parent census schema version")
        if meta.get("frozen") != "1":
            raise ValidationError("the parent census must be a frozen scores database (study freeze)")
        if meta.get("complete") != "1" and not allow_incomplete:
            raise ValidationError("the parent census records an incomplete run; branch only a complete census")
        if require_text and meta.get("evidenceTextKept") != "1":
            raise ValidationError("the text parent must be a frozen copy that keeps its text (study freeze --keep-text)")
        population = population_digest(connection)
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("reviews", "citations", "loci", "works")}
    stat = path.stat()
    pins = {key: meta.get(key) for key in ("instrumentId", "instrumentDigest", "studyBundleSha256", "complete",
                                           "evidenceTextKept", "entitledTextRedacted") if meta.get(key) is not None}
    return {"path": str(path), "sha256": scores_db.file_sha256(path), "bytes": stat.st_size,
            "mtimeNs": stat.st_mtime_ns, "meta": pins, "populationSha256": population, "counts": counts}


def load_protocol(value: Any) -> tuple[dict[str, Any], str]:
    """A follow-up protocol (a path or a mapping), checked against schemas/followup-protocol.schema.json."""
    import jsonschema
    if isinstance(value, (str, os.PathLike)):
        data = Path(value).read_bytes()
        if len(data) > 1024 * 1024:
            raise ValidationError("the protocol exceeds 1 MiB")
        try:
            protocol = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValidationError("the protocol is not UTF-8 JSON") from error
    else:
        protocol = json.loads(canonical(value))
    schema = json.loads(PROTOCOL_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(protocol), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        raise ValidationError(f"protocol: {'.'.join(map(str, first.absolute_path)) or '<root>'}: {first.message}")
    return protocol, hashlib.sha256(canonical(protocol).encode("utf-8")).hexdigest()


def store_artifact(connection: sqlite3.Connection, body: bytes, kind: str, media_type: str) -> str:
    """Store bytes once by their sha256 (zlib-compressed); returns the digest."""
    sha = hashlib.sha256(body).hexdigest()
    connection.execute("INSERT OR IGNORE INTO fu_artifacts VALUES(?,?,?,?,?,?,?)",
                       (sha, kind, media_type, len(body), "zlib", zlib.compress(body, 6), now()))
    return sha


def read_artifact(connection: sqlite3.Connection, sha256: str) -> bytes:
    row = connection.execute("SELECT body, codec FROM fu_artifacts WHERE sha256=?", (sha256,)).fetchone()
    if row is None:
        raise ValidationError(f"artifact is missing: {sha256}")
    body = zlib.decompress(row[0])
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ValidationError(f"artifact differs from its digest: {sha256}")
    return body


def register_dataset(connection: sqlite3.Connection, *, kind: str, name: str, declared_version: str, sha256: str,
                     rows: int | None = None, file_name: str | None = None,
                     detail: Mapping[str, Any] | None = None) -> str:
    """Register a dataset version once; registering the same id with other content is refused."""
    identifier = dataset_id(name, declared_version, sha256)
    existing = connection.execute("SELECT kind, sha256 FROM fu_datasets WHERE dataset_id=?", (identifier,)).fetchone()
    if existing:
        if tuple(existing) != (kind, sha256):
            raise ValidationError(f"dataset {identifier} is registered with other content")
        return identifier
    connection.execute("INSERT INTO fu_datasets VALUES(?,?,?,?,?,?,?,?,?)",
                       (identifier, kind, name, declared_version, sha256, rows, file_name,
                        canonical(dict(detail or {})), now()))
    return identifier


def require_freeze(connection: sqlite3.Connection) -> dict[str, Any]:
    """The latest protocol freeze; judging commands call this first and refuse a branch whose strata are not frozen."""
    row = connection.execute("SELECT * FROM fu_protocol_freeze ORDER BY frozen_at DESC LIMIT 1").fetchone()
    if row is None:
        raise ValidationError("the follow-up strata are not frozen: run followup strata --freeze before any judging")
    return dict(row)


class FollowupBranch:
    """An existing follow-up branch database."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValidationError(f"follow-up branch database is missing: {self.path}")
        with closing(sqlite3.connect(_uri(self.path, "?mode=ro"), uri=True)) as connection:
            problems = followup_schema.check(connection)
            if problems:
                raise ValidationError("not a usable follow-up branch: " + "; ".join(problems))
            self._meta = dict(connection.execute("SELECT key, value FROM fu_meta").fetchall())
        self.parent = Path(self._meta["parentPath"])
        self.parent_text = Path(self._meta["parentTextPath"]) if self._meta.get("parentTextPath") else None

    # ---------------------------------------------------------------- creation
    @classmethod
    def create(cls, path: Path, parent: Path, *, parent_text: Path | None = None, protocol: Any = None,
               allow_incomplete: bool = False, code_revision: str | None = None) -> dict[str, Any]:
        path = Path(path)
        if path.exists():
            raise ValidationError("the follow-up branch database already exists; branch into a new path")
        if not path.parent.is_dir():
            raise ValidationError(f"the branch directory is missing: {path.parent}")
        info = inspect_parent(Path(parent), allow_incomplete=allow_incomplete)
        text_info = None
        if parent_text is not None:
            text_info = inspect_parent(Path(parent_text), allow_incomplete=allow_incomplete, require_text=True)
            if text_info["populationSha256"] != info["populationSha256"]:
                raise ValidationError("the text parent holds another review population than the parent census")
            for key in ("instrumentDigest", "studyBundleSha256"):
                if info["meta"].get(key) != text_info["meta"].get(key):
                    raise ValidationError(f"the text parent differs from the parent census in {key}")
        loaded = load_protocol(protocol) if protocol is not None else None
        connection = sqlite3.connect(path)
        try:
            with connection:
                followup_schema.create(connection)
                rows = {"parentPath": info["path"], "parentSha256": info["sha256"], "parentBytes": str(info["bytes"]),
                        "parentMtimeNs": str(info["mtimeNs"]), "parentMeta": canonical(info["meta"]),
                        "parentPopulationSha256": info["populationSha256"], "createdAt": now(),
                        "codeRevision": code_revision or ""}
                if text_info:
                    rows.update(parentTextPath=text_info["path"], parentTextSha256=text_info["sha256"],
                                parentTextBytes=str(text_info["bytes"]), parentTextMtimeNs=str(text_info["mtimeNs"]))
                if loaded:
                    rows.update(protocol=canonical(loaded[0]), protocolSha256=loaded[1])
                connection.executemany("INSERT INTO fu_meta VALUES(?,?)", sorted(rows.items()))
                register_dataset(connection, kind="parent", name="census-scores",
                                 declared_version=info["meta"].get("studyBundleSha256", "unpinned")[:16],
                                 sha256=info["sha256"], file_name=Path(info["path"]).name,
                                 detail={"meta": info["meta"], "counts": info["counts"]})
                if text_info:
                    register_dataset(connection, kind="parent-text", name="census-scores-text",
                                     declared_version=text_info["meta"].get("studyBundleSha256", "unpinned")[:16],
                                     sha256=text_info["sha256"], file_name=Path(text_info["path"]).name)
                if loaded:
                    register_dataset(connection, kind="protocol", name="followup-protocol",
                                     declared_version=str(loaded[0].get("version", "1")), sha256=loaded[1])
        finally:
            connection.close()
        branch = cls(path)
        return {"database": str(branch.path), "parent": info, "parentText": text_info,
                "protocolSha256": loaded[1] if loaded else None, "schemaVersion": followup_schema.SCHEMA_VERSION}

    # ---------------------------------------------------------------- access
    def meta(self) -> dict[str, str]:
        with closing(self.connect(check_parent=False)) as connection:
            return dict(connection.execute("SELECT key, value FROM fu_meta").fetchall())

    def protocol(self) -> dict[str, Any] | None:
        value = self.meta().get("protocol")
        return json.loads(value) if value else None

    def _quick_check(self, path: Path, prefix: str) -> None:
        if not path.is_file():
            raise ValidationError(f"the {prefix} database is missing: {path}")
        stat = path.stat()
        if (str(stat.st_size), str(stat.st_mtime_ns)) != (self._meta.get(prefix + "Bytes"),
                                                          self._meta.get(prefix + "MtimeNs")):
            raise ValidationError(f"the {prefix} database changed since the branch was made (size or modification "
                                  "time); run followup verify")

    def connect(self, *, write: bool = False, check_parent: bool = True) -> sqlite3.Connection:
        """A connection to the branch with the parent ATTACHed read-only and immutable."""
        if check_parent:
            self._quick_check(self.parent, "parent")
            if self.parent_text is not None:
                self._quick_check(self.parent_text, "parentText")
        connection = sqlite3.connect(_uri(self.path, "" if write else "?mode=ro"), uri=True, timeout=60,
                                     check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if self.parent.is_file():
            connection.execute(f"ATTACH DATABASE ? AS {PARENT}", (_uri(self.parent, "?mode=ro&immutable=1"),))
        if self.parent_text is not None and self.parent_text.is_file():
            connection.execute(f"ATTACH DATABASE ? AS {PARENT_TEXT}",
                               (_uri(self.parent_text, "?mode=ro&immutable=1"),))
        return connection

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        """One writer at a time (``<branch>.lock``); commits on success, rolls back on error."""
        descriptor = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValidationError("another process writes this follow-up branch") from error
            connection = self.connect(write=True)
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
        finally:
            os.close(descriptor)

    def sentence(self, connection: sqlite3.Connection, journal: str, doi: str, label: str,
                 seq: int) -> tuple[str | None, str | None]:
        """The citing sentence of a parent locus and why it is unavailable (``text-redacted``, ``no-citing-text``).

        A frozen census keeps entitled sentences as ``sha256:<hex>``; the text parent supplies the text, accepted only
        when its sha256 equals the parent's value.
        """
        row = connection.execute(f"SELECT sentence FROM {PARENT}.loci WHERE journal=? AND doi=? AND label=? AND seq=?",
                                 (journal, doi, label, int(seq))).fetchone()
        if row is None or not (row[0] or "").strip():
            return None, "no-citing-text"
        value = row[0]
        if not value.startswith(REDACTED_PREFIX):
            return value, None
        if self.parent_text is None:
            return None, "text-redacted"
        text = connection.execute(f"SELECT sentence FROM {PARENT_TEXT}.loci WHERE journal=? AND doi=? AND label=? "
                                  "AND seq=?", (journal, doi, label, int(seq))).fetchone()
        if text is None or text[0].startswith(REDACTED_PREFIX):
            return None, "text-redacted"
        if hashlib.sha256(text[0].encode("utf-8")).hexdigest() != value[len(REDACTED_PREFIX):]:
            raise ValidationError(f"the text parent's sentence differs from the parent census: {journal} {doi} "
                                  f"{label} {seq}")
        return text[0], None

    # ---------------------------------------------------------------- verification
    REFERENCES = (
        ("frameLociMissingInParent", "SELECT COUNT(*) FROM fu_frame f WHERE NOT EXISTS (SELECT 1 FROM census.loci l "
         "WHERE l.journal=f.journal AND l.doi=f.doi AND l.label=f.label AND l.seq=f.seq)"),
        ("frameWorksMissingInParent", "SELECT COUNT(*) FROM fu_frame f WHERE f.work_key IS NOT NULL AND NOT EXISTS "
         "(SELECT 1 FROM census.works w WHERE w.work_key=f.work_key)"),
        ("statusRowsForUnknownWorks", "SELECT COUNT(*) FROM fu_work_status s WHERE NOT EXISTS "
         "(SELECT 1 FROM census.works w WHERE w.work_key=s.work_key)"),
        ("observationsForUnknownWorks", "SELECT COUNT(*) FROM fu_status_observations s WHERE NOT EXISTS "
         "(SELECT 1 FROM census.works w WHERE w.work_key=s.work_key)"),
        ("citedByTargetsOutsideParent", "SELECT COUNT(*) FROM fu_citedby_edges e WHERE NOT EXISTS "
         "(SELECT 1 FROM census.reviews r WHERE lower(r.doi)=e.target_doi)"),
        ("hotspotsForUnknownWorks", "SELECT COUNT(*) FROM fu_hotspots h WHERE NOT EXISTS "
         "(SELECT 1 FROM census.works w WHERE w.work_key=h.work_key)"),
    ) + tuple((f"{table}LociOutsideFrame", f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS "
               f"(SELECT 1 FROM fu_frame f WHERE f.locus_id=t.{column})")
              for table, column in (("fu_strata", "locus_id"), ("fu_cocited", "locus_id"),
                                    ("fu_cocited", "anchor_locus_id"), ("fu_support", "locus_id"),
                                    ("fu_function", "locus_id"), ("fu_dependency", "locus_id"),
                                    ("fu_numeric", "locus_id"), ("fu_secondgen_matches", "anchor_locus_id")))

    def verify(self, *, deep: bool = True) -> dict[str, Any]:
        """Parent digest unchanged, schema intact, every follow-up reference to the parent resolvable."""
        problems: list[str] = []
        parent: dict[str, Any] = {"path": str(self.parent), "recordedSha256": self._meta.get("parentSha256")}
        for path, prefix in ((self.parent, "parent"), (self.parent_text, "parentText")):
            if path is None:
                continue
            if not path.is_file():
                problems.append(f"the {prefix} database is missing: {path}")
                continue
            try:
                self._quick_check(path, prefix)
            except ValidationError as error:
                problems.append(str(error))
            if deep:
                actual = scores_db.file_sha256(path)
                parent[prefix + "Sha256"] = actual
                if actual != self._meta.get(prefix + "Sha256"):
                    problems.append(f"the {prefix} digest changed since the branch was made")
        references: dict[str, int] = {}
        status_missing = None
        if not problems or deep:
            with closing(self.connect(check_parent=False)) as connection:
                problems.extend(followup_schema.check(connection))
                if self.parent.is_file():
                    meta = dict(connection.execute(f"SELECT key, value FROM {PARENT}.meta").fetchall())
                    if meta.get("frozen") != "1":
                        problems.append("the parent census is not frozen")
                    if population_digest(connection, PARENT) != self._meta.get("parentPopulationSha256"):
                        problems.append("the parent census population differs from the one branched")
                    for name, sql in self.REFERENCES:
                        references[name] = connection.execute(sql).fetchone()[0]
                    status_missing = connection.execute(
                        f"SELECT COUNT(DISTINCT c.cited_work_key) FROM {PARENT}.citations c WHERE c.cited_work_key IS "
                        "NOT NULL AND NOT EXISTS (SELECT 1 FROM fu_work_status s WHERE s.work_key=c.cited_work_key)"
                    ).fetchone()[0]
        dangling = {name: count for name, count in references.items() if count}
        if dangling:
            problems.append("follow-up rows refer to rows the parent or the frame lacks: "
                            + ", ".join(f"{name}={count}" for name, count in sorted(dangling.items())))
        return {"ok": not problems, "problems": problems, "parent": parent, "references": references,
                "citedWorksWithoutStatusRow": status_missing, "deep": deep}

    def describe(self) -> dict[str, Any]:
        with closing(self.connect(check_parent=False)) as connection:
            counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in sorted(followup_schema.dictionary()) if table not in ("fu_dictionary", "fu_meta")}
            datasets = [dict(row) for row in connection.execute(
                "SELECT dataset_id, kind, name, declared_version, sha256, rows, file_name, registered_at FROM fu_datasets "
                "ORDER BY registered_at, dataset_id")]
            freezes = [dict(row) for row in connection.execute(
                "SELECT freeze_id, protocol_sha256, strata_sha256, status_snapshot, frozen_at FROM fu_protocol_freeze "
                "ORDER BY frozen_at")]
        meta = {key: value for key, value in self._meta.items() if key != "protocol"}
        return {"database": str(self.path), "meta": meta, "datasets": datasets, "protocolFreezes": freezes,
                "rows": counts}
