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


STUDY_BUNDLE_FORMAT = "ocsl-study-bundle-v1"


def load_study_bundle(value: Any) -> tuple[dict[str, Any] | None, str]:
    """(bundle, sha256) of ``--study-bundle``: an ``ocsl-study-bundle-v1`` file whose sha256 is checked against its
    content, or a bare sha256 (bundle None) when the bundle file is kept elsewhere."""
    import re
    from .instruments import canonical as bundle_canonical, sha256_bytes
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", text):
        return None, text
    path = Path(text)
    if not text or not path.is_file():
        raise ValidationError("--study-bundle takes a study bundle file (JSON) or the bundle's sha256")
    data = path.read_bytes()
    if len(data) > 1024 * 1024:
        raise ValidationError("the study bundle file exceeds 1 MiB")
    try:
        bundle = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValidationError("the study bundle file is not UTF-8 JSON") from error
    if not isinstance(bundle, dict) or bundle.get("format") != STUDY_BUNDLE_FORMAT:
        raise ValidationError(f"the study bundle file is no {STUDY_BUNDLE_FORMAT} record")
    bundle.pop("artifactSha256", None)
    record = {key: item for key, item in bundle.items() if key != "sha256"}
    digest = sha256_bytes(bundle_canonical(record).encode("utf-8"))
    if bundle.get("sha256") != digest:
        raise ValidationError("the study bundle's sha256 differs from its content")
    return bundle, digest


def protocol_instruments(protocol: Mapping[str, Any] | None) -> list[str]:
    """The registry ids of the instruments a protocol names (support, function and, when declared, numeric, match)."""
    named = (protocol or {}).get("instruments") or {}
    return [named[role] for role in ("support", "function", "numeric", "match") if named.get(role)]


