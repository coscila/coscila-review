# SPDX-License-Identifier: Apache-2.0
"""Census scores database: schema, data dictionary, freezing and exports.

One working SQLite file holds a census run (reviews, printed entries,
citing loci, cited works, identity proofs, evidence digests, one receipt per
model call and the append-only verdict history). ``freeze`` writes the closed
scores database (rollback journal, read-only, evidence text removed unless
kept explicitly) that is published as a dataset version. ``export`` writes
TSV files, a data dictionary and, when ``pyarrow`` is importable, Parquet.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .errors import ValidationError

SCHEMA_VERSION = 1
TABLES = ("meta", "reviews", "citations", "loci", "works", "identity", "evidence", "calls", "history",
          "review_scores", "work_scores", "sanitizer_findings", "data_dictionary")

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
  journal TEXT NOT NULL, doi TEXT NOT NULL, paper_key TEXT, title TEXT, year INTEGER, first_online TEXT,
  stratum TEXT, route TEXT, provenance_class TEXT, country TEXT, country_source TEXT, topic TEXT, topic_map TEXT,
  document_sha256 TEXT, extraction_json TEXT, uncovered INTEGER, review_order INTEGER NOT NULL,
  abstract_state TEXT NOT NULL DEFAULT 'pending', full_text_state TEXT NOT NULL DEFAULT 'pending',
  n_printed INTEGER, n_cited_inbody INTEGER, n_judged INTEGER, n_not_cited INTEGER,
  abs_sure_supported INTEGER, abs_unsure INTEGER, abs_sure_not_supported INTEGER,
  ft_read INTEGER, ft_sure_supported INTEGER, ft_unsure INTEGER, ft_sure_not_supported INTEGER,
  abs_not_supported_share REAL, ft_not_supported_share REAL,
  PRIMARY KEY(journal, doi));
CREATE TABLE IF NOT EXISTS citations(
  journal TEXT NOT NULL, doi TEXT NOT NULL, label TEXT NOT NULL, ordinal INTEGER NOT NULL, target_id TEXT, printed TEXT,
  cited_inbody INTEGER NOT NULL, n_loci INTEGER NOT NULL, n_outside_body_loci INTEGER NOT NULL DEFAULT 0,
  identity TEXT NOT NULL, identity_version TEXT, map_method TEXT,
  cited_work_key TEXT, cited_doi TEXT, cited_pmid TEXT, cited_pmcid TEXT, cited_title TEXT,
  model_judged INTEGER NOT NULL DEFAULT 0,
  abs_category TEXT, abs_support_verdict TEXT, abs_escalated INTEGER, abs_class TEXT, abs_source TEXT,
  abs_basis TEXT, abs_confidence TEXT, abs_reason TEXT, abs_quote TEXT, abs_quote_present INTEGER,
  abs_quote_in_evidence INTEGER, abs_call_id TEXT, abs_batch INTEGER, abs_position INTEGER,
  abs_evidence_id TEXT, abs_cut INTEGER, abs_sent_characters INTEGER,
  ft_category TEXT, ft_support_verdict TEXT, ft_class TEXT, ft_source TEXT, ft_retrieval TEXT, ft_reason TEXT,
  ft_quote TEXT, ft_quote_present INTEGER, ft_quote_in_evidence INTEGER, ft_call_id TEXT, ft_position INTEGER,
  ft_evidence_id TEXT, ft_window_characters INTEGER,
  PRIMARY KEY(journal, doi, label),
  FOREIGN KEY(journal, doi) REFERENCES reviews(journal, doi));
CREATE TABLE IF NOT EXISTS loci(
  journal TEXT NOT NULL, doi TEXT NOT NULL, label TEXT NOT NULL, seq INTEGER NOT NULL, sentence TEXT NOT NULL,
  section TEXT, unit INTEGER NOT NULL, char_offset INTEGER NOT NULL, unit_tag TEXT,
  in_table INTEGER NOT NULL, in_fig INTEGER NOT NULL, in_box INTEGER NOT NULL, in_list INTEGER NOT NULL,
  sent INTEGER NOT NULL, PRIMARY KEY(journal, doi, label, seq));
CREATE TABLE IF NOT EXISTS works(
  work_key TEXT PRIMARY KEY, doi TEXT, pmid TEXT, pmcid TEXT, title TEXT, venue TEXT, year TEXT, retracted INTEGER,
  identity_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS identity(
  journal TEXT NOT NULL, doi TEXT NOT NULL, label TEXT NOT NULL, work_key TEXT, identity TEXT NOT NULL, method TEXT,
  title_agrees INTEGER, ladder TEXT NOT NULL, proof_json TEXT, PRIMARY KEY(journal, doi, label));
CREATE TABLE IF NOT EXISTS evidence(
  evidence_id TEXT PRIMARY KEY, work_key TEXT NOT NULL, identity_version TEXT NOT NULL,
  unit TEXT NOT NULL CHECK(unit IN ('abstract', 'full-text')), sha256 TEXT NOT NULL, provider TEXT, route TEXT,
  provenance_class TEXT, retrieved_at TEXT, characters INTEGER, source_sha256 TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS calls(
  call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, journal TEXT NOT NULL, doi TEXT NOT NULL, tier TEXT NOT NULL,
  variant TEXT NOT NULL DEFAULT 'primary', batch INTEGER NOT NULL, kind TEXT NOT NULL, n_items INTEGER NOT NULL,
  labels_json TEXT NOT NULL, instrument_id TEXT NOT NULL, instrument_digest TEXT NOT NULL, code_revision TEXT,
  request_model TEXT, served_model TEXT, fingerprint TEXT, settings_sha256 TEXT NOT NULL,
  system_sha256 TEXT NOT NULL, user_sha256 TEXT NOT NULL, messages_sha256 TEXT NOT NULL, body_sha256 TEXT NOT NULL,
  max_tokens INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
  cached_tokens INTEGER, finish_reason TEXT, truncated INTEGER, attempts INTEGER NOT NULL, seconds REAL,
  error TEXT, n_parsed INTEGER, n_missing INTEGER, n_problems INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history(
  history_id INTEGER PRIMARY KEY AUTOINCREMENT, journal TEXT NOT NULL, doi TEXT NOT NULL, label TEXT NOT NULL,
  tier TEXT NOT NULL, variant TEXT NOT NULL DEFAULT 'primary', call_id TEXT, position INTEGER, outcome TEXT NOT NULL,
  category TEXT, support_verdict TEXT, escalate INTEGER, quote TEXT, reason TEXT, record_json TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_scores(
  journal TEXT NOT NULL, doi TEXT NOT NULL, n_classifiable INTEGER NOT NULL, n_not_supported INTEGER NOT NULL,
  expected REAL, excess REAL, p_value REAL, q_value REAL, PRIMARY KEY(journal, doi));
CREATE TABLE IF NOT EXISTS work_scores(
  work_key TEXT PRIMARY KEY, n_citations INTEGER NOT NULL, n_classifiable INTEGER NOT NULL,
  n_not_supported INTEGER NOT NULL, expected REAL, excess REAL, p_value REAL, q_value REAL);
CREATE TABLE IF NOT EXISTS sanitizer_findings(
  finding_id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('citation', 'evidence')),
  journal TEXT, doi TEXT, label TEXT, seq INTEGER, evidence_id TEXT, field TEXT NOT NULL, kind TEXT NOT NULL,
  rule TEXT NOT NULL, start INTEGER NOT NULL, length INTEGER NOT NULL, sha256 TEXT NOT NULL, sanitizer TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_dictionary(
  table_name TEXT NOT NULL, column_name TEXT NOT NULL, type TEXT NOT NULL, meaning TEXT NOT NULL,
  PRIMARY KEY(table_name, column_name));
CREATE INDEX IF NOT EXISTS ix_citations_class ON citations(journal, abs_class);
CREATE INDEX IF NOT EXISTS ix_calls_review ON calls(journal, doi, tier);
CREATE INDEX IF NOT EXISTS ix_history_entry ON history(journal, doi, label);
CREATE UNIQUE INDEX IF NOT EXISTS ux_history_primary_verdict ON history(journal, doi, label, tier)
  WHERE variant='primary' AND outcome='accepted';
CREATE TRIGGER IF NOT EXISTS history_no_update BEFORE UPDATE ON history
  BEGIN SELECT RAISE(ABORT, 'verdict history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS history_no_delete BEFORE DELETE ON history
  BEGIN SELECT RAISE(ABORT, 'verdict history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS calls_no_update BEFORE UPDATE ON calls
  BEGIN SELECT RAISE(ABORT, 'call receipts are immutable'); END;
"""

