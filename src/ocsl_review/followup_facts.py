# SPDX-License-Identifier: Apache-2.0
"""Facts v1 of a follow-up branch: a closed ``facts.sqlite3`` and ``facts.json`` (schemaVersion 1).

``facts.sqlite3`` holds only what the fact queries read, all of it aggregates, so the file stays small at census scale
and verifying a fact (which hashes the file) stays cheap:

- per-family summaries of the branch (``fs_*``): status rows and observations, frame and strata counts, support,
  function, dependency, numeric, cited-by, second-generation, judge-call, judgment and provider-request counts, each a
  GROUP BY over the branch table it names (the branch SQL is in each fact's derivation);
- the estimates and coverage of the analysis commands (``fu_estimates``, ``fu_coverage``), the stratum sizes, the
  protocol freeze and the dataset versions;
- materialized tables of the parent census: ``fu_census_counts`` (per journal, year and status, the parent's in-body
  identity-resolved loci, the denominator of the compromised share; ``no-status-row`` for a work without a status row)
  and ``fu_census_flags`` (the parent's retraction flag against the resolved status of every cited work, a diagnostic,
  never a status source);
- ``fact_inputs``: the parent, branch and protocol digests, the freeze, the instrument digests, the dataset versions,
  the code revision, the selection seed and the bootstrap replicates and seed the estimates were computed with.

No per-locus table and no text of a review or a cited work enters the file; the per-locus records are in the branch
and in the exports. Every fact is one scalar SQL query over the closed file (at most five seconds), with the file's
sha256 and the code revision; the inputs are recorded once, in the provenance. Keys are ``followup.<family>.<...>``
(status, census, strata, support, function, dependency, numeric, citedby, secondgen, calls, requests, estimates from
``fu_estimates`` with their ``followup.`` key placed under ``followup.estimates.``, coverage from ``fu_coverage``).
The estimates must share one bootstrap setting: an export over estimates computed with different replicates or seeds
is refused. ``followup verify-facts`` checks an export with ``corpus_facts.verify_export``.
"""
from __future__ import annotations

