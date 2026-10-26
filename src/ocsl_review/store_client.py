# SPDX-License-Identifier: Apache-2.0
"""Read-only access to sealed literature stores with study-first lookups.

A store is a sealed snapshot directory (closed database, packs and a manifest
with a Merkle root), for example the label-free projection
``literature-evidence@2`` mounted read-only. Stores are ATTACHed with
``mode=ro&immutable=1`` and pinned by their Merkle root in the study's
bundle.json. The study library stays the system of record: lookups ask the
study first and the stores second, and evidence taken from a store is copied
into the study (rows with their identifiers unchanged, bytes re-verified), with
an adoption receipt naming the store version. A frozen study therefore never
depends on a mount, and a store is only ever a byte cache: callers still re-check
provider records before they rely on adopted evidence.

Census inputs read byte caches without copying a library: ``EvidenceStore`` opens
a pinned ``literature-evidence@2`` projection and ``ProviderCache`` a provider
response cache, each ATTACHed ``mode=ro&immutable=1`` behind a ``GuardedReader``
whose SQLite authorizer refuses every table outside the cache's allowlist (for
the projection, its kept tables), so no verdict, score, locus, attempt, summary
or bibliography property can be read even from a store that carried them.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable, Mapping

from .corpus_library import Library, Workspace, digest_file, now
from .corpus_store import canonical, identity
from .errors import ValidationError


SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_CLOSURE = 256


class Store:
    """One sealed store version, verified when opened."""

    def __init__(self, path, *, expected_root: str | None = None, level: str = "light"):
        raw = Path(path)
        if not raw.is_absolute():
            raise ValidationError("store path must be absolute")
        if any(p.is_symlink() for p in (raw, *raw.parents)):
            raise ValidationError("store path must not use symlinks")
        self.path = raw.resolve(strict=True)
        if not (self.path / "manifest.json").is_file():
            raise ValidationError("store is not a sealed snapshot")
        try:
            self.library = Library(Workspace(self.path.parent, study_root="", dataset_roots=[]), self.path.name)
            result = self.library.verify(require_snapshot=True, level=level)
        except ValueError as error:
            raise ValidationError(f"store verification failed: {error}") from None
        if result.get("format_version") != 2:
            raise ValidationError("stores must use the packed snapshot format")
        self.merkle_root = result["merkle_root"]
        self.manifest_sha256 = result["manifest_sha256"]
        if expected_root is not None and expected_root != self.merkle_root:
            raise ValidationError("store Merkle root differs from the pinned store version")
        self.manifest = self.library.manifest()
        self.store_id = (self.manifest.get("store") or {}).get("id") or "snapshot"
        self.uri = self.library.db.as_uri() + "?mode=ro&immutable=1"

    def describe(self) -> dict:
        return {"path": str(self.path), "id": self.store_id, "merkleRoot": self.merkle_root,
                "manifestSha256": self.manifest_sha256}

    def attach(self, connection: sqlite3.Connection, alias: str) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", alias):
            raise ValidationError("invalid store alias")
        connection.execute(f"ATTACH DATABASE ? AS {alias}", (self.uri,))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.uri, uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def has_table(self, name: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def blob(self, sha256: str, byte_count: int | None = None) -> bytes:
        return self.library.read_blob(sha256, byte_count)


class StoreClient:
    """Study-first lookups across one study library and its pinned stores."""

    def __init__(self, repo, stores: list[Store]):
        self.repo = repo
        self.stores = list(stores)

    @classmethod
    def from_bundle(cls, repo) -> "StoreClient":
        entries = repo.library.bundle.get("stores") or []
        return cls(repo, [Store(entry["path"], expected_root=entry["merkleRoot"]) for entry in entries])

    # ------------------------------------------------------------- lookups
    def _study_has_work(self, identifiers: dict) -> bool:
        candidates = {"work-" + identity(f"{scheme}:{str(value).lower()}") for scheme, value in identifiers.items()}
        with self.repo.connect() as connection:
            for scheme, value in identifiers.items():
                if connection.execute("SELECT 1 FROM work_identifiers WHERE scheme=? AND value=?",
                                      (scheme, str(value).lower())).fetchone():
                    return True
            return any(connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone()
                       for work_id in candidates)

    def find_work(self, identifiers: dict):
        """(store, work_id) of the first store holding exactly one matching work, else None."""
        candidates = {"work-" + identity(f"{scheme}:{str(value).lower()}") for scheme, value in identifiers.items()}
        for store in self.stores:
            with store.connect() as connection:
                found = set()
                for scheme, value in identifiers.items():
                    found |= {row[0] for row in connection.execute(
                        "SELECT work_id FROM work_identifiers WHERE scheme=? AND value=?", (scheme, str(value).lower()))}
                found |= {work_id for work_id in candidates
                          if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone()}
            if len(found) == 1:
                return store, found.pop()
            if found:
                return None  # Ambiguous identity: the study resolves it from providers.
        return None

    def retained_sources(self, doi: str) -> list[dict]:
        """Retained review captures for a DOI: the study's own index first, then the stores."""
        doi = doi.lower()
        rows = []
        with self.repo.connect() as connection:
            if _has_table(connection, "retained_sources"):
                rows += [{**dict(row), "store": None} for row in connection.execute(
                    "SELECT * FROM retained_sources WHERE doi=? ORDER BY capture_sha256", (doi,))]
        if rows:
            return rows
        for store in self.stores:
            if not store.has_table("retained_sources"):
                continue
            with store.connect() as connection:
                rows = [{**dict(row), "store": store} for row in connection.execute(
                    "SELECT * FROM retained_sources WHERE doi=? ORDER BY capture_sha256", (doi,))]
            if rows:
                return rows
        return []

    # ------------------------------------------------------------ adoption
    def adopt_blob(self, store: Store, sha256: str) -> str:
        """Copy one registered store artifact (verified bytes and registry row) into the study."""
        with self.repo.connect() as connection:
            if connection.execute("SELECT 1 FROM source_artifacts WHERE sha256=?", (sha256,)).fetchone():
                return sha256
        with store.connect() as connection:
            row = connection.execute("SELECT * FROM source_artifacts WHERE sha256=?", (sha256,)).fetchone()
        if row is None:
            raise ValidationError("artifact is not registered in the store")
        body = store.blob(sha256, row["byte_count"])
        digest = self.repo.artifact(body, row["artifact_kind"], row["media_type"])
        if digest != sha256:
            raise ValidationError("adopted artifact changed identity")
        return digest

    def _closure(self, store: Store, roots: list[str]) -> list[str]:
        """Artifacts referenced (by SHA-256) from adopted JSON artifacts, breadth first and bounded."""
        seen, queue = [], list(dict.fromkeys(roots))
        with store.connect() as connection:
            while queue and len(seen) < MAX_CLOSURE:
                digest = queue.pop(0)
                if digest in seen:
                    continue
                row = connection.execute("SELECT media_type,byte_count FROM source_artifacts WHERE sha256=?",
                                         (digest,)).fetchone()
                if row is None:
                    continue
                seen.append(digest)
                if row["media_type"] != "application/json" or row["byte_count"] > 16 * 1024 * 1024:
                    continue
                try:
                    value = json.loads(store.blob(digest, row["byte_count"]))
                except (ValueError, UnicodeDecodeError):
                    continue
                queue += [item for item in _digests(value) if item not in seen]
        return seen

    def adopt_work(self, store: Store, work_id: str, *, tiers=("abstract",)) -> dict:
        """Copy a work's identity rows and its documents of the given tiers into the study."""
        with store.connect() as connection:
            work = connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
            if work is None:
                raise ValidationError("work is not present in the store")
            identifiers = [dict(r) for r in connection.execute("SELECT * FROM work_identifiers WHERE work_id=?", (work_id,))]
            marks = ",".join("?" for _ in tiers)
            documents = [dict(r) for r in connection.execute(
                f"SELECT * FROM corpus_documents WHERE work_id=? AND evidence_tier IN ({marks}) ORDER BY document_id",
                (work_id, *tiers))]
            sections = []
            for document in documents:
                sections += [dict(r) for r in connection.execute(
                    "SELECT * FROM source_sections WHERE work_id=? AND artifact_sha256=? ORDER BY rowid",
                    (work_id, document["artifact_sha256"]))]
        roots = [i["verified_by_sha256"] for i in identifiers] + [d["artifact_sha256"] for d in documents]
        roots += [item for d in documents for item in _digests(json.loads(d["metadata_json"]))]
        artifacts = [self.adopt_blob(store, digest) for digest in self._closure(store, roots)]
        with self.repo.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO works VALUES (?,?,?,?)",
                               (work["work_id"], work["title"], work["publication_year"], work["metadata_json"]))
            for row in identifiers:
                connection.execute("INSERT OR IGNORE INTO work_identifiers VALUES (?,?,?,?)",
                                   (row["work_id"], row["scheme"], row["value"], row["verified_by_sha256"]))
            for row in documents:
                connection.execute("INSERT OR IGNORE INTO corpus_documents VALUES (?,?,?,?,?,?,?,?,?)", tuple(
                    row[k] for k in ("document_id", "work_id", "artifact_sha256", "evidence_tier", "source_url", "provider",
                                     "complete", "metadata_json", "created_at")))
            for row in sections:
                connection.execute("INSERT OR IGNORE INTO source_sections VALUES (?,?,?,?,?,?,?,?,?)", tuple(
                    row[k] for k in ("section_id", "work_id", "artifact_sha256", "locator", "section_kind",
                                     "extracted_text", "text_sha256", "extractor_version", "is_complete")))
        receipt = {"operation": "adopt-from-store", "store": store.describe(), "workId": work_id,
                   "tiers": list(tiers), "documents": [d["document_id"] for d in documents],
                   "artifacts": artifacts, "adoptedAt": now(),
                   "limitation": "Store evidence is a byte cache; provider records are re-checked before use."}
        digest = self.repo.json_artifact(receipt, "store-adoption-receipt")
        with self.repo.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO retrievals VALUES (?,?,?,?,?,?,?,?,?,?)", (
                "retrieval-" + identity(work_id, digest), work_id, digest, "store:" + store.store_id,
                f"store://{store.merkle_root}/{work_id}", receipt["adoptedAt"], None, "acquired", None,
                canonical({"operation": "adopt-from-store"})))
        return {"workId": work_id, "documents": len(documents), "sections": len(sections),
                "artifacts": len(artifacts), "receiptSha256": digest}

    def adopt_for_identifiers(self, identifiers: dict, *, tiers=("abstract",)) -> dict | None:
        """Study first: adopt from a store only when the study has no row for these identifiers."""
        if not identifiers or self._study_has_work(identifiers):
            return None
        found = self.find_work(identifiers)
        if found is None:
            return None
        return self.adopt_work(*found, tiers=tiers)


