# SPDX-License-Identifier: Apache-2.0
"""The predeclared estimands of a follow-up audit (``followup estimands``).

Every estimand the protocol declares is computed by journal, by year and by status where its ``by`` names them (the
cross of the named breakdowns, with ``all`` margins), as a Hajek-weighted share sum(w y) / sum(w) with w = 1 / pi, the
realised inclusion probability, and a percentile bootstrap that resamples reviews with replacement within journal
(replicates and seed from the protocol unless ``--b`` / ``--bootstrap-seed`` override them; one scope tuple per
estimand and cell). Units without a verdict never enter a numerator or a denominator: each goes to ``fu_coverage``
with its reason. Abstract silence and failed downloads are reasons, never "unsupported".

| Id | Numerator | Denominator | Units |
|---|---|---|---|
| compromised-share | loci whose cited work's status is one of the protocol's S1 statuses (each status a level when "status" is in ``by``) | in-body identity-resolved frame loci with a known status | frame (census) |
| sole-or-essential | S1 loci with dependency sole or essential | S1 loci with dependency sole, essential or redundant | S1 |
| cited-after-notice | S1 loci whose review went online after the notice | S1 loci with a decidable comparison (the lag distribution in the detail) | S1 |
| acknowledges-notice | post-notice S1 loci with function notes-retraction | post-notice S1 loci with an assessable function | S1 |
| second-generation | judged later sentences that restate the claim and cite the review for it | judged later sentences (weights from the edge sample) | sentences of sampled edges |
| distortion-type | adverse S2 loci per adjudicated distortion label | adjudicated adverse S2 loci | S2 |
| numeric-classes | S3 loci per numeric class | assessable S3 loci | S3 |

Distortion labels come from adjudication, which this build does not hold: the estimand reports "not-measured" with the
number of adverse S2 loci (full-text verdict contradicted or not addressed) awaiting labels. Keys:
``followup.<estimand>[.<level>][.<status>].<journal-slug|all>.<year|all>.<share|lo|hi|n|numerator|denominator>``.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import claim_match
from .errors import ValidationError
from .followup_branch import FollowupBranch, require_freeze
from .followup_schema import ADVERSE, NUMERIC_CLASSES
from .followup_stats import (INTERVAL, QUANTILES, clear_estimates, cluster_ratio_bootstrap, lag_rows, quantile,
                             write_coverage, write_share_rows)
from .followup_strata import ALL, journal_slugs, protocol_of

ESTIMANDS = ("compromised-share", "sole-or-essential", "cited-after-notice", "acknowledges-notice", "second-generation",
             "distortion-type", "numeric-classes")
DISTORTION_LABELS = ("numeric-drift", "optimal-read-as-actual", "subgroup-read-as-population",
                     "single-arm-read-as-randomised", "negative-read-as-positive", "methods-paper-as-authority")
WEIGHTS = "1/realised inclusion probability (Hajek ratio)"


@dataclass(frozen=True)
class Unit:
    """One unit (or a group of identical census units) of an estimand: its review, cell coordinates, weight, the
    measured value, and the reason when it has no verdict."""
    cluster: tuple[str, str]
    journal: str
    year: int | None
    status: str | None
    value: Any = None
    reason: str | None = None
    weight: float = 1.0
    count: int = 1
    extra: Any = None


@dataclass(frozen=True)
class Spec:
    stratum: str
    units: Callable[[sqlite3.Connection, Mapping[str, Any]], list[Unit]]
    levels: Callable[[Mapping[str, Any], Sequence[str]], list[tuple[str | None, Callable[[Any], bool]]]]
    status_cells: bool = True
    detail: Callable[[Sequence[Unit]], dict[str, Any]] | None = None
    scope_label: str | None = None


# ----------------------------------------------------------------------------- units
def _statuses(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT work_key, status FROM fu_work_status").fetchall())


def compromised_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    """Frame loci grouped by review, year and status (census: weight 1 per locus)."""
    s1 = protocol["strata"]["S1"]
    identities = list(s1["identities"])
    inbody = " AND f.cited_inbody=1" if s1["inBodyOnly"] else ""
    units = []
    for journal, doi, year, resolved, status, count in connection.execute(
            "SELECT f.journal, f.doi, f.review_year, "
            f"f.work_key IS NOT NULL AND f.identity IN ({','.join('?' for _ in identities)}) AS resolved, "
            f"w.status, COUNT(*) FROM fu_frame f LEFT JOIN fu_work_status w ON w.work_key=f.work_key WHERE 1=1{inbody} "
            "GROUP BY f.journal, f.doi, f.review_year, resolved, w.status", identities):
        if not resolved:
            units.append(Unit((journal, doi), journal, year, None, reason="identity-unresolved", weight=float(count),
                              count=count))
        elif status in (None, "unknown"):
            units.append(Unit((journal, doi), journal, year, None, reason="status-unknown", weight=float(count),
                              count=count))
        else:
            units.append(Unit((journal, doi), journal, year, status, value=status, weight=float(count), count=count))
    return units


def _s1(connection: sqlite3.Connection, extra: str = "", join: str = "") -> list[sqlite3.Row]:
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(
        f"SELECT f.locus_id, f.journal, f.doi, f.review_year, s.inclusion_probability, w.status{extra} FROM fu_strata s "
        f"JOIN fu_frame f ON f.locus_id=s.locus_id LEFT JOIN fu_work_status w ON w.work_key=f.work_key {join}"
        "WHERE s.stratum='S1' ORDER BY f.locus_id").fetchall()


def dependency_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    units = []
    for row in _s1(connection, ", d.class AS dependency", "LEFT JOIN fu_dependency d ON d.locus_id=f.locus_id "):
        base = ((row["journal"], row["doi"]), row["journal"], row["review_year"], row["status"])
        weight = 1.0 / row["inclusion_probability"]
        if row["dependency"] in ("sole", "essential", "redundant"):
            units.append(Unit(*base, value=row["dependency"] != "redundant", weight=weight))
        else:
            units.append(Unit(*base, reason="undetermined" if row["dependency"] == "undetermined" else "not-judged",
                              weight=weight))
    return units


def after_notice_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    return [Unit((row["journal"], row["doi"]), row["journal"], row["year"], row["status"],
                 value=row["after_notice"], reason=row["reason"] if row["after_notice"] is None else None,
                 weight=row["weight"], extra=row["lag_days"]) for row in lag_rows(connection)]


def acknowledgement_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    functions = {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
        "SELECT locus_id, class, assessable, reason FROM fu_function")}
    units = []
    for row in lag_rows(connection):
        if row["after_notice"] != 1:
            continue
        base = ((row["journal"], row["doi"]), row["journal"], row["year"], row["status"])
        function = functions.get(row["locus_id"])
        if function is None:
            units.append(Unit(*base, reason="not-judged", weight=row["weight"]))
        elif function[1]:
            units.append(Unit(*base, value=function[0] == "notes-retraction", weight=row["weight"]))
        else:
            units.append(Unit(*base, reason=function[2] or "not-judged", weight=row["weight"]))
    return units


def second_generation_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    """Sentences of sampled edges (weight 1 / edge inclusion probability) and, for coverage, sampled edges whose later
    article gave no sentence to compare."""
    statuses = _statuses(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    units = []
    for row in cursor.execute(
            "SELECT m.match, m.reliance, m.assessable, m.reason, m.inclusion_probability, f.journal, f.doi, "
            "f.review_year, f.work_key FROM fu_secondgen_matches m JOIN fu_frame f ON f.locus_id=m.anchor_locus_id "
            "ORDER BY m.anchor_locus_id, m.sentence_id"):
        base = ((row["journal"], row["doi"]), row["journal"], row["review_year"], statuses.get(row["work_key"]))
        weight = 1.0 / row["inclusion_probability"]
        if row["assessable"]:
            units.append(Unit(*base, value=claim_match.propagates(dict(row)), weight=weight))
        else:
            units.append(Unit(*base, reason=row["reason"], weight=weight))
    for row in cursor.execute(
            "SELECT s.anchor_locus_id, s.inclusion_probability, c.state, c.reason, f.journal, f.doi, f.review_year, "
            "f.work_key FROM fu_secondgen_sample s JOIN fu_frame f ON f.locus_id=s.anchor_locus_id "
            "LEFT JOIN fu_secondgen_candidates c ON c.anchor_locus_id=s.anchor_locus_id AND c.citing_key=s.citing_key "
            "WHERE c.state IS NULL OR c.state='no-sentences' ORDER BY s.anchor_locus_id, s.citing_key"):
        units.append(Unit((row["journal"], row["doi"]), row["journal"], row["review_year"],
                          statuses.get(row["work_key"]), reason=row["reason"] or "not-judged",
                          weight=1.0 / row["inclusion_probability"]))
    return units


def adverse_s2_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    statuses = _statuses(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return [Unit((row["journal"], row["doi"]), row["journal"], row["review_year"], statuses.get(row["work_key"]),
                 reason="awaiting-labels", weight=1.0 / row["inclusion_probability"]) for row in cursor.execute(
        "SELECT f.journal, f.doi, f.review_year, f.work_key, s.inclusion_probability FROM fu_strata s "
        "JOIN fu_frame f ON f.locus_id=s.locus_id JOIN fu_support u ON u.locus_id=f.locus_id AND u.tier='full-text' "
        f"WHERE s.stratum='S2' AND u.assessable=1 AND u.verdict IN ({','.join('?' for _ in ADVERSE)}) "
        "ORDER BY f.locus_id", ADVERSE)]


def numeric_units(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> list[Unit]:
    statuses = _statuses(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    units = []
    for row in cursor.execute(
            "SELECT f.journal, f.doi, f.review_year, f.work_key, s.inclusion_probability, n.class, n.assessable, "
            "n.reason, n.locus_id IS NOT NULL AS checked FROM fu_strata s JOIN fu_frame f ON f.locus_id=s.locus_id "
            "LEFT JOIN fu_numeric n ON n.locus_id=f.locus_id WHERE s.stratum='S3' ORDER BY f.locus_id"):
        base = ((row["journal"], row["doi"]), row["journal"], row["review_year"], statuses.get(row["work_key"]))
        weight = 1.0 / row["inclusion_probability"]
        if row["checked"] and row["assessable"]:
            units.append(Unit(*base, value=row["class"], weight=weight))
        else:
            units.append(Unit(*base, reason=row["reason"] if row["checked"] else "not-judged", weight=weight))
    return units


# ----------------------------------------------------------------------------- levels and details
def binary(protocol: Mapping[str, Any], by: Sequence[str]) -> list[tuple[str | None, Callable[[Any], bool]]]:
    return [(None, bool)]


def status_levels(protocol: Mapping[str, Any], by: Sequence[str]) -> list[tuple[str | None, Callable[[Any], bool]]]:
    statuses = list(protocol["strata"]["S1"]["statuses"])
    levels: list[tuple[str | None, Callable[[Any], bool]]] = [(None, lambda value: value in statuses)]
    if "status" in by:
        levels += [(status, lambda value, status=status: value == status) for status in statuses]
    return levels


def numeric_levels(protocol: Mapping[str, Any], by: Sequence[str]) -> list[tuple[str | None, Callable[[Any], bool]]]:
    return [(name, lambda value, name=name: value == name) for name in NUMERIC_CLASSES]


def lag_detail(units: Sequence[Unit]) -> dict[str, Any]:
    post = sorted(float(unit.extra) for unit in units if unit.reason is None and unit.value == 1 and
                  unit.extra is not None)
    return {"lagDays": {"n": len(post), **{name: quantile(post, level) for name, level in QUANTILES}},
            "lagModel": "followup lag writes the lag quartiles with bootstrap intervals (followup.lag.*)"}


SPECS: dict[str, Spec] = {
    "compromised-share": Spec("*", compromised_units, status_levels, status_cells=False),
    "sole-or-essential": Spec("S1", dependency_units, binary),
    # The lag model's after-notice share uses the same bootstrap scope: equal B and seed give equal intervals.
    "cited-after-notice": Spec("S1", after_notice_units, binary, detail=lag_detail, scope_label="after-notice"),
    "acknowledges-notice": Spec("S1", acknowledgement_units, binary),
    "second-generation": Spec("S1", second_generation_units, binary),
    "numeric-classes": Spec("S3", numeric_units, numeric_levels),
}


# ----------------------------------------------------------------------------- computation
def _cells(units: Sequence[Unit], journals: Sequence[str], statuses: Sequence[str], by: Sequence[str],
           status_cells: bool) -> list[tuple[str | None, int | None, str | None]]:
    journal_cells = [None, *journals] if "journal" in by else [None]
    years = sorted({unit.year for unit in units if unit.year is not None})
    year_cells = [None, *years] if "year" in by else [None]
    status_cells_list = [None, *statuses] if status_cells and "status" in by else [None]
    return [(journal, year, status) for journal in journal_cells for year in year_cells for status in status_cells_list]


def _in(unit: Unit, journal: str | None, year: int | None, status: str | None) -> bool:
    return ((journal is None or unit.journal == journal) and (year is None or unit.year == year)
            and (status is None or unit.status == status))


def _coverage(units: Iterable[Unit]) -> dict[tuple[str, str], tuple[int, float]]:
    counts: dict[tuple[str, str], tuple[int, float]] = {}
    for unit in units:
        key = ("assessable", "-") if unit.reason is None else ("unassessable", unit.reason)
        count, weight = counts.get(key, (0, 0.0))
        counts[key] = (count + unit.count, weight + unit.weight)
    return counts


def compute(connection: sqlite3.Connection, protocol: Mapping[str, Any], declared: Mapping[str, Any], *, b: int,
            seed: Any) -> dict[str, Any]:
    """Estimate rows and coverage rows of one declared estimand."""
    estimand = declared["id"]
    spec = SPECS[estimand]
    by = list(declared.get("by") or [])
    journals = list(protocol["journals"])
    slugs = journal_slugs(journals)
    statuses = list(protocol["strata"]["S1"]["statuses"])
    units = spec.units(connection, protocol)
    levels = spec.levels(protocol, by)
    stratum = None if spec.stratum == "*" else spec.stratum
    rows = 0
    for journal, year, status in _cells(units, journals, statuses, by, spec.status_cells):
        cell = [unit for unit in units if _in(unit, journal, year, status)]
        assessable = [unit for unit in cell if unit.reason is None]
        clusters: dict[str, dict[tuple[str, str], list[float]]] = defaultdict(dict)
        for unit in assessable:
            sums = clusters[unit.journal].setdefault(unit.cluster, [0.0] * (len(levels) + 1))
            sums[0] += unit.weight
            for index, (_, test) in enumerate(levels, 1):
                if test(unit.value):
                    sums[index] += unit.weight
        groups = [[clusters[name][key] for key in sorted(clusters[name])] for name in sorted(clusters)]
        cell_scope = (slugs.get(journal, ALL), year if year is not None else ALL, status or ALL)
        scope_parts = (spec.scope_label, *cell_scope) if spec.scope_label else ("followup", estimand, *cell_scope)
        boot = cluster_ratio_bootstrap(groups, b=b, seed=seed, scope_parts=scope_parts, levels=len(levels))
        denominator = sum(unit.weight for unit in assessable)
        for index, (level, test) in enumerate(levels):
            detail = {"estimand": estimand, "level": level, "status": status,
                      "levelRole": "numerator" if level else None, "statusRole": "subpopulation" if status else None,
                      "numerator": declared["numerator"], "denominator": declared["denominator"],
                      "by": by, "weights": WEIGHTS, "interval": INTERVAL, "b": b, "seed": seed,
                      "scope": list(scope_parts), "replicates": boot["replicates"], "reviews": boot["clusters"],
                      **({"labelSource": declared["labelSource"]} if declared.get("labelSource") else {}),
                      **(spec.detail(cell) if spec.detail else {})}
            rows += write_share_rows(
                connection, estimand=estimand, qualifiers=[level, status], journal=journal,
                slug=slugs.get(journal, ALL), year=year, stratum=stratum,
                numerator=sum(unit.weight for unit in assessable if test(unit.value)), denominator=denominator,
                units=sum(unit.count for unit in assessable), estimate=boot["estimates"][index],
                bounds=boot["bounds"][index], detail=detail)
        write_coverage(connection, estimand + (f".{status}" if status else ""), journal, year, spec.stratum,
                       _coverage(cell))
    if estimand == "numeric-classes" and not units:
        excluded(connection, estimand, journals)
    return {"units": sum(unit.count for unit in units),
            "assessable": sum(unit.count for unit in units if unit.reason is None), "rows": rows}


def excluded(connection: sqlite3.Connection, estimand: str, journals: Sequence[str]) -> None:
    """Coverage of a dropped stratum: its eligible loci are excluded by the protocol."""
    for journal in [None, *journals]:
        row = connection.execute("SELECT eligible, selected FROM fu_stratum_sizes WHERE stratum='S3' AND journal=?",
                                 (journal or "*",)).fetchone()
        if row and row[0] and not row[1]:
            write_coverage(connection, estimand, journal, None, "S3",
                           {("unassessable", "excluded-by-protocol"): (int(row[0]), float(row[0]))})


def distortion(connection: sqlite3.Connection, protocol: Mapping[str, Any], declared: Mapping[str, Any], *, b: int,
               seed: Any) -> dict[str, Any]:
    """Not measured until adjudication labels exist: the adverse S2 loci awaiting labels, per cell."""
    by = list(declared.get("by") or [])
    journals = list(protocol["journals"])
    slugs = journal_slugs(journals)
    statuses = list(protocol["strata"]["S1"]["statuses"])
    units = adverse_s2_units(connection, protocol)
    rows = 0
    for journal, year, status in _cells(units, journals, statuses, by, True):
        cell = [unit for unit in units if _in(unit, journal, year, status)]
        detail = {"estimand": "distortion-type", "status": status, "numerator": declared["numerator"],
                  "denominator": declared["denominator"], "labelSource": declared.get("labelSource") or "adjudication",
                  "labels": list(DISTORTION_LABELS), "awaitingLabels": len(cell), "b": b, "seed": seed,
                  "note": "adjudicated distortion labels are not part of this build; the estimand is not measured"}
        rows += write_share_rows(connection, estimand="distortion-type", qualifiers=[status], journal=journal,
                                 slug=slugs.get(journal, ALL), year=year, stratum="S2", numerator=0.0,
                                 denominator=0.0, units=0, estimate=None, bounds=(None, None), detail=detail)
        write_coverage(connection, "distortion-type" + (f".{status}" if status else ""), journal, year, "S2",
                       _coverage(cell))
    return {"units": len(units), "assessable": 0, "awaitingLabels": len(units), "rows": rows}


def run_estimands(connection: sqlite3.Connection, protocol: Mapping[str, Any], *, b: int, seed: Any) -> dict[str, Any]:
    require_freeze(connection)
    declared = list(protocol["estimands"])
    ids = [item["id"] for item in declared]
    if len(set(ids)) != len(ids):
        raise ValidationError("the protocol declares an estimand twice")
    results = {}
    for item in declared:
        clear_estimates(connection, item["id"])
        if item["id"] == "distortion-type":
            results[item["id"]] = distortion(connection, protocol, item, b=b, seed=seed)
        else:
            results[item["id"]] = compute(connection, protocol, item, b=b, seed=seed)
    return {"estimands": results, "b": b, "seed": seed, "weights": WEIGHTS, "interval": INTERVAL}


def cmd_estimands(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = protocol_of(branch)
    b = int(args.b) if getattr(args, "b", None) is not None else int(protocol["bootstrap"]["b"])
    seed = args.bootstrap_seed if getattr(args, "bootstrap_seed", None) is not None else protocol["bootstrap"]["seed"]
    if b < 1:
        raise ValidationError("--b must be a positive number of bootstrap replicates")
    with branch.writing() as connection:
        return {"database": str(branch.path), **run_estimands(connection, protocol, b=b, seed=seed)}
