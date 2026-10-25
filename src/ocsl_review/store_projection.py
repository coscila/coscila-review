# SPDX-License-Identifier: Apache-2.0
"""Build and verify the label-free store projection ``literature-evidence@2``.

The projection keeps the evidence of a literature store and nothing that
interprets it: works and identifiers, source documents and sections with their
bytes, and provider records stay; every work observation (all ``legacy.*``
properties among them: scores, loci, attempts, summaries and bibliography rows),
assessment, model receipt, citation locus, bibliography entry, study selection,
task queue and fact is dropped. The only derived table is a neutral
retained-source index (DOI, capture digest, media type, route, capture date)
built from the legacy review manifests, which is how retained review captures
are found again.

The rules are deny-by-default and digested into the projection. The source
store is opened read-only and immutable, its database and manifest are checked
against each other, and its stat signature must not change during the build;
it is never modified. Rows are streamed in keyset pages and bytes are copied
blob by blob into packs (each verified against its SHA-256), so memory stays
bounded for large stores. The build fails closed: a removed-row query must
return no rows, foreign keys must resolve and no kept text or byte may contain
legacy verdict vocabulary. The build receipt (counts of what was dropped, the
source digests) is returned to the operator and is not stored in the projection.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time

from .corpus_library import (SCHEMA, Library, Workspace, digest_file, now, snapshot_manifest, write_json)
from .corpus_packs import INDEX as PACK_INDEX, TARGET_PACK_BYTES, ZLIB, PackStore, build_index, decode, encode, signature
from .corpus_store import canonical
from .errors import ReviewError, ValidationError
from .scholarly import normalize_doi


FORMAT = "literature-evidence@2"
CORPUS_ID = "literature-evidence"
CORPUS_V2 = Path(__file__).resolve().parents[2] / "schemas/corpus-v2.sql"
REVIEW_MANIFEST = "legacy.review_manifest"
MANIFEST_PARSE_LIMIT = 64 * 1024 * 1024  # larger (older) manifests are read by head and tail only
SHA256 = re.compile(r"[0-9a-f]{64}")

# Tables that may hold rows; every other table of the schema must stay empty.
KEPT_TABLES = ("corpora", "works", "work_identifiers", "source_artifacts", "retrievals", "source_sections",
               "corpus_documents", "retained_sources", "store_meta")
# Artifact kinds whose bytes may enter the projection; all other kinds are withheld.
ALWAYS_KINDS = ("source-document", "provider-document", "raw-source", "raw-provider-response",
                "raw-publisher-source", "acquisition-receipt", "identity-reconciliation", "title-resolution")
# Archived legacy evidence kinds, kept only when a kept row refers to them.
REFERENCED_KINDS = ("legacy-original-source", "legacy-provider-response", "legacy-evidence-shard")
# Documents whose provenance is a judged-sample selection (membership correlates with legacy verdicts).
DENIED_PROVIDERS = ("legacy-audit-excerpt",)
DENIED_DOCUMENT_KEYS = ("selectionManifestSha256", "selectionLine", "legacySourceClaim", "sourceDatabaseSha256")
WORK_METADATA_KEYS = (
    "abstract", "abstractSections", "authors", "container", "containerAliases", "creators", "date", "doi",
    "hasReferences", "hasTextMinedTerms", "id", "isOpenAccess", "isRetracted", "issns", "issue", "journal",
    "legacyInvalidDoi", "licenses", "noticeRoute", "notices", "pages", "pmcid", "pmid", "provider",
    "publicationTypes", "publicationYear", "publisher", "rawMetadata", "rawStatusFields", "relatedIdentifiers",
    "relation", "source", "subtype", "title", "titles", "type", "types", "updates", "url", "venue", "version",
    "volume", "xml", "year",
)
ROUTES = {"application/xml": "jats", "text/xml": "jats", "text/html": "html", "application/pdf": "pdf",
          "text/plain": "text"}
# Legacy verdict vocabulary that must not occur in any kept text or byte.
VOCABULARY = {
    "legacy-property": rb"legacy\.(?:citation_score|citation_locus|corrected_locus|corrected_bibliography"
                       rb"|census_attempt|review_summary|review_manifest|bibliography)\b",
    "three-class-label": rb"\bsure_(?:not_)?supported\b",
    "score-column": rb"\b(?:abs|ft)_(?:category|support_verdict|class|escalated|source|sure_supported|unsure"
                    rb"|sure_not_supported|not_supported_share)\b",
    "verdict-field": rb"\"(?:support_verdict|supportVerdict)\"\s*:",
    "compact-verdict": rb"\"c\"\s*:\s*\"[TFNU]\"\s*,\s*\"v\"\s*:\s*\"(?:direct|scope|partial|contra|notaddr|abstain)\"",
    "mapped-verdict": rb"\"(?:direct-support|scope-caveat|partial-indirect|not-addressed)\"",
}
PREFILTER = (b"legacy.", b"sure_", b"abs_", b"ft_", b"upport_verdict", b"upportVerdict", b'"c"', b"-support\"",
             b"scope-caveat", b"partial-indirect", b"not-addressed")
PATTERNS = {name: re.compile(pattern) for name, pattern in VOCABULARY.items()}

EXTRA_SCHEMA = """
CREATE TABLE retained_sources (
  doi TEXT NOT NULL CHECK(length(doi) > 3),
  capture_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
  media_type TEXT NOT NULL,
  route TEXT NOT NULL CHECK(route IN ('jats','html','pdf','text')),
  captured_at TEXT NOT NULL,
  PRIMARY KEY(doi, capture_sha256)
);
CREATE INDEX retained_sources_by_capture ON retained_sources(capture_sha256);
CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