def retained_captures(repo, doi: str) -> list[dict]:
    """Retained review captures for a DOI: the study's own index (or its parent's) first, then pinned stores.

    Captures found in a store are adopted into the study (bytes re-verified) so the
    caller can read them like any registered artifact.
    """
    doi = doi.lower()
    with repo.connect() as connection:
        if _has_table(connection, "retained_sources"):
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM retained_sources WHERE doi=? ORDER BY capture_sha256", (doi,))]
            if rows:
                return rows
    client = repo.store_client
    if client is None:
        return []
    found = []
    for row in client.retained_sources(doi):
        store = row.pop("store")
        if store is not None:
            client.adopt_blob(store, row["capture_sha256"])
        found.append(row)
    return found


def _has_table(connection, name: str) -> bool:
    """A table, or a branch's view of it, is visible under this name."""
    return connection.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table' UNION ALL "
                              "SELECT 1 FROM sqlite_temp_master WHERE name=? AND type='view'", (name, name)).fetchone() is not None


def _digests(value):
    if isinstance(value, str):
        if SHA256.fullmatch(value):
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _digests(item)
    elif isinstance(value, list):
        for item in value:
            yield from _digests(item)


def attach_store(repo, path, *, level: str = "standard") -> dict:
    """Verify a store and pin it (path and Merkle root) in a mutable study library."""
    if repo.library.is_snapshot:
        raise ValidationError("immutable snapshots cannot be modified; branch first")
    store = Store(Path(path), level=level)
    bundle_path = repo.root / "bundle.json"
    bundle = json.loads(bundle_path.read_text())
    stores = [entry for entry in bundle.get("stores", []) if entry.get("path") != str(store.path)]
    stores.append({"path": str(store.path), "id": store.store_id, "merkleRoot": store.merkle_root,
                   "manifestSha256": store.manifest_sha256})
    bundle["stores"] = stores
    temporary = bundle_path.with_name(".bundle.json.tmp")
    temporary.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, bundle_path)
    repo.library.bundle = bundle
    repo._store_client = None
    return {"library": repo.relative, "stores": stores}


