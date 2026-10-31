# SPDX-License-Identifier: Apache-2.0
"""Numeric checks of statistics-report citations (``followup numeric --tables``): the numeric-check@1 parse of every
S3 locus and the deterministic comparison ``numeric-comparison@1`` against a pinned table source.

Licensing first: the command refuses a protocol that drops S3 (table licensing not verified), a table source whose
licensing is ``unverified``, a ``synthetic`` source unless ``--allow-synthetic-tables`` (tests only), and a source
whose sha256 or declared version differs from the protocol's pin (``strata.S3.tableSource``). The source is registered
as a ``table-source`` dataset version; only the cells a comparison matched are stored (``fu_numeric.matched_json``).

Items carry the locus id, the marker of the cited work, the citing sentence and the cited work's title and year, with
the provenance class of the citing text for routing; table values, status, stratum and verdicts are never sent.
Entitled sentences reach only on-premises judges (``followup_judge.run_items`` refuses otherwise); a redacted sentence
without the private text copy is recorded as ``text-redacted`` and never sent.

``numeric-comparison@1`` normalises valueText to a number with its stated precision (thousands separators, decimal
comma, thousand / million / billion, percent, ordinals) and maps condition, metric and geography through the source's
versioned synonym lists, then classifies in order: not epidemiological -> ``non-epidemiological``; abstained ->
unassessable ``abstained``; the cited work is no report of the source -> unassessable ``no-matching-table``; no cells
for the cited edition -> unassessable ``table-unavailable``; a valueText the rule cannot read -> unassessable
``invalid-output``; a cell of the cited edition for the claimed condition, metric, geography and year whose value,
rounded to the claim's precision, equals the claim -> ``consistent``; the value of a cell of the cited edition for
another geography -> ``wrong-geography``; the claimed statistic in a cell of another edition -> ``wrong-edition``;
otherwise ``number-absent-or-different``.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import followup_tables, numeric_check
from .census_extraction import MARK_PATTERN, marker_name
from .errors import ValidationError
from .followup_branch import FollowupBranch, register_dataset, require_freeze
from .followup_judge import (accepted, block_items, close_judge, judge_from_args, judge_summary, prepare_judge,
                             run_items)
from .followup_model import NUMERIC_RULE, TableCell, WorkRef, canonical
from .followup_strata import protocol_of, replicate_keys

RULE = NUMERIC_RULE
TIER = numeric_check.TIER
SCALES = {"thousand": 3, "million": 6, "billion": 9}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10}
_SPACES = str.maketrans({"\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2007": " ", "−": "-", "–": "-"})
_NUMBER = re.compile(r"(?<![\w.,])(-?)(\d(?:[\d,.]|\s(?=\d{3}(?!\d)))*)")
_ORDINAL_DIGITS = re.compile(r"^\s*(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE)


# ----------------------------------------------------------------------------- numbers
@dataclass(frozen=True)
class ClaimValue:
    """A claimed number: mantissa as written, its decimals (stated precision), a power-of-ten scale and percent."""
    mantissa: Decimal
    decimals: int
    exponent: int = 0
    percent: bool = False

    @property
    def value(self) -> float:
        return float(self.mantissa.scaleb(self.exponent))

    def matches(self, cell_value: float, cell_unit: str = "") -> bool:
        """The cell's value, rounded to the claim's stated precision, equals the claimed number (percent to percent)."""
        exponent, percent = cell_scale(cell_unit)
        if percent != self.percent:
            return False
        absolute = Decimal(repr(float(cell_value))).scaleb(exponent)
        quantum = Decimal(1).scaleb(-self.decimals)
        return absolute.scaleb(-self.exponent).quantize(quantum, ROUND_HALF_UP) == self.mantissa.quantize(
            quantum, ROUND_HALF_UP)

    def record(self) -> dict[str, Any]:
        return {"mantissa": str(self.mantissa), "decimals": self.decimals, "exponent": self.exponent,
                "percent": self.percent, "value": self.value}


def _digits(token: str) -> tuple[str, int] | None:
    """A numeric token as a canonical decimal string and its number of decimals (see normalize_value)."""
    token = token.rstrip(",.")
    groups = token.replace(" ", "")
    if " " in token and not re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", token):
        return None
    commas, dots = groups.count(","), groups.count(".")
    if commas and dots:
        decimal_mark = "," if groups.rfind(",") > groups.rfind(".") else "."
        thousands = "." if decimal_mark == "," else ","
        whole, _, fraction = groups.rpartition(decimal_mark)
        if groups.count(decimal_mark) != 1 or not re.fullmatch(r"\d{1,3}(?:" + re.escape(thousands) + r"\d{3})*", whole):
            return None
        whole = whole.replace(thousands, "")
    elif commas:
        if commas > 1 or re.fullmatch(r"\d{1,3},\d{3}", groups):
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})+", groups):
                return None
            whole, fraction = groups.replace(",", ""), ""
        else:
            whole, _, fraction = groups.partition(",")
    elif dots > 1:
        if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+", groups):
            return None
        whole, fraction = groups.replace(".", ""), ""
    else:
        whole, _, fraction = groups.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    return (whole + ("." + fraction if fraction else "")), len(fraction)


def normalize_value(text: Any) -> ClaimValue | None:
    """The number a valueText states, or None.

    Thousands separators are commas, points (more than one) or spaces between groups of three digits; one comma
    followed by exactly three digits separates thousands, any other single comma is a decimal comma; with both marks the
    later one is the decimal mark. "thousand", "million" and "billion" scale the number; "%", "percent" or "per cent"
    mark a percentage; "first" to "tenth" and "1st", "2nd", ... are ranks. The stated precision is the number of
    decimals as written.
    """
    value = str(text or "").translate(_SPACES).strip()
    folded = value.casefold()
    if folded in ORDINALS:
        return ClaimValue(Decimal(ORDINALS[folded]), 0)
    ordinal = _ORDINAL_DIGITS.match(value)
    if ordinal:
        return ClaimValue(Decimal(int(ordinal.group(1))), 0)
    match = _NUMBER.search(value)
    if not match:
        return None
    parsed = _digits(match.group(2))
    if parsed is None:
        return None
    digits, decimals = parsed
    rest = value[match.end():].casefold()
    exponent = 0
    scale = re.match(r"\s*(thousand|million|billion)s?\b", rest)
    if scale:
        exponent = SCALES[scale.group(1)]
        rest = rest[scale.end():]
    percent = bool(re.match(r"\s*(?:%|percent\b|per cent\b)", rest))
    mantissa = Decimal(("-" if match.group(1) else "") + digits)
    return ClaimValue(mantissa, decimals, exponent, percent)


def cell_scale(unit: Any) -> tuple[int, bool]:
    """(power-of-ten scale, percent) a cell's unit declares: "thousand deaths" scales by 10^3, "percent" is a share."""
    folded = followup_tables.fold(unit)
    scale = re.match(r"(thousand|million|billion)s?\b", folded)
    percent = folded in ("%", "percent", "per cent") or "percent" in folded.split() or folded.startswith("%")
    return (SCALES[scale.group(1)] if scale else 0), percent


