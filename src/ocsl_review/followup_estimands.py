# SPDX-License-Identifier: Apache-2.0
"""The predeclared estimands of a follow-up audit (``followup estimands``).

Every estimand the protocol declares is computed by journal, by year and by status where its ``by`` names them (the
cross of the named breakdowns, with ``all`` margins), as a Hajek-weighted share sum(w y) / sum(w) with w = 1 / pi, the
realised inclusion probability, and a percentile bootstrap that resamples reviews with replacement within journal
(replicates and seed from the protocol unless ``--b`` / ``--bootstrap-seed`` override them; one scope tuple per
estimand and cell). Units without a verdict never enter a numerator or a denominator: each goes to ``fu_coverage``
with its reason. Journal and status cells form the protocol's full grid; a year cell without any unit is not written.
Abstract silence and failed downloads are reasons, never "unsupported".

| Id | Numerator | Denominator | Units |
|---|---|---|---|
| compromised-share | loci whose cited work's status is one of the protocol's S1 statuses (each status a level when "status" is in ``by``) | every in-body identity-resolved frame locus, a work of unknown status included (the design's estimand); sensitivity ``available-case``: loci with a known status only | frame (census) |
| sole-or-essential | S1 loci with dependency sole or essential; level ``anchor-supported``: of those, loci whose own cited work supports the claim at full text | S1 loci whose own full-text verdict is assessable and whose dependency is sole, essential or redundant | S1 |
| cited-after-notice | S1 loci whose review went online after the notice (plus the protocol's grace days) | S1 loci with a decidable comparison (the lag distribution in the detail); sensitivity ``grace-<N>d`` with the protocol's sensitivity grace days | S1 |
| acknowledges-notice | post-notice S1 loci with function notes-retraction | post-notice S1 loci with an assessable function; sensitivity as for cited-after-notice | S1 |
| second-generation | sampled later articles (cited-by edges) with a judged candidate sentence that restates the claim and cites the review for it | sampled later articles whose candidate sentences were all judged or one of them confirmed (weights from the edge sample) | edges of sole or essential anchors |
| distortion-type | adverse S2 loci per adjudicated distortion label | adjudicated adverse S2 loci | S2 |
| numeric-classes | S3 loci per numeric class | assessable S3 loci | S3 |

``contrasts: ["journal"]`` predeclares journal contrasts of an estimand: every later journal of the protocol against
its first, as the ratio and the difference of the two shares with intervals from the same bootstrap (reviews resampled
within each journal, the journals independent).

``--scope pilot`` computes the judged estimands (sole-or-essential, cited-after-notice, acknowledges-notice,
second-generation) over the pilot subsample only, weighted by 1 / pilot probability (the realised probability of the
seeded pilot draw per status), under the keys ``followup.pilot.<estimand>...``; the census estimands
(compromised-share, numeric-classes, distortion-type) have no pilot form.

Distortion labels come from adjudication, which this build does not hold: the estimand reports "not-measured" with the
number of adverse S2 loci (full-text verdict contradicted or not addressed) awaiting labels. Keys:
``followup[.pilot].<estimand>[.<variant>][.<level>][.<status>].<journal-slug|all>.<year|all>.<share|lo|hi|n|numerator|
denominator>``; contrasts ``followup.<estimand>.journal-ratio|journal-difference[.<variant>][.<level>].<journal>-vs-
<first>.all.<value|lo|hi|n>``.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import claim_match, estimands as census_estimands
from .errors import ValidationError
from .followup_branch import FollowupBranch, require_freeze
from .followup_model import SUPPORT_INSTRUMENTS
from .followup_schema import ADVERSE, NUMERIC_CLASSES
from .followup_stats import (INTERVAL, QUANTILES, clear_estimates, cluster_ratio_bootstrap, grace_days, lag_rows,
                             quantile, rng, write_coverage, write_share_rows, write_value_rows)
from .followup_strata import ALL, journal_slugs, protocol_of

ESTIMANDS = ("compromised-share", "sole-or-essential", "cited-after-notice", "acknowledges-notice", "second-generation",
             "distortion-type", "numeric-classes")
DISTORTION_LABELS = ("numeric-drift", "optimal-read-as-actual", "subgroup-read-as-population",
                     "single-arm-read-as-randomised", "negative-read-as-positive", "methods-paper-as-authority")
WEIGHTS = "1/realised inclusion probability (Hajek ratio)"
PILOT_WEIGHTS = "1/realised pilot probability (Hajek ratio over the pilot subsample)"
PILOT_ESTIMANDS = ("sole-or-essential", "cited-after-notice", "acknowledges-notice", "second-generation")


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
    note: str | None = None     # an assessable unit's coverage note (a denominator unit of unknown status)


@dataclass(frozen=True)
class Spec:
    stratum: str
    # (connection, protocol, scope) -> {variant qualifier (None: the primary estimand): units}
    units: Callable[[sqlite3.Connection, Mapping[str, Any], str], dict[str | None, list[Unit]]]
    levels: Callable[[Mapping[str, Any], Sequence[str]], list[tuple[str | None, Callable[[Any], bool]]]]
    status_cells: bool = True
    detail: Callable[[Sequence[Unit]], dict[str, Any]] | None = None
    scope_label: str | None = None


# ----------------------------------------------------------------------------- units
def _statuses(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT work_key, status FROM fu_work_status").fetchall())


def compromised_units(connection: sqlite3.Connection, protocol: Mapping[str, Any],
                      scope: str = "all") -> dict[str | None, list[Unit]]:
    """Frame loci grouped by review, year and status (census: weight 1 per locus). The primary estimand keeps loci
    whose work's status is unknown in the denominator (the design's estimand over every in-body identity-resolved
    locus; a coverage note says how many); the ``available-case`` sensitivity moves them to coverage."""
    s1 = protocol["strata"]["S1"]
    identities = list(s1["identities"])
    inbody = " AND f.cited_inbody=1" if s1["inBodyOnly"] else ""
    primary, available = [], []
    for journal, doi, year, resolved, status, count in connection.execute(
            "SELECT f.journal, f.doi, f.review_year, "
            f"f.work_key IS NOT NULL AND f.identity IN ({','.join('?' for _ in identities)}) AS resolved, "
            f"w.status, COUNT(*) FROM fu_frame f LEFT JOIN fu_work_status w ON w.work_key=f.work_key WHERE 1=1{inbody} "
            "GROUP BY f.journal, f.doi, f.review_year, resolved, w.status", identities):
        base = ((journal, doi), journal, year)
        if not resolved:
            unit = Unit(*base, None, reason="identity-unresolved", weight=float(count), count=count)
            primary.append(unit)
            available.append(unit)
        elif status in (None, "unknown"):
            primary.append(Unit(*base, None, value="unknown", weight=float(count), count=count, note="status-unknown"))
            available.append(Unit(*base, None, reason="status-unknown", weight=float(count), count=count))
        else:
            unit = Unit(*base, status, value=status, weight=float(count), count=count)
            primary.append(unit)
            available.append(unit)
    return {None: primary, "available-case": available}


def _s1(connection: sqlite3.Connection, extra: str = "", join: str = "", *, pilot: bool = False) -> list[sqlite3.Row]:
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(
        f"SELECT f.locus_id, f.journal, f.doi, f.review_year, s.inclusion_probability, s.pilot_probability, "
        f"w.status{extra} FROM fu_strata s JOIN fu_frame f ON f.locus_id=s.locus_id LEFT JOIN fu_work_status w "
        f"ON w.work_key=f.work_key {join}WHERE s.stratum='S1'" + (" AND s.pilot=1" if pilot else "") +
        " ORDER BY f.locus_id").fetchall()


def _weight(row: Mapping[str, Any], scope: str) -> float:
    return 1.0 / (row["pilot_probability"] if scope == "pilot" else row["inclusion_probability"])


def dependency_units(connection: sqlite3.Connection, protocol: Mapping[str, Any],
                     scope: str = "all") -> dict[str | None, list[Unit]]:
    """S1 anchors with a determined dependency and an assessable full-text verdict of their own (the design's "S1 loci
    with assessable full text"); the value is (sole or essential, own work supports the claim)."""
    from .followup_dependency import support_classes
    supported = support_classes()["supported"]
    units = []
    for row in _s1(connection, ", d.class AS dependency, u.verdict AS anchor_verdict, u.assessable AS "
                               "anchor_assessable, u.reason AS anchor_reason",
                   "LEFT JOIN fu_dependency d ON d.locus_id=f.locus_id LEFT JOIN fu_support u ON "
                   "u.locus_id=f.locus_id AND u.tier='full-text' ", pilot=scope == "pilot"):
        base = ((row["journal"], row["doi"]), row["journal"], row["review_year"], row["status"])
        weight = _weight(row, scope)
        if row["dependency"] not in ("sole", "essential", "redundant"):
            units.append(Unit(*base, reason="undetermined" if row["dependency"] == "undetermined" else "not-judged",
                              weight=weight))
        elif not row["anchor_assessable"]:
            units.append(Unit(*base, reason=row["anchor_reason"] or "not-judged", weight=weight))
        else:
            units.append(Unit(*base, value=(row["dependency"] != "redundant", row["anchor_verdict"] in supported),
                              weight=weight))
    return {None: units}


def _lag_units(rows: Sequence[Mapping[str, Any]]) -> list[Unit]:
    return [Unit((row["journal"], row["doi"]), row["journal"], row["year"], row["status"],
                 value=row["after_notice"], reason=row["reason"] if row["after_notice"] is None else None,
                 weight=row["weight"], extra=row["lag_days"]) for row in rows]


def _graces(protocol: Mapping[str, Any]) -> dict[str | None, int]:
    """The primary grace days and, when the protocol predeclares one, the sensitivity reading's."""
    graces: dict[str | None, int] = {None: grace_days(protocol)}
    sensitivity = ((protocol.get("timing") or {}).get("sensitivityGraceDays"))
    if sensitivity is not None:
        graces[f"grace-{int(sensitivity)}d"] = int(sensitivity)
    return graces


def after_notice_units(connection: sqlite3.Connection, protocol: Mapping[str, Any],
                       scope: str = "all") -> dict[str | None, list[Unit]]:
    return {variant: _lag_units(lag_rows(connection, grace=grace, pilot_only=scope == "pilot"))
            for variant, grace in _graces(protocol).items()}


def acknowledgement_units(connection: sqlite3.Connection, protocol: Mapping[str, Any],
                          scope: str = "all") -> dict[str | None, list[Unit]]:
    functions = {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
        "SELECT locus_id, class, assessable, reason FROM fu_function")}
    variants: dict[str | None, list[Unit]] = {}
    for variant, grace in _graces(protocol).items():
        units = []
        for row in lag_rows(connection, grace=grace, pilot_only=scope == "pilot"):
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
        variants[variant] = units
    return variants


