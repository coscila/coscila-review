# SPDX-License-Identifier: Apache-2.0
"""Local lexical retrieval over evidence units: SQLite FTS5 with BM25 ranking and receipts.

Retrieval never leaves the machine. Every search returns a receipt that names the query terms,
the index digest, the scope, k, and the ranked unit ids with scores, so a later replay against
the same index can show that the evidence a judge saw was the evidence the index returned.
Cited works are searched first (scope = their units); claims without a citation search the
whole store. An optional re-rank hook may reorder results; it is recorded, never hidden.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RETRIEVAL_VERSION = "retrieval-fts5-1"
TOKENIZER = "unicode61 remove_diacritics 2"
MAX_QUERY_TERMS = 32
K1, B = 1.2, 0.75
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here
hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of
off on once only or other our ours ourselves out over own same she should so some such than that the their
theirs them themselves then there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours yourself yourselves et al via within without
however thus therefore whereas whether may might must shall upon among across per vs
""".split())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fold(text: str) -> str:
    """Approximate FTS5 unicode61 remove_diacritics folding for the pure-Python fallback."""

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", fold(text))


def query_terms(text: str, *, limit: int = MAX_QUERY_TERMS) -> list[str]:
    """Content terms of a claim: folded, de-duplicated, stopwords and single letters dropped."""

    terms: list[str] = []
    for token in tokens(text):
        if token in STOPWORDS or (len(token) < 2 and not token.isdigit()):
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def fts5_available() -> bool:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
            return True
        finally:
            connection.close()
    except sqlite3.Error:
        return False


@dataclass(frozen=True)
class EvidenceUnit:
    unit_id: str
    work_id: str
    kind: str
    text: str
    locator: str = ""
    complete: bool = True
    source_sha256: str = ""

    @property
    def text_sha256(self) -> str:
        return sha256_text(self.text)


@dataclass
class Work:
    work_id: str
    identifiers: dict[str, str] = field(default_factory=dict)
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    container: str = ""
    units: list[EvidenceUnit] = field(default_factory=list)


@dataclass(frozen=True)
class Hit:
    rank: int
    unit_id: str
    work_id: str
    score: float


@dataclass(frozen=True)
class RetrievalReceipt:
    receipt_id: str
    index_digest: str
    engine: str
    query_sha256: str
    terms: tuple[str, ...]
    scope: Mapping[str, Any]
    k: int
    hits: tuple[Hit, ...]
    reranker: str | None = None
    created_at: str = ""

    def record(self) -> dict[str, Any]:
        return {
            "receiptId": self.receipt_id, "version": RETRIEVAL_VERSION, "indexDigest": self.index_digest,
            "engine": self.engine, "querySha256": self.query_sha256, "terms": list(self.terms),
            "scope": dict(self.scope), "k": self.k, "reranker": self.reranker, "createdAt": self.created_at,
            "results": [{"rank": hit.rank, "unitId": hit.unit_id, "workId": hit.work_id, "score": hit.score}
                        for hit in self.hits],
        }


def _receipt_id(index_digest: str, engine: str, terms: Sequence[str], scope: Mapping[str, Any], k: int,
                hits: Sequence[Hit], reranker: str | None) -> str:
    return sha256_text(canonical({
        "indexDigest": index_digest, "engine": engine, "terms": list(terms), "scope": dict(scope), "k": k,
        "reranker": reranker, "results": [[hit.unit_id, hit.work_id, hit.score] for hit in hits]}))