# ----------------------------------------------------------------------------- comparison
def _cell_record(cell: TableCell) -> dict[str, Any]:
    return {"report": cell.report_key, "edition": cell.edition, "condition": cell.condition, "metric": cell.metric,
            "geography": cell.geography, "year": cell.year, "value": cell.value, "unit": cell.unit,
            "locator": cell.locator}


def _unassessable(reason: str) -> dict[str, Any]:
    return {"class": None, "assessable": 0, "reason": reason, "matched": None, "value": None}


def compare(output: Mapping[str, Any], work: WorkRef, source: Any) -> dict[str, Any]:
    """numeric-comparison@1 of one accepted numeric-check@1 output (see the module docstring)."""
    decision = output["decision"]
    if decision == "not-epidemiological":
        return {"class": "non-epidemiological", "assessable": 1, "reason": None, "matched": None, "value": None}
    if decision != "parsed":
        return _unassessable("abstained")
    located = source.report_for(work)
    if located is None:
        return _unassessable("no-matching-table")
    report, edition = located
    cited = list(source.cells(report, edition)) if edition else []
    if not cited:
        return _unassessable("table-unavailable")
    claim = normalize_value(output["valueText"])
    if claim is None:
        return _unassessable("invalid-output")
    condition = source.canonical("condition", output["condition"])
    metric = source.canonical("metric", output["metric"])
    geography = source.canonical("geography", output["geography"]) if (output["geography"] or "").strip() else None
    year = output["year"]

    def same(cell: TableCell, *, place: bool = True, when: bool = True) -> bool:
        return (source.canonical("condition", cell.condition) == condition
                and source.canonical("metric", cell.metric) == metric
                and (not place or geography is None or source.canonical("geography", cell.geography) == geography)
                and (not when or year is None or cell.year == year))
    identity = [cell for cell in cited if same(cell)]
    matched = [cell for cell in identity if claim.matches(cell.value, cell.unit)]
    verdict = "consistent"
    if not matched and geography is not None:
        matched = [cell for cell in cited if same(cell, place=False, when=False)
                   and source.canonical("geography", cell.geography) != geography and claim.matches(cell.value, cell.unit)]
        verdict = "wrong-geography"
    if not matched:
        matched = [cell for other in source.editions(report) if other != edition
                   for cell in source.cells(report, other) if same(cell, when=False) and claim.matches(cell.value,
                                                                                                       cell.unit)]
        verdict = "wrong-edition"
    if not matched:
        matched, verdict = identity, "number-absent-or-different"
    return {"class": verdict, "assessable": 1, "reason": None, "value": claim.value,
            "matched": {"rule": RULE, "report": report, "edition": edition, "claim": claim.record(),
                        "cells": [_cell_record(cell) for cell in matched],
                        "synonyms": source.describe().get("synonyms")}}