RULES = {
    "format": FORMAT,
    "keptTables": list(KEPT_TABLES),
    "alwaysKinds": list(ALWAYS_KINDS),
    "referencedKinds": list(REFERENCED_KINDS),
    "deniedDocumentProviders": list(DENIED_PROVIDERS),
    "deniedDocumentMetadataKeys": list(DENIED_DOCUMENT_KEYS),
    "workMetadataKeys": list(WORK_METADATA_KEYS),
    "retainedIndex": {"from": REVIEW_MANIFEST, "routes": ROUTES,
                      "fields": ["doi", "capture_sha256", "media_type", "route", "captured_at"]},
    "vocabulary": {name: pattern.decode() for name, pattern in VOCABULARY.items()},
}
RULES_SHA256 = hashlib.sha256(canonical(RULES).encode()).hexdigest()


def vocabulary_hits(data: bytes) -> list[str]:
    if not any(token in data for token in PREFILTER):
        return []
    return sorted(name for name, pattern in PATTERNS.items() if pattern.search(data))


def _marks(values) -> str:
    return ",".join("?" for _ in values)


class SourceStore:
    """A sealed source store opened read-only; its database must match its manifest."""

    def __init__(self, path):
        raw = Path(path)
        if not raw.is_absolute():
            raw = raw.absolute()
        if any(p.is_symlink() for p in (raw, *raw.parents)):
            raise ValidationError("source store must not use symlinks")
        self.path = raw.resolve(strict=True)
        self.db, self.manifest = self.path / "corpus.sqlite3", self.path / "manifest.json"
        for item in (self.db, self.manifest):
            if item.is_symlink() or not item.is_file():
                raise ValidationError("source store must be a sealed snapshot (manifest.json and corpus.sqlite3)")
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(self.db) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValidationError("source store has an unexpected SQLite journal")
        head = _manifest_head(self.manifest)
        if head.get("format") != "ocsl-literature-snapshot" or head.get("version") not in (1, 2):
            raise ValidationError("unsupported source store manifest")
        self.version = head["version"]
        expected = head.get("database") or {}
        self.signature = (signature(self.db), signature(self.manifest))
        self.manifest_sha256 = digest_file(self.manifest)
        self.database_sha256 = digest_file(self.db)
        if expected.get("sha256") != self.database_sha256 or expected.get("byte_count") != self.db.stat().st_size:
            raise ValidationError("source store database differs from its manifest")
        self.library = Library(Workspace(self.path.parent, study_root="", dataset_roots=[]), self.path.name)
        self.uri = self.db.as_uri() + "?mode=ro&immutable=1"

    def unchanged(self) -> bool:
        return self.signature == (signature(self.db), signature(self.manifest))

    def describe(self) -> dict:
        return {"path": str(self.path), "manifestSha256": self.manifest_sha256,
                "databaseSha256": self.database_sha256, "formatVersion": self.version}