class EvidenceIndex:
    """An immutable FTS5 index of evidence units in one SQLite file."""

    def __init__(self, path: Path, *, engine: str | None = None):
        self.path = Path(path)
        uri = self.path.resolve().as_uri() + "?mode=ro&immutable=1"
        self.connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        meta = {row["key"]: row["value"] for row in self.connection.execute("SELECT key, value FROM meta")}
        if meta.get("version") != RETRIEVAL_VERSION:
            raise ValueError("unsupported evidence index version")
        self.digest = meta["digest"]
        self.tokenizer = meta["tokenizer"]
        self.engine = engine or ("sqlite-fts5-bm25" if meta.get("fts5") == "1" else "python-bm25")
        if self.engine == "sqlite-fts5-bm25" and meta.get("fts5") != "1":
            raise ValueError("this index was built without FTS5")
        self._fallback: dict[str, Any] | None = None

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> EvidenceIndex:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @classmethod
    def build(cls, path: Path, units: Iterable[EvidenceUnit], *, use_fts5: bool | None = None) -> EvidenceIndex:
        path = Path(path)
        if path.exists():
            raise ValueError(f"evidence index already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(units, key=lambda unit: (unit.work_id, unit.unit_id))
        identifiers = [unit.unit_id for unit in ordered]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("evidence unit ids must be unique")
        fts5 = fts5_available() if use_fts5 is None else use_fts5
        digest = sha256_text(canonical({
            "version": RETRIEVAL_VERSION, "tokenizer": TOKENIZER,
            "units": [[unit.unit_id, unit.work_id, unit.kind, unit.locator, unit.text_sha256, unit.complete,
                       unit.source_sha256] for unit in ordered]}))
        connection = sqlite3.connect(path)
        try:
            with connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE units(rowid INTEGER PRIMARY KEY, unit_id TEXT NOT NULL UNIQUE, work_id TEXT NOT NULL,"
                    " kind TEXT NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL,"
                    " complete INTEGER NOT NULL, source_sha256 TEXT NOT NULL)")
                connection.execute("CREATE INDEX units_by_work ON units(work_id)")
                connection.executemany(
                    "INSERT INTO units(unit_id, work_id, kind, locator, text, text_sha256, complete, source_sha256)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(unit.unit_id, unit.work_id, unit.kind, unit.locator, unit.text, unit.text_sha256,
                      int(unit.complete), unit.source_sha256) for unit in ordered])
                if fts5:
                    connection.execute(f"CREATE VIRTUAL TABLE units_fts USING fts5(text, content='units',"
                                       f" content_rowid='rowid', tokenize='{TOKENIZER}')")
                    connection.execute("INSERT INTO units_fts(rowid, text) SELECT rowid, text FROM units")
                connection.executemany("INSERT INTO meta VALUES (?, ?)", [
                    ("version", RETRIEVAL_VERSION), ("digest", digest), ("tokenizer", TOKENIZER),
                    ("fts5", "1" if fts5 else "0"), ("units", str(len(ordered))),
                    ("works", str(len({unit.work_id for unit in ordered})))])
        finally:
            connection.close()
        return cls(path)

    def stats(self) -> dict[str, Any]:
        meta = {row["key"]: row["value"] for row in self.connection.execute("SELECT key, value FROM meta")}
        return {"digest": self.digest, "units": int(meta.get("units", 0)), "works": int(meta.get("works", 0)),
                "engine": self.engine, "tokenizer": self.tokenizer, "version": RETRIEVAL_VERSION,
                "sqliteVersion": sqlite3.sqlite_version}

    def units_for(self, work_id: str) -> list[EvidenceUnit]:
        rows = self.connection.execute(
            "SELECT unit_id, work_id, kind, text, locator, complete, source_sha256 FROM units WHERE work_id=?"
            " ORDER BY rowid", (work_id,))
        return [EvidenceUnit(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6]) for row in rows]

    def unit(self, unit_id: str) -> EvidenceUnit | None:
        row = self.connection.execute(
            "SELECT unit_id, work_id, kind, text, locator, complete, source_sha256 FROM units WHERE unit_id=?",
            (unit_id,)).fetchone()
        return EvidenceUnit(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6]) if row else None

    def _fts5(self, terms: Sequence[str], work_ids: Sequence[str] | None, limit: int) -> list[tuple[str, str, float]]:
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        sql = ("SELECT u.unit_id, u.work_id, bm25(units_fts) AS rank FROM units_fts JOIN units u"
               " ON u.rowid = units_fts.rowid WHERE units_fts MATCH ?")
        parameters: list[Any] = [expression]
        if work_ids is not None:
            sql += f" AND u.work_id IN ({','.join('?' for _ in work_ids)})"
            parameters.extend(work_ids)
        sql += " ORDER BY rank, u.unit_id LIMIT ?"
        parameters.append(limit)
        return [(row[0], row[1], -float(row[2])) for row in self.connection.execute(sql, parameters)]

    def _python(self, terms: Sequence[str], work_ids: Sequence[str] | None, limit: int) -> list[tuple[str, str, float]]:
        if self._fallback is None:
            documents = []
            frequencies: Counter[str] = Counter()
            for row in self.connection.execute("SELECT unit_id, work_id, text FROM units ORDER BY rowid"):
                counted = Counter(tokens(row[2]))
                documents.append((row[0], row[1], counted, sum(counted.values())))
                frequencies.update(counted.keys())
            average = sum(document[3] for document in documents) / len(documents) if documents else 0.0
            self._fallback = {"documents": documents, "frequencies": frequencies, "average": average}
        documents = self._fallback["documents"]
        frequencies = self._fallback["frequencies"]
        average = self._fallback["average"] or 1.0
        total = len(documents)
        allowed = set(work_ids) if work_ids is not None else None
        scored = []
        for unit_id, work_id, counted, length in documents:
            if allowed is not None and work_id not in allowed:
                continue
            score = 0.0
            matched = False
            for term in terms:
                frequency = counted.get(term, 0)
                if not frequency:
                    continue
                matched = True
                containing = frequencies[term]
                idf = math.log((total - containing + 0.5) / (containing + 0.5))
                idf = idf if idf > 0 else 1e-6
                score += idf * (frequency * (K1 + 1)) / (frequency + K1 * (1 - B + B * length / average))
            if matched:
                scored.append((unit_id, work_id, score))
        scored.sort(key=lambda item: (-item[2], item[0]))
        return scored[:limit]

    def search(self, query: str, *, work_ids: Sequence[str] | None = None, k: int = 5,
               reranker: Callable[[str, list[Hit]], list[Hit]] | None = None,
               terms: Sequence[str] | None = None) -> RetrievalReceipt:
        if not 1 <= int(k) <= 200:
            raise ValueError("k must be from 1 to 200")
        selected = list(terms) if terms is not None else query_terms(query)
        scope = {"kind": "store"} if work_ids is None else {"kind": "works", "workIds": sorted(set(work_ids))}
        rows: list[tuple[str, str, float]] = []
        if selected and (work_ids is None or work_ids):
            ordered_ids = sorted(set(work_ids)) if work_ids is not None else None
            rows = (self._fts5 if self.engine == "sqlite-fts5-bm25" else self._python)(selected, ordered_ids, int(k))
        hits = [Hit(rank, unit_id, work_id, round(score, 6)) for rank, (unit_id, work_id, score) in enumerate(rows, 1)]
        reranker_name = None
        if reranker is not None and hits:
            reranker_name = getattr(reranker, "name", getattr(reranker, "__name__", "reranker"))
            reordered = reranker(query, list(hits))
            if sorted(hit.unit_id for hit in reordered) != sorted(hit.unit_id for hit in hits):
                raise ValueError("a re-rank hook may reorder hits but never add or drop them")
            hits = [Hit(rank, hit.unit_id, hit.work_id, hit.score) for rank, hit in enumerate(reordered, 1)]
        receipt_id = _receipt_id(self.digest, self.engine, selected, scope, int(k), hits, reranker_name)
        return RetrievalReceipt(receipt_id, self.digest, self.engine, sha256_text(query), tuple(selected), scope,
                                int(k), tuple(hits), reranker_name, datetime.now(UTC).isoformat())

    def replay(self, receipt: Mapping[str, Any], *, tolerance: float = 1e-6) -> dict[str, Any]:
        """Re-run a recorded search on this index; it must return the recorded ranking."""

        if receipt.get("indexDigest") != self.digest:
            return {"reproduced": False, "reason": "index digest differs"}
        if receipt.get("reranker"):
            return {"reproduced": False, "reason": "re-ranked receipts need their re-rank hook"}
        scope = receipt.get("scope") or {}
        work_ids = scope.get("workIds") if scope.get("kind") == "works" else None
        again = self.search("", work_ids=work_ids, k=int(receipt["k"]), terms=receipt.get("terms") or [])
        recorded = receipt.get("results") or []
        same_ids = [hit.unit_id for hit in again.hits] == [item["unitId"] for item in recorded]
        same_scores = same_ids and all(abs(hit.score - float(item["score"])) <= tolerance
                                       for hit, item in zip(again.hits, recorded))
        return {"reproduced": bool(same_ids and same_scores), "receiptId": receipt.get("receiptId"),
                "replayedReceiptId": again.receipt_id}