# ----------------------------------------------------------------------------- licensing
def check_source(source: Any, protocol: Mapping[str, Any], *, allow_synthetic: bool = False) -> None:
    """Refuse a source the protocol does not admit (see the module docstring)."""
    s3 = protocol["strata"].get("S3")
    if not s3 or not s3.get("licensingVerified"):
        raise ValidationError("the protocol drops S3 because its table licensing is not verified; followup numeric "
                              "compares nothing")
    if source.licensing == "unverified":
        raise ValidationError("the table source's licensing is unverified; such tables are never ingested")
    if source.licensing == "synthetic" and not allow_synthetic:
        raise ValidationError("a synthetic table source is accepted only with --allow-synthetic-tables (tests)")
    if source.licensing not in ("synthetic", "verified"):
        raise ValidationError(f"unknown table source licensing: {source.licensing!r}")
    pin = s3.get("tableSource")
    if not pin:
        raise ValidationError("the protocol pins no table source (strata.S3.tableSource with sha256 and declaredVersion)")
    if pin["sha256"] != source.sha256:
        raise ValidationError("the table source differs from the one the protocol pins (sha256)")
    if pin["declaredVersion"] != source.declared_version:
        raise ValidationError("the table source's declared version differs from the protocol's pin")


# ----------------------------------------------------------------------------- units and items
def marker_for(sentence: str, target_id: str | None, label: str, route: str | None) -> str | None:
    """The marker of the cited work in its citing sentence: the census rendering of the entry's target, else the only
    marker of the sentence, else the one marker whose digits are the entry's label; None when it cannot be told."""
    names = MARK_PATTERN.findall(sentence or "")
    present = set(names)
    for name in ([marker_name(target_id, html=route == "html"), target_id, marker_name(target_id, html=True)]
                 if target_id else []):
        if name in present:
            return f"⟨{name}⟩"
    if len(present) == 1:
        return f"⟨{names[0]}⟩"
    by_label = sorted(name for name in present if re.sub(r"\D", "", name) == str(label))
    return f"⟨{by_label[0]}⟩" if len(by_label) == 1 else None


