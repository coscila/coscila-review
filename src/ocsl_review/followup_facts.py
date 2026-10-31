# SPDX-License-Identifier: Apache-2.0
"""Facts v1 of a follow-up branch: a closed ``facts.sqlite3`` and ``facts.json`` (schemaVersion 1).

``facts.sqlite3`` is the branch's follow-up tables copied with ``VACUUM INTO`` and closed (rollback journal,
read-only), without any text that could be entitled (stored request and response bodies, evidence units, quotes,
parsed verdicts, section trails and sentence text are removed), plus materialized tables of the parent census:

- ``fu_census_counts``: per journal, year and status, the parent's in-body identity-resolved loci (the denominator of
  the compromised share; ``no-status-row`` for a work without a status row);
- ``fu_census_flags``: the parent's retraction flag (what the census judge was shown) against the resolved status of
  every cited work (a diagnostic, never a status source);
- ``fact_inputs``: the parent, branch and protocol digests, the freeze, the instrument digests, the dataset versions,
  the code revision and the protocol's bootstrap replicates and seeds.

Every fact is one scalar SQL query over the closed file (at most five seconds), with the file's sha256, the inputs and
the code revision; keys are ``followup.<family>.<...>`` (status, census, strata, support, function, dependency,
numeric, citedby, secondgen, calls, requests, estimates from ``fu_estimates``, coverage from ``fu_coverage``).
``followup verify-facts`` checks an export with ``corpus_facts.verify_export``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import scores_db
from .corpus_facts import FACT_KEY, slug, verify_export
from .errors import ValidationError
from .followup_branch import FollowupBranch
from .followup_schema import (CITEDBY_SOURCES, DEPENDENCY_CLASSES, FUNCTION_CLASSES, MATCH_CLASSES, NUMERIC_CLASSES,
                              RELIANCE_CLASSES, STATUSES, STRATA, SUPPORT_TIERS, SUPPORT_VERDICTS, UNASSESSABLE)

FORMAT = "ocsl-followup-facts-v1"
INTERPRETATION = "Model measurements on predeclared instruments with coverage, not human-labelled truth"
# Columns that can hold text of a citing review or a cited work (entitled or not): never part of the facts file.
TEXT_COLUMNS = frozenset({"text", "quote", "body", "verdict_json", "support_json", "evidence_json", "value_text",
                          "marker", "section", "sentence", "error"})
KEEP_COLUMNS = {"fu_requests": {"error"}, "fu_calls": {"error"}}
COVERAGE_STATES = ("complete", "truncated", "unavailable", "error", "no-identifier")
DECIDING_KINDS = ("retraction", "expression-of-concern", "correction")


def _q(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def key_part(value: Any) -> str:
    text = str(value if value is not None else "none")
    if text in ("*", ""):
        return "all"
    return slug(text)


def _text_columns(connection: sqlite3.Connection, table: str) -> list[tuple[str, bool]]:
    keep = KEEP_COLUMNS.get(table, set())
    return [(row[1], bool(row[3])) for row in connection.execute(f"PRAGMA table_info({table})")
            if row[1] in TEXT_COLUMNS and row[1] not in keep]


def redact(connection: sqlite3.Connection) -> dict[str, int]:
    """Remove every text that could come from a citing review or a cited work from the facts copy."""
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        connection.execute(f"DROP TRIGGER {name}")
    counts = {"fu_artifacts": connection.execute("DELETE FROM fu_artifacts").rowcount,
              "fu_evidence_units": connection.execute("DELETE FROM fu_evidence_units").rowcount}
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE "
                                                   "'fu\\_%' ESCAPE '\\'")]
    for table in tables:
        for column, required in _text_columns(connection, table):
            replacement = "''" if required else "NULL"
            if table == "fu_alerts" and column in ("support_json", "evidence_json"):
                replacement = "'{}'"
            condition = f"{column} IS NOT NULL" + ("" if replacement == "NULL" else f" AND {column} != {replacement}")
            changed = connection.execute(f"UPDATE {table} SET {column}={replacement} WHERE {condition}").rowcount
            if changed:
                counts[f"{table}.{column}"] = changed
    return counts


def census_counts(connection: sqlite3.Connection, identities: Iterable[str]) -> list[tuple]:
    identities = list(identities)
    marks = ",".join("?" for _ in identities)
    return connection.execute(
        "SELECT r.journal, r.year, COALESCE(s.status, 'no-status-row') AS status, COUNT(*), COUNT(DISTINCT r.doi) "
        "FROM census.loci l JOIN census.citations c ON c.journal=l.journal AND c.doi=l.doi AND c.label=l.label "
        "JOIN census.reviews r ON r.journal=l.journal AND r.doi=l.doi LEFT JOIN fu_work_status s "
        "ON s.work_key=c.cited_work_key WHERE c.cited_inbody=1 AND c.cited_work_key IS NOT NULL AND c.identity IN "
        f"({marks}) GROUP BY r.journal, r.year, status ORDER BY r.journal, r.year, status", identities).fetchall()


def census_flags(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT COALESCE(w.retracted, 0) AS flag, COALESCE(s.status, 'no-status-row') AS status, COUNT(*) FROM "
        "(SELECT DISTINCT cited_work_key AS work_key FROM census.citations WHERE cited_work_key IS NOT NULL) k "
        "LEFT JOIN census.works w ON w.work_key=k.work_key LEFT JOIN fu_work_status s ON s.work_key=k.work_key "
        "GROUP BY flag, status ORDER BY flag, status").fetchall()


# ----------------------------------------------------------------------------- fact definitions
class Facts:
    def __init__(self) -> None:
        self.queries: dict[str, tuple[str, str, Mapping[str, Any] | None]] = {}

    def add(self, key: str, sql: str, method: str, derivation: Mapping[str, Any] | None = None) -> None:
        if not FACT_KEY.fullmatch(key):
            raise ValidationError(f"invalid fact key: {key}")
        if key in self.queries:
            raise ValidationError(f"fact key defined twice: {key}")
        self.queries[key] = (sql, method, derivation)


def _distinct(connection: sqlite3.Connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(sql).fetchall()]


def define_facts(connection: sqlite3.Connection) -> Facts:
    facts = Facts()
    count = "SELECT COUNT(*) FROM "
    # status
    facts.add("followup.status.works", count + "fu_work_status", "cited works with a status row")
    for status in STATUSES:
        facts.add(f"followup.status.{status}", count + f"fu_work_status WHERE status={_q(status)}",
                  f"cited works whose resolved status is {status} (status-resolution@1)")
    facts.add("followup.status.reinstated", count + "fu_work_status WHERE reinstated=1",
              "cited works whose retraction a later reinstatement reverses")
    for status in ("retracted", "expression-of-concern", "corrected"):
        facts.add(f"followup.status.{status}.dated", count + f"fu_work_status WHERE status={_q(status)} AND "
                  "notice_date IS NOT NULL", f"{status} works whose deciding notice has a date")
    for (source,) in _distinct(connection, "SELECT DISTINCT decided_by FROM fu_work_status WHERE decided_by IS NOT "
                                           "NULL ORDER BY decided_by"):
        facts.add(f"followup.status.decided-by.{key_part(source)}", count + f"fu_work_status WHERE decided_by="
                  f"{_q(source)}", f"cited works whose deciding notice comes from {source}")
    for kind in DECIDING_KINDS:
        facts.add(f"followup.status.unknown-kind.{kind}", count + "fu_work_status WHERE unknown_kinds_json LIKE "
                  + _q(f'%"{kind}"%'), f"cited works for which no answering source covers {kind} notices")
    for source, status in _distinct(connection, "SELECT DISTINCT source, status FROM fu_status_observations ORDER BY "
                                                "source, status"):
        facts.add(f"followup.status.observations.{key_part(source)}.{status}",
                  count + f"fu_status_observations WHERE source={_q(source)} AND status={_q(status)}",
                  f"status observations of {source} reporting {status}")
    for flag, status in _distinct(connection, "SELECT census_flag, status FROM fu_census_flags ORDER BY census_flag, "
                                              "status"):
        facts.add(f"followup.status.census-flag.{'flagged' if flag else 'unflagged'}.{key_part(status)}",
                  f"SELECT works FROM fu_census_flags WHERE census_flag={int(flag)} AND status={_q(status)}",
                  "cited works by the parent's retraction flag and the resolved status (diagnostic)")
    facts.add("followup.status.cited-works-without-status-row",
              "SELECT COALESCE(SUM(works), 0) FROM fu_census_flags WHERE status='no-status-row'",
              "cited works of the parent without a status row (gate: 0)")
    # census denominators
    for (journal,) in _distinct(connection, "SELECT DISTINCT journal FROM fu_census_counts ORDER BY journal"):
        j = key_part(journal)
        where = f"journal={_q(journal)}"
        facts.add(f"followup.census.{j}.loci", f"SELECT SUM(loci) FROM fu_census_counts WHERE {where}",
                  f"{journal}: in-body identity-resolved loci of the parent census")
        for (status,) in _distinct(connection, f"SELECT DISTINCT status FROM fu_census_counts WHERE {where} "
                                               "ORDER BY status"):
            facts.add(f"followup.census.{j}.{key_part(status)}.loci",
                      f"SELECT SUM(loci) FROM fu_census_counts WHERE {where} AND status={_q(status)}",
                      f"{journal}: in-body identity-resolved loci citing a work whose status is {status}")
        for year, status in _distinct(connection, f"SELECT DISTINCT year, status FROM fu_census_counts WHERE {where} "
                                                  "ORDER BY year, status"):
            year_sql = "year IS NULL" if year is None else f"year={int(year)}"
            facts.add(f"followup.census.{j}.{key_part(year)}.{key_part(status)}.loci",
                      f"SELECT SUM(loci) FROM fu_census_counts WHERE {where} AND {year_sql} AND status={_q(status)}",
                      f"{journal}, {year}: in-body identity-resolved loci citing a work whose status is {status}")
    # strata
    facts.add("followup.strata.frame", count + "fu_frame", "loci in the follow-up frame")
    facts.add("followup.strata.freezes", count + "fu_protocol_freeze", "protocol freezes")
    for stratum in STRATA:
        s = key_part(stratum)
        facts.add(f"followup.strata.{s}.units", count + f"fu_strata WHERE stratum={_q(stratum)}",
                  f"loci in stratum {stratum}")
        facts.add(f"followup.strata.{s}.pilot", count + f"fu_strata WHERE stratum={_q(stratum)} AND pilot=1",
                  f"loci of stratum {stratum} in the pilot subsample")
    for stratum, journal in _distinct(connection, "SELECT stratum, journal FROM fu_stratum_sizes ORDER BY stratum, "
                                                  "journal"):
        for column in ("eligible", "selected"):
            facts.add(f"followup.strata.{key_part(stratum)}.{key_part(journal)}.{column}",
                      f"SELECT {column} FROM fu_stratum_sizes WHERE stratum={_q(stratum)} AND journal={_q(journal)}",
                      f"{column} units of stratum {stratum} ({journal})")
    # support
    verdicts = [verdict for verdict in SUPPORT_VERDICTS if verdict != "abstain"]
    for tier in SUPPORT_TIERS:
        base = f"fu_support WHERE tier={_q(tier)}"
        facts.add(f"followup.support.{tier}.units", count + base, f"{tier} tier: units with a support row")
        facts.add(f"followup.support.{tier}.assessable", count + base + " AND assessable=1",
                  f"{tier} tier: units with an assessable citation-support@2 verdict")
        for verdict in verdicts:
            facts.add(f"followup.support.{tier}.verdict.{verdict}", count + base + f" AND assessable=1 AND "
                      f"verdict={_q(verdict)}", f"{tier} tier: units with verdict {verdict}")
        for reason in UNASSESSABLE:
            facts.add(f"followup.support.{tier}.reason.{reason}", count + base + f" AND assessable=0 AND "
                      f"reason={_q(reason)}", f"{tier} tier: units not assessable ({reason})")
        for klass in ("public", "entitled"):
            facts.add(f"followup.support.{tier}.provenance.{klass}", count + base + f" AND assessable=1 AND "
                      f"provenance_class={_q(klass)}", f"{tier} tier: assessable units with {klass} provenance")
        for stratum in STRATA:
            join = (f"fu_support u JOIN fu_strata s ON s.locus_id=u.locus_id WHERE u.tier={_q(tier)} AND "
                    f"s.stratum={_q(stratum)}")
            facts.add(f"followup.support.{tier}.{key_part(stratum)}.units", count + join,
                      f"{tier} tier, stratum {stratum}: units")
            facts.add(f"followup.support.{tier}.{key_part(stratum)}.assessable", count + join + " AND u.assessable=1",
                      f"{tier} tier, stratum {stratum}: assessable units")
            for verdict in verdicts:
                facts.add(f"followup.support.{tier}.{key_part(stratum)}.verdict.{verdict}",
                          count + join + f" AND u.assessable=1 AND u.verdict={_q(verdict)}",
                          f"{tier} tier, stratum {stratum}: units with verdict {verdict}")
    # function
    facts.add("followup.function.units", count + "fu_function", "units with a citation-function@1 row")
    facts.add("followup.function.assessable", count + "fu_function WHERE assessable=1", "classified units")
    for klass in FUNCTION_CLASSES:
        facts.add(f"followup.function.class.{klass}", count + f"fu_function WHERE class={_q(klass)}",
                  f"units classified {klass}")
        for stratum in ("S0", "S1", "S2", "S3"):
            facts.add(f"followup.function.{key_part(stratum)}.class.{klass}",
                      count + f"fu_function f JOIN fu_strata s ON s.locus_id=f.locus_id WHERE s.stratum={_q(stratum)} "
                      f"AND f.class={_q(klass)}", f"stratum {stratum}: units classified {klass}")
    for reason in UNASSESSABLE:
        facts.add(f"followup.function.reason.{reason}", count + f"fu_function WHERE assessable=0 AND "
                  f"reason={_q(reason)}", f"units not classified ({reason})")
    # dependency
    facts.add("followup.dependency.anchors", count + "fu_dependency", "S1 and S2 anchors with a dependency row")
    facts.add("followup.dependency.with-anchor-verdict", count + "fu_dependency WHERE anchor_verdict IS NOT NULL",
              "anchors with an assessable full-text verdict of their own")
    for klass in DEPENDENCY_CLASSES:
        facts.add(f"followup.dependency.class.{klass}", count + f"fu_dependency WHERE class={_q(klass)}",
                  f"anchors whose dependency is {klass} (dependency-aggregation@1)")
        for stratum in ("S1", "S2"):
            facts.add(f"followup.dependency.{key_part(stratum)}.class.{klass}",
                      count + f"fu_dependency d JOIN fu_strata s ON s.locus_id=d.locus_id WHERE "
                      f"s.stratum={_q(stratum)} AND d.class={_q(klass)}", f"stratum {stratum}: dependency {klass}")
    # numeric
    facts.add("followup.numeric.units", count + "fu_numeric", "S3 units with a numeric row")
    for klass in NUMERIC_CLASSES:
        facts.add(f"followup.numeric.class.{klass}", count + f"fu_numeric WHERE class={_q(klass)}",
                  f"S3 units of numeric class {klass}")
    for reason in UNASSESSABLE:
        facts.add(f"followup.numeric.reason.{reason}", count + f"fu_numeric WHERE assessable=0 AND "
                  f"reason={_q(reason)}", f"S3 units not compared ({reason})")
    # cited-by
    facts.add("followup.citedby.targets", "SELECT COUNT(DISTINCT target_doi) FROM fu_citedby_coverage",
              "reviews whose citing works were traversed")
    facts.add("followup.citedby.edges", count + "fu_citedby_edges", "cited-by edges over all sources")
    facts.add("followup.citedby.citing-works", "SELECT COUNT(*) FROM (SELECT DISTINCT target_doi, citing_key FROM "
              "fu_citedby_edges)", "distinct citing works per target, united across sources")
    for source in CITEDBY_SOURCES:
        facts.add(f"followup.citedby.{source}.edges", count + f"fu_citedby_edges WHERE source={_q(source)}",
                  f"cited-by edges from {source}")
        for state in COVERAGE_STATES:
            facts.add(f"followup.citedby.{source}.{state}", count + f"fu_citedby_coverage WHERE source={_q(source)} "
                      f"AND state={_q(state)}", f"targets whose {source} traversal is {state}")
    # second generation
    facts.add("followup.secondgen.sentences", count + "fu_secondgen_matches", "second-generation candidate sentences")
    facts.add("followup.secondgen.judged", count + "fu_secondgen_matches WHERE assessable=1",
              "second-generation sentences judged")
    facts.add("followup.secondgen.confirmed", count + "fu_secondgen_matches WHERE assessable=1 AND "
              "match='same-claim' AND reliance='cites-review-for-claim'",
              "later sentences restating the claim on the review's authority")
    for klass in MATCH_CLASSES:
        facts.add(f"followup.secondgen.match.{klass}", count + f"fu_secondgen_matches WHERE match={_q(klass)}",
                  f"second-generation sentences matched as {klass}")
    for klass in RELIANCE_CLASSES:
        facts.add(f"followup.secondgen.reliance.{klass}", count + f"fu_secondgen_matches WHERE reliance={_q(klass)}",
                  f"second-generation sentences with reliance {klass}")
    for reason in UNASSESSABLE:
        facts.add(f"followup.secondgen.reason.{reason}", count + f"fu_secondgen_matches WHERE assessable=0 AND "
                  f"reason={_q(reason)}", f"second-generation sentences not judged ({reason})")
    # judge calls and provider requests
    facts.add("followup.calls.total", count + "fu_calls", "judge call receipts")
    facts.add("followup.calls.with-fingerprint", count + "fu_calls WHERE fingerprint IS NOT NULL",
              "judge call receipts carrying a fingerprint")
    facts.add("followup.calls.entitled-to-external", count + "fu_calls WHERE provenance_classes LIKE '%\"entitled\"%' "
              "AND judge_location!='on-prem'", "calls that sent entitled text to a judge not on premises (gate: 0)")
    for instrument_id, tier, variant in _distinct(connection, "SELECT DISTINCT instrument_id, tier, variant FROM "
                                                              "fu_calls ORDER BY instrument_id, tier, variant"):
        prefix = f"followup.calls.{key_part(instrument_id)}.{key_part(tier)}.{key_part(variant)}"
        where = f"instrument_id={_q(instrument_id)} AND tier={_q(tier)} AND variant={_q(variant)}"
        label = f"{instrument_id} {tier} ({variant})"
        for name, condition in (("calls", "kind='first'"), ("repair-calls", "kind='repair'"),
                                ("failed", "outcome='failed'"), ("model-drift", "outcome='model-drift'"),
                                ("with-fingerprint", "fingerprint IS NOT NULL")):
            facts.add(f"{prefix}.{name}", count + f"fu_calls WHERE {where} AND {condition}", f"{label}: {name}")
        for name in ("prompt_tokens", "completion_tokens"):
            facts.add(f"{prefix}.{name.replace('_', '-')}", f"SELECT SUM({name}) FROM fu_calls WHERE {where}",
                      f"{label}: provider-reported {name.replace('_', ' ')}")
        decided = f"fu_judgments j WHERE j.{where.replace(' AND ', ' AND j.')}"
        facts.add(f"{prefix}.accepted-items", "SELECT COUNT(DISTINCT j.item_key) FROM " + decided +
                  " AND j.outcome='accepted'", f"{label}: items with an accepted output")
        facts.add(f"{prefix}.invalid-items", "SELECT COUNT(DISTINCT j.item_key) FROM " + decided +
                  " AND j.outcome='rejected' AND NOT EXISTS (SELECT 1 FROM fu_judgments a WHERE "
                  "a.instrument_digest=j.instrument_digest AND a.tier=j.tier AND a.variant=j.variant AND "
                  "a.item_key=j.item_key AND a.outcome='accepted')", f"{label}: items rejected after repair")
    for provider, outcome in _distinct(connection, "SELECT DISTINCT provider, outcome FROM fu_requests ORDER BY "
                                                   "provider, outcome"):
        facts.add(f"followup.requests.{key_part(provider)}.{outcome}",
                  count + f"fu_requests WHERE provider={_q(provider)} AND outcome={_q(outcome)}",
                  f"provider requests to {provider} with outcome {outcome}")
    # estimates and coverage of the analysis track
    for key, estimand, detail in _distinct(connection, "SELECT key, estimand, detail_json FROM fu_estimates "
                                                       "ORDER BY key"):
        fact = key if str(key).startswith("followup.estimates.") else "followup.estimates." + str(key)
        if not FACT_KEY.fullmatch(fact):
            fact = "followup.estimates." + ".".join(key_part(part) for part in str(key).split("."))
        try:
            derivation = json.loads(detail) if detail else None
        except ValueError:
            derivation = None
        facts.add(fact, f"SELECT value FROM fu_estimates WHERE key={_q(key)}", str(estimand),
                  derivation if isinstance(derivation, dict) else None)
    for row in _distinct(connection, "SELECT estimand, journal, year, stratum, state, reason FROM fu_coverage "
                                     "ORDER BY estimand, journal, year, stratum, state, reason"):
        estimand, journal, year, stratum, state, reason = row
        prefix = "followup.coverage." + ".".join(key_part(part) for part in row)
        where = " AND ".join(f"{column}={_q(value)}" for column, value in
                             zip(("estimand", "journal", "year", "stratum", "state", "reason"), row))
        facts.add(prefix + ".units", f"SELECT units FROM fu_coverage WHERE {where}",
                  f"coverage of {estimand}: {state} units ({reason})")
        facts.add(prefix + ".weighted", f"SELECT weighted FROM fu_coverage WHERE {where}",
                  f"coverage of {estimand}: {state} units weighted by inverse inclusion probability ({reason})")
    return facts


# ----------------------------------------------------------------------------- export
def _revision() -> str:
    from .census_runner import code_revision
    return code_revision()


def export_facts(database: Path, output: Path, *, code_revision: str | None = None) -> dict[str, Any]:
    branch = FollowupBranch(Path(database))
    output = Path(output)
    target = output / "facts.sqlite3"
    if output.exists() and not output.is_dir():
        raise ValidationError("the facts output is not a directory; export into a new directory")
    if target.exists() or (output / "facts.json").exists():
        raise ValidationError("a facts export already exists there; export into a new directory")
    report = branch.verify(deep=True)
    if not report["ok"]:
        raise ValidationError("the follow-up branch does not verify: " + "; ".join(report["problems"]))
    protocol = branch.protocol() or {}
    meta = branch.meta()
    identities = ((protocol.get("strata") or {}).get("S1") or {}).get("identities") or ["resolved",
                                                                                         "resolved_unverified"]
    output.mkdir(parents=True, exist_ok=True)
    with closing(branch.connect()) as connection:
        counts = census_counts(connection, identities)
        flags = census_flags(connection)
        freeze = connection.execute("SELECT freeze_id FROM fu_protocol_freeze ORDER BY frozen_at DESC LIMIT 1"
                                    ).fetchone()
        digests: dict[str, list[str]] = {}
        for instrument_id, instrument_digest in connection.execute(
                "SELECT DISTINCT instrument_id, instrument_digest FROM fu_calls ORDER BY instrument_id"):
            digests.setdefault(instrument_id, []).append(instrument_digest)
        datasets = [row[0] for row in connection.execute("SELECT dataset_id FROM fu_datasets ORDER BY registered_at, "
                                                         "dataset_id")]
        connection.execute("VACUUM main INTO ?", (str(target),))
    branch_sha = scores_db.file_sha256(branch.path)
    revision = code_revision or _revision()
    bootstrap = protocol.get("bootstrap") or {}
    inputs = {"parentSha256": meta.get("parentSha256", ""), "parentPopulationSha256":
              meta.get("parentPopulationSha256", ""), "branchSha256": branch_sha,
              "protocolSha256": meta.get("protocolSha256", ""), "freezeId": freeze[0] if freeze else "",
              "instrumentDigests": json.dumps(digests, sort_keys=True), "datasetVersions": json.dumps(datasets),
              "codeRevision": revision, "bootstrapB": str(bootstrap.get("b", "")),
              "bootstrapSeed": str(bootstrap.get("seed", "")), "selectionSeed": str(protocol.get("seed", "")),
              "identities": json.dumps(list(identities))}
    with closing(sqlite3.connect(target)) as closed:
        closed.execute("PRAGMA journal_mode=DELETE")
        redacted = redact(closed)
        closed.execute("CREATE TABLE fu_census_counts(journal TEXT NOT NULL, year INTEGER, status TEXT NOT NULL, "
                       "loci INTEGER NOT NULL, reviews INTEGER NOT NULL)")
        closed.executemany("INSERT INTO fu_census_counts VALUES(?,?,?,?,?)", counts)
        closed.execute("CREATE TABLE fu_census_flags(census_flag INTEGER NOT NULL, status TEXT NOT NULL, "
                       "works INTEGER NOT NULL)")
        closed.executemany("INSERT INTO fu_census_flags VALUES(?,?,?)", [(int(bool(flag)), status, works)
                                                                          for flag, status, works in flags])
        closed.execute("CREATE TABLE fact_inputs(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        closed.executemany("INSERT INTO fact_inputs VALUES(?,?)", sorted(inputs.items()))
        facts = define_facts(closed)
        closed.commit()
        closed.execute("VACUUM")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(target) + suffix).exists():
            raise ValidationError("the facts database left a journal behind")
    os.chmod(target, 0o444)
    target_sha = scores_db.file_sha256(target)
    records = {}
    with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as closed:
        for key, (sql, method, derivation) in sorted(facts.queries.items()):
            started = time.monotonic()
            value = closed.execute(sql).fetchone()[0]
            if time.monotonic() - started > 5:
                raise ValidationError(f"{key}: derivation exceeds 5 s")
            if value is None or isinstance(value, bool):
                continue
            record = {"value": value, "sql": sql, "method": method,
                      "source": {"path": target.name, "sha256": target_sha}, "inputs": inputs,
                      "codeRevision": revision}
            if derivation is not None:
                record["derivation"] = derivation
            records[key] = record
    bundle = {"schemaVersion": 1, "facts": records,
              "provenance": {"format": FORMAT, "inputs": inputs, "codeRevision": revision,
                             "bootstrap": {"b": bootstrap.get("b"), "seed": bootstrap.get("seed")},
                             "interpretation": INTERPRETATION}}
    (output / "facts.json").write_text(json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    return {"facts": str(output / "facts.json"), "database": str(target), "databaseSha256": target_sha,
            "count": len(records), "redacted": redacted, "inputs": inputs}


def cmd_facts(args: Any) -> dict[str, Any]:
    return export_facts(Path(args.database), Path(args.output))


def cmd_verify_facts(args: Any) -> dict[str, Any]:
    path = Path(args.facts)
    if path.is_dir():
        path = path / "facts.json"
    if not path.is_file():
        raise ValidationError(f"the facts export is missing: {path}")
    return verify_export(path)