DICTIONARY: dict[str, dict[str, str]] = {
    "meta": {"key": "run metadata key (schema version, instrument id and digest, protocol digest, journal, seed, code revision)",
             "value": "metadata value"},
    "reviews": {
        "journal": "journal of the review (study input)", "doi": "review DOI, lower case", "paper_key": "local review key",
        "title": "review title", "year": "first-online year", "first_online": "first-online date (ISO 8601)",
        "stratum": "publisher article-type label", "route": "extraction route (jats or html)",
        "provenance_class": "public, entitled or unavailable", "country": "corresponding-author country (descriptive only)",
        "country_source": "how the country was derived", "topic": "topic from title keywords", "topic_map": "version of the topic keyword map",
        "document_sha256": "sha256 of the review document that was extracted", "extraction_json": "extraction diagnostics",
        "uncovered": "anchors not owned exactly once (gate: 0)", "review_order": "processing order (enumeration order)",
        "abstract_state": "pending, completed or paused for the abstract tier", "full_text_state": "pending or completed for the full-text tier",
        "n_printed": "printed bibliography entries", "n_cited_inbody": "entries with at least one in-body citation",
        "n_judged": "entries classified at the abstract tier (sure supported, unsure, sure not supported)",
        "n_not_cited": "entries never cited in the body",
        "abs_sure_supported": "abstract tier: T and not escalated", "abs_unsure": "abstract tier: N, U, escalated, unresolved or invalid",
        "abs_sure_not_supported": "abstract tier: F and not escalated", "ft_read": "entries with a full-text verdict",
        "ft_sure_supported": "full-text tier: T", "ft_unsure": "full-text tier: N or U", "ft_sure_not_supported": "full-text tier: F",
        "abs_not_supported_share": "abs_sure_not_supported / (abs_sure_supported + abs_sure_not_supported)",
        "ft_not_supported_share": "ft_sure_not_supported / (ft_sure_supported + ft_sure_not_supported)"},
    "citations": {
        "journal": "journal of the citing review", "doi": "citing review DOI", "label": "printed bibliography label (alphanumeric)",
        "ordinal": "position in the printed bibliography (document order)", "target_id": "bibliography target id in the markup",
        "printed": "printed bibliography entry text", "cited_inbody": "1 when at least one in-body locus exists",
        "n_loci": "in-body citation loci (one per marker occurrence)", "n_outside_body_loci": "loci outside the body (diagnostic only)",
        "identity": "resolved, resolved_unverified or unresolved", "identity_version": "identity ladder version of the cited work",
        "map_method": "how the identity was found (markup-doi, markup-pmid, printed-doi, title, label, supplied)",
        "cited_work_key": "cited work key", "cited_doi": "cited work DOI", "cited_pmid": "cited work PMID",
        "cited_pmcid": "cited work PMCID", "cited_title": "cited work title",
        "model_judged": "1 when the abstract tier produced a parseable verdict for this entry",
        "abs_category": "abstract tier category: true, false, not_assessable, unsure (NULL for never cited)",
        "abs_support_verdict": "abstract tier support type", "abs_escalated": "1 when the judge escalated to full text",
        "abs_class": "sure_supported, unsure, sure_not_supported or not_cited", "abs_source": "abstract provider",
        "abs_basis": "model, deterministic:not-cited, deterministic:unresolved-identity or invalid:no-parseable-output",
        "abs_confidence": "rule-based confidence from category and escalation; never calibrated",
        "abs_reason": "judge rationale", "abs_quote": "verbatim quote returned for F",
        "abs_quote_present": "for F: 1 when a quote was returned", "abs_quote_in_evidence": "for F: 1 when the quote occurs in the abstract sent",
        "abs_call_id": "call receipt of the accepted verdict", "abs_batch": "batch index within the review (0-based)",
        "abs_position": "position within the call (1-based)", "abs_evidence_id": "abstract evidence unit",
        "abs_cut": "1 when the abstract sent was cut", "abs_sent_characters": "abstract characters sent",
        "ft_category": "full-text tier category", "ft_support_verdict": "full-text tier support type", "ft_class": "full-text class",
        "ft_source": "full-text source (europe-pmc-fulltextxml, entitled, cache)",
        "ft_retrieval": "retrieval outcome (cache, europe-pmc-fulltextxml, entitled, fetch-failed, no-pmcid)",
        "ft_reason": "full-text rationale", "ft_quote": "full-text quote", "ft_quote_present": "for F: quote returned",
        "ft_quote_in_evidence": "for F: quote occurs in the window sent", "ft_call_id": "call receipt of the full-text verdict",
        "ft_position": "position within the full-text call", "ft_evidence_id": "full-text evidence unit",
        "ft_window_characters": "full-text window characters sent"},
    "loci": {"journal": "journal", "doi": "citing review DOI", "label": "bibliography label", "seq": "document order of the locus",
             "sentence": "citing sentence as rendered for the judge", "section": "section trail", "unit": "citing unit index",
             "char_offset": "marker offset in the rendered unit", "unit_tag": "markup tag of the citing unit",
             "in_table": "inside a table", "in_fig": "inside a figure", "in_box": "inside a box", "in_list": "inside a list",
             "sent": "1 when the sentence was among the citing sentences sent"},
    "works": {"work_key": "cited work key", "doi": "DOI", "pmid": "PMID", "pmcid": "PMCID", "title": "title",
              "venue": "venue", "year": "year", "retracted": "retraction flag sent to the judge",
              "identity_version": "identity ladder version; a change invalidates downstream evidence and verdicts"},
    "identity": {"journal": "journal", "doi": "citing review DOI", "label": "bibliography label", "work_key": "resolved work",
                 "identity": "identity class", "method": "ladder step", "title_agrees": "printed entry contains the work title",
                 "ladder": "identity ladder version", "proof_json": "identifiers and comparisons used"},
    "evidence": {"evidence_id": "evidence unit id", "work_key": "work the evidence belongs to", "identity_version": "identity version bound to",
                 "unit": "abstract or full-text", "sha256": "sha256 of the text (abstract) or of the window sent (full text)",
                 "provider": "provider", "route": "retrieval route", "provenance_class": "public or entitled",
                 "retrieved_at": "retrieval time", "characters": "text characters", "source_sha256": "sha256 of the source bytes",
                 "text": "evidence text in the working database; removed when frozen unless kept"},
    "calls": {"call_id": "receipt id", "run_id": "census run", "journal": "journal", "doi": "review DOI", "tier": "abstract or full-text",
              "variant": "primary or a predeclared sensitivity", "batch": "batch or call index within the review",
              "kind": "first, repair or full-text", "n_items": "items in the call", "labels_json": "labels in call order",
              "instrument_id": "instrument id", "instrument_digest": "instrument bundle digest", "code_revision": "code tag",
              "request_model": "model name requested", "served_model": "model reported by the server", "fingerprint": "server fingerprint when offered",
              "settings_sha256": "digest of the request settings without messages", "system_sha256": "sha256 of the system prompt",
              "user_sha256": "sha256 of the user message", "messages_sha256": "sha256 of the JSON message list",
              "body_sha256": "sha256 of the bytes sent", "max_tokens": "output token limit", "prompt_tokens": "provider prompt tokens",
              "completion_tokens": "provider completion tokens", "total_tokens": "provider total tokens",
              "cached_tokens": "provider cached prompt tokens", "finish_reason": "provider finish reason",
              "truncated": "1 when the finish reason is length", "attempts": "transport attempts", "seconds": "latency of the last attempt",
              "error": "transport, model_drift or other failure", "n_parsed": "labels parsed", "n_missing": "labels missing",
              "n_problems": "parser problems", "created_at": "receipt time"},
    "history": {"history_id": "append-only row id", "journal": "journal", "doi": "review DOI", "label": "label", "tier": "tier",
                "variant": "primary or sensitivity", "call_id": "receipt", "position": "position in call",
                "outcome": "accepted, superseded, missing, deterministic or invalid", "category": "category", "support_verdict": "support type",
                "escalate": "escalate flag", "quote": "quote", "reason": "rationale", "record_json": "parsed record",
                "created_at": "time"},
    "review_scores": {"journal": "journal", "doi": "review DOI", "n_classifiable": "sure supported + sure not supported",
                      "n_not_supported": "sure not supported", "expected": "expected count under the fitted beta-binomial",
                      "excess": "observed minus expected", "p_value": "upper-tail beta-binomial p-value",
                      "q_value": "Benjamini-Hochberg q-value"},
    "work_scores": {"work_key": "cited work", "n_citations": "citing entries", "n_classifiable": "classifiable citing entries",
                    "n_not_supported": "sure not supported", "expected": "expected count", "excess": "observed minus expected",
                    "p_value": "upper-tail p-value", "q_value": "Benjamini-Hochberg q-value"},
    "sanitizer_findings": {
        "finding_id": "finding id (digest of scope, field, offset and rule)",
        "scope": "citation (a citing sentence or a full-text window of one entry) or evidence (an abstract)",
        "journal": "journal (citation scope)", "doi": "citing review DOI (citation scope)",
        "label": "bibliography label (citation scope)", "seq": "locus order of the citing sentence",
        "evidence_id": "abstract evidence unit (evidence scope)",
        "field": "citing_sentence, abstract or fulltext_window", "kind": "prompt-injection or a hidden-character class",
        "rule": "sanitizer rule that matched", "start": "offset of the match in the text (characters)",
        "length": "matched characters", "sha256": "sha256 of the matched text; the text itself is not copied",
        "sanitizer": "sanitizer version (detect mode: the text sent to the judge is unchanged)",
        "created_at": "detection time"},
    "data_dictionary": {"table_name": "table", "column_name": "column", "type": "declared type", "meaning": "meaning"},
}


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if not create and not path.is_file():
        raise ValidationError(f"scores database is missing: {path}")
    if path.exists() and path.stat().st_nlink != 1:
        raise ValidationError("scores database shares mutable inodes")
    connection = sqlite3.connect(path, timeout=60, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if create:
        connection.executescript(DDL)
        write_dictionary(connection)
        connection.execute("INSERT OR IGNORE INTO meta VALUES('schemaVersion', ?)", (str(SCHEMA_VERSION),))
        connection.commit()
    version = connection.execute("SELECT value FROM meta WHERE key='schemaVersion'").fetchone()
    if not version or int(version[0]) != SCHEMA_VERSION:
        connection.close()
        raise ValidationError("unsupported scores database schema version")
    return connection


def write_dictionary(connection: sqlite3.Connection) -> None:
    rows = []
    for table in TABLES:
        declared = {r[1]: r[2] for r in connection.execute(f"PRAGMA table_info({table})")}
        meanings = DICTIONARY.get(table, {})
        missing = set(declared) - set(meanings)
        if missing:
            raise ValidationError(f"data dictionary lacks {table}: {', '.join(sorted(missing))}")
        rows.extend((table, column, declared[column] or "", meanings[column]) for column in declared)
    connection.executemany("INSERT OR REPLACE INTO data_dictionary VALUES(?,?,?,?)", rows)


def set_meta(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    for key, value in values.items():
        rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
        existing = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if existing and existing[0] != rendered:
            raise ValidationError(f"census run metadata {key} is pinned and differs")
        connection.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, rendered))


def meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in connection.execute("SELECT key, value FROM meta")}


def aggregate_reviews(connection: sqlite3.Connection, journal: str | None = None, doi: str | None = None) -> None:
    """Recompute per-review counts from the citation rows (3 classes x 2 tiers, plus never cited)."""
    where, params = ("WHERE journal=? AND doi=?", (journal, doi)) if journal is not None else ("", ())
    connection.execute("""
      UPDATE reviews SET
        n_printed = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi),
        n_cited_inbody = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.cited_inbody=1),
        n_judged = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi
                    AND c.abs_class IN ('sure_supported','unsure','sure_not_supported')),
        n_not_cited = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.abs_class='not_cited'),
        abs_sure_supported = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.abs_class='sure_supported'),
        abs_unsure = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.abs_class='unsure'),
        abs_sure_not_supported = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.abs_class='sure_not_supported'),
        ft_read = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.ft_class IS NOT NULL),
        ft_sure_supported = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.ft_class='sure_supported'),
        ft_unsure = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.ft_class='unsure'),
        ft_sure_not_supported = (SELECT COUNT(*) FROM citations c WHERE c.journal=reviews.journal AND c.doi=reviews.doi AND c.ft_class='sure_not_supported')
    """ + where, params)
    connection.execute("""
      UPDATE reviews SET
        abs_not_supported_share = CASE WHEN abs_sure_supported + abs_sure_not_supported > 0
          THEN 1.0 * abs_sure_not_supported / (abs_sure_supported + abs_sure_not_supported) END,
        ft_not_supported_share = CASE WHEN ft_sure_supported + ft_sure_not_supported > 0
          THEN 1.0 * ft_sure_not_supported / (ft_sure_supported + ft_sure_not_supported) END
    """ + where, params)