def instrument_pins(connection: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """Instrument digests the branch pins: from the study bundle's content and from the protocol's
    ``instruments.digests`` (keyed by registry id)."""
    meta = dict(connection.execute("SELECT key, value FROM fu_meta WHERE key IN ('studyBundle', 'protocol')"
                                   ).fetchall())
    pins: dict[str, dict[str, str]] = {}
    if meta.get("studyBundle"):
        for instrument_id, digest in (json.loads(meta["studyBundle"]).get("instruments") or {}).items():
            pins.setdefault(instrument_id, {})["studyBundle"] = digest
    if meta.get("protocol"):
        declared = (json.loads(meta["protocol"]).get("instruments") or {}).get("digests") or {}
        for instrument_id, digest in declared.items():
            pins.setdefault(instrument_id, {})["protocol"] = digest
    return pins


def check_instrument(connection: sqlite3.Connection, instrument: Any) -> str | None:
    """Before any call: the instrument's registry digest must equal every pin of it (the study bundle's content and the
    protocol's ``instruments.digests``), and a pinned bundle file must pin it at all; returns the pinned bundle's
    sha256 (recorded in every receipt), or None."""
    meta = dict(connection.execute("SELECT key, value FROM fu_meta WHERE key IN ('studyBundleSha256', 'studyBundle')"
                                   ).fetchall())
    pins = instrument_pins(connection).get(instrument.id, {})
    if meta.get("studyBundle") and "studyBundle" not in pins:
        raise ValidationError(f"the pinned study bundle pins no digest for {instrument.id}; pin a bundle that covers "
                              "every instrument the follow-up runs")
    for source, digest in sorted(pins.items()):
        if digest != instrument.digest:
            where = "the pinned study bundle" if source == "studyBundle" else "the protocol (instruments.digests)"
            raise ValidationError(f"{where} pins {instrument.id} as {digest}; the installed instrument's registry "
                                  f"digest is {instrument.digest}: a changed builder, validator or bundle never runs "
                                  "silently under a new digest")
    return meta.get("studyBundleSha256")


def pin_study_bundle(connection: sqlite3.Connection, value: Any) -> dict[str, Any]:
    """Pin the study bundle in fu_meta before the first judge call; its sha256 goes into every call receipt.

    A bundle file must be ``ocsl-study-bundle-v1`` with a matching sha256, carry the branch protocol's sha256 and pin
    every instrument the protocol names at its registry digest. A branch pins one bundle: the same bundle again is a
    no-op, another one is refused, and a pin after the first call is refused.
    """
    from . import instruments
    bundle, digest = load_study_bundle(value)
    meta = dict(connection.execute("SELECT key, value FROM fu_meta").fetchall())
    pinned = meta.get("studyBundleSha256")
    if pinned:
        if pinned != digest:
            raise ValidationError(f"the branch already pins the study bundle {pinned}; a branch pins one bundle")
        return {"studyBundleSha256": digest, "contentVerified": bool(meta.get("studyBundle")), "alreadyPinned": True}
    if connection.execute("SELECT 1 FROM fu_calls LIMIT 1").fetchone():
        raise ValidationError("the study bundle is pinned before the first call: this branch already holds call "
                              "receipts without it")
    values = {"studyBundleSha256": digest}
    if bundle is not None:
        protocol = json.loads(meta["protocol"]) if meta.get("protocol") else None
        if protocol is not None and bundle.get("protocolSha256") != meta.get("protocolSha256"):
            raise ValidationError("the study bundle names another protocol than the one the branch registered")
        pinned_instruments = bundle.get("instruments") or {}
        for instrument_id in protocol_instruments(protocol):
            registry = instruments.load(instrument_id).digest
            if pinned_instruments.get(instrument_id) != registry:
                raise ValidationError(f"the study bundle pins {instrument_id} as "
                                      f"{pinned_instruments.get(instrument_id) or 'nothing'}; the installed instrument's "
                                      f"registry digest is {registry}")
        values["studyBundle"] = canonical(bundle)
    connection.executemany("INSERT OR REPLACE INTO fu_meta VALUES(?,?)", sorted(values.items()))
    return {"studyBundleSha256": digest, "contentVerified": bundle is not None, "alreadyPinned": False}


def require_freeze(connection: sqlite3.Connection) -> dict[str, Any]:
    """The latest protocol freeze; judging commands call this first and refuse a branch whose strata are not frozen."""
    row = connection.execute("SELECT * FROM fu_protocol_freeze ORDER BY frozen_at DESC LIMIT 1").fetchone()
    if row is None:
        raise ValidationError("the follow-up strata are not frozen: run followup strata --freeze before any judging")
    return dict(row)


def _empty_database(path: Path) -> bool:
    """Whether ``path`` is an SQLite database without any schema object."""
    try:
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    except sqlite3.DatabaseError:
        return False


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
               allow_incomplete: bool = False, code_revision: str | None = None,
               study_bundle: Any = None) -> dict[str, Any]:
        path = Path(path)
        if path.exists() and _empty_database(path):
            # What an init killed before its commit leaves: an SQLite file without any schema object (its journal
            # rolled back when it was opened). It holds nothing, so the branch is made in its place.
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        if path.exists():
            made = cls._made_before(path, parent, parent_text=parent_text, protocol=protocol,
                                    allow_incomplete=allow_incomplete, study_bundle=study_bundle)
            if made is not None:
                return made
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
                if allow_incomplete and info["meta"].get("complete") != "1":
                    connection.execute("INSERT OR REPLACE INTO fu_meta VALUES('parentIncompleteAllowed', '1')")
                pinned = pin_study_bundle(connection, study_bundle) if study_bundle is not None else None
        except BaseException:
            connection.close()
            path.unlink(missing_ok=True)
            raise
        connection.close()
        branch = cls(path)
        return {"database": str(branch.path), "parent": info, "parentText": text_info,
                "protocolSha256": loaded[1] if loaded else None, "schemaVersion": followup_schema.SCHEMA_VERSION,
                "studyBundle": pinned}

    @classmethod
    def _made_before(cls, path: Path, parent: Path, *, parent_text: Path | None, protocol: Any,
                     allow_incomplete: bool, study_bundle: Any) -> dict[str, Any] | None:
        """An init asked again with the inputs that made the branch at ``path`` (the same command run again after its
        result was lost): the same parent and text parent (path and digest), protocol digest, incomplete-parent
        allowance and study bundle pin. Returns the init result marked ``alreadyMade``, or None when any input
        differs."""
        try:
            branch = cls(path)
        except ValidationError:
            return None
        meta = branch.meta()
        info = inspect_parent(Path(parent), allow_incomplete=allow_incomplete)
        if (meta.get("parentPath"), meta.get("parentSha256")) != (info["path"], info["sha256"]):
            return None
        text_info = None
        if parent_text is not None:
            text_info = inspect_parent(Path(parent_text), allow_incomplete=allow_incomplete, require_text=True)
            if (meta.get("parentTextPath"), meta.get("parentTextSha256")) != (text_info["path"], text_info["sha256"]):
                return None
        elif meta.get("parentTextPath"):
            return None
        loaded = load_protocol(protocol) if protocol is not None else None
        if (loaded[1] if loaded else None) != (meta.get("protocolSha256") or None):
            return None
        if bool(meta.get("parentIncompleteAllowed")) != bool(allow_incomplete and info["meta"].get("complete") != "1"):
            return None
        bundle = load_study_bundle(study_bundle)[1] if study_bundle is not None else None
        if bundle != (meta.get("studyBundleSha256") or None):
            return None
        pinned = ({"studyBundleSha256": bundle, "contentVerified": bool(meta.get("studyBundle")), "alreadyPinned": True}
                  if bundle else None)
        return {"database": str(branch.path), "parent": info, "parentText": text_info,
                "protocolSha256": loaded[1] if loaded else None, "schemaVersion": followup_schema.SCHEMA_VERSION,
                "studyBundle": pinned, "alreadyMade": True}

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

    @property
    def text_mounted(self) -> bool:
        """The private text parent this branch was made with is present (it needs to be present only for the commands
        that read citing sentences, typically on-premises judging)."""
        return self.parent_text is not None and self.parent_text.is_file()

    def require_text(self) -> None:
        """Refuse a command that reads citing sentences when the branch was made with a text parent that is not
        mounted: entitled sentences would otherwise silently become text-redacted."""
        if self.parent_text is not None and not self.parent_text.is_file():
            raise ValidationError(f"this command reads citing sentences, and the branch's text parent is not mounted: "
                                  f"{self.parent_text} (the private keep-text copy must be at that path for this "
                                  "command)")

    def connect(self, *, write: bool = False, check_parent: bool = True) -> sqlite3.Connection:
        """A connection to the branch with the parent ATTACHed read-only and immutable, and the text parent when it is
        mounted (a command run without it never reads an entitled sentence; ``require_text`` guards the commands that
        do)."""
        if check_parent:
            self._quick_check(self.parent, "parent")
            if self.text_mounted:
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
        self.require_text()
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
        notes: list[str] = []
        parent: dict[str, Any] = {"path": str(self.parent), "recordedSha256": self._meta.get("parentSha256")}
        if self.parent_text is not None:
            parent["parentTextMounted"] = self.text_mounted
        for path, prefix in ((self.parent, "parent"), (self.parent_text, "parentText")):
            if path is None:
                continue
            if prefix == "parentText" and not path.is_file():
                # The private text copy needs to be present only for the commands that read sentences; its digest is
                # checked where it is.
                notes.append(f"the text parent is not mounted here ({path}); its digest is checked where it is mounted")
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
        return {"ok": not problems, "problems": problems, "notes": notes, "parent": parent, "references": references,
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
