# SPDX-License-Identifier: Apache-2.0
"""Statistics of a follow-up audit: the hotspot test (``followup hotspots``), the retraction-lag model (``followup
lag``) and the weighted, review-clustered bootstrap the estimands use.

Hotspot test (``s2-beta-binomial@1``, the S2 selection). The unit is a cited work *w*. Over the parent entries that
cite *w* in the body, with an identity class the protocol lists, that the census model judged with an abstract-tier
category T or F (the census instrument's headline category): k_w = F entries, n_w = T + F entries. The base rate p_c
of each base cell (the protocol's journal and/or year) comes from all such entries; the work's expected rate is
mu_w = sum_c n_wc p_c / n_w. A work is a candidate when its distinct citing reviews reach the protocol's minimum and it
belongs to no excluded family. The overdispersion of the beta-binomial null is the moment estimate over candidates,
rho = sum[(k - n mu)^2 - n mu (1 - mu)] / sum[n mu (1 - mu) (n - 1)], clipped to [0, 1); the p-value is the upper tail
P(X >= k_w) (``estimands.upper_tail``), q-values are Benjamini-Hochberg over candidates (``estimands.
benjamini_hochberg``), and a candidate is selected when q <= the protocol's FDR level.

Retraction-lag model. For every S1 locus: the citing review's first-online date minus the deciding notice's date. The
two dates are compared at the coarser of their precisions (day, month, year); equal at that precision is undecidable
(``after_notice`` NULL, coverage); days are counted only when both dates are given to the day. Outputs (``fu_estimates``
keys ``followup.lag.*``): the share of loci cited after the notice by journal and year; quartiles of the lag of
post-notice citations with review-clustered bootstrap intervals; the rate ratio of citing reviews per work-year after
versus before the notice (work-clustered, ``estimands.ratio_bootstrap``); the trend of the after-notice share by year
(``estimands.year_trend``). Replicates and seed come from the protocol; every estimate has its own scope tuple.
"""
from __future__ import annotations

import math
import random
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from . import estimands
from .errors import ValidationError
from .followup_branch import FollowupBranch, require_freeze
from .followup_model import canonical
from .followup_strata import (ALL, excluded_patterns, family_works, frozen, journal_slugs, protocol_of, scope)

MODEL = "s2-beta-binomial@1"
LAG_RULE = "retraction-lag@1"
RHO_CEILING = math.nextafter(1.0, 0.0)
QUANTILES = (("days-q1", 0.25), ("days-median", 0.5), ("days-q3", 0.75))
PRECISION_ORDER = {"day": 0, "month": 1, "year": 2}
INTERVAL = "percentile bootstrap: reviews resampled with replacement within journal"


# ----------------------------------------------------------------------------- hotspot test
ENTRY_FIELDS = {"journal": 0, "year": 4}


