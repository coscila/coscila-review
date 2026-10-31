# SPDX-License-Identifier: Apache-2.0
"""Schema of the follow-up branch database: a compromised-evidence dependency audit of a frozen census.

A follow-up branch is one SQLite file that holds only the follow-up's own rows. Every connection ATTACHes the frozen
parent census scores database read-only and immutable as ``census`` (and, when the study supplies it, the private
frozen copy that keeps entitled citing sentences as ``census_text``). Nothing of the parent is copied and no foreign key
crosses databases: rows name the parent by value, a citation locus by (journal, doi, label, seq) and a cited work by
``work_key``. ``followup_branch.FollowupBranch.verify`` checks every such reference against the parent and that the
parent's digest is unchanged.

The tables here are the shared contract: the ledger tables every module writes (datasets, artifacts, provider requests,
judge calls and judgments) and the interface tables one module writes and others read (status, frame and strata,
support, function, dependency, numeric checks, cited-by edges, second-generation matches, estimates and coverage).
Tables private to one module live in ``followup_schema_a`` and ``followup_schema_b``; ``ddl()`` and ``dictionary()``
join all three, so one schema version covers the branch.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping

from .errors import ValidationError

SCHEMA_VERSION = 1
FORMAT = "ocsl-followup-branch-v1"

# Vocabularies (each CHECK constraint below is generated from these tuples).
STATUSES = ("retracted", "expression-of-concern", "corrected", "none", "unknown")
OBSERVED_STATUSES = ("retracted", "expression-of-concern", "corrected", "reinstated", "none", "unknown")
COMPROMISED = ("retracted", "expression-of-concern", "corrected")
NOTICE_KINDS = ("retraction", "expression-of-concern", "correction", "reinstatement")
STATUS_SOURCES = ("crossref:publisher", "crossref:retraction-watch", "crossref", "retraction-watch-csv", "pubmed",
                  "europe-pmc")
DATASET_KINDS = ("parent", "parent-text", "protocol", "status-snapshot", "retraction-watch-csv", "cited-by-graph",
                 "table-source", "evidence-store")
PROVENANCE = ("public", "entitled", "unavailable")
STRATA = ("S0", "S1", "S2", "S3", "co-cited")
SELECTIONS = ("census", "sampled")
SUPPORT_TIERS = ("abstract", "full-text")
SUPPORT_VERDICTS = ("direct-support", "scope-caveat", "partial-indirect", "contradicted", "not-addressed", "abstain")
SUPPORTING = ("direct-support", "scope-caveat")
ADVERSE = ("contradicted", "not-addressed")
FUNCTION_CLASSES = ("relies", "background", "critical", "notes-retraction")
DEPENDENCY_CLASSES = ("sole", "essential", "redundant", "undetermined")
NUMERIC_CLASSES = ("consistent", "number-absent-or-different", "wrong-geography", "wrong-edition",
                   "non-epidemiological")
MATCH_CLASSES = ("same-claim", "related-claim", "different-claim")
RELIANCE_CLASSES = ("cites-review-for-claim", "cites-review-otherwise", "unclear")
CITEDBY_SOURCES = ("europe-pmc", "openalex")
REQUEST_OUTCOMES = ("ok", "not-found", "unavailable", "error")
CALL_OUTCOMES = ("completed", "failed", "refused", "model-drift")
JUDGMENT_OUTCOMES = ("accepted", "rejected", "blocked")
# Why a unit has no verdict. Abstract silence and failed downloads are reasons, never verdicts: an unassessable unit
# goes into the coverage tables and never into a numerator as "unsupported".
UNASSESSABLE = ("no-citing-text", "text-redacted", "identity-unresolved", "fetch-failed", "no-open-full-text",
                "abstract-incomplete", "abstained", "invalid-output", "not-judged", "excluded-by-protocol",
                "table-unavailable", "no-matching-table")


def _in(column: str, values: Iterable[str], *, nullable: bool = False) -> str:
    listed = ",".join("'" + value.replace("'", "''") + "'" for value in values)
    check = f"{column} IN ({listed})"
    return f"({column} IS NULL OR {check})" if nullable else check


def _append_only(table: str, what: str) -> str:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{what} are append-only'); END;\n"
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{what} are append-only'); END;\n")


CORE_DDL = f"""
CREATE TABLE IF NOT EXISTS fu_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_dictionary(
  table_name TEXT NOT NULL, column_name TEXT NOT NULL, type TEXT NOT NULL, meaning TEXT NOT NULL,
  PRIMARY KEY(table_name, column_name));