def _manifest_head(path: Path) -> dict:
    """format, version and database entry without loading a large manifest whole.

    Manifests are written with sorted keys, so "database" and "format" precede the
    (possibly huge) "sources" list of older snapshots.
    """
    if path.stat().st_size <= MANIFEST_PARSE_LIMIT:
        value = json.loads(path.read_text())
        return {key: value.get(key) for key in ("format", "version", "database")}
    with path.open("rb") as handle:
        head = handle.read(4 * 1024 * 1024).decode("utf-8", "replace")
    result = {}
    database = re.search(r'"database"\s*:\s*(\{[^{}]*\})', head)
    fmt = re.search(r'"format"\s*:\s*"([^"]+)"', head)
    result["database"] = json.loads(database.group(1)) if database else None
    result["format"] = fmt.group(1) if fmt else None
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 4096))
        tail = handle.read().decode("utf-8", "replace")
    version = re.search(r'"version"\s*:\s*(\d+)\s*\}\s*$', tail)
    result["version"] = int(version.group(1)) if version else None
    return result


def _work_metadata(text: str, report: dict) -> str:
    value = json.loads(text or "{}")
    kept = {key: item for key, item in value.items() if key in WORK_METADATA_KEYS}
    for key in value.keys() - kept.keys():
        report["droppedWorkMetadataKeys"][key] = report["droppedWorkMetadataKeys"].get(key, 0) + 1
    return canonical(kept)


def _scrub(value, withheld):
    """Remove denied keys and pointers to withheld artifacts, recursively."""
    if isinstance(value, dict):
        return {key: _scrub(item, withheld) for key, item in value.items()
                if key not in DENIED_DOCUMENT_KEYS and not (isinstance(item, str) and withheld(item))}
    if isinstance(value, list):
        return [_scrub(item, withheld) for item in value if not (isinstance(item, str) and withheld(item))]
    return value