def write_scores(connection: sqlite3.Connection) -> dict[str, int]:
    """Fill review_scores and work_scores with beta-binomial excess and BH-FDR q-values."""
    from .estimands import beta_binomial_screen
    connection.execute("DELETE FROM review_scores")
    connection.execute("DELETE FROM work_scores")
    counts = {"reviews": 0, "works": 0}
    for journal in [r[0] for r in connection.execute("SELECT DISTINCT journal FROM reviews ORDER BY journal")]:
        units = [(r["doi"], r["k"], r["n"]) for r in connection.execute(
            "SELECT doi, SUM(abs_class='sure_not_supported') AS k, SUM(abs_class IN ('sure_supported','sure_not_supported')) AS n "
            "FROM citations WHERE journal=? GROUP BY doi ORDER BY doi", (journal,))]
        for doi, k, n, expected, excess, p, q in beta_binomial_screen(units):
            connection.execute("INSERT INTO review_scores VALUES(?,?,?,?,?,?,?,?)", (journal, doi, n, k, expected, excess, p, q))
            counts["reviews"] += 1
    works = [(r["work_key"], r["m"], r["k"], r["n"]) for r in connection.execute(
        "SELECT cited_work_key AS work_key, COUNT(*) AS m, SUM(abs_class='sure_not_supported') AS k, "
        "SUM(abs_class IN ('sure_supported','sure_not_supported')) AS n FROM citations "
        "WHERE cited_work_key IS NOT NULL AND cited_inbody=1 GROUP BY cited_work_key ORDER BY cited_work_key")]
    totals = {w[0]: w[1] for w in works}
    for key, k, n, expected, excess, p, q in beta_binomial_screen([(w[0], w[2], w[3]) for w in works]):
        connection.execute("INSERT INTO work_scores VALUES(?,?,?,?,?,?,?,?)", (key, totals[key], n, k, expected, excess, p, q))
        counts["works"] += 1
    return counts


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze(source: Path, output: Path, *, keep_text: bool = False) -> dict[str, Any]:
    """Write the closed scores database: rollback journal, no WAL, read-only, evidence text removed by default."""
    output = Path(output)
    if output.exists():
        raise ValidationError("frozen scores database already exists; freeze into a new path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(Path(source))) as connection:
        aggregate_reviews(connection)
        scores = write_scores(connection)
        connection.commit()
        connection.execute("VACUUM INTO ?", (str(output),))
    with closing(sqlite3.connect(output)) as frozen:
        frozen.execute("PRAGMA journal_mode=DELETE")
        if not keep_text:
            frozen.execute("UPDATE evidence SET text=NULL")
        frozen.execute("INSERT OR REPLACE INTO meta VALUES('frozen', '1')")
        frozen.execute("INSERT OR REPLACE INTO meta VALUES('evidenceTextKept', ?)", ("1" if keep_text else "0",))
        frozen.commit()
        frozen.execute("VACUUM")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(output) + suffix).exists():
            raise ValidationError("frozen scores database left a journal behind")
    os.chmod(output, 0o444)
    return {"path": str(output), "sha256": file_sha256(output), "bytes": output.stat().st_size, "evidenceTextKept": keep_text,
            "scored": scores}