import json
import os
import re
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
COVERAGE_STATES = ("complete", "truncated", "unavailable", "error", "no-identifier")
DECIDING_KINDS = ("retraction", "expression-of-concern", "correction")
# Aggregate tables of the branch the facts read: (table, columns, GROUP BY query over the branch).
SUMMARIES: tuple[tuple[str, str, str], ...] = (
    ("fs_work_status", "status TEXT, reinstated INTEGER, dated INTEGER, decided_by TEXT, unknown_kinds_json TEXT, "
     "works INTEGER", "SELECT status, reinstated, notice_date IS NOT NULL, decided_by, unknown_kinds_json, COUNT(*) "
     "FROM fu_work_status GROUP BY 1, 2, 3, 4, 5"),
    ("fs_status_observations", "source TEXT, status TEXT, observations INTEGER",
     "SELECT source, status, COUNT(*) FROM fu_status_observations GROUP BY 1, 2"),
    ("fs_totals", "name TEXT PRIMARY KEY, value INTEGER",
     "SELECT 'frame-loci', COUNT(*) FROM fu_frame UNION ALL SELECT 'citedby-targets', COUNT(DISTINCT target_doi) "
     "FROM fu_citedby_coverage UNION ALL SELECT 'citing-works', COUNT(*) FROM (SELECT DISTINCT target_doi, citing_key "
     "FROM fu_citedby_edges)"),
    ("fs_strata", "stratum TEXT, pilot INTEGER, units INTEGER",
     "SELECT stratum, pilot, COUNT(*) FROM fu_strata GROUP BY 1, 2"),
    ("fs_support", "tier TEXT, verdict TEXT, assessable INTEGER, reason TEXT, provenance_class TEXT, units INTEGER",
     "SELECT tier, verdict, assessable, reason, provenance_class, COUNT(*) FROM fu_support GROUP BY 1, 2, 3, 4, 5"),
    ("fs_support_strata", "tier TEXT, stratum TEXT, verdict TEXT, assessable INTEGER, units INTEGER",
     "SELECT u.tier, s.stratum, u.verdict, u.assessable, COUNT(*) FROM fu_support u JOIN fu_strata s "
     "ON s.locus_id=u.locus_id GROUP BY 1, 2, 3, 4"),
    ("fs_function", "class TEXT, assessable INTEGER, reason TEXT, units INTEGER",
     "SELECT class, assessable, reason, COUNT(*) FROM fu_function GROUP BY 1, 2, 3"),
    ("fs_function_strata", "stratum TEXT, class TEXT, units INTEGER",
     "SELECT s.stratum, f.class, COUNT(*) FROM fu_function f JOIN fu_strata s ON s.locus_id=f.locus_id "
     "GROUP BY 1, 2"),
    ("fs_dependency", "class TEXT, with_anchor_verdict INTEGER, units INTEGER",
     "SELECT class, anchor_verdict IS NOT NULL, COUNT(*) FROM fu_dependency GROUP BY 1, 2"),
    ("fs_dependency_strata", "stratum TEXT, class TEXT, units INTEGER",
     "SELECT s.stratum, d.class, COUNT(*) FROM fu_dependency d JOIN fu_strata s ON s.locus_id=d.locus_id "
     "GROUP BY 1, 2"),
    ("fs_numeric", "class TEXT, assessable INTEGER, reason TEXT, units INTEGER",
     "SELECT class, assessable, reason, COUNT(*) FROM fu_numeric GROUP BY 1, 2, 3"),
    ("fs_citedby", "source TEXT, edges INTEGER", "SELECT source, COUNT(*) FROM fu_citedby_edges GROUP BY 1"),
    ("fs_citedby_coverage", "source TEXT, state TEXT, targets INTEGER",
     "SELECT source, state, COUNT(*) FROM fu_citedby_coverage GROUP BY 1, 2"),
    ("fs_secondgen", "match TEXT, reliance TEXT, assessable INTEGER, reason TEXT, units INTEGER",
     "SELECT match, reliance, assessable, reason, COUNT(*) FROM fu_secondgen_matches GROUP BY 1, 2, 3, 4"),
    ("fs_calls", "instrument_id TEXT, tier TEXT, variant TEXT, kind TEXT, outcome TEXT, fingerprinted INTEGER, "
     "entitled_external INTEGER, calls INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER",
     "SELECT instrument_id, tier, variant, kind, outcome, fingerprint IS NOT NULL, "
     "provenance_classes LIKE '%\"entitled\"%' AND judge_location!='on-prem', COUNT(*), SUM(prompt_tokens), "
     "SUM(completion_tokens) FROM fu_calls GROUP BY 1, 2, 3, 4, 5, 6, 7"),
    ("fs_judgments", "instrument_id TEXT, tier TEXT, variant TEXT, accepted_items INTEGER, invalid_items INTEGER",
     "SELECT j.instrument_id, j.tier, j.variant, COUNT(DISTINCT CASE WHEN j.outcome='accepted' THEN j.item_key END), "
     "COUNT(DISTINCT CASE WHEN j.outcome='rejected' AND NOT EXISTS (SELECT 1 FROM fu_judgments a WHERE "
     "a.instrument_digest=j.instrument_digest AND a.tier=j.tier AND a.variant=j.variant AND a.item_key=j.item_key AND "
     "a.outcome='accepted') THEN j.item_key END) FROM fu_judgments j GROUP BY 1, 2, 3"),
    ("fs_requests", "provider TEXT, outcome TEXT, requests INTEGER",
     "SELECT provider, outcome, COUNT(*) FROM fu_requests GROUP BY 1, 2"),
)
# Branch tables copied as they are: aggregates and versions, no per-locus rows and no text of a review or a work.
COPIED = ("fu_estimates", "fu_coverage", "fu_stratum_sizes", "fu_protocol_freeze", "fu_datasets")