def removed_rows(connection: sqlite3.Connection, schema: str = "main") -> dict[str, int]:
    """The gate: every count must be zero (rows a label-free projection must not contain)."""
    tables = [row[0] for row in connection.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    found = {}
    for table in tables:
        if table not in KEPT_TABLES:
            count = connection.execute(f'SELECT count(*) FROM {schema}."{table}"').fetchone()[0]
            if count:
                found[f"table:{table}"] = count
    allowed = ALWAYS_KINDS + REFERENCED_KINDS
    checks = {
        "artifact-kind": (f"SELECT count(*) FROM {schema}.source_artifacts WHERE artifact_kind NOT IN ({_marks(allowed)})",
                          allowed),
        "document-provider": (f"SELECT count(*) FROM {schema}.corpus_documents WHERE provider IN "
                              f"({_marks(DENIED_PROVIDERS)})", DENIED_PROVIDERS),
        "legacy-property": (f"SELECT count(*) FROM {schema}.work_observations WHERE property LIKE 'legacy.%'", ()),
    }
    for name, (sql, params) in checks.items():
        count = connection.execute(sql, params).fetchone()[0]
        if count:
            found[name] = count
    return found


def _text_hits(connection: sqlite3.Connection, schema: str = "main") -> dict[str, int]:
    hits: dict[str, int] = {}
    queries = (f"SELECT metadata_json FROM {schema}.works", f"SELECT metadata_json FROM {schema}.corpus_documents",
               f"SELECT coalesce(detail,'') || ' ' || request_url || ' ' || provider FROM {schema}.retrievals",
               f"SELECT extracted_text || ' ' || locator FROM {schema}.source_sections",
               f"SELECT doi || ' ' || route || ' ' || media_type FROM {schema}.retained_sources",
               f"SELECT key || ' ' || value FROM {schema}.store_meta",
               f"SELECT artifact_kind || ' ' || media_type FROM {schema}.source_artifacts")
    for query in queries:
        cursor = connection.execute(query)
        while rows := cursor.fetchmany(10000):
            for (text,) in rows:
                for name in vocabulary_hits(str(text).encode("utf-8", "surrogatepass")):
                    hits[name] = hits.get(name, 0) + 1
    return hits


def build_projection(source, output, *, workers: int = 8, level: int = 6, target_bytes: int = TARGET_PACK_BYTES,
                     page: int = 5000, progress=None) -> dict:
    started = time.monotonic()
    src = SourceStore(source)
    output = Path(output)
    output = output if output.is_absolute() else output.absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValidationError("projection output must not use symlinks")
    if output.exists():
        raise ValidationError("projection output already exists")
    if output.is_relative_to(src.path) or src.path.is_relative_to(output):
        raise ValidationError("projection output must be separate from the source store")
    if not output.parent.is_dir():
        raise ValidationError("projection output directory's parent does not exist")
    output.mkdir(mode=0o700)
    try:
        receipt = _build(src, output, workers=workers, level=level, target_bytes=target_bytes, page=page,
                         progress=progress)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    if not src.unchanged():
        shutil.rmtree(output, ignore_errors=True)
        raise ValidationError("source store changed during the projection; rebuild from a quiescent store")
    receipt["seconds"] = round(time.monotonic() - started, 3)
    return receipt


def _note(progress, **event):
    if progress:
        progress(event)


def _build(src: SourceStore, output: Path, *, workers, level, target_bytes, page, progress) -> dict:
    (output / "sources").mkdir()
    packs = PackStore.create(output / "packs", target_bytes=target_bytes, level=level)
    db = output / "corpus.sqlite3"
    connection = sqlite3.connect(db)
    report = {"kept": {}, "dropped": {}, "droppedObservationProperties": {}, "withheldArtifactKinds": {},
              "droppedWorkMetadataKeys": {}, "retainedIndex": {"rows": 0, "skipped": 0}}
    try:
        connection.executescript(SCHEMA.read_text())
        connection.executescript(CORPUS_V2.read_text())
        connection.executescript(EXTRA_SCHEMA)
        for pragma in ("journal_mode=OFF", "synchronous=OFF", "foreign_keys=OFF", "temp_store=FILE",
                       "cache_size=-262144"):
            connection.execute(f"PRAGMA {pragma}")
        connection.execute("ATTACH DATABASE ? AS src", (src.uri,))
        if connection.execute("PRAGMA src.user_version").fetchone()[0] not in (2, 3):
            # Version 3 only adds derived progress counters, which the projection drops.
            raise ValidationError("source store must use corpus schema version 2 or 3")
        for table in KEPT_TABLES[1:7] + ("work_observations",):
            mine = [r[1] for r in connection.execute(f"PRAGMA main.table_info({table})")]
            theirs = [r[1] for r in connection.execute(f"PRAGMA src.table_info({table})")]
            if mine != theirs:
                raise ValidationError(f"unsupported source store schema: {table}")
        _account(connection, report)
        _retained_index(connection, report, page)
        _note(progress, phase="retained-index", rows=report["retainedIndex"]["rows"])
        _keep_set(connection)
        _rows(connection, report, page)
        _note(progress, phase="rows", **report["kept"])
        hits = _copy_blobs(connection, src, packs, workers=workers, page=page, progress=progress)
        if hits:
            raise ValidationError("kept artifacts contain legacy verdict vocabulary: "
                                  + ", ".join(f"{k}={v}" for k, v in sorted(hits.items())))
        connection.execute("INSERT INTO main.source_artifacts SELECT a.* FROM src.source_artifacts a "
                           "JOIN temp.keep k ON k.sha256=a.sha256 ORDER BY a.sha256")
        connection.execute("INSERT INTO main.corpora VALUES (?,?,?)",
                           (CORPUS_ID, "Label-free literature evidence (" + FORMAT + ")", now()))
        connection.executemany("INSERT INTO main.store_meta VALUES (?,?)",
                               [("format", FORMAT), ("rules_sha256", RULES_SHA256), ("built_at", now())])
        connection.commit()
        report["kept"]["source_artifacts"] = connection.execute("SELECT count(*) FROM main.source_artifacts").fetchone()[0]
        removed = removed_rows(connection)
        if removed:
            raise ValidationError(f"projection gate failed: {removed}")
        problems = connection.execute("PRAGMA main.foreign_key_check").fetchmany(5)
        if problems:
            raise ValidationError(f"projection references do not resolve: {problems}")
        text = _text_hits(connection)
        if text:
            raise ValidationError(f"kept text contains legacy verdict vocabulary: {text}")
        connection.execute("DETACH DATABASE src")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.commit()
    finally:
        connection.close()
    manifest = _seal(output, packs)
    projection = Library(Workspace(output.parent, study_root="", dataset_roots=[]), output.name)
    verified = verify_projection(output, level="standard")
    return {"format": FORMAT, "output": str(output), "rulesSha256": RULES_SHA256,
            "merkleRoot": manifest["merkle"]["root"], "manifestSha256": verified["manifestSha256"],
            "files": len(manifest["files"]) + 1, "source": src.describe(), "gate": verified["gate"],
            "counts": projection.verify(level="light")["counts"], **report}


def _account(connection, report):
    """Operator-facing counts of everything the projection drops (never stored in it)."""
    for table in [row[0] for row in connection.execute(
            "SELECT name FROM src.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]:
        if table not in KEPT_TABLES:
            count = connection.execute(f'SELECT count(*) FROM src."{table}"').fetchone()[0]
            if count:
                report["dropped"][table] = count
    report["droppedObservationProperties"] = dict(connection.execute(
        "SELECT property, count(*) FROM src.work_observations GROUP BY property ORDER BY property").fetchall())


def _retained_index(connection, report, page):
    last = ""
    while True:
        rows = connection.execute(
            "SELECT o.observation_id, o.work_id, o.value_json, w.metadata_json FROM src.work_observations o "
            "LEFT JOIN src.works w USING(work_id) WHERE o.property=? AND o.observation_id>? "
            "ORDER BY o.observation_id LIMIT ?", (REVIEW_MANIFEST, last, page)).fetchall()
        if not rows:
            return
        last = rows[-1][0]
        records = []
        for _, _, value_json, metadata_json in rows:
            value = json.loads(value_json)
            row = value.get("row") if isinstance(value.get("row"), dict) else {}
            doi = row.get("doi") or json.loads(metadata_json or "{}").get("doi")
            try:
                doi = normalize_doi(str(doi)) if doi else None
            except ReviewError:
                doi = None
            for item in value.get("sources") or []:
                digest, media = str(item.get("sha256") or ""), item.get("mediaType")
                if not doi or item.get("state") != "retained" or not SHA256.fullmatch(digest) or media not in ROUTES:
                    report["retainedIndex"]["skipped"] += 1
                    continue
                artifact = connection.execute("SELECT artifact_kind, created_at FROM src.source_artifacts WHERE sha256=?",
                                              (digest,)).fetchone()
                if artifact is None or artifact[0] not in ALWAYS_KINDS + REFERENCED_KINDS:
                    report["retainedIndex"]["skipped"] += 1
                    continue
                records.append((doi, digest, media, ROUTES[media], artifact[1]))
        connection.executemany("INSERT OR IGNORE INTO main.retained_sources VALUES (?,?,?,?,?)", records)
        connection.commit()
        report["retainedIndex"]["rows"] = connection.execute("SELECT count(*) FROM main.retained_sources").fetchone()[0]


def _keep_set(connection):
    """Artifacts whose bytes the projection keeps (temp.keep); everything else is withheld."""
    referenced = REFERENCED_KINDS
    connection.executescript("DROP TABLE IF EXISTS temp.keep; CREATE TEMP TABLE keep(sha256 TEXT PRIMARY KEY) WITHOUT ROWID;")
    connection.execute(f"INSERT OR IGNORE INTO temp.keep SELECT sha256 FROM src.source_artifacts "
                       f"WHERE artifact_kind IN ({_marks(ALWAYS_KINDS)})", ALWAYS_KINDS)
    statements = (
        f"SELECT d.artifact_sha256 FROM src.corpus_documents d JOIN src.source_artifacts a ON a.sha256=d.artifact_sha256 "
        f"WHERE d.provider NOT IN ({_marks(DENIED_PROVIDERS)}) AND a.artifact_kind IN ({_marks(referenced)})",
        f"SELECT r.capture_sha256 FROM main.retained_sources r",
        f"SELECT i.verified_by_sha256 FROM src.work_identifiers i JOIN src.source_artifacts a ON a.sha256=i.verified_by_sha256 "
        f"WHERE a.artifact_kind IN ({_marks(referenced)})",
        f"SELECT r.artifact_sha256 FROM src.retrievals r JOIN src.source_artifacts a ON a.sha256=r.artifact_sha256 "
        f"WHERE a.artifact_kind IN ({_marks(referenced)})",
        f"SELECT s.artifact_sha256 FROM src.source_sections s JOIN src.source_artifacts a ON a.sha256=s.artifact_sha256 "
        f"WHERE a.artifact_kind IN ({_marks(referenced)}) AND NOT EXISTS (SELECT 1 FROM src.corpus_documents d "
        f"WHERE d.work_id=s.work_id AND d.artifact_sha256=s.artifact_sha256)",
    )
    params = ((*DENIED_PROVIDERS, *referenced), (), referenced, referenced, referenced)
    for sql, values in zip(statements, params):
        connection.execute("INSERT OR IGNORE INTO temp.keep " + sql, values)
    connection.commit()


def _rows(connection, report, page):
    def withheld(value):
        return bool(SHA256.fullmatch(value)) and connection.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM temp.keep WHERE sha256=?) AND "
            "EXISTS (SELECT 1 FROM src.source_artifacts WHERE sha256=?)", (value, value)).fetchone()[0] == 1

    last = ""
    while rows := connection.execute("SELECT work_id,title,publication_year,metadata_json FROM src.works "
                                     "WHERE work_id>? ORDER BY work_id LIMIT ?", (last, page)).fetchall():
        last = rows[-1][0]
        connection.executemany("INSERT INTO main.works VALUES (?,?,?,?)",
                               [(w, t, y, _work_metadata(m, report)) for w, t, y, m in rows])
        connection.commit()
    connection.execute("INSERT INTO main.work_identifiers SELECT i.* FROM src.work_identifiers i "
                       "WHERE i.verified_by_sha256 IN (SELECT sha256 FROM temp.keep) ORDER BY i.scheme, i.value")
    connection.execute("INSERT INTO main.retrievals SELECT r.* FROM src.retrievals r WHERE r.artifact_sha256 IS NULL "
                       "OR r.artifact_sha256 IN (SELECT sha256 FROM temp.keep) ORDER BY r.retrieval_id")
    connection.commit()
    last = ""
    columns = "document_id,work_id,artifact_sha256,evidence_tier,source_url,provider,complete,metadata_json,created_at"
    while rows := connection.execute(f"SELECT {columns} FROM src.corpus_documents WHERE document_id>? "
                                     f"ORDER BY document_id LIMIT ?", (last, page)).fetchall():
        last = rows[-1][0]
        kept = []
        for row in rows:
            if row[5] in DENIED_PROVIDERS or not connection.execute(
                    "SELECT 1 FROM temp.keep WHERE sha256=?", (row[2],)).fetchone():
                continue
            kept.append((*row[:7], canonical(_scrub(json.loads(row[7]), withheld)), row[8]))
        connection.executemany(f"INSERT INTO main.corpus_documents({columns}) VALUES (?,?,?,?,?,?,?,?,?)", kept)
        connection.commit()
    connection.execute(
        "INSERT INTO main.source_sections SELECT s.* FROM src.source_sections s "
        "WHERE s.artifact_sha256 IN (SELECT sha256 FROM temp.keep) AND ("
        "EXISTS (SELECT 1 FROM main.corpus_documents d WHERE d.work_id=s.work_id AND d.artifact_sha256=s.artifact_sha256) "
        "OR NOT EXISTS (SELECT 1 FROM src.corpus_documents d WHERE d.work_id=s.work_id AND d.artifact_sha256=s.artifact_sha256)) "
        "ORDER BY s.rowid")
    connection.commit()
    for table in ("works", "work_identifiers", "retrievals", "corpus_documents", "source_sections", "retained_sources"):
        report["kept"][table] = connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0]
        total = connection.execute(f"SELECT count(*) FROM src.{table}").fetchone()[0] if table != "retained_sources" else None
        if total is not None and total != report["kept"][table]:
            report["dropped"][table] = total - report["kept"][table]
    report["withheldArtifactKinds"] = dict(connection.execute(
        "SELECT artifact_kind, count(*) FROM src.source_artifacts a WHERE NOT EXISTS "
        "(SELECT 1 FROM temp.keep k WHERE k.sha256=a.sha256) GROUP BY artifact_kind ORDER BY artifact_kind").fetchall())