# ------------------------------------------------------------ census byte caches
PROVIDER_CACHE_TABLES = ("responses",)
MAX_CACHED_BYTES = 100 * 1024 * 1024
ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")


class TableAllowlistError(ValidationError):
    """A read of a table outside a byte cache's allowlist was refused."""


class ByteCacheError(ValidationError):
    """Cached bytes are missing, differ from the digest their cache records, or carry legacy verdict vocabulary."""


class GuardedReader:
    """Read-only lookups in one ATTACHed database whose SQLite authorizer refuses every table outside an allowlist.

    A statement may read the allowlisted tables of the attached database and nothing else: no other table, view
    target, schema table or table-valued function, and no write, schema change, pragma, recursive query or further
    ATTACH. A refused read raises ``TableAllowlistError`` naming the table, never an empty result. One connection
    serves all threads, one statement at a time.
    """

    def __init__(self, uri: str, allowed: Iterable[str], *, label: str, alias: str):
        if not ALIAS.fullmatch(alias):
            raise ValidationError("invalid cache alias")
        self.allowed = frozenset(allowed)
        self.label, self.alias = label, alias
        self._lock = threading.Lock()
        self._refused: list[str] = []
        connection = sqlite3.connect(":memory:", uri=True, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
            connection.execute("PRAGMA query_only=ON")
            connection.set_authorizer(self._authorize)
        except sqlite3.DatabaseError as error:
            connection.close()
            raise ValidationError(f"{label} cannot be opened read-only: {error}") from None
        except BaseException:
            connection.close()
            raise
        self.connection = connection

    def _authorize(self, action, first, second, database, trigger):
        if action == sqlite3.SQLITE_READ:
            if first in self.allowed and database == self.alias:
                return sqlite3.SQLITE_OK
            self._refused.append(f"{database}.{first}")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT or (action == sqlite3.SQLITE_FUNCTION and second != "load_extension"):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._refused.clear()
            try:
                return self.connection.execute(sql, tuple(params)).fetchall()
            except sqlite3.DatabaseError as error:
                if self._refused:
                    raise TableAllowlistError(
                        f"{self.label} reads only {', '.join(sorted(self.allowed))}; refused table {self._refused[0]}"
                    ) from None
                raise ValidationError(f"{self.label} query failed: {error}") from None

    def close(self) -> None:
        with self._lock:
            self.connection.close()


def _checked_bytes(body: bytes, digest: str, label: str) -> bytes:
    """Bytes that hash to their recorded digest and carry no legacy verdict vocabulary."""
    from .store_projection import vocabulary_hits
    if hashlib.sha256(body).hexdigest() != digest:
        raise ByteCacheError(f"{label} bytes differ from their recorded digest")
    hits = vocabulary_hits(body)
    if hits:
        raise ByteCacheError(f"{label} bytes carry legacy verdict vocabulary ({', '.join(hits)}); refused")
    return body


class EvidenceStore:
    """A pinned ``literature-evidence@2`` projection read as a byte cache for census inputs.

    Opening verifies the sealed store (Merkle root, against a pinned root when given), that it declares the
    projection format with the current rules, and the projection gate (no row in any table outside the kept
    tables, no legacy property, no withheld artifact kind); that gate is the only statement run outside the guard.
    Every lookup then runs through a ``GuardedReader`` limited to the kept tables, and bytes are verified against
    their registered digest and refused if they carry legacy verdict vocabulary.
    """

    def __init__(self, path, *, expected_root: str | None = None, level: str = "light"):
        from .store_projection import FORMAT, KEPT_TABLES, RULES_SHA256, removed_rows
        self.store = Store(Path(path), expected_root=expected_root, level=level)
        declared = self.store.manifest.get("store") or {}
        if declared.get("id") != FORMAT:
            raise ValidationError(f"census inputs read only the label-free projection {FORMAT}, not this store")
        if declared.get("rules_sha256") != RULES_SHA256:
            raise ValidationError("store projection was built with different rules")
        with self.store.connect() as connection:
            removed = removed_rows(connection)
        if removed:
            raise ValidationError("store holds rows outside the projection allowlist: " + ", ".join(sorted(removed)))
        self.tables = tuple(KEPT_TABLES)
        self.reader = GuardedReader(self.store.uri, self.tables, label=f"{FORMAT} store", alias="evidence")
        try:
            meta = {row[0]: row[1] for row in self.reader.rows("SELECT key, value FROM evidence.store_meta")}
            if meta.get("format") != FORMAT or meta.get("rules_sha256") != RULES_SHA256:
                raise ValidationError("store metadata differs from its manifest")
        except BaseException:
            self.reader.close()
            raise
        self.version = {"kind": "store", "id": FORMAT, "merkleRoot": self.store.merkle_root,
                        "manifestSha256": self.store.manifest_sha256, "rulesSha256": RULES_SHA256}

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.reader.rows(sql, params)

    def close(self) -> None:
        self.reader.close()

    # -------------------------------------------------------------- identity
    def _work_ids(self, scheme: str, value: str) -> set[str]:
        found = {row[0] for row in self.rows(
            "SELECT work_id FROM evidence.work_identifiers WHERE scheme=? AND value=?", (scheme, value))}
        rule = "work-" + identity(f"{scheme}:{value}")
        if self.rows("SELECT 1 FROM evidence.works WHERE work_id=?", (rule,)):
            found.add(rule)
        return found

    def known_identifiers(self, work_id: str) -> dict[str, set[str]]:
        """Identifiers a stored work carries: its identifier rows and its bibliographic metadata."""
        known: dict[str, set[str]] = {}
        for scheme, value in self.rows("SELECT scheme, value FROM evidence.work_identifiers WHERE work_id=?", (work_id,)):
            known.setdefault(str(scheme), set()).add(str(value).lower())
        rows = self.rows("SELECT metadata_json FROM evidence.works WHERE work_id=?", (work_id,))
        try:
            metadata = json.loads(rows[0][0] or "{}") if rows else {}
        except ValueError:
            metadata = {}
        for scheme in ("doi", "pmid", "pmcid"):
            value = metadata.get(scheme) if isinstance(metadata, dict) else None
            if value:
                known.setdefault(scheme, set()).add(str(value).strip().lower())
        return known

    def find_work(self, identifiers: Mapping[str, str | None]) -> tuple[str | None, str]:
        """(work id, scheme) of the first identifier, in DOI, PMID, PMCID order, naming exactly one stored work.

        Each scheme is looked up in the identifier rows and through the store's own work-id rule (the digest of
        ``scheme:value``). A work whose other identifiers contradict the request is refused (``identity-mismatch``);
        two works for one identifier are ``ambiguous-identity``; nothing found is ``not-in-store``.
        """
        wanted = {scheme: str(value).strip().lower() for scheme, value in identifiers.items()
                  if scheme in ("doi", "pmid", "pmcid") and value}
        for scheme in ("doi", "pmid", "pmcid"):
            value = wanted.get(scheme)
            if not value:
                continue
            found = self._work_ids(scheme, value)
            if not found:
                continue
            if len(found) > 1:
                return None, "ambiguous-identity"
            work_id = found.pop()
            known = self.known_identifiers(work_id)
            if any(known.get(other) and expected not in known[other] for other, expected in wanted.items()):
                return None, "identity-mismatch"
            return work_id, scheme
        return None, "not-in-store"

    # ----------------------------------------------------------------- bytes
    def artifact(self, sha256: str) -> dict[str, Any] | None:
        if not SHA256.fullmatch(str(sha256)):
            return None
        rows = self.rows("SELECT sha256, byte_count, media_type, artifact_kind, created_at FROM evidence.source_artifacts "
                         "WHERE sha256=?", (sha256,))
        return dict(rows[0]) if rows else None

    def read(self, sha256: str) -> bytes:
        """Verified bytes of a registered artifact; missing, changed or labelled bytes raise ``ByteCacheError``."""
        row = self.artifact(sha256)
        if row is None:
            raise ByteCacheError("artifact is not registered in the store")
        try:
            body = self.store.blob(sha256, row["byte_count"])
        except ValueError as error:
            raise ByteCacheError(f"store bytes failed verification: {error}") from None
        return _checked_bytes(body, sha256, "store")

    # --------------------------------------------------------------- lookups
    def retained_captures(self, doi: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows(
            "SELECT doi, capture_sha256, media_type, route, captured_at FROM evidence.retained_sources WHERE doi=? "
            "ORDER BY captured_at DESC, capture_sha256", (doi,))]

    def documents(self, work_id: str, tier: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows(
            "SELECT document_id, work_id, artifact_sha256, evidence_tier, source_url, provider, complete, metadata_json, "
            "created_at FROM evidence.corpus_documents WHERE work_id=? AND evidence_tier=? "
            "ORDER BY complete DESC, created_at DESC, document_id", (work_id, tier))]


@dataclass(frozen=True)
class CachedResponse:
    body: bytes
    status: int
    headers: dict
    url: str
    retrieved_at: str
    sha256: str
    key: str


class ProviderCache:
    """A provider response cache (the corpus acquisition layout) read as a byte cache.

    The cache database is ATTACHed ``mode=ro&immutable=1`` and read only through its ``responses`` table
    (request digest, artifact digest, status, URL, retrieval time): leases, rate slots and recorded failures are
    refused, so a failed or negative lookup is never reused. Bytes come from the content-addressed artifact
    directory and must hash to the recorded digest. The cache must be quiescent (no journal beside it); its
    database digest is the version a study pins.
    """

    def __init__(self, path):
        raw = Path(path)
        if not raw.is_absolute():
            raise ValidationError("provider cache path must be absolute")
        if any(p.is_symlink() for p in (raw, *raw.parents)):
            raise ValidationError("provider cache path must not use symlinks")
        if not raw.is_file():
            raise ValidationError("provider cache database is missing")
        self.path = raw.resolve(strict=True)
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValidationError("provider cache has an open journal; mount a quiescent copy")
        self.artifacts = self.path.parent / (self.path.stem + "-artifacts")
        if self.artifacts.is_symlink() or not self.artifacts.is_dir():
            raise ValidationError("provider cache artifact directory is missing")
        self.sha256 = digest_file(self.path)
        self.reader = GuardedReader(self.path.as_uri() + "?mode=ro&immutable=1", PROVIDER_CACHE_TABLES,
                                    label="provider cache", alias="provider")
        try:
            self.reader.rows("SELECT key, request, sha256, status, headers, url, retrieved_at FROM provider.responses LIMIT 0")
        except ValidationError as error:
            self.reader.close()
            if isinstance(error, TableAllowlistError):
                raise
            raise ValidationError("not a provider response cache") from None
        self.version = {"kind": "provider-cache", "sha256": self.sha256}

    def close(self) -> None:
        self.reader.close()

    def response(self, key: str) -> CachedResponse | None:
        """The cached successful response for a request digest, bytes verified; None when absent or not a success."""
        rows = self.reader.rows("SELECT sha256, status, headers, url, retrieved_at FROM provider.responses WHERE key=?", (key,))
        if not rows:
            return None
        row = rows[0]
        if not 200 <= int(row["status"]) < 300:
            return None
        digest = str(row["sha256"])
        if not SHA256.fullmatch(digest):
            raise ByteCacheError("provider cache records an invalid artifact digest")
        try:
            descriptor = os.open(self.artifacts / digest, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise ByteCacheError("provider cache artifact is missing") from None
        with os.fdopen(descriptor, "rb") as handle:
            body = handle.read(MAX_CACHED_BYTES + 1)
        if len(body) > MAX_CACHED_BYTES:
            raise ByteCacheError("provider cache artifact exceeds the document bound")
        _checked_bytes(body, digest, "provider cache")
        try:
            headers = json.loads(row["headers"] or "{}")
        except ValueError:
            headers = {}
        return CachedResponse(body, int(row["status"]), headers if isinstance(headers, dict) else {}, str(row["url"]),
                              str(row["retrieved_at"]), digest, key)

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
            document: bool = False) -> CachedResponse | None:
        """The response cached for exactly this request (the cache's own request digest)."""
        from .corpus_acquisition import CorpusResponseCache
        key, _ = CorpusResponseCache.request_key(url, dict(params) if params else None,
                                                 dict(headers) if headers else None, document)
        return self.response(key)