def _q(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def key_part(value: Any) -> str:
    text = str(value if value is not None else "none")
    if text in ("*", ""):
        return "all"
    if text == "-":  # the reason of an assessable coverage row
        return "none"
    return slug(text)


def estimate_fact(key: Any) -> str:
    """The fact key of an ``fu_estimates`` key: ``followup.<estimand>...`` becomes ``followup.estimates.<estimand>...``
    (a key without the prefix gets it), so estimates never collide with the other fact families."""
    name = str(key)
    for prefix in ("followup.estimates.", "followup."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    fact = "followup.estimates." + name
    if not FACT_KEY.fullmatch(fact):
        fact = "followup.estimates." + ".".join(key_part(part) for part in name.split("."))
    return fact


def bootstrap_settings(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> dict[str, Any]:
    """The bootstrap replicates and seed the estimates were computed with (one setting, or the protocol's when there
    are no estimates); estimates computed with different settings are refused."""
    used = set()
    for (detail,) in connection.execute("SELECT DISTINCT detail_json FROM fu_estimates"):
        try:
            value = json.loads(detail or "{}")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("b") is not None:
            used.add((int(value["b"]), str(value.get("seed"))))
    declared = (protocol.get("bootstrap") or {})
    protocol_setting = (declared.get("b"), str(declared.get("seed")) if declared.get("seed") is not None else None)
    if len(used) > 1:
        raise ValidationError("the estimates were computed with different bootstrap settings ("
                              + ", ".join(f"B={b} seed={seed}" for b, seed in sorted(used)) + "): run followup lag "
                              "and followup estimands again with one setting (the protocol's) before the facts")
    b, seed = next(iter(used)) if used else protocol_setting
    return {"b": b, "seed": seed, "asProtocol": (b, seed) == protocol_setting}


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
    """Every fact as one scalar query over the closed facts file (its summaries, copies and census tables)."""
    facts = Facts()
    total = "SELECT COALESCE(SUM({column}), 0) FROM {table}"

    def counted(key: str, table: str, column: str, where: str, method: str) -> None:
        facts.add(key, total.format(column=column, table=table) + (f" WHERE {where}" if where else ""), method)
    # status
    counted("followup.status.works", "fs_work_status", "works", "", "cited works with a status row")
    for status in STATUSES:
        counted(f"followup.status.{status}", "fs_work_status", "works", f"status={_q(status)}",
                f"cited works whose resolved status is {status} (status-resolution@1)")
    counted("followup.status.reinstated", "fs_work_status", "works", "reinstated=1",
            "cited works whose retraction a later reinstatement reverses")
    for status in ("retracted", "expression-of-concern", "corrected"):
        counted(f"followup.status.{status}.dated", "fs_work_status", "works", f"status={_q(status)} AND dated=1",
                f"{status} works whose deciding notice has a date")
    for (source,) in _distinct(connection, "SELECT DISTINCT decided_by FROM fs_work_status WHERE decided_by IS NOT "
                                           "NULL ORDER BY decided_by"):
        counted(f"followup.status.decided-by.{key_part(source)}", "fs_work_status", "works",
                f"decided_by={_q(source)}", f"cited works whose deciding notice comes from {source}")
    for kind in DECIDING_KINDS:
        counted(f"followup.status.unknown-kind.{kind}", "fs_work_status", "works",
                "unknown_kinds_json LIKE " + _q(f'%"{kind}"%'),
                f"cited works for which no answering source covers {kind} notices")
    for source, status in _distinct(connection, "SELECT DISTINCT source, status FROM fs_status_observations ORDER BY "
                                                "source, status"):
        counted(f"followup.status.observations.{key_part(source)}.{status}", "fs_status_observations", "observations",
                f"source={_q(source)} AND status={_q(status)}", f"status observations of {source} reporting {status}")
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
    facts.add("followup.strata.frame", "SELECT value FROM fs_totals WHERE name='frame-loci'",
              "loci in the follow-up frame")
    facts.add("followup.strata.freezes", "SELECT COUNT(*) FROM fu_protocol_freeze", "protocol freezes")
    for stratum in STRATA:
        s_ = key_part(stratum)
        counted(f"followup.strata.{s_}.units", "fs_strata", "units", f"stratum={_q(stratum)}",
                f"loci in stratum {stratum}")
        counted(f"followup.strata.{s_}.pilot", "fs_strata", "units", f"stratum={_q(stratum)} AND pilot=1",
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
        base = f"tier={_q(tier)}"
        counted(f"followup.support.{tier}.units", "fs_support", "units", base, f"{tier} tier: units with a support row")
        counted(f"followup.support.{tier}.assessable", "fs_support", "units", base + " AND assessable=1",
                f"{tier} tier: units with an assessable citation-support@2 verdict")
        for verdict in verdicts:
            counted(f"followup.support.{tier}.verdict.{verdict}", "fs_support", "units",
                    base + f" AND assessable=1 AND verdict={_q(verdict)}", f"{tier} tier: units with verdict {verdict}")
        for reason in UNASSESSABLE:
            counted(f"followup.support.{tier}.reason.{reason}", "fs_support", "units",
                    base + f" AND assessable=0 AND reason={_q(reason)}", f"{tier} tier: units not assessable ({reason})")
        for klass in ("public", "entitled"):
            counted(f"followup.support.{tier}.provenance.{klass}", "fs_support", "units",
                    base + f" AND assessable=1 AND provenance_class={_q(klass)}",
                    f"{tier} tier: assessable units with {klass} provenance")
        for stratum in STRATA:
            where = f"tier={_q(tier)} AND stratum={_q(stratum)}"
            counted(f"followup.support.{tier}.{key_part(stratum)}.units", "fs_support_strata", "units", where,
                    f"{tier} tier, stratum {stratum}: units")
            counted(f"followup.support.{tier}.{key_part(stratum)}.assessable", "fs_support_strata", "units",
                    where + " AND assessable=1", f"{tier} tier, stratum {stratum}: assessable units")
            for verdict in verdicts:
                counted(f"followup.support.{tier}.{key_part(stratum)}.verdict.{verdict}", "fs_support_strata", "units",
                        where + f" AND assessable=1 AND verdict={_q(verdict)}",
                        f"{tier} tier, stratum {stratum}: units with verdict {verdict}")
    # function
    counted("followup.function.units", "fs_function", "units", "", "units with a citation-function@1 row")
    counted("followup.function.assessable", "fs_function", "units", "assessable=1", "classified units")
    for klass in FUNCTION_CLASSES:
        counted(f"followup.function.class.{klass}", "fs_function", "units", f"class={_q(klass)}",
                f"units classified {klass}")
        for stratum in ("S0", "S1", "S2", "S3"):
            counted(f"followup.function.{key_part(stratum)}.class.{klass}", "fs_function_strata", "units",
                    f"stratum={_q(stratum)} AND class={_q(klass)}", f"stratum {stratum}: units classified {klass}")
    for reason in UNASSESSABLE:
        counted(f"followup.function.reason.{reason}", "fs_function", "units", f"assessable=0 AND reason={_q(reason)}",
                f"units not classified ({reason})")
    # dependency
    counted("followup.dependency.anchors", "fs_dependency", "units", "", "S1 and S2 anchors with a dependency row")
    counted("followup.dependency.with-anchor-verdict", "fs_dependency", "units", "with_anchor_verdict=1",
            "anchors with an assessable full-text verdict of their own")
    for klass in DEPENDENCY_CLASSES:
        counted(f"followup.dependency.class.{klass}", "fs_dependency", "units", f"class={_q(klass)}",
                f"anchors whose dependency is {klass} (dependency-aggregation@1)")
        for stratum in ("S1", "S2"):
            counted(f"followup.dependency.{key_part(stratum)}.class.{klass}", "fs_dependency_strata", "units",
                    f"stratum={_q(stratum)} AND class={_q(klass)}", f"stratum {stratum}: dependency {klass}")
    # numeric
    counted("followup.numeric.units", "fs_numeric", "units", "", "S3 units with a numeric row")
    for klass in NUMERIC_CLASSES:
        counted(f"followup.numeric.class.{klass}", "fs_numeric", "units", f"class={_q(klass)}",
                f"S3 units of numeric class {klass}")
    for reason in UNASSESSABLE:
        counted(f"followup.numeric.reason.{reason}", "fs_numeric", "units", f"assessable=0 AND reason={_q(reason)}",
                f"S3 units not compared ({reason})")
    # cited-by
    facts.add("followup.citedby.targets", "SELECT value FROM fs_totals WHERE name='citedby-targets'",
              "reviews whose citing works were traversed")
    counted("followup.citedby.edges", "fs_citedby", "edges", "", "cited-by edges over all sources")
    facts.add("followup.citedby.citing-works", "SELECT value FROM fs_totals WHERE name='citing-works'",
              "distinct citing works per target, united across sources")
    for source in CITEDBY_SOURCES:
        counted(f"followup.citedby.{source}.edges", "fs_citedby", "edges", f"source={_q(source)}",
                f"cited-by edges from {source}")
        for state in COVERAGE_STATES:
            counted(f"followup.citedby.{source}.{state}", "fs_citedby_coverage", "targets",
                    f"source={_q(source)} AND state={_q(state)}", f"targets whose {source} traversal is {state}")
    # second generation
    counted("followup.secondgen.sentences", "fs_secondgen", "units", "", "second-generation candidate sentences")
    counted("followup.secondgen.judged", "fs_secondgen", "units", "assessable=1", "second-generation sentences judged")
    counted("followup.secondgen.confirmed", "fs_secondgen", "units",
            "assessable=1 AND match='same-claim' AND reliance='cites-review-for-claim'",
            "later sentences restating the claim on the review's authority")
    for klass in MATCH_CLASSES:
        counted(f"followup.secondgen.match.{klass}", "fs_secondgen", "units", f"match={_q(klass)}",
                f"second-generation sentences matched as {klass}")
    for klass in RELIANCE_CLASSES:
        counted(f"followup.secondgen.reliance.{klass}", "fs_secondgen", "units", f"reliance={_q(klass)}",
                f"second-generation sentences with reliance {klass}")
    for reason in UNASSESSABLE:
        counted(f"followup.secondgen.reason.{reason}", "fs_secondgen", "units",
                f"assessable=0 AND reason={_q(reason)}", f"second-generation sentences not judged ({reason})")
    # judge calls and provider requests
    counted("followup.calls.total", "fs_calls", "calls", "", "judge call receipts")
    counted("followup.calls.with-fingerprint", "fs_calls", "calls", "fingerprinted=1",
            "judge call receipts carrying a fingerprint")
    counted("followup.calls.entitled-to-external", "fs_calls", "calls", "entitled_external=1",
            "calls that sent entitled text to a judge not on premises (gate: 0)")
    for instrument_id, tier, variant in _distinct(connection, "SELECT DISTINCT instrument_id, tier, variant FROM "
                                                              "fs_calls ORDER BY instrument_id, tier, variant"):
        prefix = f"followup.calls.{key_part(instrument_id)}.{key_part(tier)}.{key_part(variant)}"
        where = f"instrument_id={_q(instrument_id)} AND tier={_q(tier)} AND variant={_q(variant)}"
        label = f"{instrument_id} {tier} ({variant})"
        for name, condition in (("calls", "kind='first'"), ("repair-calls", "kind='repair'"),
                                ("failed", "outcome='failed'"), ("model-drift", "outcome='model-drift'"),
                                ("with-fingerprint", "fingerprinted=1")):
            counted(f"{prefix}.{name}", "fs_calls", "calls", f"{where} AND {condition}", f"{label}: {name}")
        for name in ("prompt_tokens", "completion_tokens"):
            facts.add(f"{prefix}.{name.replace('_', '-')}", f"SELECT SUM({name}) FROM fs_calls WHERE {where}",
                      f"{label}: provider-reported {name.replace('_', ' ')}")
        counted(f"{prefix}.accepted-items", "fs_judgments", "accepted_items", where,
                f"{label}: items with an accepted output")
        counted(f"{prefix}.invalid-items", "fs_judgments", "invalid_items", where, f"{label}: items rejected after repair")
    for provider, outcome in _distinct(connection, "SELECT DISTINCT provider, outcome FROM fs_requests ORDER BY "
                                                   "provider, outcome"):
        counted(f"followup.requests.{key_part(provider)}.{outcome}", "fs_requests", "requests",
                f"provider={_q(provider)} AND outcome={_q(outcome)}", f"provider requests to {provider} with outcome "
                f"{outcome}")
    # estimates and coverage of the analysis commands
    for key, estimand, detail in _distinct(connection, "SELECT key, estimand, detail_json FROM fu_estimates "
                                                       "ORDER BY key"):
        fact = estimate_fact(key)
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
    with closing(branch.connect()) as connection:
        bootstrap = bootstrap_settings(connection, protocol)
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
        summaries = [(table, columns, [tuple(row) for row in connection.execute(sql)])
                     for table, columns, sql in SUMMARIES]
        copies = [(connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                                      (table,)).fetchone()[0],
                   table, [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]) for table in COPIED]
    output.mkdir(parents=True, exist_ok=True)
    branch_sha = scores_db.file_sha256(branch.path)
    revision = code_revision or _revision()
    inputs = {"parentSha256": meta.get("parentSha256", ""), "parentPopulationSha256":
              meta.get("parentPopulationSha256", ""), "branchSha256": branch_sha,
              "protocolSha256": meta.get("protocolSha256", ""), "freezeId": freeze[0] if freeze else "",
              "instrumentDigests": json.dumps(digests, sort_keys=True), "datasetVersions": json.dumps(datasets),
              "studyBundleSha256": meta.get("studyBundleSha256", ""), "codeRevision": revision,
              "bootstrapB": str(bootstrap["b"] if bootstrap["b"] is not None else ""),
              "bootstrapSeed": str(bootstrap["seed"] if bootstrap["seed"] is not None else ""),
              "bootstrapAsProtocol": "1" if bootstrap["asProtocol"] else "0",
              "selectionSeed": str(protocol.get("seed", "")), "identities": json.dumps(list(identities))}
    with closing(sqlite3.connect(target)) as closed:
        closed.execute("PRAGMA journal_mode=DELETE")
        for table, columns, rows in summaries:
            closed.execute(f"CREATE TABLE {table}({columns})")
            if rows:
                closed.executemany(f"INSERT INTO {table} VALUES({','.join('?' for _ in rows[0])})", rows)
        for create, table, rows in copies:
            closed.execute(create)
            if rows:
                closed.executemany(f"INSERT INTO {table} VALUES({','.join('?' for _ in rows[0])})", rows)
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
    summary_sql = {table: sql for table, _, sql in SUMMARIES}
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
                      "source": {"path": target.name, "sha256": target_sha}, "codeRevision": revision}
            read = re.search(r"\bFROM (\w+)", sql)
            table = read.group(1) if read and read.group(1) in summary_sql else None
            if derivation is not None:
                record["derivation"] = derivation
            elif table is not None:
                record["derivation"] = {"summary": table, "branchSql": summary_sql[table]}
            records[key] = record
    bundle = {"schemaVersion": 1, "facts": records,
              "provenance": {"format": FORMAT, "inputs": inputs, "codeRevision": revision,
                             "bootstrap": {"b": bootstrap["b"], "seed": bootstrap["seed"],
                                           "asProtocol": bootstrap["asProtocol"]},
                             "interpretation": INTERPRETATION}}
    (output / "facts.json").write_text(json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    return {"facts": str(output / "facts.json"), "database": str(target), "databaseSha256": target_sha,
            "count": len(records), "bytes": target.stat().st_size, "inputs": inputs}


def cmd_facts(args: Any) -> dict[str, Any]:
    return export_facts(Path(args.database), Path(args.output))


def cmd_verify_facts(args: Any) -> dict[str, Any]:
    path = Path(args.facts)
    if path.is_dir():
        path = path / "facts.json"
    if not path.is_file():
        raise ValidationError(f"the facts export is missing: {path}")
    return verify_export(path)
