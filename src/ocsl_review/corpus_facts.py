# SPDX-License-Identifier: Apache-2.0
"""Facts export for a frozen census scores database (schemaVersion 1).

Writes a closed ``facts.sqlite3`` (the scores tables plus an ``estimates``
table holding interval bounds, bootstrap results, trends and ladder steps)
and ``facts.json``. Every fact is one scalar SQL query over the closed file,
recomputed from it at export time, with the file's sha256, the input hashes
and the code revision. Interval bounds are separate facts; a ``derivation``
block (estimand, seed, B, digests) documents values that SQL cannot rederive
and is ignored by version-1 verifiers. The contract is the one enforced by
``ocsl-writing facts verify``: snapshot inside the export directory, no WAL,
read-only scalar derivations that read data, exact value equality.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import estimands as E
from . import scores_db
from .errors import ValidationError

FACT_KEY = re.compile(r"[A-Za-z0-9_.-]+")
D2 = "cited_inbody=1 AND identity IN ('resolved','resolved_unverified') AND model_judged=1 AND abs_category IN ('true','false')"


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    if not text:
        raise ValidationError("cannot derive a fact key from an empty name")
    return text


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _count_facts(journal: str) -> dict[str, tuple[str, str]]:
    j = _quote(journal)
    c = f"FROM citations WHERE journal={j}"
    return {
        "reviews": (f"SELECT COUNT(*) FROM reviews WHERE journal={j}", "reviews enumerated for this journal"),
        "reviews_read": (f"SELECT COUNT(*) FROM reviews WHERE journal={j} AND document_sha256 IS NOT NULL",
                         "reviews with an extracted document"),
        "reviews_uncovered": (f"SELECT COUNT(*) FROM reviews WHERE journal={j} AND uncovered>0",
                              "reviews with anchors not owned exactly once (gate: 0)"),
        "entries_printed": (f"SELECT COUNT(*) {c}", "printed bibliography entries"),
        "entries_cited_inbody": (f"SELECT COUNT(*) {c} AND cited_inbody=1", "entries cited in the body"),
        "entries_not_cited": (f"SELECT COUNT(*) {c} AND abs_class='not_cited'", "entries never cited in the body"),
        "entries_resolved": (f"SELECT COUNT(*) {c} AND cited_inbody=1 AND identity IN ('resolved','resolved_unverified')",
                             "in-body entries with a resolved identity"),
        "entries_unresolved": (f"SELECT COUNT(*) {c} AND cited_inbody=1 AND identity='unresolved'",
                               "in-body entries without an identity (class unsure)"),
        "entries_judged": (f"SELECT COUNT(*) {c} AND model_judged=1", "entries with a parseable model verdict"),
        "entries_invalid": (f"SELECT COUNT(*) {c} AND abs_basis LIKE 'invalid%'", "entries without a parseable verdict after repair"),
        "invalid_share": (f"SELECT 1.0*SUM(abs_basis LIKE 'invalid%')/NULLIF(SUM(abs_basis='model' OR abs_basis LIKE 'invalid%'),0) {c}",
                          "invalid outputs among entries sent to the judge (gate: at most 0.01)"),
        "d2_n": (f"SELECT COUNT(*) {c} AND {D2}", "denominator 2: T + F"),
        "d2_false": (f"SELECT COUNT(*) {c} AND {D2} AND abs_category='false'", "denominator 2: F, escalated F included"),
        "d2_true": (f"SELECT COUNT(*) {c} AND {D2} AND abs_category='true'", "denominator 2: T"),
        "headline_share": (f"SELECT 1.0*SUM(abs_category='false')/NULLIF(COUNT(*),0) {c} AND {D2}",
                           "headline F/(T+F) at the abstract tier"),
        "abs_sure_supported": (f"SELECT COUNT(*) {c} AND abs_class='sure_supported'", "abstract tier: sure supported"),
        "abs_unsure": (f"SELECT COUNT(*) {c} AND abs_class='unsure'", "abstract tier: unsure"),
        "abs_sure_not_supported": (f"SELECT COUNT(*) {c} AND abs_class='sure_not_supported'", "abstract tier: sure not supported"),
        "abs_share_of_sure": (f"SELECT 1.0*SUM(abs_class='sure_not_supported')/NULLIF(SUM(abs_class IN ('sure_supported','sure_not_supported')),0) {c}",
                              "sure not supported among sure classes, abstract tier"),
        "escalated": (f"SELECT COUNT(*) {c} AND model_judged=1 AND abs_escalated=1", "entries escalated to the full-text tier"),
        "ft_retrieved": (f"SELECT COUNT(*) {c} AND ft_evidence_id IS NOT NULL", "escalated entries with a full-text window"),
        "ft_read": (f"SELECT COUNT(*) {c} AND ft_class IS NOT NULL", "entries with a full-text verdict"),
        "ft_sure_supported": (f"SELECT COUNT(*) {c} AND ft_class='sure_supported'", "full-text tier: sure supported"),
        "ft_unsure": (f"SELECT COUNT(*) {c} AND ft_class='unsure'", "full-text tier: unsure"),
        "ft_sure_not_supported": (f"SELECT COUNT(*) {c} AND ft_class='sure_not_supported'", "full-text tier: sure not supported"),
        "ft_overturn_false_share": (
            f"SELECT 1.0*SUM(abs_category='false' AND ft_category='true')/NULLIF(SUM(abs_category='false' AND ft_category IN ('true','false')),0) {c}",
            "abstract-tier F re-read in full text that became T"),
        "ft_overturn_true_share": (
            f"SELECT 1.0*SUM(abs_category='true' AND ft_category='false')/NULLIF(SUM(abs_category='true' AND ft_category IN ('true','false')),0) {c}",
            "abstract-tier T re-read in full text that became F"),
        "cut_abstracts": (f"SELECT COUNT(*) {c} AND model_judged=1 AND abs_cut=1", "judged entries whose abstract was cut"),
    }


def _call_facts(journal: str) -> dict[str, tuple[str, str]]:
    j = _quote(journal)
    k = f"FROM calls WHERE journal={j} AND variant='primary'"
    return {
        "calls_abstract": (f"SELECT COUNT(*) {k} AND tier='abstract'", "abstract-tier call receipts"),
        "calls_full_text": (f"SELECT COUNT(*) {k} AND tier='full-text'", "full-text call receipts"),
        "calls_transport_failed": (f"SELECT COUNT(*) {k} AND error IS NOT NULL AND error!='model_drift'",
                                   "calls that failed after their transport attempts"),
        "calls_with_fingerprint": (f"SELECT COUNT(*) {k} AND fingerprint IS NOT NULL", "receipts carrying a server fingerprint"),
        "prompt_tokens": (f"SELECT COALESCE(SUM(prompt_tokens),0) {k}", "provider-reported prompt tokens"),
        "completion_tokens": (f"SELECT COALESCE(SUM(completion_tokens),0) {k}", "provider-reported completion tokens"),
    }


def _store(estimates: list[tuple], key: str, value: Any, estimand: str, journal: str | None, detail: Mapping[str, Any]) -> None:
    if value is None or isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
        return
    value = value if isinstance(value, int) else float(value)
    estimates.append((key, value, estimand, journal, json.dumps(detail, sort_keys=True, ensure_ascii=False, default=str)))


def compute_estimates(connection: sqlite3.Connection, journals: Sequence[str], *, b: int, seed: int,
                      ratio_pair: tuple[str, str] | None, trend_years: tuple[int, int] | None,
                      panels: Sequence[str]) -> tuple[list[tuple], dict[str, Any]]:
    estimates: list[tuple] = []
    report = E.report(connection, journals, b=b, seed=seed, ratio_pair=ratio_pair, panels=panels, trend_years=trend_years)
    for journal, result in report["journals"].items():
        s = slug(journal)
        head = result["headline"]
        detail = {"estimand": head["estimand"], "b": b, "seed": seed, "z": E.Z95}
        _store(estimates, f"census.{s}.icc", head["icc"], "one-way ANOVA ICC over reviews", journal, detail)
        _store(estimates, f"census.{s}.deff", head["deff"], "1 + (mean cluster - 1) * ICC", journal, detail)
        _store(estimates, f"census.{s}.mean_cluster", head["meanCluster"], "mean classifiable entries per review", journal, detail)
        for name, (lo, hi) in (("wilson_deff", head["wilsonDeff"]), ("bootstrap", head["bootstrap"])):
            _store(estimates, f"census.{s}.headline_{name}_lo", lo, f"headline {name} lower bound", journal, detail)
            _store(estimates, f"census.{s}.headline_{name}_hi", hi, f"headline {name} upper bound", journal, detail)
        for by, table in result["panels"].items():
            for cell, value in table["cells"].items():
                cell_key = slug(cell) if cell not in ("None", "") else "unknown"
                prefix = f"census.{s}.{by}.{cell_key}"
                cell_detail = {**detail, "panel": by, "cell": cell}
                _store(estimates, prefix + ".share", value["share"], "cell F/(T+F)", journal, cell_detail)
                _store(estimates, prefix + ".n", value["n"], "cell T + F", journal, cell_detail)
                _store(estimates, prefix + ".reviews", value["reviews"], "cell reviews", journal, cell_detail)
                for name, (lo, hi) in (("wilson_deff", value["wilsonDeff"]), ("bootstrap", value["bootstrap"])):
                    _store(estimates, f"{prefix}.{name}_lo", lo, f"cell {name} lower bound", journal, cell_detail)
                    _store(estimates, f"{prefix}.{name}_hi", hi, f"cell {name} upper bound", journal, cell_detail)
        trend = result["trend"]
        for name in ("slope", "lo", "hi"):
            _store(estimates, f"census.{s}.trend_{name}", trend.get(name), "OLS slope of the yearly share", journal,
                   {**detail, "years": trend.get("years")})
        for step in result["ladder"]["steps"]:
            key = f"census.{s}.ladder.{slug(step['step'])}"
            _store(estimates, key + ".share", step.get("share"), step["definition"], journal, detail)
            _store(estimates, key + ".n", step.get("n"), step["definition"], journal, detail)
    if ratio_pair and report.get("ratio"):
        r = report["ratio"]
        key = f"census.ratio.{slug(ratio_pair[0])}.{slug(ratio_pair[1])}"
        detail = {"estimand": "ratio of headline shares", "b": b, "seed": seed}
        for name in ("estimate", "lo", "hi"):
            _store(estimates, f"{key}.{name}", r.get(name), "ratio of headline shares", None, detail)
    return estimates, report


def export_facts(scores_path: Path, output: Path, journals: Sequence[str], *, b: int = E.DEFAULT_B, seed: int = 0,
                 ratio_pair: tuple[str, str] | None = None, trend_years: tuple[int, int] | None = None,
                 panels: Sequence[str] = ("year", "stratum", "topic", "country"),
                 code_revision: str | None = None) -> dict[str, Any]:
    scores_path, output = Path(scores_path), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    database = output / "facts.sqlite3"
    if database.exists() or (output / "facts.json").exists():
        raise ValidationError("facts export already exists; export into a new directory")
    with closing(scores_db.open_frozen(scores_path)) as frozen:
        meta = scores_db.meta(frozen)
        if meta.get("frozen") != "1":
            raise ValidationError("facts need a frozen scores database")
        known = {r[0] for r in frozen.execute("SELECT DISTINCT journal FROM reviews")}
        missing = set(journals) - known
        if missing:
            raise ValidationError("unknown journals: " + ", ".join(sorted(missing)))
        estimates, report = compute_estimates(frozen, journals, b=b, seed=seed, ratio_pair=ratio_pair,
                                              trend_years=trend_years, panels=panels)
        frozen.execute("VACUUM INTO ?", (str(database),))
    scores_sha = scores_db.file_sha256(scores_path)
    revision = code_revision or _revision()
    with closing(sqlite3.connect(database)) as target:
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("CREATE TABLE estimates(key TEXT PRIMARY KEY, value NOT NULL, estimand TEXT NOT NULL, "
                       "journal TEXT, detail_json TEXT NOT NULL)")
        target.executemany("INSERT INTO estimates VALUES(?,?,?,?,?)", estimates)
        target.execute("CREATE TABLE fact_inputs(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        target.executemany("INSERT INTO fact_inputs VALUES(?,?)", [
            ("scoresSha256", scores_sha), ("instrumentDigest", meta.get("instrumentDigest", "")),
            ("instrumentId", meta.get("instrumentId", "")), ("codeRevision", revision),
            ("bootstrapB", str(b)), ("seed", str(seed))])
        target.commit()
        target.execute("VACUUM")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(database) + suffix).exists():
            raise ValidationError("facts database left a journal behind")
    os.chmod(database, 0o444)
    database_sha = scores_db.file_sha256(database)
    inputs = {"scoresSha256": scores_sha, "instrumentDigest": meta.get("instrumentDigest"),
              "instrumentId": meta.get("instrumentId"), "estimandsModule": "ocsl_review.estimands"}
    queries: dict[str, tuple[str, str, dict[str, Any] | None]] = {}
    for journal in journals:
        s = slug(journal)
        for name, (sql, method) in {**_count_facts(journal), **_call_facts(journal)}.items():
            queries[f"census.{s}.{name}"] = (sql, method, None)
    for key, _value, estimand, journal, detail in estimates:
        queries[key] = (f"SELECT value FROM estimates WHERE key={_quote(key)}", estimand, json.loads(detail))
    facts = {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as closed:
        for key, (sql, method, derivation) in sorted(queries.items()):
            if not FACT_KEY.fullmatch(key):
                raise ValidationError(f"invalid fact key: {key}")
            started = time.monotonic()
            value = closed.execute(sql).fetchone()[0]
            if time.monotonic() - started > 5:
                raise ValidationError(f"{key}: derivation exceeds 5 s")
            if value is None:
                continue
            record = {"value": value, "sql": sql, "method": method,
                      "source": {"path": database.name, "sha256": database_sha},
                      "inputs": inputs, "codeRevision": revision}
            if derivation is not None:
                record["derivation"] = derivation
            facts[key] = record
    bundle = {"schemaVersion": 1, "facts": facts,
              "provenance": {"format": "ocsl-census-facts-v1", "journals": list(journals), "inputs": inputs,
                             "codeRevision": revision, "bootstrap": {"b": b, "seed": seed},
                             "interpretation": "Model measurements on a predeclared instrument, not human-labelled truth"}}
    (output / "facts.json").write_text(json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"facts": str(output / "facts.json"), "database": str(database), "databaseSha256": database_sha,
            "count": len(facts), "report": report}


def _revision() -> str:
    from .census_runner import code_revision
    return code_revision()


def verify_export(path: Path) -> dict[str, Any]:
    """Postflight with the version-1 facts contract: hashes, closed snapshot, scalar read-only derivations."""
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != 1 or not isinstance(data.get("facts"), dict) or not data["facts"]:
        raise ValidationError("expected a non-empty version-1 facts export")
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    checked = 0
    for key, fact in data["facts"].items():
        if not FACT_KEY.fullmatch(key):
            raise ValidationError(f"invalid fact key: {key}")
        source = fact.get("source") or {}
        relative = Path(source.get("path", ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError(f"{key}: source escapes the export")
        snapshot = (path.parent / relative).resolve()
        if not snapshot.is_relative_to(path.parent) or not snapshot.is_file():
            raise ValidationError(f"{key}: source snapshot missing")
        if scores_db.file_sha256(snapshot) != source.get("sha256"):
            raise ValidationError(f"{key}: source sha256 mismatch")
        if Path(str(snapshot) + "-wal").exists():
            raise ValidationError(f"{key}: snapshot has a WAL")
        reads = set()

        def authorize(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_READ:
                reads.add(first)
            if action not in allowed or (action == sqlite3.SQLITE_FUNCTION and second == "load_extension"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.set_authorizer(authorize)
            rows = connection.execute(fact["sql"]).fetchmany(2)
        if not reads or reads <= {"sqlite_master", "sqlite_schema"}:
            raise ValidationError(f"{key}: derivation reads no data")
        if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] != fact["value"] or isinstance(fact["value"], bool):
            raise ValidationError(f"{key}: derivation differs from the exported value")
        checked += 1
    return {"ok": True, "facts": checked}