class _Reader:
    """Thread-safe reads of verified source bytes from source packs or loose files."""

    def __init__(self, src: SourceStore, level: int):
        self.src, self.level = src, level
        self.descriptors: dict[str, int] = {}
        self.lock = threading.Lock()

    def _descriptor(self, name: str) -> int:
        with self.lock:
            descriptor = self.descriptors.get(name)
            if descriptor is None:
                descriptor = self.descriptors[name] = os.open(self.src.library.packs.pack_path(name),
                                                              os.O_RDONLY | os.O_NOFOLLOW)
            return descriptor

    def close(self):
        with self.lock:
            for descriptor in self.descriptors.values():
                os.close(descriptor)
            self.descriptors.clear()

    def load(self, row):
        digest, byte_count, pack, offset, stored_length, raw_length, codec = row
        if pack is not None:
            stored = os.pread(self._descriptor(pack), stored_length, offset)
            if len(stored) != stored_length:
                raise ValidationError(f"source pack is truncated: {pack}")
            body = decode(digest, codec, stored, raw_length)
            if codec != ZLIB:
                codec, stored = encode(body, self.level)
        else:
            body = self.src.library.read_blob(digest, byte_count)
            codec, stored = encode(body, self.level)
        if len(body) != byte_count:
            raise ValidationError(f"source artifact size differs from its registry row: {digest}")
        return digest, len(body), codec, stored, vocabulary_hits(body)