def open_readonly(path: Path) -> sqlite3.Connection:
    """A read-only connection to a working or frozen scores database: no DDL, no metadata pins, no writes."""
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"scores database is missing: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute("SELECT value FROM meta WHERE key='schemaVersion'").fetchone()
    except sqlite3.DatabaseError as error:
        connection.close()
        raise ValidationError("not a census scores database") from error
    if not version or int(version[0]) != SCHEMA_VERSION:
        connection.close()
        raise ValidationError("unsupported scores database schema version")
    return connection


def open_frozen(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _arrow_schema(pa, connection: sqlite3.Connection, table: str, columns: list[str]):
    """Parquet column types from the declared SQLite types (INTEGER, REAL, otherwise text)."""
    declared = {r[1]: (r[2] or "").upper() for r in connection.execute(f"PRAGMA table_info({table})")}
    kinds = {"INTEGER": pa.int64(), "REAL": pa.float64()}
    return pa.schema([(column, kinds.get(declared.get(column, ""), pa.string())) for column in columns])


def export(path: Path, output: Path, *, tables: Iterable[str] = TABLES, parquet: bool = True) -> dict[str, Any]:
    """TSV (always) and Parquet (when pyarrow is importable) for each table, plus a manifest with digests."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files, parquet_state = [], "not requested"
    try:
        if parquet:
            import pyarrow as pa
            import pyarrow.parquet as pq
            parquet_state = "written"
    except ImportError:
        parquet_state = "unavailable: pyarrow is not installed"
        parquet = False
    with closing(open_frozen(path)) as connection:
        for table in tables:
            order = "ORDER BY key" if table == "meta" else "ORDER BY rowid"
            cursor = connection.execute(f"SELECT * FROM {table} {order}")
            columns = [d[0] for d in cursor.description]
            target = output / f"{table}.tsv"
            count = 0
            writer_parquet = None
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
                writer.writerow(columns)
                while True:
                    rows = cursor.fetchmany(20_000)
                    if not rows:
                        break
                    writer.writerows(["" if v is None else v for v in row] for row in rows)
                    count += len(rows)
                    if parquet:
                        if writer_parquet is None:
                            schema = _arrow_schema(pa, connection, table, columns)
                            writer_parquet = pq.ParquetWriter(output / f"{table}.parquet", schema)
                        writer_parquet.write_table(pa.table({column: [row[i] for row in rows] for i, column in enumerate(columns)},
                                                            schema=writer_parquet.schema))
            files.append({"path": target.name, "rows": count, "sha256": file_sha256(target)})
            if parquet:
                if writer_parquet is None:
                    schema = _arrow_schema(pa, connection, table, columns)
                    pq.write_table(pa.table({column: [] for column in columns}, schema=schema), output / f"{table}.parquet")
                else:
                    writer_parquet.close()
                files.append({"path": f"{table}.parquet", "rows": count, "sha256": file_sha256(output / f"{table}.parquet")})
    manifest = {"format": "ocsl-census-scores-export-v1", "source": {"path": Path(path).name, "sha256": file_sha256(path)},
                "parquet": parquet_state, "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
