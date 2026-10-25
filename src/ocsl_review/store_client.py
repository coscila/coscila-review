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
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3

from .corpus_library import Library, Workspace, now
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