def _year(value: Any) -> Any:
    text = str(value or "").strip()
    return int(text) if re.fullmatch(r"\d{4}", text) else (text or None)


def numeric_units(connection: sqlite3.Connection, branch: FollowupBranch, *,
                  pilot_only: bool = False) -> list[dict[str, Any]]:
    """S3 loci with their citing sentence (or why it is unavailable), marker, cited work and provenance."""
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    units = []
    for row in cursor.execute(
            "SELECT f.locus_id, f.journal, f.doi, f.label, f.seq, f.work_key, f.provenance_class, c.target_id, r.route, "
            "w.doi AS work_doi, w.pmid AS work_pmid, w.pmcid AS work_pmcid, w.title AS work_title, w.year AS work_year "
            "FROM fu_strata s JOIN fu_frame f ON f.locus_id=s.locus_id "
            "JOIN census.citations c ON c.journal=f.journal AND c.doi=f.doi AND c.label=f.label "
            "JOIN census.reviews r ON r.journal=f.journal AND r.doi=f.doi "
            "LEFT JOIN census.works w ON w.work_key=f.work_key WHERE s.stratum='S3'"
            + (" AND s.pilot=1" if pilot_only else "") + " ORDER BY f.journal, f.doi, f.unit, f.seq, f.label"):
        sentence, reason = branch.sentence(connection, row["journal"], row["doi"], row["label"], row["seq"])
        marker = marker_for(sentence, row["target_id"], row["label"], row["route"]) if sentence else None
        if sentence and marker is None:
            reason = "no-citing-text"
        units.append({"locus_id": row["locus_id"], "sentence": sentence if marker else None, "reason": reason,
                      "marker": marker, "provenance": "public" if row["provenance_class"] == "public" else "entitled",
                      "work": WorkRef(row["work_key"], row["work_doi"], row["work_pmid"], row["work_pmcid"],
                                      row["work_title"], row["work_year"])})
    return units


def item_of(unit: Mapping[str, Any]) -> dict[str, Any]:
    work = unit["work"]
    return {"id": unit["locus_id"], "marker": unit["marker"], "citingSentence": unit["sentence"],
            "citedWork": {"title": work.title or "", "year": _year(work.year)}, "provenance": unit["provenance"]}


def _block_new(connection: sqlite3.Connection, instrument: Any, blocked: Sequence[tuple[str, str, str]],
               variant: str) -> int:
    known = {(row[0], row[1]) for row in connection.execute(
        "SELECT item_key, error_code FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant=? "
        "AND outcome='blocked'", (instrument.digest, TIER, variant))}
    fresh = [item for item in blocked if (item[0], item[1]) not in known]
    return block_items(connection, instrument, TIER, fresh, variant=variant) if fresh else 0


# ----------------------------------------------------------------------------- rows
def write_rows(connection: sqlite3.Connection, units: Sequence[Mapping[str, Any]], source: Any, dataset: str,
               instrument: Any) -> dict[str, int]:
    """One fu_numeric row per S3 locus from the primary judgments and the comparison rule."""
    verdicts = accepted(connection, instrument.digest, TIER, "primary")
    judgment_ids = dict(connection.execute(
        "SELECT item_key, judgment_id FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant='primary' "
        "AND outcome='accepted'", (instrument.digest, TIER)).fetchall())
    rejected = {row[0] for row in connection.execute(
        "SELECT DISTINCT item_key FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant='primary' "
        "AND outcome='rejected'", (instrument.digest, TIER))}
    counts: dict[str, int] = {}
    for unit in units:
        key = unit["locus_id"]
        parse = {"epidemiological": None, "condition": None, "metric": None, "geography": None, "year": None,
                 "value_text": None, "unit": None, "quote": None}
        judgment = None
        if unit["sentence"] is None:
            result = _unassessable(unit["reason"] or "no-citing-text")
        elif key in verdicts:
            parse = numeric_check.parse_fields(verdicts[key])
            judgment = judgment_ids.get(key)
            result = compare(verdicts[key], unit["work"], source)
        else:
            result = _unassessable("invalid-output" if key in rejected else "not-judged")
        connection.execute(
            "INSERT OR REPLACE INTO fu_numeric(locus_id, parse_judgment_id, epidemiological, condition, metric, "
            "geography, year, value, value_text, unit, quote, table_source, class, assessable, reason, matched_json, "
            "rule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, judgment, parse["epidemiological"], parse["condition"], parse["metric"], parse["geography"],
             parse["year"], result["value"], parse["value_text"], parse["unit"], parse["quote"], dataset,
             result["class"], result["assessable"], result["reason"],
             canonical(result["matched"]) if result["matched"] is not None else None, RULE))
        label = result["class"] or result["reason"]
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


