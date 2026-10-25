# SPDX-License-Identifier: Apache-2.0
"""Transactional corpus storage shared by import, acquisition and execution.

Immutable evidence is content addressed; queue state is separate from scientific
observations. No provider access or worker dispatch happens in this module.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from . import corpus_library as library
from .corpus_packs import PackIntegrityError
from .errors import ValidationError


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
            if version not in (1, 2):
                raise ValidationError("unsupported corpus database version")
        self.version = version
        self._verified_artifacts = {}

    @classmethod
    def create(cls, workspace: Path, relative: str, question: str, analysis_id: str,
               protocol: dict[str, Any], code_revision: str | None = None):
        library.initialize(library.Workspace(workspace), relative, question, analysis_id,
                           protocol, code_revision)
        result = cls(workspace, relative)
        result.migrate()
        return result

    def migrate(self):
        if self.version == 2:
            return
        if (self.root / "manifest.json").exists():
            raise ValidationError("branch or copy an immutable snapshot before migrating")
        backup = self.root / "corpus-v1-backup.sqlite3"
        if not backup.exists():
            self.library.backup(backup)
        migration = Path(__file__).resolve().parents[2] / "schemas/corpus-v2.sql"
        with self.connect(write=True) as connection:
            connection.executescript("BEGIN IMMEDIATE;\n" + migration.read_text() + "\nCOMMIT;")
        self.version = 2

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

    def analysis(self, analysis_id: str | None = None) -> dict[str, Any]:
        if analysis_id is None:
            analysis_id = self.library.active_analysis_id()
        with self.connect() as connection:
            row = connection.execute("SELECT a.*,r.protocol_json,r.protocol_sha256,r.corpus_id,r.code_revision FROM analyses a JOIN runs r USING(run_id) WHERE a.analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            raise ValidationError("unknown analysis")
        return {**dict(row), "protocol": json.loads(row["protocol_json"])}

    def artifact(self, body: bytes, kind: str, media_type: str = "application/json") -> str:
        if (self.root / "manifest.json").exists():
            raise ValidationError("immutable snapshots cannot be modified; branch first")
        digest = hashlib.sha256(body).hexdigest()
        # Bytes are durable (pack fsync or loose file fsync) before the registry row exists.
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

    def _registered(self, digest: str):
        with self.connect() as connection:
            item = connection.execute("SELECT * FROM source_artifacts WHERE sha256=?", (digest,)).fetchone()
        if item is None:
            raise ValidationError("artifact is not registered")
        if item["relative_path"] != "sources/" + item["sha256"]:
            raise ValidationError("artifact paths must be content-addressed sources/<sha256>")
        return item

    def read_artifact(self, digest: str) -> bytes:
        item = self._registered(digest)
        try:
            return self.library.read_blob(digest, item["byte_count"])
        except PackIntegrityError:
            raise ValidationError("registered artifact hash mismatch") from None

    def verify_artifact(self, digest: str):
        """Check a registered artifact's bytes; repeated checks of unchanged storage are cached."""
        item = self._registered(digest)
        found = self.library.locate(digest)
        if found is None:
            raise ValueError(f"missing source: {item['relative_path']}")
        path = found[1].pack_path(found[2].pack) if found[0] == "pack" else found[1]
        stat = path.stat()
        signature = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if self._verified_artifacts.get(digest) == signature:
            return found
        try:
            self.library.read_blob(digest, item["byte_count"])
        except PackIntegrityError:
            raise ValidationError("registered artifact hash mismatch") from None
        self._verified_artifacts[digest] = signature
        return found

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

    def add_task(self, analysis_id: str, locus_id: str, tier: str, spec: dict[str, Any], reason: str | None = None) -> str:
        task_id = "task-" + identity(analysis_id, locus_id, tier, spec)
        stored = {**spec, "id": task_id}
        digest = self.json_artifact(stored, "task-input")
        with self.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO corpus_tasks VALUES (?,?,?,?,?,?,?,?,?)", (task_id, analysis_id, locus_id, tier, digest,
                               "blocked" if reason else "pending", reason, 0, library.now()))
        return task_id

    def tasks(self, analysis_id: str, *, state: str | None = None, tier: str | None = None, limit: int | None = None):
        query, params = "SELECT * FROM corpus_tasks WHERE analysis_id=?", [analysis_id]
        if state:
            query += " AND state=?"; params.append(state)
        if tier:
            query += " AND tier=?"; params.append(tier)
        query += " ORDER BY task_id"
        if limit is not None:
            query += " LIMIT ?"; params.append(limit)
        with self.connect() as connection:
            rows = [dict(r) for r in connection.execute(query, params)]
        return [{**row, "spec": json.loads(self.read_artifact(row["spec_sha256"]))} for row in rows]

    def status(self, analysis_id: str | None = None) -> dict[str, Any]:
        analysis = self.analysis(analysis_id)
        with self.connect() as connection:
            counts = {t: connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                      ("reviews", "bibliography_entries", "works", "claim_loci", "source_artifacts", "source_sections")}
            tasks = [dict(r) for r in connection.execute("SELECT tier,state,count(*) AS count FROM corpus_tasks WHERE analysis_id=? GROUP BY tier,state", (analysis["analysis_id"],))]
            assessments = [dict(r) for r in connection.execute("SELECT evidence_level,validation_state,count(DISTINCT locus_id) AS count FROM assessments WHERE analysis_id=? GROUP BY evidence_level,validation_state", (analysis["analysis_id"],))]
            reviews = [dict(r) for r in connection.execute("SELECT phase,state,count(*) AS count FROM corpus_reviews GROUP BY phase,state")]
        return {"analysisId": analysis["analysis_id"], "schemaVersion": self.version, "counts": counts,
                "reviewStates": reviews, "taskStates": tasks, "assessments": assessments}