def _copy_blobs(connection, src, packs, *, workers, page, progress) -> dict:
    """Stream kept bytes into packs; workers read, verify, scan and compress, one writer appends.

    Source pack locations come from one join per page; zlib streams of a packed
    source are reused after verification instead of being compressed again.
    """
    hits: dict[str, int] = {}
    budget = 256 * 1024 * 1024
    source_packs = src.library.packs
    if source_packs is not None:
        connection.execute("ATTACH DATABASE ? AS srcidx", (source_packs.index_path.as_uri() + "?mode=ro&immutable=1",))
        located = ("p.name, b.offset, b.stored_length, b.raw_length, b.codec FROM src.source_artifacts a "
                   "JOIN temp.keep k ON k.sha256=a.sha256 LEFT JOIN srcidx.blobs b ON b.sha256=a.sha256 "
                   "LEFT JOIN srcidx.packs p ON p.id=b.pack_id")
    else:
        located = "NULL, NULL, NULL, NULL, NULL FROM src.source_artifacts a JOIN temp.keep k ON k.sha256=a.sha256"
    reader = _Reader(src, packs.level)
    last, copied = "", 0
    try:
        with packs.batch(), concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            while rows := connection.execute(f"SELECT a.sha256, a.byte_count, {located} WHERE a.sha256>? "
                                             f"ORDER BY a.sha256 LIMIT ?", (last, page)).fetchall():
                last = rows[-1][0]
                batch, size = [], 0
                for row in rows:
                    batch.append(row)
                    size += row[1]
                    if size >= budget:
                        copied += _flush(pool, reader, packs, batch, hits)
                        batch, size = [], 0
                copied += _flush(pool, reader, packs, batch, hits)
                _note(progress, phase="blobs", copied=copied)
    finally:
        reader.close()
        if source_packs is not None:
            connection.execute("DETACH DATABASE srcidx")
    return hits