# ----------------------------------------------------------------------------- command
def run_numeric(branch: FollowupBranch, tables: Path, *, judge: Any, policy: Any, model: str,
                allow_synthetic: bool = False, variant: str = "primary", scope: str = "all", public_only: bool = False,
                limit: int | None = None, concurrency: int = 1, transport_retries: int = 0, cooldown: float = 0.0,
                sleep: Callable[[float], None] = time.sleep, code_revision: str | None = None) -> dict[str, Any]:
    with branch.writing() as connection:
        require_freeze(connection)
        protocol = protocol_of(branch)
        if model != protocol["model"]["served"]:
            raise ValidationError("--model must be the served model the protocol pins (model.served)")
        source = followup_tables.load(tables)
        check_source(source, protocol, allow_synthetic=allow_synthetic)
        described = source.describe()
        dataset = register_dataset(connection, kind="table-source", name=source.name,
                                   declared_version=source.declared_version, sha256=source.sha256,
                                   rows=described["cells"], file_name=Path(tables).name, detail=described)
        instrument = numeric_check.instrument()
        prepare_judge(judge, instrument, TIER)
        units = numeric_units(connection, branch)
        selected = numeric_units(connection, branch, pilot_only=True) if scope == "pilot" else units
        blocked = [(unit["locus_id"], unit["reason"], "the citing sentence is not available")
                   for unit in selected if unit["sentence"] is None]
        items = [item_of(unit) for unit in selected if unit["sentence"] is not None]
        if variant == "replicate":
            keep = set(replicate_keys(protocol, [item["id"] for item in items], scope_parts=(instrument.id,)))
            items = [item for item in items if item["id"] in keep]
        if public_only:
            items = [item for item in items if item["provenance"] == "public"]
        if limit is not None:
            items = items[:max(0, int(limit))]
        newly_blocked = _block_new(connection, instrument, blocked, variant)
        summary = run_items(connection, instrument, items, judge, tier=TIER, model=model, policy=policy,
                            variant=variant, concurrency=concurrency, code_revision=code_revision,
                            transport_retries=transport_retries, cooldown=cooldown, sleep=sleep)
        rows = write_rows(connection, units, source, dataset, instrument) if variant == "primary" else None
    return {"database": str(branch.path), "rule": RULE, "tableSource": dataset, "licensing": source.licensing,
            "units": len(units), "sent": len(items), "blocked": len(blocked), "newlyBlocked": newly_blocked,
            "judging": summary, "rows": rows, "judge": judge_summary(judge)}


def cmd_numeric(args: Any) -> dict[str, Any]:
    from .census_runner import code_revision
    judge, policy = judge_from_args(args)
    try:
        return run_numeric(FollowupBranch(Path(args.database)), Path(args.tables), judge=judge, policy=policy,
                           model=args.model, allow_synthetic=bool(getattr(args, "allow_synthetic_tables", False)),
                           variant=args.variant, scope=args.scope, public_only=bool(args.public_only),
                           limit=args.limit, concurrency=args.concurrency, transport_retries=args.transport_retries,
                           cooldown=args.transport_cooldown, code_revision=code_revision())
    finally:
        close_judge(judge)