def second_generation_units(connection: sqlite3.Connection, protocol: Mapping[str, Any],
                            scope: str = "all") -> dict[str | None, list[Unit]]:
    """One unit per sampled edge (a later article citing the anchor's review), weight 1 / edge inclusion probability
    (times 1 / pilot probability in the pilot scope). The article restates the claim when one of its judged candidate
    sentences is same-claim and cites the review for it; it does not when every candidate was judged and none does;
    otherwise (candidates left unjudged, no located sentence, no anchor text) it goes to coverage with its reason.
    Sentences beyond the BM25 cut are no candidates (counted in the detail)."""
    statuses = _statuses(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    pilot = {row[0]: row[1] for row in connection.execute(
        "SELECT locus_id, pilot_probability FROM fu_strata WHERE stratum='S1' AND pilot=1")}
    matches: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in cursor.execute("SELECT anchor_locus_id, citing_key, rank, match, reliance, assessable, reason "
                              "FROM fu_secondgen_matches ORDER BY anchor_locus_id, citing_key, rank"):
        matches[(row["anchor_locus_id"], row["citing_key"])].append(row)
    units = []
    for row in cursor.execute(
            "SELECT s.anchor_locus_id, s.citing_key, s.inclusion_probability, c.state, c.reason, f.journal, f.doi, "
            "f.review_year, f.work_key FROM fu_secondgen_sample s JOIN fu_frame f ON f.locus_id=s.anchor_locus_id "
            "LEFT JOIN fu_secondgen_candidates c ON c.anchor_locus_id=s.anchor_locus_id AND c.citing_key=s.citing_key "
            "ORDER BY s.anchor_locus_id, s.citing_key").fetchall():
        if scope == "pilot" and row["anchor_locus_id"] not in pilot:
            continue
        weight = 1.0 / row["inclusion_probability"] / (pilot[row["anchor_locus_id"]] if scope == "pilot" else 1.0)
        base = ((row["journal"], row["doi"]), row["journal"], row["review_year"], statuses.get(row["work_key"]))
        found = matches.get((row["anchor_locus_id"], row["citing_key"]), [])
        candidates = [match for match in found if match["rank"] is not None]
        beyond = len(found) - len(candidates)
        if any(match["assessable"] and claim_match.propagates(dict(match)) for match in candidates):
            units.append(Unit(*base, value=True, weight=weight, extra=beyond))
        elif candidates and all(match["assessable"] for match in candidates):
            units.append(Unit(*base, value=False, weight=weight, extra=beyond))
        elif candidates:
            open_reason = next(match["reason"] for match in candidates if not match["assessable"])
            units.append(Unit(*base, reason=open_reason or "not-judged", weight=weight, extra=beyond))
        elif found:
            units.append(Unit(*base, reason=found[0]["reason"] or "not-judged", weight=weight, extra=beyond))
        else:
            units.append(Unit(*base, reason=row["reason"] or "not-judged", weight=weight, extra=beyond))
    return {None: units}


def second_generation_detail(units: Sequence[Unit]) -> dict[str, Any]:
    return {"unit": "sampled cited-by edge (a later article)",
            "edgesWithSentencesBeyondK": sum(1 for unit in units if unit.extra),
            "bm25Cut": "sentences beyond the protocol's k are no candidates; an article whose top-k candidates were "
                       "all judged without a restatement counts as not restating"}


def adverse_s2_units(connection: sqlite3.Connection, protocol: Mapping[str, Any], scope: str = "all"
                     ) -> list[Unit]:
    statuses = _statuses(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return [Unit((row["journal"], row["doi"]), row["journal"], row["review_year"], statuses.get(row["work_key"]),
                 reason="awaiting-labels", weight=1.0 / row["inclusion_probability"]) for row in cursor.execute(
        "SELECT f.journal, f.doi, f.review_year, f.work_key, s.inclusion_probability FROM fu_strata s "
        "JOIN fu_frame f ON f.locus_id=s.locus_id JOIN fu_support u ON u.locus_id=f.locus_id AND u.tier='full-text' "
        f"WHERE s.stratum='S2' AND u.assessable=1 AND u.verdict IN ({','.join('?' for _ in ADVERSE)}) "
        "ORDER BY f.locus_id", ADVERSE)]


def numeric_units(connection: sqlite3.Connection, protocol: Mapping[str, Any],
                  scope: str = "all") -> dict[str | None, list[Unit]]:
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
    return {None: units}


# ----------------------------------------------------------------------------- levels and details
def binary(protocol: Mapping[str, Any], by: Sequence[str]) -> list[tuple[str | None, Callable[[Any], bool]]]:
    return [(None, bool)]


def dependency_levels(protocol: Mapping[str, Any],
                      by: Sequence[str]) -> list[tuple[str | None, Callable[[Any], bool]]]:
    """Sole or essential; and, of those, the anchors whose own cited work supports the claim at full text."""
    return [(None, lambda value: bool(value[0])), ("anchor-supported", lambda value: bool(value[0] and value[1]))]


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
    "sole-or-essential": Spec("S1", dependency_units, dependency_levels),
    # The lag model's after-notice share uses the same bootstrap scope: equal B and seed give equal intervals.
    "cited-after-notice": Spec("S1", after_notice_units, binary, detail=lag_detail, scope_label="after-notice"),
    "acknowledges-notice": Spec("S1", acknowledgement_units, binary),
    "second-generation": Spec("S1", second_generation_units, binary, detail=second_generation_detail),
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
        key = ("assessable", unit.note or "-") if unit.reason is None else ("unassessable", unit.reason)
        count, weight = counts.get(key, (0, 0.0))
        counts[key] = (count + unit.count, weight + unit.weight)
    return counts


def _clusters(units: Sequence[Unit], levels: Sequence[tuple[str | None, Callable[[Any], bool]]]
              ) -> dict[str, dict[tuple[str, str], list[float]]]:
    clusters: dict[str, dict[tuple[str, str], list[float]]] = defaultdict(dict)
    for unit in units:
        if unit.reason is not None:
            continue
        sums = clusters[unit.journal].setdefault(unit.cluster, [0.0] * (len(levels) + 1))
        sums[0] += unit.weight
        for index, (_, test) in enumerate(levels, 1):
            if test(unit.value):
                sums[index] += unit.weight
    return clusters


def journal_contrasts(connection: sqlite3.Connection, units: Sequence[Unit],
                      levels: Sequence[tuple[str | None, Callable[[Any], bool]]], *, label: str, estimand: str,
                      variant: str | None, journals: Sequence[str], slugs: Mapping[str, str], stratum: str | None,
                      b: int, seed: Any, detail: Mapping[str, Any]) -> int:
    """Every later journal against the first: ratio and difference of the shares, reviews resampled within each
    journal (the journals are independent strata), one scope tuple per pair."""
    clusters = _clusters(units, levels)
    reference = journals[0]
    written = 0
    for journal in journals[1:]:
        pair = [[clusters.get(name, {})[key] for key in sorted(clusters.get(name, {}))] for name in (journal, reference)]
        scope_parts = ("followup", estimand, variant or ALL, "journal-contrast", slugs[journal], slugs[reference])
        generator = rng(seed, *scope_parts)

        def shares(groups: Sequence[Sequence[Sequence[float]]]) -> list[list[float | None]]:
            totals = [[sum(cluster[i] for cluster in group) for i in range(len(levels) + 1)] for group in groups]
            return [[total[i] / total[0] if total[0] else None for i in range(1, len(levels) + 1)]
                    for total in totals]
        observed = shares(pair)
        replicates: list[list[tuple[float, float]]] = [[] for _ in levels]
        if all(pair):
            for _ in range(int(b)):
                drawn = [[group[index] for index in generator.choices(range(len(group)), k=len(group))]
                         for group in pair]
                for level, (a, c) in enumerate(zip(*shares(drawn))):
                    if a is not None and c is not None:
                        replicates[level].append((a / c if c else None, a - c))
        name = f"{slugs[journal]}-vs-{slugs[reference]}"
        for index, (level, _) in enumerate(levels):
            a, c = observed[0][index], observed[1][index]
            ratio = a / c if a is not None and c else None
            difference = a - c if a is not None and c is not None else None
            ratios = [value for value, _ in replicates[index] if value is not None]
            differences = [value for _, value in replicates[index]]
            common = {**detail, "contrast": f"{journal} against {reference}", "level": level, "variant": variant,
                      "interval": "percentile bootstrap: reviews resampled with replacement within each journal",
                      "scope": list(scope_parts), "b": b, "seed": seed, "replicates": len(replicates[index])}
            n = sum(1 for unit in units if unit.reason is None and unit.journal in (journal, reference))
            for qualifier, value, values in (("journal-ratio", ratio, ratios),
                                             ("journal-difference", difference, differences)):
                written += write_value_rows(
                    connection, estimand=label, qualifiers=[qualifier, variant, level], journal=None, slug=name,
                    year=None, stratum=stratum, value=value,
                    bounds=census_estimands.percentile_bounds(values) if value is not None else (None, None), units=n,
                    detail={**common, "statistic": qualifier})
    return written


def compute(connection: sqlite3.Connection, protocol: Mapping[str, Any], declared: Mapping[str, Any], *, b: int,
            seed: Any, scope: str = "all") -> dict[str, Any]:
    """Estimate rows and coverage rows of one declared estimand (its variants and predeclared contrasts)."""
    estimand = declared["id"]
    spec = SPECS[estimand]
    label = ("pilot." if scope == "pilot" else "") + estimand
    by = list(declared.get("by") or [])
    journals = list(protocol["journals"])
    slugs = journal_slugs(journals)
    statuses = list(protocol["strata"]["S1"]["statuses"])
    variants = spec.units(connection, protocol, scope)
    levels = spec.levels(protocol, by)
    stratum = None if spec.stratum == "*" else spec.stratum
    rows = 0
    for variant, units in variants.items():
        for journal, year, status in _cells(units, journals, statuses, by, spec.status_cells):
            cell = [unit for unit in units if _in(unit, journal, year, status)]
            if year is not None and not cell:
                continue
            assessable = [unit for unit in cell if unit.reason is None]
            clusters = _clusters(cell, levels)
            groups = [[clusters[name][key] for key in sorted(clusters[name])] for name in sorted(clusters)]
            cell_scope = (slugs.get(journal, ALL), year if year is not None else ALL, status or ALL)
            prefix = (spec.scope_label,) if spec.scope_label else ("followup", estimand)
            scope_parts = (*prefix, *(("pilot",) if scope == "pilot" else ()), *((variant,) if variant else ()),
                           *cell_scope)
            boot = cluster_ratio_bootstrap(groups, b=b, seed=seed, scope_parts=scope_parts, levels=len(levels))
            denominator = sum(unit.weight for unit in assessable)
            extra = spec.detail(cell) if spec.detail else {}
            for index, (level, test) in enumerate(levels):
                detail = {"estimand": estimand, "scope": scope, "variant": variant, "level": level, "status": status,
                          "levelRole": "numerator" if level else None,
                          "statusRole": "subpopulation" if status else None,
                          "numerator": declared["numerator"], "denominator": declared["denominator"],
                          "by": by, "weights": PILOT_WEIGHTS if scope == "pilot" else WEIGHTS, "interval": INTERVAL,
                          "b": b, "seed": seed, "bootstrapScope": list(scope_parts), "replicates": boot["replicates"],
                          "reviews": boot["clusters"],
                          **({"labelSource": declared["labelSource"]} if declared.get("labelSource") else {}),
                          **extra}
                rows += write_share_rows(
                    connection, estimand=label, qualifiers=[variant, level, status], journal=journal,
                    slug=slugs.get(journal, ALL), year=year, stratum=stratum,
                    numerator=sum(unit.weight for unit in assessable if test(unit.value)), denominator=denominator,
                    units=sum(unit.count for unit in assessable), estimate=boot["estimates"][index],
                    bounds=boot["bounds"][index], detail=detail)
            write_coverage(connection, label + (f".{variant}" if variant else "") + (f".{status}" if status else ""),
                           journal, year, spec.stratum, _coverage(cell))
        if "journal" in (declared.get("contrasts") or []) and len(journals) > 1:
            rows += journal_contrasts(connection, units, levels, label=label, estimand=estimand, variant=variant,
                                      journals=journals, slugs=slugs, stratum=stratum, b=b, seed=seed,
                                      detail={"estimand": estimand, "numerator": declared["numerator"],
                                              "denominator": declared["denominator"]})
    primary = variants.get(None, [])
    if estimand == "numeric-classes" and not primary:
        excluded(connection, estimand, journals)
    return {"units": sum(unit.count for unit in primary),
            "assessable": sum(unit.count for unit in primary if unit.reason is None), "rows": rows,
            "variants": [variant for variant in variants if variant]}


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
        if year is not None and not cell:
            continue
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


# ----------------------------------------------------------------------------- replicate agreement
AGREEMENT = "replicate-agreement"
AGREEMENT_FIELDS = {"citation-function@1": "function", "numeric-check@1": "decision", "claim-match@1": "match"}


def agreement_label(instrument_id: str, verdict: Mapping[str, Any]) -> str | None:
    """The class a replicate is compared on: citation-support@2's class map (supported, adverse, uncertain) for both
    support instruments; the class or decision field of the follow-up instruments (abstain is its own class)."""
    if instrument_id in SUPPORT_INSTRUMENTS:
        from .followup_dependency import support_classes
        value = verdict.get("supportVerdict")
        return next((name for name, members in support_classes().items() if value in members), None)
    field = AGREEMENT_FIELDS.get(instrument_id)
    return verdict.get(field) if field else None


def cohen_kappa(pairs: Sequence[tuple[str, str]]) -> tuple[float | None, float | None]:
    """(raw agreement, Cohen's kappa) of (primary, replicate) class pairs; kappa None when chance agreement is 1."""
    if not pairs:
        return None, None
    n = len(pairs)
    observed = sum(1 for a, c in pairs if a == c) / n
    first, second = defaultdict(int), defaultdict(int)
    for a, c in pairs:
        first[a] += 1
        second[c] += 1
    expected = sum(first[label] * second.get(label, 0) for label in first) / (n * n)
    return observed, ((observed - expected) / (1 - expected) if expected < 1 else None)


def replicate_agreement(connection: sqlite3.Connection, protocol: Mapping[str, Any], *, b: int,
                        seed: Any) -> dict[str, Any]:
    """Agreement of every replicate verdict with the primary verdict of the same item, per instrument digest and
    tier, overall and per replicate group: pairs, raw agreement and Cohen's kappa over the instrument's classes, with
    percentile intervals from a bootstrap that resamples reviews within journal. A replicate measures stability across
    two runs of one model, not accuracy."""
    from .followup_strata import replicate_groups
    clear_estimates(connection, AGREEMENT)
    journals = list(protocol["journals"])
    frame = {row[0]: (row[1], row[2]) for row in connection.execute("SELECT locus_id, journal, doi FROM fu_frame")}
    reported = []
    for instrument_id, instrument_digest, tier in connection.execute(
            "SELECT DISTINCT instrument_id, instrument_digest, tier FROM fu_judgments WHERE variant='replicate' AND "
            "outcome='accepted' ORDER BY instrument_id, instrument_digest, tier").fetchall():
        verdicts = {}
        for variant in ("primary", "replicate"):
            verdicts[variant] = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT item_key, verdict_json FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant=? "
                "AND outcome='accepted'", (instrument_digest, tier, variant))}
        loci = {key: key.split("|")[0] for key in verdicts["replicate"]}
        groups = replicate_groups(connection, set(loci.values()))
        pairs: list[tuple[tuple[str, str], str, str, str]] = []
        unpaired = 0
        for key, verdict in sorted(verdicts["replicate"].items()):
            primary = verdicts["primary"].get(key)
            locus = loci[key]
            first = agreement_label(instrument_id, primary) if primary else None
            second = agreement_label(instrument_id, verdict)
            if first is None or second is None or locus not in frame:
                unpaired += 1
                continue
            pairs.append((frame[locus], groups.get(locus, "unstratified"), first, second))
        name = census_estimands_slug(instrument_id)
        rows = 0
        for group in [None, *sorted({pair[1] for pair in pairs})]:
            selected = [pair for pair in pairs if group is None or pair[1] == group]
            observed, kappa = cohen_kappa([(pair[2], pair[3]) for pair in selected])
            clusters: dict[str, dict[tuple[str, str], list[tuple[str, str]]]] = defaultdict(dict)
            for cluster, _, first, second in selected:
                clusters[cluster[0]].setdefault(cluster, []).append((first, second))
            nested = [[clusters[journal][key] for key in sorted(clusters[journal])] for journal in sorted(clusters)]
            scope_parts = (AGREEMENT, name, tier, group or ALL)
            generator = rng(seed, *scope_parts)
            agreements, kappas = [], []
            if selected:
                for _ in range(int(b)):
                    drawn = [pair for journal in nested
                             for index in generator.choices(range(len(journal)), k=len(journal))
                             for pair in journal[index]]
                    value, coefficient = cohen_kappa(drawn)
                    if value is not None:
                        agreements.append(value)
                    if coefficient is not None:
                        kappas.append(coefficient)
            detail = {"estimand": AGREEMENT, "instrument": instrument_id, "instrumentDigest": instrument_digest,
                      "tier": tier, "group": group, "pairs": len(selected), "unpaired": unpaired if group is None else
                      None, "classes": sorted({label for pair in selected for label in pair[2:]}),
                      "classMap": "citation-support@2 class map" if instrument_id in SUPPORT_INSTRUMENTS else
                      AGREEMENT_FIELDS.get(instrument_id), "interval": INTERVAL, "b": b, "seed": seed,
                      "scope": list(scope_parts), "replicates": len(kappas),
                      "interpretation": "agreement of two runs of one model: stability, not accuracy"}
            qualifiers = [name, tier, census_estimands_slug(group) if group else None]
            rows += write_value_rows(connection, estimand=AGREEMENT, qualifiers=[*qualifiers, "agreement"],
                                     journal=None, slug=ALL, year=None, stratum=group, value=observed,
                                     bounds=census_estimands.percentile_bounds(agreements) if observed is not None
                                     else (None, None), units=len(selected), detail=detail)
            rows += write_value_rows(connection, estimand=AGREEMENT, qualifiers=[*qualifiers, "kappa"],
                                     journal=None, slug=ALL, year=None, stratum=group, value=kappa,
                                     bounds=census_estimands.percentile_bounds(kappas) if kappa is not None
                                     else (None, None), units=len(selected), detail=detail)
        write_coverage(connection, f"{AGREEMENT}.{name}.{tier}", None, None, "*",
                       {key: value for key, value in {("assessable", "-"): (len(pairs), float(len(pairs))),
                                                      ("unassessable", "unpaired"): (unpaired, float(unpaired))}.items()
                        if value[0]})
        reported.append({"instrument": instrument_id, "digest": instrument_digest, "tier": tier,
                         "pairs": len(pairs), "unpaired": unpaired, "rows": rows})
    return {"journals": journals, "reported": reported}