def _entry_filter(protocol: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clause, params = scope(protocol)
    identities = list(protocol["strata"]["S1"]["identities"])
    return ("c.cited_inbody=1 AND c.model_judged=1 AND c.abs_category IN ('true','false') AND c.cited_work_key IS NOT "
            f"NULL AND c.identity IN ({','.join('?' for _ in identities)}) AND {clause}", [*identities, *params])


def hotspot_entries(connection: sqlite3.Connection, protocol: Mapping[str, Any], *,
                    limit: int | None = None) -> sqlite3.Cursor:
    """Classifiable parent entries, grouped by cited work: (journal, doi, work key, abstract category, review year)."""
    condition, params = _entry_filter(protocol)
    cursor = connection.cursor()
    cursor.row_factory = None
    return cursor.execute(
        "SELECT c.journal, c.doi, c.cited_work_key, c.abs_category, r.year FROM census.citations c "
        f"JOIN census.reviews r ON r.journal=c.journal AND r.doi=c.doi WHERE {condition} "
        "ORDER BY c.cited_work_key, c.journal, c.doi, c.label" + (" LIMIT ?" if limit else ""),
        [*params, *([int(limit)] if limit else [])])


def base_rates(connection: sqlite3.Connection, protocol: Mapping[str, Any], base: Sequence[str]) -> dict[tuple, Any]:
    """Not-supported share and counts of every base cell over all classifiable entries."""
    condition, params = _entry_filter(protocol)
    columns = ", ".join({"journal": "c.journal", "year": "r.year"}[name] for name in base)
    return {tuple(row[:-2]): {"k": row[-2], "n": row[-1], "rate": row[-2] / row[-1]} for row in connection.execute(
        f"SELECT {columns}, SUM(c.abs_category='false'), COUNT(*) FROM census.citations c "
        f"JOIN census.reviews r ON r.journal=c.journal AND r.doi=c.doi WHERE {condition} GROUP BY {columns}", params)}


def moment_rho(units: Iterable[tuple[int, int, float]]) -> float:
    """Moment estimate of the beta-binomial intraclass correlation over (k, n, mu) units, clipped to [0, 1)."""
    numerator = denominator = 0.0
    for k, n, mu in units:
        numerator += (k - n * mu) ** 2 - n * mu * (1 - mu)
        denominator += n * mu * (1 - mu) * (n - 1)
    if denominator <= 0:
        return 0.0
    return min(max(numerator / denominator, 0.0), RHO_CEILING)


def work_rows(entries: Iterable[Sequence[Any]], rates: Mapping[tuple, Mapping[str, Any]], *, base: Sequence[str],
              min_reviews: int, excluded: Callable[[str], bool]) -> Iterator[dict[str, Any]]:
    """One row per cited work from entries grouped by work: n, k, mu, expected, excess, candidate, family flag."""
    def row(work: str, cells: Mapping[tuple, list[int]], reviews: set) -> dict[str, Any]:
        n = sum(value[1] for value in cells.values())
        k = sum(value[0] for value in cells.values())
        mu = sum(value[1] * rates[cell]["rate"] for cell, value in cells.items()) / n
        family = bool(excluded(work))
        return {"work_key": work, "citing_reviews": len(reviews), "n": n, "k": k, "mu": mu, "expected": n * mu,
                "excess": k - n * mu, "family_excluded": int(family),
                "candidate": int(len(reviews) >= min_reviews and not family)}
    current, cells, reviews = None, defaultdict(lambda: [0, 0]), set()
    for entry in entries:
        if entry[2] != current:
            if current is not None:
                yield row(current, cells, reviews)
            current, cells, reviews = entry[2], defaultdict(lambda: [0, 0]), set()
        value = cells[tuple(entry[ENTRY_FIELDS[name]] for name in base)]
        value[0] += int(entry[3] == "false")
        value[1] += 1
        reviews.add((entry[0], entry[1]))
    if current is not None:
        yield row(current, cells, reviews)


def screen_candidates(candidates: Sequence[dict[str, Any]], fdr: float) -> float:
    """rho over the candidates, then each candidate's upper-tail p, BH q and selection (in place); returns rho."""
    rho = moment_rho((row["k"], row["n"], row["mu"]) for row in candidates)
    p_values = [estimands.upper_tail(row["k"], row["n"], row["mu"], rho) for row in candidates]
    for row, p, q in zip(candidates, p_values, estimands.benjamini_hochberg(p_values)):
        row.update(p_value=p, q_value=q, rho=rho, selected=int(q is not None and q <= fdr))
    return rho


def hotspot_test(entries: Iterable[Sequence[Any]], *, min_reviews: int, fdr: float, base: Sequence[str],
                 excluded: Callable[[str], bool]) -> dict[str, Any]:
    """The S2 test over in-memory entries (journal, doi, work key, category, year); see the module docstring."""
    ordered = sorted(entries, key=lambda entry: str(entry[2]))
    totals: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for entry in ordered:
        cell = totals[tuple(entry[ENTRY_FIELDS[name]] for name in base)]
        cell[0] += int(entry[3] == "false")
        cell[1] += 1
    rates = {cell: {"k": k, "n": n, "rate": k / n} for cell, (k, n) in totals.items()}
    rows = list(work_rows(ordered, rates, base=base, min_reviews=min_reviews, excluded=excluded))
    rho = screen_candidates([row for row in rows if row["candidate"]], fdr)
    for row in rows:
        row.setdefault("p_value", None)
        row.setdefault("q_value", None)
        row.setdefault("selected", 0)
        row["rho"] = rho
    return {"rows": rows, "rho": rho, "cells": rates}


HOTSPOT_INSERT = ("INSERT INTO fu_hotspots(work_key, citing_reviews, n, k, expected, excess, rho, p_value, q_value, "
                  "candidate, family_excluded, selected, model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _hotspot_row(row: Mapping[str, Any]) -> tuple:
    return (row["work_key"], row["citing_reviews"], row["n"], row["k"], row["expected"], row["excess"],
            row.get("rho", 0.0), row.get("p_value"), row.get("q_value"), row["candidate"], row["family_excluded"],
            row.get("selected", 0), MODEL)


def run_hotspots(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Stream the entries work by work: non-candidates are written at once, candidates are tested together."""
    if frozen(connection):
        raise ValidationError("the follow-up strata are frozen; the hotspot test that selects S2 cannot run again")
    spec = protocol["strata"]["S2"]
    base, fdr = list(spec["base"]), float(spec["fdr"])
    excluded_keys = family_works(connection, excluded_patterns(protocol))
    rates = base_rates(connection, protocol, base)
    connection.execute("DELETE FROM fu_hotspots")
    candidates: list[dict[str, Any]] = []
    works = family = 0
    for row in work_rows(hotspot_entries(connection, protocol), rates, base=base,
                         min_reviews=int(spec["minCitingReviews"]), excluded=excluded_keys.__contains__):
        works += 1
        family += row["family_excluded"]
        if row["candidate"]:
            candidates.append(row)
        else:
            connection.execute(HOTSPOT_INSERT, _hotspot_row(row))
    rho = screen_candidates(candidates, fdr)
    connection.executemany(HOTSPOT_INSERT, [_hotspot_row(row) for row in candidates])
    connection.execute("UPDATE fu_hotspots SET rho=?", (rho,))
    return {"model": MODEL, "works": works, "candidates": len(candidates), "familyExcluded": family,
            "selected": sum(row["selected"] for row in candidates), "rho": rho,
            "minCitingReviews": int(spec["minCitingReviews"]), "fdr": fdr, "base": base, "baseCells": len(rates)}


def cmd_hotspots(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = protocol_of(branch)
    with branch.writing() as connection:
        return {"database": str(branch.path), **run_hotspots(connection, protocol)}


# ----------------------------------------------------------------------------- weighted clustered bootstrap
def rng(seed: Any, *scope_parts: Any) -> random.Random:
    """The bootstrap generator of one estimate (the seed joined with the estimate's scope tuple)."""
    return random.Random(":".join(str(part) for part in (seed, *scope_parts)))


def cluster_ratio_bootstrap(groups: Sequence[Sequence[Sequence[float]]], *, b: int, seed: Any,
                            scope_parts: Sequence[Any], levels: int = 1) -> dict[str, Any]:
    """Hajek ratios with a stratified cluster bootstrap.

    ``groups`` holds one list of clusters per stratum (journal); a cluster is (sum of weights, sum of weighted
    numerator 1, ..., numerator ``levels``). Each replicate resamples clusters with replacement within every group;
    the estimate of level l is sum(numerator l) / sum(weights). Returns the estimates, the percentile bounds per level
    and the number of usable replicates.
    """
    clusters = [cluster for group in groups for cluster in group]
    width = int(levels) + 1
    if any(len(cluster) != width for cluster in clusters):
        raise ValueError("every cluster needs its weight sum and one numerator per level")
    totals = [sum(cluster[i] for cluster in clusters) for i in range(width)]
    estimates = [totals[i] / totals[0] if totals[0] else None for i in range(1, width)]
    result: dict[str, Any] = {"estimates": estimates, "bounds": [(None, None)] * (width - 1), "b": b, "seed": seed,
                              "scope": list(scope_parts), "replicates": 0,
                              "clusters": len(clusters), "groups": len([group for group in groups if group])}
    if not clusters or not totals[0]:
        return result
    generator = rng(seed, *scope_parts)
    columns = [[[cluster[i] for cluster in group] for i in range(width)] for group in groups if group]
    replicates: list[list[float]] = [[] for _ in range(width - 1)]
    for _ in range(int(b)):
        sums = [0.0] * width
        for group in columns:
            size = len(group[0])
            draw = generator.choices(range(size), k=size)
            for i in range(width):
                sums[i] += sum(map(group[i].__getitem__, draw))
        if sums[0] > 0:
            for level in range(width - 1):
                replicates[level].append(sums[level + 1] / sums[0])
    result["bounds"] = [estimands.percentile_bounds(values) for values in replicates]
    result["replicates"] = len(replicates[0]) if replicates else 0
    return result


def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation quantile of sorted ``values`` (the sample's (n - 1) q order statistic)."""
    if not values:
        return None
    position = (len(values) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (position - low) * (values[high] - values[low])


def cluster_quantile_bootstrap(groups: Sequence[Sequence[Sequence[float]]], levels: Sequence[float], *, b: int,
                               seed: Any, scope_parts: Sequence[Any]) -> dict[str, Any]:
    """Quantiles of pooled cluster values with a stratified cluster bootstrap (clusters resampled within group)."""
    pooled = sorted(value for group in groups for cluster in group for value in cluster)
    result: dict[str, Any] = {"estimates": [quantile(pooled, level) for level in levels],
                              "bounds": [(None, None)] * len(levels), "b": b, "seed": seed,
                              "scope": list(scope_parts), "n": len(pooled), "replicates": 0}
    nonempty = [list(group) for group in groups if group]
    if not pooled:
        return result
    generator = rng(seed, *scope_parts)
    replicates: list[list[float]] = [[] for _ in levels]
    for _ in range(int(b)):
        values: list[float] = []
        for group in nonempty:
            for index in generator.choices(range(len(group)), k=len(group)):
                values.extend(group[index])
        if values:
            values.sort()
            for position, level in enumerate(levels):
                replicates[position].append(quantile(values, level))
    result["bounds"] = [estimands.percentile_bounds(values) for values in replicates]
    result["replicates"] = len(replicates[0])
    return result


# ----------------------------------------------------------------------------- estimate rows
def estimate_key(estimand: str, journal_slug: str, year: Any, stat: str, *qualifiers: str | None) -> str:
    parts = ["followup", estimand, *[q for q in qualifiers if q], journal_slug, str(year) if year is not None else ALL,
             stat]
    return ".".join(parts)


def write_share_rows(connection: sqlite3.Connection, *, estimand: str, qualifiers: Sequence[str | None],
                     journal: str | None, slug: str, year: Any, stratum: str | None, numerator: float,
                     denominator: float, units: int, estimate: float | None, bounds: tuple[Any, Any],
                     detail: Mapping[str, Any]) -> int:
    """share, lo, hi, n, numerator and denominator rows of one cell (share "not-measured" without a denominator)."""
    detail_json = canonical(dict(detail))
    values = [("share", estimate if estimate is not None else "not-measured"), ("n", units),
              ("numerator", numerator), ("denominator", denominator)]
    if estimate is not None and bounds[0] is not None:
        values += [("lo", bounds[0]), ("hi", bounds[1])]
    for stat, value in values:
        connection.execute("INSERT OR REPLACE INTO fu_estimates(key, value, estimand, journal, year, stratum, numerator, "
                           "denominator, units, detail_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (estimate_key(estimand, slug, year, stat, *qualifiers), value, estimand, journal, year,
                            stratum, numerator, denominator, units, detail_json))
    return len(values)


def write_value_rows(connection: sqlite3.Connection, *, estimand: str, qualifiers: Sequence[str | None],
                     journal: str | None, slug: str, year: Any, stratum: str | None, value: float | None,
                     bounds: tuple[Any, Any], units: int, detail: Mapping[str, Any]) -> int:
    """value, lo, hi and n rows of a statistic that is not a share (value "not-measured" when it has none)."""
    detail_json = canonical(dict(detail))
    values = [("value", value if value is not None else "not-measured"), ("n", units)]
    if value is not None and bounds[0] is not None:
        values += [("lo", bounds[0]), ("hi", bounds[1])]
    for stat, item in values:
        connection.execute("INSERT OR REPLACE INTO fu_estimates(key, value, estimand, journal, year, stratum, numerator, "
                           "denominator, units, detail_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (estimate_key(estimand, slug, year, stat, *qualifiers), item, estimand, journal, year,
                            stratum, None, None, units, detail_json))
    return len(values)


def clear_estimates(connection: sqlite3.Connection, estimand: str) -> None:
    connection.execute("DELETE FROM fu_estimates WHERE key LIKE ?", (f"followup.{estimand}.%",))
    connection.execute("DELETE FROM fu_coverage WHERE estimand=? OR estimand LIKE ?", (estimand, f"{estimand}.%"))


def write_coverage(connection: sqlite3.Connection, estimand: str, journal: str | None, year: Any, stratum: str,
                   counts: Mapping[tuple[str, str], tuple[int, float]]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO fu_coverage(estimand, journal, year, stratum, state, reason, units, weighted) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(estimand, journal or "*", str(year) if year is not None else "*", stratum, state, reason, units, weighted)
         for (state, reason), (units, weighted) in sorted(counts.items())])


# ----------------------------------------------------------------------------- retraction lag
def parse_date(text: Any, declared: str | None = None) -> tuple[date, str] | None:
    """An ISO date (day, month or year precision) with its precision; a declared coarser precision wins."""
    match = re.match(r"^\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", str(text or ""))
    if not match:
        return None
    year, month, day = int(match.group(1)), match.group(2), match.group(3)
    precision = "day" if day else "month" if month else "year"
    if declared in PRECISION_ORDER and PRECISION_ORDER[declared] > PRECISION_ORDER[precision]:
        precision = declared
    try:
        value = date(year, int(month or 1) if precision != "year" else 1,
                     int(day or 1) if precision == "day" else 1)
    except ValueError:
        return None
    return value, precision


def compare_dates(notice: Any, notice_precision: str | None, citing: Any) -> dict[str, Any]:
    """after_notice, lag_days, the precision compared at and the reason when undecidable."""
    first = parse_date(notice, notice_precision)
    second = parse_date(citing)
    if first is None:
        return {"after_notice": None, "lag_days": None, "date_basis": "none", "reason": "notice-date-unknown"}
    if second is None:
        return {"after_notice": None, "lag_days": None, "date_basis": "none", "reason": "first-online-unknown"}
    basis = max(first[1], second[1], key=PRECISION_ORDER.__getitem__)

    def truncated(value: date) -> tuple[int, ...]:
        return (value.year,) if basis == "year" else (value.year, value.month) if basis == "month" else (
            value.year, value.month, value.day)
    a, c = truncated(first[0]), truncated(second[0])
    days = (second[0] - first[0]).days if first[1] == second[1] == "day" else None
    if a == c:
        return {"after_notice": None, "lag_days": days, "date_basis": basis, "reason": "undecidable-at-precision"}
    return {"after_notice": int(c > a), "lag_days": days, "date_basis": basis, "reason": None}


def lag_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per S1 locus: review, cited work, status, notice and citing dates and the comparison."""
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    rows = []
    for row in cursor.execute(
            "SELECT f.locus_id, f.journal, f.doi, f.review_year, f.review_first_online, f.work_key, "
            "s.inclusion_probability, w.status, w.notice_date, w.date_precision FROM fu_strata s "
            "JOIN fu_frame f ON f.locus_id=s.locus_id JOIN fu_work_status w ON w.work_key=f.work_key "
            "WHERE s.stratum='S1' ORDER BY f.locus_id"):
        rows.append({"locus_id": row["locus_id"], "journal": row["journal"], "doi": row["doi"],
                     "year": row["review_year"], "work_key": row["work_key"], "status": row["status"],
                     "weight": 1.0 / row["inclusion_probability"], "notice_date": row["notice_date"],
                     "notice_precision": row["date_precision"], "citing_first_online": row["review_first_online"],
                     **compare_dates(row["notice_date"], row["date_precision"], row["review_first_online"])})
    return rows


def _cells(rows: Sequence[Mapping[str, Any]], journals: Sequence[str], *, by_year: bool) -> list[tuple[Any, Any]]:
    years = sorted({row["year"] for row in rows if row["year"] is not None}) if by_year else []
    return [(journal, year) for journal in [None, *journals] for year in [None, *years]]


def _in_cell(row: Mapping[str, Any], journal: Any, year: Any) -> bool:
    return (journal is None or row["journal"] == journal) and (year is None or row["year"] == year)


def _groups(rows: Iterable[Mapping[str, Any]], value: Callable[[Mapping[str, Any]], Sequence[float]],
            journals: Sequence[str]) -> list[list[list[float]]]:
    clusters: dict[str, dict[tuple[str, str], list[float]]] = {journal: {} for journal in journals}
    for row in rows:
        values = list(value(row))
        cluster = clusters.setdefault(row["journal"], {}).setdefault((row["journal"], row["doi"]),
                                                                      [0.0] * len(values))
        for index, item in enumerate(values):
            cluster[index] += item
    return [[clusters[journal][key] for key in sorted(clusters[journal])] for journal in sorted(clusters)]


def after_notice_share(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], *, estimand: str,
                       journals: Sequence[str], b: int, seed: Any, statuses: Sequence[str] = ()) -> int:
    """Hajek share of decidable S1 loci cited after the notice, by journal and year (and status), with coverage."""
    slugs = journal_slugs(journals)
    written = 0
    for status in [None, *statuses]:
        selected = [row for row in rows if status is None or row["status"] == status]
        for journal, year in _cells(selected, journals, by_year=True):
            cell = [row for row in selected if _in_cell(row, journal, year)]
            decidable = [row for row in cell if row["after_notice"] is not None]
            # One scope for the lag model and the cited-after-notice estimand: equal B and seed give equal intervals.
            scope_parts = ("after-notice", slugs.get(journal, ALL), year if year is not None else ALL, status or ALL)
            boot = cluster_ratio_bootstrap(_groups(decidable, lambda r: (r["weight"], r["weight"] * r["after_notice"]),
                                                   journals), b=b, seed=seed, scope_parts=scope_parts)
            post = sorted(row["lag_days"] for row in decidable if row["after_notice"] == 1 and row["lag_days"] is not None)
            detail = {"estimand": estimand, "status": status, "rule": LAG_RULE, "weights": "1/realised inclusion "
                      "probability (Hajek)", "interval": INTERVAL, "b": b, "seed": seed, "scope": list(scope_parts),
                      "replicates": boot["replicates"], "reviews": boot["clusters"],
                      "lagDays": {"n": len(post), **{name: quantile(post, level) for name, level in QUANTILES}}}
            written += write_share_rows(
                connection, estimand=estimand, qualifiers=[status], journal=journal, slug=slugs.get(journal, ALL),
                year=year, stratum="S1", numerator=sum(r["weight"] * r["after_notice"] for r in decidable),
                denominator=sum(r["weight"] for r in decidable), units=len(decidable), estimate=boot["estimates"][0],
                bounds=boot["bounds"][0], detail=detail)
            counts: dict[tuple[str, str], tuple[int, float]] = {}
            for row in cell:
                key = ("assessable", "-") if row["after_notice"] is not None else ("unassessable", row["reason"])
                units, weighted = counts.get(key, (0, 0.0))
                counts[key] = (units + 1, weighted + row["weight"])
            write_coverage(connection, estimand + (f".{status}" if status else ""), journal, year, "S1", counts)
    return written


def _window(connection: sqlite3.Connection, protocol: Mapping[str, Any], journal: str | None) -> tuple[date, date] | None:
    years = protocol.get("years")
    clause, params = scope(protocol)
    if journal is not None:
        clause += " AND r.journal=?"
        params.append(journal)
    dates = [parse_date(row[0]) for row in connection.execute(
        f"SELECT r.first_online FROM census.reviews r WHERE {clause} AND r.first_online IS NOT NULL", params)]
    dates = [value[0] for value in dates if value]
    start = date(int(years["from"]), 1, 1) if years else (min(dates) if dates else None)
    end = date(int(years["to"]), 12, 31) if years else (max(dates) if dates else None)
    return (start, end) if start and end and end > start else None


def _midpoint(value: tuple[date, str]) -> date:
    day, precision = value
    if precision == "month":
        return day + timedelta(days=14)
    if precision == "year":
        return date(day.year, 7, 2)
    return day


def rate_ratio(connection: sqlite3.Connection, protocol: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *,
               journals: Sequence[str], b: int, seed: Any, statuses: Sequence[str]) -> int:
    """Citing reviews per work-year after vs before the notice (work-clustered ratio bootstrap)."""
    slugs = journal_slugs(journals)
    publication = {row[0]: row[1] for row in connection.execute("SELECT work_key, year FROM census.works")}
    written = 0
    for status in [None, *statuses]:
        selected = [row for row in rows if status is None or row["status"] == status]
        for journal in [None, *journals]:
            window = _window(connection, protocol, journal)
            cell = [row for row in selected if journal is None or row["journal"] == journal]
            works: dict[str, dict[str, Any]] = {}
            for row in cell:
                entry = works.setdefault(row["work_key"], {"notice": parse_date(row["notice_date"],
                                                                                row["notice_precision"]),
                                                           "before": set(), "after": set()})
                if row["after_notice"] is not None:
                    entry["after" if row["after_notice"] else "before"].add((row["journal"], row["doi"]))
            after_units, before_units, skipped = [], [], 0
            for work, entry in sorted(works.items()):
                if window is None or entry["notice"] is None:
                    skipped += 1
                    continue
                notice = _midpoint(entry["notice"])
                published = date(int(publication[work][:4]), 1, 1) if re.match(r"^\d{4}", str(
                    publication.get(work) or "")) else window[0]
                start, end = max(window[0], published), window[1]
                before_years = max(0.0, (min(notice, end) - start).days / 365.25)
                after_years = max(0.0, (end - max(notice, start)).days / 365.25)
                after_units.append((work, len(entry["after"]), after_years))
                before_units.append((work, len(entry["before"]), before_years))
            scope_parts = ("lag.rate-ratio", slugs.get(journal, ALL), status or ALL)
            result = estimands.ratio_bootstrap(after_units, before_units, b=b, seed=seed, scope=scope_parts)
            detail = {"estimand": "lag.rate-ratio", "status": status, "rule": LAG_RULE,
                      "definition": "citing reviews per work-year after the notice divided by citing reviews per "
                                    "work-year before it, within the census window",
                      "interval": "percentile bootstrap: works resampled with replacement (estimands.ratio_bootstrap)",
                      "b": b, "seed": seed, "scope": list(scope_parts), "works": len(after_units),
                      "worksWithoutNoticeDate": skipped, "replicates": result.get("replicates", 0),
                      "window": [window[0].isoformat(), window[1].isoformat()] if window else None,
                      "exposureDates": "a notice dated to the month or year counts from the middle of that period",
                      "citationsAfter": sum(unit[1] for unit in after_units),
                      "citationsBefore": sum(unit[1] for unit in before_units),
                      "workYearsAfter": sum(unit[2] for unit in after_units),
                      "workYearsBefore": sum(unit[2] for unit in before_units)}
            written += write_value_rows(connection, estimand="lag.rate-ratio", qualifiers=[status], journal=journal,
                                        slug=slugs.get(journal, ALL), year=None, stratum="S1",
                                        value=result["estimate"], bounds=(result["lo"], result["hi"]),
                                        units=len(after_units), detail=detail)
    return written


def lag_quantiles(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], *, journals: Sequence[str],
                  b: int, seed: Any) -> int:
    slugs = journal_slugs(journals)
    written = 0
    post = [row for row in rows if row["after_notice"] == 1 and row["lag_days"] is not None]
    for journal in [None, *journals]:
        cell = [row for row in post if journal is None or row["journal"] == journal]
        clusters: dict[str, dict[tuple[str, str], list[float]]] = {name: {} for name in journals}
        for row in cell:
            clusters[row["journal"]].setdefault((row["journal"], row["doi"]), []).append(float(row["lag_days"]))
        groups = [[clusters[name][key] for key in sorted(clusters[name])] for name in sorted(clusters)]
        scope_parts = ("lag.days", slugs.get(journal, ALL))
        result = cluster_quantile_bootstrap(groups, [level for _, level in QUANTILES], b=b, seed=seed,
                                            scope_parts=scope_parts)
        for (name, level), estimate, bounds in zip(QUANTILES, result["estimates"], result["bounds"]):
            detail = {"estimand": f"lag.{name}", "rule": LAG_RULE, "quantile": level,
                      "definition": "days from the notice to the citing review's first-online date, post-notice "
                                    "citations with both dates given to the day",
                      "interval": INTERVAL, "b": b, "seed": seed, "scope": list(scope_parts),
                      "replicates": result["replicates"]}
            written += write_value_rows(connection, estimand=f"lag.{name}", qualifiers=[], journal=journal,
                                        slug=slugs.get(journal, ALL), year=None, stratum="S1", value=estimate,
                                        bounds=bounds, units=len(cell), detail=detail)
    return written


def lag_trend(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], *, journals: Sequence[str], b: int,
              seed: Any) -> int:
    slugs = journal_slugs(journals)
    written = 0
    decidable = [row for row in rows if row["after_notice"] is not None and row["year"] is not None]
    for journal in [None, *journals]:
        by_year: dict[int, dict[tuple[str, str], list[int]]] = defaultdict(dict)
        for row in decidable:
            if journal is None or row["journal"] == journal:
                cluster = by_year[int(row["year"])].setdefault((row["journal"], row["doi"]), [0, 0])
                cluster[0] += int(row["after_notice"])
                cluster[1] += 1
        units = {year: [(key, k, n) for key, (k, n) in sorted(clusters.items())] for year, clusters in by_year.items()}
        scope_parts = ("lag.trend", slugs.get(journal, ALL))
        result = estimands.year_trend(units, b=b, seed=seed, scope=scope_parts)
        detail = {"estimand": "lag.trend", "rule": LAG_RULE,
                  "definition": "least-squares slope of the yearly share of decidable S1 loci cited after the notice",
                  "interval": "percentile bootstrap: reviews resampled within year (estimands.year_trend)", "b": b,
                  "seed": seed, "scope": list(scope_parts), "years": result["years"],
                  "shares": {str(year): share for year, share in result["shares"].items()}}
        written += write_value_rows(connection, estimand="lag.trend", qualifiers=[], journal=journal,
                                    slug=slugs.get(journal, ALL), year=None, stratum="S1", value=result["slope"],
                                    bounds=(result["lo"], result["hi"]),
                                    units=sum(n for clusters in units.values() for _, _, n in clusters), detail=detail)
    return written


def run_lag(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> dict[str, Any]:
    require_freeze(connection)
    rows = lag_rows(connection)
    connection.execute("DELETE FROM fu_lag")
    connection.executemany("INSERT INTO fu_lag(locus_id, work_key, status, notice_date, citing_first_online, lag_days, "
                           "after_notice, date_basis, reason) VALUES(?,?,?,?,?,?,?,?,?)",
                           [(row["locus_id"], row["work_key"], row["status"], row["notice_date"],
                             row["citing_first_online"], row["lag_days"], row["after_notice"], row["date_basis"],
                             row["reason"]) for row in rows])
    journals = list(protocol["journals"])
    statuses = sorted({row["status"] for row in rows})
    b, seed = int(protocol["bootstrap"]["b"]), protocol["bootstrap"]["seed"]
    for estimand in ("lag.after-notice", "lag.rate-ratio", "lag.trend", *(f"lag.{name}" for name, _ in QUANTILES)):
        clear_estimates(connection, estimand)
    written = after_notice_share(connection, rows, estimand="lag.after-notice", journals=journals, b=b, seed=seed)
    written += lag_quantiles(connection, rows, journals=journals, b=b, seed=seed)
    written += rate_ratio(connection, protocol, rows, journals=journals, b=b, seed=seed, statuses=statuses)
    written += lag_trend(connection, rows, journals=journals, b=b, seed=seed)
    reasons: dict[str, int] = defaultdict(int)
    for row in rows:
        reasons[row["reason"] or "decidable"] += 1
    return {"rule": LAG_RULE, "loci": len(rows), "afterNotice": sum(1 for row in rows if row["after_notice"] == 1),
            "beforeNotice": sum(1 for row in rows if row["after_notice"] == 0), "comparisons": dict(sorted(reasons.items())),
            "estimateRows": written, "b": b, "seed": seed}


def cmd_lag(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = protocol_of(branch)
    with branch.writing() as connection:
        return {"database": str(branch.path), **run_lag(connection, protocol)}