def _normal_title(value: str) -> str:
    return " ".join(tokens(value))


class WorkCatalog:
    """Store works and their identifiers; maps printed references to store works."""

    def __init__(self, works: Iterable[Work]):
        self.works: dict[str, Work] = {}
        self.by_identifier: dict[tuple[str, str], str] = {}
        for work in works:
            if work.work_id in self.works:
                raise ValueError(f"duplicate work id in evidence store: {work.work_id}")
            self.works[work.work_id] = work
            for scheme, value in work.identifiers.items():
                key = (scheme.lower(), normalise_identifier(scheme, value))
                if key[1]:
                    self.by_identifier.setdefault(key, work.work_id)

    def match(self, identifiers: Mapping[str, Sequence[str] | str], printed: str) -> dict[str, Any]:
        """Identity ladder: printed identifier, then exact title containment; never a guess."""

        found: dict[str, str] = {}
        for scheme in ("doi", "pmid", "pmcid"):
            values = identifiers.get(scheme) or []
            for value in ([values] if isinstance(values, str) else values):
                work_id = self.by_identifier.get((scheme, normalise_identifier(scheme, str(value))))
                if work_id:
                    found[scheme] = work_id
        if found:
            targets = set(found.values())
            if len(targets) == 1:
                return {"workId": targets.pop(), "method": "identifier:" + ",".join(sorted(found)), "state": "resolved"}
            return {"workId": None, "method": "identifier-conflict", "state": "conflict",
                    "candidates": sorted(targets)}
        printed_normal = " " + _normal_title(printed) + " "
        matches = [work.work_id for work in self.works.values()
                   if len(_normal_title(work.title)) >= 20 and " " + _normal_title(work.title) + " " in printed_normal]
        if len(matches) == 1:
            return {"workId": matches[0], "method": "title-containment", "state": "resolved_unverified"}
        if len(matches) > 1:
            return {"workId": None, "method": "title-ambiguous", "state": "unresolved", "candidates": sorted(matches)}
        return {"workId": None, "method": "", "state": "unresolved"}