def census_estimands_slug(value: str) -> str:
    from .corpus_facts import slug
    return slug(value)


def run_estimands(connection: sqlite3.Connection, protocol: Mapping[str, Any], *, b: int, seed: Any,
                  scope: str = "all") -> dict[str, Any]:
    require_freeze(connection)
    if scope not in ("all", "pilot"):
        raise ValidationError("--scope is pilot or all")
    declared = list(protocol["estimands"])
    ids = [item["id"] for item in declared]
    if len(set(ids)) != len(ids):
        raise ValidationError("the protocol declares an estimand twice")
    results: dict[str, Any] = {}
    skipped = []
    for item in declared:
        if scope == "pilot" and item["id"] not in PILOT_ESTIMANDS:
            skipped.append(item["id"])
            continue
        label = ("pilot." if scope == "pilot" else "") + item["id"]
        clear_estimates(connection, label)
        if item["id"] == "distortion-type":
            results[item["id"]] = distortion(connection, protocol, item, b=b, seed=seed)
        else:
            results[item["id"]] = compute(connection, protocol, item, b=b, seed=seed, scope=scope)
    agreement = replicate_agreement(connection, protocol, b=b, seed=seed) if scope == "all" else None
    return {"estimands": results, "scope": scope, "b": b, "seed": seed,
            "weights": PILOT_WEIGHTS if scope == "pilot" else WEIGHTS, "interval": INTERVAL,
            **({"noPilotForm": skipped} if skipped else {}),
            **({"replicateAgreement": agreement["reported"]} if agreement is not None else {})}


def cmd_estimands(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = protocol_of(branch)
    b = int(args.b) if getattr(args, "b", None) is not None else int(protocol["bootstrap"]["b"])
    seed = args.bootstrap_seed if getattr(args, "bootstrap_seed", None) is not None else protocol["bootstrap"]["seed"]
    if b < 1:
        raise ValidationError("--b must be a positive number of bootstrap replicates")
    with branch.writing() as connection:
        result = run_estimands(connection, protocol, b=b, seed=seed, scope=getattr(args, "scope", None) or "all")
    overridden = (b, seed) != (int(protocol["bootstrap"]["b"]), protocol["bootstrap"]["seed"])
    return {"database": str(branch.path), **result,
            **({"bootstrapOverride": "B or seed differ from the protocol's: exploratory estimates; facts refuse "
                                     "mixed settings and the gates report them"} if overridden else {})}