def _flush(pool, reader, packs, batch, hits) -> int:
    for digest, raw_length, codec, stored, found in pool.map(reader.load, batch):
        for name in found:
            hits[name] = hits.get(name, 0) + 1
        packs.put_encoded(digest, raw_length, codec, stored, verified=True, new=True)
    return len(batch)


def _seal(output: Path, packs: PackStore) -> dict:
    packs.seal_open()
    sealed = packs.packs("sealed")
    closed = output / "packs" / "index.closed.sqlite3"
    build_index(closed, sealed, [packs.index_path])
    for name in (PACK_INDEX, PACK_INDEX + "-wal", PACK_INDEX + "-shm", ".lock"):
        (output / "packs" / name).unlink(missing_ok=True)
    os.replace(closed, output / "packs" / PACK_INDEX)
    db, index = output / "corpus.sqlite3", output / "packs" / PACK_INDEX
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        counts = {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()}
        artifacts = connection.execute("SELECT count(*), coalesce(sum(byte_count),0) FROM source_artifacts").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    files = [{"path": "corpus.sqlite3", "sha256": digest_file(db), "byte_count": db.stat().st_size},
             {"path": "packs/" + PACK_INDEX, "sha256": digest_file(index), "byte_count": index.stat().st_size}]
    files += [{"path": "packs/" + p["name"], "sha256": p["sha256"], "byte_count": p["byte_count"]} for p in sealed]
    manifest = snapshot_manifest(files, version=version, counts=counts, analyses=[],
                                 artifacts={"count": artifacts[0], "bytes": artifacts[1]},
                                 extra={"store": {"id": FORMAT, "rules_sha256": RULES_SHA256}})
    write_json(output / "manifest.json", manifest)
    for path in (db, index, output / "manifest.json"):
        path.chmod(0o444)
    return manifest