def normalise_identifier(scheme: str, value: str) -> str:
    value = str(value or "").strip()
    scheme = scheme.lower()
    if scheme == "doi":
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I).rstrip(".,;)")
        return value.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", value, re.I) else ""
    if scheme == "pmid":
        return value if re.fullmatch(r"\d{1,10}", value) else ""
    if scheme == "pmcid":
        value = value.upper()
        return value if re.fullmatch(r"PMC\d+", value) else ""
    return value.lower()


def load_store(path: Path) -> list[Work]:
    """Read an evidence store: JSON {"works": [...]} or JSON Lines, one work per line."""

    raw = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() in {".jsonl", ".ndjson"}:
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        value = json.loads(raw)
        records = value.get("works", []) if isinstance(value, dict) else value
    works = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("workId"), str):
            raise ValueError("every evidence-store work needs a workId")
        units = []
        for position, unit in enumerate(record.get("units", []), 1):
            if not isinstance(unit, dict) or not isinstance(unit.get("text"), str):
                raise ValueError("every evidence unit needs text")
            units.append(EvidenceUnit(str(unit.get("unitId") or f"{record['workId']}#u{position}"), record["workId"],
                                      str(unit.get("kind") or "full-text"), unit["text"], str(unit.get("locator") or ""),
                                      bool(unit.get("complete", True)), str(unit.get("sourceSha256") or "")))
        identifiers = {str(key).lower(): str(value) for key, value in (record.get("identifiers") or {}).items() if value}
        year = record.get("year")
        works.append(Work(record["workId"], identifiers, str(record.get("title") or ""),
                          [str(author) for author in record.get("authors") or []],
                          int(year) if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else None,
                          str(record.get("container") or ""), units))
    return works


def load_library(database: Path) -> list[Work]:
    """Read works and source sections from a literature library, opened immutable and read-only."""

    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        works: dict[str, Work] = {}
        for row in connection.execute("SELECT work_id, title, publication_year, metadata_json FROM works"):
            metadata = json.loads(row["metadata_json"] or "{}")
            works[row["work_id"]] = Work(row["work_id"], {}, row["title"] or "",
                                         [str(author) for author in metadata.get("authors", []) if author],
                                         row["publication_year"], str(metadata.get("container") or ""), [])
        for row in connection.execute("SELECT work_id, scheme, value FROM work_identifiers"):
            if row["work_id"] in works:
                works[row["work_id"]].identifiers.setdefault(row["scheme"].lower(), row["value"])
        for row in connection.execute(
                "SELECT section_id, work_id, section_kind, extracted_text, locator, is_complete, artifact_sha256"
                " FROM source_sections ORDER BY work_id, section_id"):
            if row["work_id"] in works:
                kind = "abstract" if "abstract" in (row["section_kind"] or "").lower() else "full-text"
                works[row["work_id"]].units.append(EvidenceUnit(
                    row["section_id"], row["work_id"], kind, row["extracted_text"], row["locator"] or "",
                    bool(row["is_complete"]), row["artifact_sha256"]))
        return list(works.values())
    finally:
        connection.close()


__all__ = [
    "EvidenceIndex", "EvidenceUnit", "Hit", "RETRIEVAL_VERSION", "RetrievalReceipt", "TOKENIZER", "Work",
    "WorkCatalog", "fts5_available", "load_library", "load_store", "normalise_identifier", "query_terms",
]