CREATE TABLE IF NOT EXISTS fu_datasets(
  dataset_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK({_in('kind', DATASET_KINDS)}),
  name TEXT NOT NULL, declared_version TEXT NOT NULL, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
  rows INTEGER, file_name TEXT, detail_json TEXT NOT NULL DEFAULT '{{}}', registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_artifacts(
  sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64), kind TEXT NOT NULL, media_type TEXT NOT NULL,
  bytes INTEGER NOT NULL, codec TEXT NOT NULL CHECK(codec IN ('zlib')), body BLOB NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_requests(
  request_id TEXT PRIMARY KEY, provider TEXT NOT NULL, operation TEXT NOT NULL, url TEXT NOT NULL,
  params_json TEXT NOT NULL, http_status INTEGER, outcome TEXT NOT NULL CHECK({_in('outcome', REQUEST_OUTCOMES)}),
  response_sha256 TEXT, error TEXT, retrieved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_calls(
  call_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, instrument_digest TEXT NOT NULL, tier TEXT NOT NULL,
  variant TEXT NOT NULL DEFAULT 'primary', kind TEXT NOT NULL CHECK(kind IN ('first','repair')),
  n_items INTEGER NOT NULL, item_keys_json TEXT NOT NULL, judge TEXT NOT NULL, judge_location TEXT NOT NULL,
  endpoint TEXT, request_model TEXT, served_model TEXT, fingerprint TEXT, request_sha256 TEXT NOT NULL,
  response_sha256 TEXT, settings_sha256 TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
  finish_reason TEXT, outcome TEXT NOT NULL CHECK({_in('outcome', CALL_OUTCOMES)}), error TEXT,
  provenance_classes TEXT NOT NULL, code_revision TEXT, study_bundle_sha256 TEXT,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_judgments(
  judgment_id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT NOT NULL, instrument_digest TEXT NOT NULL,
  tier TEXT NOT NULL, variant TEXT NOT NULL DEFAULT 'primary', item_key TEXT NOT NULL, call_id TEXT,
  outcome TEXT NOT NULL CHECK({_in('outcome', JUDGMENT_OUTCOMES)}), verdict_json TEXT, error_code TEXT, error TEXT,
  created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fu_judgments_accepted ON fu_judgments(instrument_digest, tier, variant, item_key)
  WHERE outcome='accepted';
CREATE INDEX IF NOT EXISTS ix_fu_judgments_item ON fu_judgments(item_key, tier);
CREATE TABLE IF NOT EXISTS fu_status_observations(
  observation_id TEXT PRIMARY KEY, work_key TEXT NOT NULL,
  source TEXT NOT NULL CHECK({_in('source', STATUS_SOURCES)}),
  status TEXT NOT NULL CHECK({_in('status', OBSERVED_STATUSES)}),
  notice_type TEXT, notice_doi TEXT, notice_pmid TEXT, notice_date TEXT,
  date_precision TEXT CHECK({_in('date_precision', ('day', 'month', 'year'), nullable=True)}),
  covers_json TEXT NOT NULL, retrieved_at TEXT NOT NULL, snapshot_id TEXT NOT NULL, receipt_sha256 TEXT,
  detail_json TEXT NOT NULL DEFAULT '{{}}');
CREATE INDEX IF NOT EXISTS ix_fu_status_observations_work ON fu_status_observations(work_key, snapshot_id);
CREATE TABLE IF NOT EXISTS fu_work_status(
  work_key TEXT PRIMARY KEY, status TEXT NOT NULL CHECK({_in('status', STATUSES)}),
  reinstated INTEGER NOT NULL DEFAULT 0 CHECK(reinstated IN (0,1)), notice_doi TEXT, notice_date TEXT,
  date_precision TEXT, decided_by TEXT, sources_json TEXT NOT NULL, unknown_kinds_json TEXT NOT NULL DEFAULT '[]',
  checked_at TEXT NOT NULL, snapshot_id TEXT NOT NULL, rule_version TEXT NOT NULL, resolved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_frame(
  locus_id TEXT PRIMARY KEY, journal TEXT NOT NULL, doi TEXT NOT NULL, label TEXT NOT NULL, seq INTEGER NOT NULL,
  work_key TEXT, claim_id TEXT NOT NULL, sentence_sha256 TEXT NOT NULL, unit INTEGER NOT NULL, section TEXT,
  review_year INTEGER, review_first_online TEXT, topic TEXT, review_stratum TEXT,
  provenance_class TEXT NOT NULL CHECK({_in('provenance_class', PROVENANCE)}), identity TEXT NOT NULL,
  cited_inbody INTEGER NOT NULL, census_abs_category TEXT, census_ft_category TEXT,
  UNIQUE(journal, doi, label, seq));
CREATE INDEX IF NOT EXISTS ix_fu_frame_claim ON fu_frame(claim_id);
CREATE INDEX IF NOT EXISTS ix_fu_frame_work ON fu_frame(work_key);
CREATE TABLE IF NOT EXISTS fu_strata(
  stratum TEXT NOT NULL CHECK({_in('stratum', STRATA)}), locus_id TEXT NOT NULL REFERENCES fu_frame(locus_id),
  inclusion_probability REAL NOT NULL CHECK(inclusion_probability > 0 AND inclusion_probability <= 1),
  selection TEXT NOT NULL CHECK({_in('selection', SELECTIONS)}), cell TEXT,
  pilot INTEGER NOT NULL DEFAULT 0 CHECK(pilot IN (0,1)), pilot_probability REAL,
  PRIMARY KEY(stratum, locus_id));
CREATE TABLE IF NOT EXISTS fu_cocited(
  anchor_locus_id TEXT NOT NULL REFERENCES fu_frame(locus_id), locus_id TEXT NOT NULL REFERENCES fu_frame(locus_id),
  claim_id TEXT NOT NULL, PRIMARY KEY(anchor_locus_id, locus_id));
CREATE TABLE IF NOT EXISTS fu_stratum_sizes(
  stratum TEXT NOT NULL CHECK({_in('stratum', STRATA)}), journal TEXT NOT NULL, eligible INTEGER NOT NULL,
  selected INTEGER NOT NULL, pilot INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(stratum, journal));
CREATE TABLE IF NOT EXISTS fu_protocol_freeze(
  freeze_id TEXT PRIMARY KEY, protocol_sha256 TEXT NOT NULL, strata_sha256 TEXT NOT NULL,
  status_snapshot TEXT NOT NULL, parent_sha256 TEXT NOT NULL, estimands_json TEXT NOT NULL, sizes_json TEXT NOT NULL,
  frozen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_hotspots(
  work_key TEXT PRIMARY KEY, citing_reviews INTEGER NOT NULL, n INTEGER NOT NULL, k INTEGER NOT NULL,
  expected REAL NOT NULL, excess REAL NOT NULL, rho REAL NOT NULL, p_value REAL, q_value REAL,
  candidate INTEGER NOT NULL CHECK(candidate IN (0,1)), family_excluded INTEGER NOT NULL CHECK(family_excluded IN (0,1)),
  selected INTEGER NOT NULL CHECK(selected IN (0,1)), model TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_support(
  locus_id TEXT NOT NULL REFERENCES fu_frame(locus_id), tier TEXT NOT NULL CHECK({_in('tier', SUPPORT_TIERS)}),
  verdict TEXT CHECK({_in('verdict', SUPPORT_VERDICTS, nullable=True)}),
  assessable INTEGER NOT NULL CHECK(assessable IN (0,1)), reason TEXT CHECK({_in('reason', UNASSESSABLE, nullable=True)}),
  evidence_coverage TEXT, provenance_class TEXT, judgment_id INTEGER, evidence_id TEXT, quote TEXT,
  PRIMARY KEY(locus_id, tier),
  CHECK((assessable=1 AND verdict IS NOT NULL AND verdict!='abstain' AND reason IS NULL)
        OR (assessable=0 AND reason IS NOT NULL)));
CREATE TABLE IF NOT EXISTS fu_function(
  locus_id TEXT PRIMARY KEY REFERENCES fu_frame(locus_id),
  class TEXT CHECK({_in('class', FUNCTION_CLASSES, nullable=True)}),
  assessable INTEGER NOT NULL CHECK(assessable IN (0,1)), reason TEXT CHECK({_in('reason', UNASSESSABLE, nullable=True)}),
  quote TEXT, quote_field TEXT, judgment_id INTEGER,
  CHECK((assessable=1 AND class IS NOT NULL AND reason IS NULL) OR (assessable=0 AND class IS NULL AND reason IS NOT NULL)));
CREATE TABLE IF NOT EXISTS fu_dependency(
  locus_id TEXT PRIMARY KEY REFERENCES fu_frame(locus_id), claim_id TEXT NOT NULL, cocited INTEGER NOT NULL,
  cocited_supporting INTEGER NOT NULL, cocited_unassessable INTEGER NOT NULL,
  class TEXT NOT NULL CHECK({_in('class', DEPENDENCY_CLASSES)}), anchor_verdict TEXT, rule_version TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{{}}');
CREATE TABLE IF NOT EXISTS fu_numeric(
  locus_id TEXT PRIMARY KEY REFERENCES fu_frame(locus_id), parse_judgment_id INTEGER, epidemiological INTEGER,
  condition TEXT, metric TEXT, geography TEXT, year INTEGER, value REAL, value_text TEXT, unit TEXT, quote TEXT,
  table_source TEXT, class TEXT CHECK({_in('class', NUMERIC_CLASSES, nullable=True)}),
  assessable INTEGER NOT NULL CHECK(assessable IN (0,1)), reason TEXT CHECK({_in('reason', UNASSESSABLE, nullable=True)}),
  matched_json TEXT, rule_version TEXT,
  CHECK((assessable=1 AND class IS NOT NULL AND reason IS NULL) OR (assessable=0 AND class IS NULL AND reason IS NOT NULL)));
CREATE TABLE IF NOT EXISTS fu_citedby_edges(
  edge_id TEXT PRIMARY KEY, target_doi TEXT NOT NULL, target_journal TEXT, citing_key TEXT NOT NULL,
  citing_doi TEXT, citing_pmid TEXT, citing_pmcid TEXT, citing_openalex TEXT, citing_title TEXT, citing_year INTEGER,
  citing_first_online TEXT, source TEXT NOT NULL CHECK({_in('source', CITEDBY_SOURCES)}), request_id TEXT NOT NULL,
  retrieved_at TEXT NOT NULL, UNIQUE(target_doi, source, citing_key));
CREATE TABLE IF NOT EXISTS fu_secondgen_matches(
  anchor_locus_id TEXT NOT NULL REFERENCES fu_frame(locus_id), sentence_id TEXT NOT NULL, citing_key TEXT NOT NULL,
  rank INTEGER, score REAL, match TEXT CHECK({_in('match', MATCH_CLASSES, nullable=True)}),
  reliance TEXT CHECK({_in('reliance', RELIANCE_CLASSES, nullable=True)}),
  assessable INTEGER NOT NULL CHECK(assessable IN (0,1)), reason TEXT CHECK({_in('reason', UNASSESSABLE, nullable=True)}),
  quote TEXT, judgment_id INTEGER,
  inclusion_probability REAL NOT NULL CHECK(inclusion_probability > 0 AND inclusion_probability <= 1),
  PRIMARY KEY(anchor_locus_id, sentence_id),
  CHECK((assessable=1 AND match IS NOT NULL AND reliance IS NOT NULL AND reason IS NULL)
        OR (assessable=0 AND reason IS NOT NULL)));
CREATE TABLE IF NOT EXISTS fu_estimates(
  key TEXT PRIMARY KEY, value NOT NULL, estimand TEXT NOT NULL, journal TEXT, year INTEGER, stratum TEXT,
  numerator REAL, denominator REAL, units INTEGER, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fu_coverage(
  estimand TEXT NOT NULL, journal TEXT NOT NULL, year TEXT NOT NULL, stratum TEXT NOT NULL, state TEXT NOT NULL,
  reason TEXT NOT NULL, units INTEGER NOT NULL, weighted REAL,
  PRIMARY KEY(estimand, journal, year, stratum, state, reason));
{_append_only('fu_datasets', 'dataset registrations')}
{_append_only('fu_artifacts', 'artifacts')}
{_append_only('fu_requests', 'provider request receipts')}
{_append_only('fu_calls', 'judge call receipts')}
{_append_only('fu_judgments', 'judgments')}
{_append_only('fu_status_observations', 'status observations')}
{_append_only('fu_protocol_freeze', 'protocol freezes')}
"""

CORE_DICTIONARY: dict[str, dict[str, str]] = {
    "fu_meta": {"key": "metadata key", "value": "metadata value (JSON for structured values)"},
    "fu_dictionary": {"table_name": "table", "column_name": "column", "type": "declared type", "meaning": "meaning"},
    "fu_datasets": {
        "dataset_id": "<name>@<declared version>#<first 12 hex of the sha256>", "kind": "role of the dataset version",
        "name": "dataset name", "declared_version": "version the provider or operator declared",
        "sha256": "sha256 of the dataset bytes (a file, or the canonical JSON of a snapshot's rows)",
        "rows": "rows in the dataset version, when it has rows", "file_name": "file name without its directory",
        "detail_json": "further facts about the version (no credentials, no contact identity)",
        "registered_at": "time the branch registered the version"},
    "fu_artifacts": {
        "sha256": "sha256 of the uncompressed bytes", "kind": "judge-request, judge-response, provider-response, ...",
        "media_type": "media type of the bytes", "bytes": "uncompressed size", "codec": "compression of body",
        "body": "compressed bytes", "created_at": "time stored"},
    "fu_requests": {
        "request_id": "request id (digest of provider, operation, url, params and retrieval time)",
        "provider": "crossref, pubmed, europe-pmc, openalex, ...", "operation": "what the request was for",
        "url": "address without query string", "params_json": "query parameters as sent, minus contact and key fields",
        "http_status": "HTTP status, when a response came", "outcome": "ok, not-found, unavailable or error",
        "response_sha256": "stored response body (fu_artifacts)", "error": "failure class, never response text",
        "retrieved_at": "time of the response"},
    "fu_calls": {
        "call_id": "receipt id", "instrument_id": "registry id (name@version)",
        "instrument_digest": "registry digest of the bundle", "tier": "instrument tier",
        "variant": "primary or a predeclared variant (replicate)", "kind": "first call or repair call",
        "n_items": "items in the call", "item_keys_json": "item keys in call order", "judge": "judge name",
        "judge_location": "on-prem or external", "endpoint": "judge endpoint without query or credentials",
        "request_model": "model requested", "served_model": "model the response named",
        "fingerprint": "system_fingerprint of the response", "request_sha256": "stored request body",
        "response_sha256": "stored response body", "settings_sha256": "sha256 of the request settings",
        "prompt_tokens": "provider-reported prompt tokens", "completion_tokens": "provider-reported completion tokens",
        "finish_reason": "finish reason", "outcome": "completed, failed, refused or model-drift",
        "error": "failure class", "provenance_classes": "JSON list of the provenance classes of the items sent",
        "code_revision": "code revision of the engine", "study_bundle_sha256": "study bundle the call ran under",
        "started_at": "call start", "finished_at": "call end"},
    "fu_judgments": {
        "judgment_id": "append-only row id", "instrument_id": "registry id", "instrument_digest": "registry digest",
        "tier": "instrument tier", "variant": "primary or replicate", "item_key": "the item's key (for example a locus id)",
        "call_id": "receipt of the call", "outcome": "accepted, rejected or blocked",
        "verdict_json": "validated output item", "error_code": "rejection or block code", "error": "message",
        "created_at": "time recorded"},
    "fu_status_observations": {
        "observation_id": "digest of (work_key, source, status, notice DOI, notice date, retrieved_at)",
        "work_key": "cited work of the parent census", "source": "status source (Crossref with its update origin, "
        "the Retraction Watch CSV snapshot, PubMed, Europe PMC)",
        "status": "status this source reports; none = the source answered without a notice of the kinds it covers; "
                  "unknown = the source could not answer",
        "notice_type": "the source's own label of the notice", "notice_doi": "DOI of the notice",
        "notice_pmid": "PMID of the notice", "notice_date": "date of the notice as the source gives it (ISO)",
        "date_precision": "day, month or year", "covers_json": "notice kinds this source can report",
        "retrieved_at": "time the source answered", "snapshot_id": "status snapshot (dataset version) of the row",
        "receipt_sha256": "stored provider response or CSV snapshot digest", "detail_json": "source details"},
    "fu_work_status": {
        "work_key": "cited work of the parent census", "status": "retracted, expression-of-concern, corrected, none "
        "or unknown (status-resolution rule)", "reinstated": "1 when a later reinstatement reverses a retraction",
        "notice_doi": "DOI of the deciding notice", "notice_date": "date of the deciding notice",
        "date_precision": "precision of notice_date", "decided_by": "source of the deciding notice",
        "sources_json": "sources that answered, with their retrieval times",
        "unknown_kinds_json": "notice kinds no answering source covers", "checked_at": "latest answer considered",
        "snapshot_id": "status snapshot resolved", "rule_version": "status-resolution rule version",
        "resolved_at": "time resolved"},
    "fu_frame": {
        "locus_id": "follow-up locus id (digest of journal, doi, label, seq)", "journal": "journal of the review",
        "doi": "review DOI", "label": "bibliography label", "seq": "document order of the locus",
        "work_key": "cited work (NULL: identity unresolved)", "claim_id": "citing sentence id (digest of journal, doi, "
        "unit and sentence digest): loci of one claim share it", "sentence_sha256": "sha256 of the citing sentence",
        "unit": "citing unit index", "section": "section trail", "review_year": "first-online year of the review",
        "review_first_online": "first-online date of the review", "topic": "review topic (parent census)",
        "review_stratum": "publisher article-type stratum of the review",
        "provenance_class": "public, entitled or unavailable (the review's citing text)",
        "identity": "identity class of the entry", "cited_inbody": "1 when the entry is cited in the body",
        "census_abs_category": "parent census abstract-tier category of the entry",
        "census_ft_category": "parent census full-text category of the entry"},
    "fu_strata": {
        "stratum": "S0, S1, S2, S3 or co-cited", "locus_id": "frame locus",
        "inclusion_probability": "realised inclusion probability of the locus in this stratum",
        "selection": "census (every eligible unit) or sampled", "cell": "sampling cell (journal x year x topic for S0)",
        "pilot": "1 when the locus is in the pilot subsample", "pilot_probability": "inclusion probability in the pilot"},
    "fu_cocited": {"anchor_locus_id": "an S1 or S2 locus", "locus_id": "another locus of the same citing sentence",
                   "claim_id": "the shared citing sentence"},
    "fu_stratum_sizes": {"stratum": "stratum", "journal": "journal (* for all)", "eligible": "eligible units",
                         "selected": "selected units", "pilot": "units in the pilot subsample"},
    "fu_protocol_freeze": {
        "freeze_id": "freeze id", "protocol_sha256": "sha256 of the protocol", "strata_sha256": "digest of the strata",
        "status_snapshot": "status snapshot the strata used", "parent_sha256": "parent census digest at freeze time",
        "estimands_json": "predeclared estimands with numerators and denominators", "sizes_json": "stratum sizes",
        "frozen_at": "time frozen (before any follow-up judging)"},
    "fu_hotspots": {
        "work_key": "cited work", "citing_reviews": "distinct citing reviews", "n": "classifiable citing entries",
        "k": "not-supported citing entries", "expected": "expected k under the journal x year base",
        "excess": "k minus expected", "rho": "overdispersion of the beta-binomial null",
        "p_value": "upper-tail beta-binomial p-value", "q_value": "Benjamini-Hochberg q-value over candidates",
        "candidate": "1 when the work meets the citing-review minimum", "family_excluded": "1 for an excluded family",
        "selected": "1 when the work enters S2", "model": "test specification version"},
    "fu_support": {
        "locus_id": "frame locus", "tier": "abstract or full-text", "verdict": "citation-support@2 verdict",
        "assessable": "1 when the verdict is assessable", "reason": "why not assessable",
        "evidence_coverage": "complete, excerpt or missing", "provenance_class": "public or entitled evidence",
        "judgment_id": "fu_judgments row", "evidence_id": "evidence the verdict used", "quote": "first exact quote"},
    "fu_function": {
        "locus_id": "frame locus", "class": "citation-function@1 class", "assessable": "1 when classified",
        "reason": "why not assessable", "quote": "exact quote", "quote_field": "field the quote comes from",
        "judgment_id": "fu_judgments row"},
    "fu_dependency": {
        "locus_id": "S1 or S2 locus", "claim_id": "citing sentence", "cocited": "other cited works in the sentence",
        "cocited_supporting": "co-cited works supporting the claim at full text",
        "cocited_unassessable": "co-cited works without an assessable full-text verdict",
        "class": "sole, essential, redundant or undetermined", "anchor_verdict": "full-text verdict of the locus's own work",
        "rule_version": "aggregation rule version", "detail_json": "co-cited loci with their verdicts"},
    "fu_numeric": {
        "locus_id": "S3 locus", "parse_judgment_id": "numeric-check@1 judgment", "epidemiological": "1 when the "
        "claim states a population statistic", "condition": "condition as parsed", "metric": "metric as parsed",
        "geography": "geography as parsed", "year": "reference year as parsed", "value": "number as parsed",
        "value_text": "number as written", "unit": "unit as parsed", "quote": "exact quote",
        "table_source": "table source dataset id", "class": "deterministic comparison class",
        "assessable": "1 when compared", "reason": "why not assessable", "matched_json": "table cells compared",
        "rule_version": "comparison rule version"},
    "fu_citedby_edges": {
        "edge_id": "edge id", "target_doi": "cited article (an affected review), DOI in lower case",
        "target_journal": "journal of the cited article", "citing_key": "doi:, pmid: or openalex: key of the citing work",
        "citing_doi": "citing DOI", "citing_pmid": "citing PMID", "citing_pmcid": "citing PMCID",
        "citing_openalex": "OpenAlex work id", "citing_title": "citing title", "citing_year": "citing year",
        "citing_first_online": "first-online date of the citing work", "source": "europe-pmc or openalex",
        "request_id": "provider request (fu_requests)", "retrieved_at": "time retrieved"},
    "fu_secondgen_matches": {
        "anchor_locus_id": "S1 locus whose claim is followed", "sentence_id": "second-generation citing sentence",
        "citing_key": "citing work", "rank": "BM25 rank", "score": "BM25 score", "match": "claim-match@1 match class",
        "reliance": "claim-match@1 reliance class", "assessable": "1 when judged", "reason": "why not assessable",
        "quote": "exact quote from the candidate", "judgment_id": "fu_judgments row",
        "inclusion_probability": "realised inclusion probability of the sentence"},
    "fu_estimates": {
        "key": "estimate key", "value": "value", "estimand": "estimand id and definition", "journal": "journal or NULL",
        "year": "year or NULL", "stratum": "stratum or NULL", "numerator": "weighted numerator",
        "denominator": "weighted denominator", "units": "unweighted units in the denominator",
        "detail_json": "derivation (weights, B, seed, interval method)"},
    "fu_coverage": {
        "estimand": "estimand id", "journal": "journal (* for all)", "year": "year (* for all)",
        "stratum": "stratum (* for all)", "state": "assessable or unassessable", "reason": "reason (- when assessable)",
        "units": "units", "weighted": "weighted units"},
}


def ddl() -> str:
    from . import followup_schema_a, followup_schema_b
    return CORE_DDL + followup_schema_a.DDL + followup_schema_b.DDL


def dictionary() -> dict[str, dict[str, str]]:
    from . import followup_schema_a, followup_schema_b
    merged: dict[str, dict[str, str]] = {}
    for part in (CORE_DICTIONARY, followup_schema_a.DICTIONARY, followup_schema_b.DICTIONARY):
        clash = set(merged) & set(part)
        if clash:
            raise ValidationError(f"follow-up tables declared twice: {', '.join(sorted(clash))}")
        merged.update(part)
    return merged


def tables(connection: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {row[0] for row in connection.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def create(connection: sqlite3.Connection) -> None:
    """Create every follow-up table, the data dictionary and the schema version in ``main``."""
    connection.executescript(ddl())
    write_dictionary(connection)
    connection.execute("INSERT OR IGNORE INTO fu_meta VALUES('format', ?)", (FORMAT,))
    connection.execute("INSERT OR IGNORE INTO fu_meta VALUES('schemaVersion', ?)", (str(SCHEMA_VERSION),))


def write_dictionary(connection: sqlite3.Connection) -> None:
    rows = []
    for table, meanings in sorted(dictionary().items()):
        declared = {r[1]: r[2] for r in connection.execute(f"PRAGMA main.table_info({table})")}
        if not declared:
            raise ValidationError(f"follow-up table is missing: {table}")
        missing, extra = set(declared) - set(meanings), set(meanings) - set(declared)
        if missing or extra:
            raise ValidationError(f"data dictionary of {table} differs from its columns: missing="
                                  f"{sorted(missing)}, extra={sorted(extra)}")
        rows.extend((table, column, declared[column] or "", meanings[column]) for column in declared)
    connection.executemany("INSERT OR REPLACE INTO fu_dictionary VALUES(?,?,?,?)", rows)


def check(connection: sqlite3.Connection) -> list[str]:
    """Problems with the branch schema: version, unknown or missing tables, dictionary drift."""
    problems = []
    present = tables(connection)
    if "fu_meta" not in present:
        return ["not a follow-up branch database (no fu_meta)"]
    meta = dict(connection.execute("SELECT key, value FROM fu_meta").fetchall())
    if meta.get("format") != FORMAT:
        problems.append(f"unexpected branch format: {meta.get('format')!r}")
    if meta.get("schemaVersion") != str(SCHEMA_VERSION):
        problems.append(f"unsupported branch schema version: {meta.get('schemaVersion')!r}")
    declared = dictionary()
    unknown = present - set(declared)
    missing = set(declared) - present
    if unknown:
        problems.append("tables outside the follow-up schema: " + ", ".join(sorted(unknown)))
    if missing:
        problems.append("follow-up tables missing: " + ", ".join(sorted(missing)))
    for table in sorted(set(declared) & present):
        columns = {r[1] for r in connection.execute(f"PRAGMA main.table_info({table})")}
        if columns != set(declared[table]):
            problems.append(f"columns of {table} differ from the data dictionary")
    return problems


def migrate(connection: sqlite3.Connection, steps: Mapping[int, str] | None = None) -> int:
    """Apply additive migration steps above the stored schema version; returns the version reached.

    ``steps`` maps a target version to its SQL (ALTER TABLE ... ADD COLUMN, CREATE TABLE IF NOT EXISTS). Version 1 has
    none; a later version adds its step here, bumps SCHEMA_VERSION and extends the dictionary in the same change.
    """
    stored = int(dict(connection.execute("SELECT key, value FROM fu_meta").fetchall()).get("schemaVersion", "0"))
    if stored > SCHEMA_VERSION:
        raise ValidationError("the branch was written by a newer follow-up schema; use a newer engine")
    for version in sorted(v for v in (steps or {}) if stored < v <= SCHEMA_VERSION):
        connection.executescript((steps or {})[version])
        connection.execute("INSERT OR REPLACE INTO fu_meta VALUES('schemaVersion', ?)", (str(version),))
        stored = version
    write_dictionary(connection)
    return stored