def verify_projection(path, *, level: str = "standard") -> dict:
    """Snapshot integrity plus the label-free gate: zero removed rows, zero vocabulary hits."""
    path = Path(path).absolute()
    try:
        store = Library(Workspace(path.parent, study_root="", dataset_roots=[]), path.name)
        result = store.verify(require_snapshot=True, level=level)
    except ValueError as error:
        raise ValidationError(f"projection verification failed: {error}") from None
    manifest = store.manifest()
    declared = manifest.get("store") or {}
    if declared.get("id") != FORMAT:
        raise ValidationError("store is not a literature-evidence@2 projection")
    if declared.get("rules_sha256") != RULES_SHA256:
        raise ValidationError("projection was built with different rules")
    connection = sqlite3.connect(store.db.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        meta = dict(connection.execute("SELECT key, value FROM store_meta").fetchall())
        if meta.get("format") != FORMAT or meta.get("rules_sha256") != RULES_SHA256:
            raise ValidationError("projection metadata differs from its manifest")
        removed = removed_rows(connection)
        text = _text_hits(connection) if level != "light" else {}
    finally:
        connection.close()
    blobs = {}
    if level == "deep":
        with store.connect() as connection:
            for (digest, byte_count) in connection.execute("SELECT sha256, byte_count FROM source_artifacts"):
                for name in vocabulary_hits(store.read_blob(digest, byte_count)):
                    blobs[name] = blobs.get(name, 0) + 1
    gate = {"removedRows": sum(removed.values()), "removed": removed, "vocabularyHits": sum(text.values()) + sum(blobs.values()),
            "textHits": text, "blobHits": blobs, "blobsScanned": level == "deep"}
    if gate["removedRows"] or gate["vocabularyHits"]:
        raise ValidationError(f"projection gate failed: {gate}")
    return {"verified": True, "format": FORMAT, "level": level, "merkleRoot": result["merkle_root"],
            "manifestSha256": result["manifest_sha256"], "rulesSha256": RULES_SHA256, "gate": gate,
            "counts": result["counts"]}
