# SPDX-License-Identifier: Apache-2.0
"""Estimands for the entry-level citation census (pure Python, stdlib only).

Unit: the printed bibliography entry. Denominator 2: entries cited in the
body, with a resolved identity (resolved or resolved_unverified), that the
model judged. Headline: F / (T + F) at the abstract tier, escalated F
included. Uncertainty: Wilson intervals with the effective sample size
n / DEFF, DEFF = 1 + (m - 1) * ICC with m the mean number of classifiable
entries per review and the ICC measured per journal (one-way ANOVA
estimator), plus a review-clustered percentile bootstrap with a fixed seed.

All functions take per-review sums ``(key, k, n)`` (k not supported among n
classifiable) or a scores database connection, never verdict files.
"""
from __future__ import annotations

import math
import random
import sqlite3
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping, Sequence

Z95 = 1.96
DEFAULT_B = 10_000
RESOLVED = ("resolved", "resolved_unverified")
DENOMINATOR_2 = ("cited_inbody=1 AND identity IN ('resolved','resolved_unverified') AND model_judged=1 "
                 "AND abs_category IN ('true','false')")
DENOMINATOR_2_SQL = ("SELECT doi, SUM(abs_category='false') AS k, COUNT(*) AS n FROM citations "
                     "WHERE journal=? AND " + DENOMINATOR_2 + "{extra} GROUP BY doi ORDER BY doi")


# ----------------------------------------------------------------------------- intervals
def wilson(p: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion ``p`` observed on (effective) sample size ``n``."""
    if n <= 0:
        return (0.0, 0.0)
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def icc_anova(units: Iterable[tuple[int, int]]) -> float:
    """One-way ANOVA intraclass correlation for binary outcomes clustered in reviews.

    MSB = sum n_i (p_i - p)^2 / (m - 1); MSW = sum n_i p_i (1 - p_i) / (N - m);
    n0 = (N - sum n_i^2 / N) / (m - 1); ICC = (MSB - MSW) / (MSB + (n0 - 1) MSW).
    Reviews without classifiable entries are ignored; fewer than two reviews give 0.
    """
    pairs = [(k, n) for k, n in units if n > 0]
    m = len(pairs)
    total = sum(n for _, n in pairs)
    if m < 2 or total <= m:
        return 0.0
    p = sum(k for k, _ in pairs) / total
    msb = sum(n * (k / n - p) ** 2 for k, n in pairs) / (m - 1)
    msw = sum(k * (1 - k / n) ** 2 + (n - k) * (k / n) ** 2 for k, n in pairs) / (total - m)
    n0 = (total - sum(n * n for _, n in pairs) / total) / (m - 1)
    denominator = msb + (n0 - 1) * msw
    return (msb - msw) / denominator if denominator > 0 else 0.0


def design_effect(mean_cluster: float, icc: float) -> float:
    return 1 + (mean_cluster - 1) * max(0.0, icc)


def wilson_deff(k: int, n: int, icc: float, mean_cluster: float, z: float = Z95) -> dict[str, Any]:
    """Wilson interval on n_eff = n / DEFF."""
    if n <= 0:
        return {"k": k, "n": n, "share": None, "deff": None, "nEffective": 0, "lo": None, "hi": None}
    deff = design_effect(mean_cluster, icc)
    share = k / n
    lo, hi = wilson(share, n / deff, z)
    return {"k": k, "n": n, "share": share, "deff": deff, "nEffective": n / deff, "lo": lo, "hi": hi}


def percentile_bounds(estimates: Sequence[float]) -> tuple[float | None, float | None]:
    """2.5 % and 97.5 % order statistics as ``sorted[int(0.025 B)]`` and ``sorted[int(0.975 B)]``."""
    ordered = sorted(estimates)
    if not ordered:
        return (None, None)
    return (ordered[int(0.025 * len(ordered))], ordered[min(len(ordered) - 1, int(0.975 * len(ordered)))])


def _rng(seed: int | str, *scope: Any) -> random.Random:
    return random.Random(":".join(str(part) for part in (seed, *scope)) if scope else seed)


def clustered_bootstrap(units: Sequence[tuple[Any, int, int]], *, b: int = DEFAULT_B, seed: int | str = 0,
                        scope: Sequence[Any] = ()) -> dict[str, Any]:
    """Review-clustered percentile bootstrap of sum(k) / sum(n): resample reviews with replacement."""
    pairs = [(k, n) for _, k, n in units]
    m = len(pairs)
    total_n = sum(n for _, n in pairs)
    estimate = sum(k for k, _ in pairs) / total_n if total_n else None
    if m == 0 or not total_n:
        return {"estimate": estimate, "lo": None, "hi": None, "se": None, "b": b, "seed": seed, "reviews": m}
    rng = _rng(seed, *scope)
    ks, ns = [k for k, _ in pairs], [n for _, n in pairs]
    indices = range(m)
    estimates = []
    for _ in range(b):
        draw = rng.choices(indices, k=m)
        denominator = sum(map(ns.__getitem__, draw))
        if denominator:
            estimates.append(sum(map(ks.__getitem__, draw)) / denominator)
    lo, hi = percentile_bounds(estimates)
    mean = sum(estimates) / len(estimates)
    se = math.sqrt(sum((x - mean) ** 2 for x in estimates) / (len(estimates) - 1)) if len(estimates) > 1 else 0.0
    return {"estimate": estimate, "lo": lo, "hi": hi, "se": se, "b": b, "seed": seed, "reviews": m,
            "replicates": len(estimates)}


def ratio_bootstrap(numerator_units, denominator_units, *, b: int = DEFAULT_B, seed: int | str = 0,
                    scope: Sequence[Any] = ()) -> dict[str, Any]:
    """Ratio of two clustered shares; reviews are resampled independently within each group."""
    a = [(k, n) for _, k, n in numerator_units]
    c = [(k, n) for _, k, n in denominator_units]
    def share(pairs):
        total = sum(n for _, n in pairs)
        return sum(k for k, _ in pairs) / total if total else None
    pa, pc = share(a), share(c)
    estimate = pa / pc if pa is not None and pc else None
    if not a or not c or estimate is None:
        return {"estimate": estimate, "lo": None, "hi": None, "b": b, "seed": seed}
    rng = _rng(seed, *scope)
    ka, na = [k for k, _ in a], [n for _, n in a]
    kc, nc = [k for k, _ in c], [n for _, n in c]
    estimates = []
    for _ in range(b):
        da = rng.choices(range(len(a)), k=len(a))
        dc = rng.choices(range(len(c)), k=len(c))
        sa, sc = sum(map(na.__getitem__, da)), sum(map(nc.__getitem__, dc))
        if not sa or not sc:
            continue
        top = sum(map(ka.__getitem__, da)) / sa
        bottom = sum(map(kc.__getitem__, dc)) / sc
        if bottom:
            estimates.append(top / bottom)
    lo, hi = percentile_bounds(estimates)
    return {"estimate": estimate, "lo": lo, "hi": hi, "b": b, "seed": seed, "replicates": len(estimates),
            "excludesOne": lo is not None and (lo > 1 or hi < 1)}


def ols_slope(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    mx = sum(x for x, _ in points) / len(points)
    my = sum(y for _, y in points) / len(points)
    sxx = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else None


def year_trend(units_by_year: Mapping[int, Sequence[tuple[Any, int, int]]], *, b: int = DEFAULT_B,
               seed: int | str = 0, scope: Sequence[Any] = ()) -> dict[str, Any]:
    """OLS slope of the yearly share on the year, with a review bootstrap stratified by year."""
    years = sorted(y for y, units in units_by_year.items() if sum(n for _, _, n in units))
    def shares(selection):
        return [(y, sum(k for _, k, _ in selection[y]) / sum(n for _, _, n in selection[y])) for y in years
                if sum(n for _, _, n in selection[y])]
    observed = shares(units_by_year)
    slope = ols_slope(observed)
    if slope is None:
        return {"slope": None, "lo": None, "hi": None, "years": years, "shares": dict(observed), "b": b, "seed": seed}
    rng = _rng(seed, *scope)
    estimates = []
    for _ in range(b):
        sample = {}
        for y in years:
            units = units_by_year[y]
            sample[y] = [units[i] for i in rng.choices(range(len(units)), k=len(units))]
        value = ols_slope(shares(sample))
        if value is not None:
            estimates.append(value)
    lo, hi = percentile_bounds(estimates)
    return {"slope": slope, "lo": lo, "hi": hi, "years": years, "shares": dict(observed), "b": b, "seed": seed,
            "coversZero": lo is not None and lo <= 0 <= hi}


# ----------------------------------------------------------------------------- screening
def _log_binomial(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def upper_tail(k: int, n: int, mean: float, rho: float) -> float:
    """P(X >= k) for a beta-binomial with mean ``mean`` and intraclass correlation ``rho`` (binomial when rho <= 0)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if mean <= 0:
        return 0.0
    if mean >= 1:
        return 1.0
    if rho >= 1:
        # Perfect clustering: every unit is all-or-none, so X is n with probability mean and 0 otherwise.
        return mean
    terms = []
    if rho <= 0:
        for j in range(k, n + 1):
            terms.append(_log_binomial(n, j) + j * math.log(mean) + (n - j) * math.log1p(-mean))
    else:
        alpha, beta = mean * (1 - rho) / rho, (1 - mean) * (1 - rho) / rho
        base = _log_beta(alpha, beta)
        for j in range(k, n + 1):
            terms.append(_log_binomial(n, j) + _log_beta(j + alpha, n - j + beta) - base)
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def benjamini_hochberg(p_values: Sequence[float | None]) -> list[float | None]:
    indexed = sorted((p, i) for i, p in enumerate(p_values) if p is not None)
    m = len(indexed)
    q = [None] * len(p_values)
    running = 1.0
    for rank in range(m, 0, -1):
        p, index = indexed[rank - 1]
        running = min(running, p * m / rank)
        q[index] = min(1.0, running)
    return q


def beta_binomial_screen(units: Sequence[tuple[Any, int, int]]) -> list[tuple[Any, int, int, float, float, float | None, float | None]]:
    """Per unit: (key, k, n, expected, excess, p, q) under a beta-binomial fitted by mean and ANOVA ICC."""
    total = sum(n for _, _, n in units)
    mean = sum(k for _, k, _ in units) / total if total else 0.0
    rho = max(0.0, icc_anova((k, n) for _, k, n in units))
    rows = []
    for key, k, n in units:
        expected = n * mean
        rows.append([key, k, n, expected, k - expected, upper_tail(k, n, mean, rho) if n else None])
    q = benjamini_hochberg([row[5] for row in rows])
    return [tuple(row + [q[i]]) for i, row in enumerate(rows)]


# ----------------------------------------------------------------------------- database estimands
def _rows(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Named-column rows regardless of the caller's row factory."""
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, params).fetchall()


def denominator_units(connection: sqlite3.Connection, journal: str, extra: str = "",
                      params: Sequence[Any] = ()) -> list[tuple[str, int, int]]:
    sql = DENOMINATOR_2_SQL.format(extra=(" AND " + extra) if extra else "")
    return [(r[0], int(r[1]), int(r[2])) for r in connection.execute(sql, (journal, *params))]


def summarise(units: Sequence[tuple[Any, int, int]], icc: float, *, b: int, seed: int | str,
              scope: Sequence[Any]) -> dict[str, Any]:
    k = sum(u[1] for u in units)
    n = sum(u[2] for u in units)
    m = len([u for u in units if u[2]])
    mean_cluster = n / m if m else 0.0
    interval = wilson_deff(k, n, icc, mean_cluster)
    boot = clustered_bootstrap(units, b=b, seed=seed, scope=scope)
    return {"k": k, "n": n, "reviews": m, "share": interval["share"], "meanCluster": mean_cluster,
            "icc": icc, "deff": interval["deff"], "wilsonDeff": [interval["lo"], interval["hi"]],
            "bootstrap": [boot["lo"], boot["hi"]], "bootstrapSe": boot.get("se"), "b": b, "seed": seed}


def headline(connection: sqlite3.Connection, journal: str, *, b: int = DEFAULT_B, seed: int | str = 0) -> dict[str, Any]:
    """F / (T + F) on denominator 2 at the abstract tier (escalated F included)."""
    units = denominator_units(connection, journal)
    icc = icc_anova((k, n) for _, k, n in units)
    return {"journal": journal, "estimand": "abstract-tier F/(T+F), denominator 2",
            **summarise(units, icc, b=b, seed=seed, scope=("headline", journal))}


def three_classes(connection: sqlite3.Connection, journal: str) -> dict[str, Any]:
    """Abstract and full-text classes, never cited, and the secondary composite with availability strata."""
    abstract = {r[0]: r[1] for r in connection.execute(
        "SELECT abs_class, COUNT(*) FROM citations WHERE journal=? GROUP BY abs_class", (journal,))}
    full_text = {r[0]: r[1] for r in connection.execute(
        "SELECT ft_class, COUNT(*) FROM citations WHERE journal=? AND ft_class IS NOT NULL GROUP BY ft_class", (journal,))}
    composite = defaultdict(lambda: defaultdict(int))
    for row in _rows(connection,
            "SELECT abs_class, abs_escalated, ft_class, ft_retrieval FROM citations WHERE journal=? AND abs_class!='not_cited'",
            (journal,)):
        stratum = "full text read" if row["ft_class"] else ("escalated, full text unavailable" if row["abs_escalated"]
                                                           else "not escalated")
        value = row["ft_class"] or (row["abs_class"] if not row["abs_escalated"] else "unsure")
        composite[stratum][value] += 1
    def share(counts):
        sure = counts.get("sure_supported", 0) + counts.get("sure_not_supported", 0)
        return counts.get("sure_not_supported", 0) / sure if sure else None
    return {"journal": journal,
            "abstract": {c: abstract.get(c, 0) for c in ("sure_supported", "unsure", "sure_not_supported")},
            "notCited": abstract.get("not_cited", 0), "abstractShareOfSure": share(abstract),
            "fullText": {c: full_text.get(c, 0) for c in ("sure_supported", "unsure", "sure_not_supported")},
            "fullTextShareOfSure": share(full_text),
            "composite": {s: dict(v) for s, v in sorted(composite.items())}}


PANELS = {"year": "r.year", "stratum": "r.stratum", "topic": "r.topic", "country": "r.country",
          "route": "r.route", "provenance": "r.provenance_class"}


def panel(connection: sqlite3.Connection, journal: str, by: str, *, b: int = DEFAULT_B, seed: int | str = 0) -> dict[str, Any]:
    """Per cell of ``by``: counts, share, Wilson x DEFF (journal ICC, cell mean cluster) and bootstrap CI."""
    if by not in PANELS:
        raise ValueError(f"unknown panel: {by}")
    icc = icc_anova((k, n) for _, k, n in denominator_units(connection, journal))
    rows = _rows(connection,
        f"SELECT {PANELS[by]} AS cell, c.doi AS doi, SUM(c.abs_category='false') AS k, COUNT(*) AS n "
        "FROM citations c JOIN reviews r ON r.journal=c.journal AND r.doi=c.doi WHERE c.journal=? AND "
        + " AND ".join("c." + part.strip() for part in DENOMINATOR_2.split(" AND ")) +
        " GROUP BY cell, c.doi ORDER BY cell, c.doi", (journal,))
    cells = defaultdict(list)
    for row in rows:
        cells[row["cell"]].append((row["doi"], int(row["k"]), int(row["n"])))
    result = {}
    for cell in sorted(cells, key=lambda value: (value is None, str(value))):
        result[str(cell)] = summarise(cells[cell], icc, b=b, seed=seed, scope=("panel", journal, by, cell))
    return {"journal": journal, "by": by, "icc": icc, "cells": result}


def ratio(connection: sqlite3.Connection, numerator: str, denominator: str, *, b: int = DEFAULT_B,
          seed: int | str = 0) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            **ratio_bootstrap(denominator_units(connection, numerator), denominator_units(connection, denominator),
                              b=b, seed=seed, scope=("ratio", numerator, denominator))}


def trend(connection: sqlite3.Connection, journal: str, *, b: int = DEFAULT_B, seed: int | str = 0,
          years: tuple[int, int] | None = None) -> dict[str, Any]:
    rows = _rows(connection,
        "SELECT r.year AS year, c.doi AS doi, SUM(c.abs_category='false') AS k, COUNT(*) AS n FROM citations c "
        "JOIN reviews r ON r.journal=c.journal AND r.doi=c.doi WHERE c.journal=? AND "
        + " AND ".join("c." + part.strip() for part in DENOMINATOR_2.split(" AND ")) +
        " AND r.year IS NOT NULL GROUP BY r.year, c.doi ORDER BY r.year, c.doi", (journal,))
    by_year = defaultdict(list)
    for row in rows:
        if years is None or years[0] <= row["year"] <= years[1]:
            by_year[int(row["year"])].append((row["doi"], int(row["k"]), int(row["n"])))
    return {"journal": journal, **year_trend(by_year, b=b, seed=seed, scope=("trend", journal))}


LADDERS = (
    ("escalated-unread-F-not-assessable", "escalated F without a full-text verdict removed from numerator and denominator",
     "NOT (abs_category='false' AND abs_escalated=1 AND ft_category IS NULL)"),
    ("F-without-quote-excluded", "F without a returned quote removed",
     "NOT (abs_category='false' AND COALESCE(abs_quote_present, 0)=0)"),
    ("F-quote-not-in-abstract-excluded", "F whose quote does not occur in the abstract sent removed",
     "NOT (abs_category='false' AND COALESCE(abs_quote_in_evidence, 0)=0)"),
    ("resolved-identity-only", "identity class resolved only", "identity='resolved'"),
    ("position-1-10", "entries at call positions 1 to 10", "abs_position BETWEEN 1 AND 10"),
    ("position-31-40", "entries at call positions 31 to 40", "abs_position BETWEEN 31 AND 40"),
    ("uncut-abstracts", "entries whose abstract was not cut", "abs_cut=0"),
    ("cut-abstracts", "entries whose abstract was cut", "abs_cut=1"),
    ("europe-pmc-abstracts-only", "abstracts from Europe PMC only", "abs_source LIKE 'europe%'"),
)


def sensitivity_ladder(connection: sqlite3.Connection, journal: str) -> dict[str, Any]:
    """Predeclared ladder: each step recomputes F/(T+F) under one restriction, never tuned."""
    base = denominator_units(connection, journal)
    k0, n0 = sum(u[1] for u in base), sum(u[2] for u in base)
    steps = [{"step": "headline", "definition": "denominator 2", "k": k0, "n": n0, "share": k0 / n0 if n0 else None}]
    for name, definition, condition in LADDERS:
        units = denominator_units(connection, journal, condition)
        k, n = sum(u[1] for u in units), sum(u[2] for u in units)
        steps.append({"step": name, "definition": definition, "k": k, "n": n, "share": k / n if n else None})
    for stratum in [r[0] for r in connection.execute(
            "SELECT DISTINCT stratum FROM reviews WHERE journal=? AND stratum IS NOT NULL ORDER BY stratum", (journal,))]:
        units = denominator_units(connection, journal,
                                  "doi IN (SELECT doi FROM reviews WHERE journal=citations.journal AND stratum=?)", (stratum,))
        k, n = sum(u[1] for u in units), sum(u[2] for u in units)
        steps.append({"step": "stratum:" + stratum, "definition": "reviews of this stratum only", "k": k, "n": n,
                      "share": k / n if n else None})
    for route in [r[0] for r in connection.execute(
            "SELECT DISTINCT route FROM reviews WHERE journal=? AND route IS NOT NULL ORDER BY route", (journal,))]:
        units = denominator_units(connection, journal,
                                  "doi IN (SELECT doi FROM reviews WHERE journal=citations.journal AND route=?)", (route,))
        k, n = sum(u[1] for u in units), sum(u[2] for u in units)
        steps.append({"step": "route:" + route, "definition": "reviews read through this route", "k": k, "n": n,
                      "share": k / n if n else None})
    variant = no_truncation_comparison(connection, journal)
    if variant["entries"]:
        steps.append({"step": "no-truncation", "definition": "sampled entries re-judged without the abstract cut",
                      "k": variant["all"]["variantFalse"], "n": variant["entries"], "share": variant["all"]["variantShare"],
                      "primaryShare": variant["all"]["primaryShare"], "agreement": variant["all"]["agreement"],
                      "cut": variant["cut"], "uncut": variant["uncut"]})
    return {"journal": journal, "steps": steps}


def no_truncation_comparison(connection: sqlite3.Connection, journal: str) -> dict[str, Any]:
    """Primary vs no-truncation verdicts on the predeclared sample (both must be T or F)."""
    rows = _rows(connection, """
      SELECT c.abs_category AS primary_category, c.abs_cut AS cut, h.category AS variant_category
      FROM history h JOIN citations c ON c.journal=h.journal AND c.doi=h.doi AND c.label=h.label
      WHERE h.journal=? AND h.variant='no-truncation' AND h.outcome='accepted' AND c.model_judged=1
        AND c.cited_inbody=1 AND c.identity IN ('resolved','resolved_unverified')""", (journal,))
    pairs = [(r["primary_category"], r["variant_category"], r["cut"]) for r in rows
             if r["primary_category"] in ("true", "false") and r["variant_category"] in ("true", "false")]
    def share(values):
        return sum(1 for v in values if v == "false") / len(values) if values else None
    result = {"entries": len(pairs)}
    for label, selection in (("all", pairs), ("cut", [p for p in pairs if p[2]]), ("uncut", [p for p in pairs if not p[2]])):
        result[label] = {"n": len(selection), "primaryShare": share([p[0] for p in selection]),
                         "variantShare": share([p[1] for p in selection]),
                         "primaryFalse": sum(1 for p in selection if p[0] == "false"),
                         "variantFalse": sum(1 for p in selection if p[1] == "false"),
                         "agreement": sum(1 for p in selection if p[0] == p[1]) / len(selection) if selection else None}
    return result


def coverage(connection: sqlite3.Connection, journal: str, by: str = "year") -> list[dict[str, Any]]:
    """Coverage table: reviews, extraction, identity, judged and full-text counts per cell."""
    if by not in PANELS:
        raise ValueError(f"unknown coverage dimension: {by}")
    column = PANELS[by]
    rows = _rows(connection, f"""
      SELECT {column} AS cell, COUNT(DISTINCT r.doi) AS reviews,
        COUNT(DISTINCT CASE WHEN r.document_sha256 IS NOT NULL THEN r.doi END) AS reviews_read,
        SUM(c.label IS NOT NULL) AS printed, SUM(c.cited_inbody=1) AS cited_inbody,
        SUM(c.cited_inbody=1 AND c.identity IN ('resolved','resolved_unverified')) AS resolved,
        SUM(c.model_judged=1) AS judged, SUM(c.abs_basis LIKE 'invalid%') AS invalid,
        SUM(c.abs_category IN ('true','false') AND c.model_judged=1 AND c.cited_inbody=1
            AND c.identity IN ('resolved','resolved_unverified')) AS denominator_2,
        SUM(c.abs_escalated=1) AS escalated, SUM(c.ft_class IS NOT NULL) AS full_text_read
      FROM reviews r LEFT JOIN citations c ON c.journal=r.journal AND c.doi=r.doi
      WHERE r.journal=? GROUP BY cell ORDER BY cell""", (journal,))
    return [dict(row) for row in rows]


DECOMPOSITION = {"provenance": "r.provenance_class", "route": "r.route", "abstract_provider": "c.abs_source",
                 "identity": "c.identity", "position_band": "CASE WHEN c.abs_position<=10 THEN '1-10' WHEN c.abs_position<=20 "
                 "THEN '11-20' WHEN c.abs_position<=30 THEN '21-30' ELSE '31-40' END",
                 "served_model": "(SELECT served_model FROM calls k WHERE k.call_id=c.abs_call_id)",
                 "cut": "c.abs_cut"}


def decompose(connection: sqlite3.Connection, journal: str, *, year: int | None = None, stratum: str | None = None,
              dimensions: Sequence[str] = tuple(DECOMPOSITION)) -> dict[str, Any]:
    """Split an anomalous cell's share by coverage, extraction route, judge route, abstract provider and more."""
    filters, params = ["c.journal=?"], [journal]
    if year is not None:
        filters.append("r.year=?")
        params.append(year)
    if stratum is not None:
        filters.append("r.stratum=?")
        params.append(stratum)
    base = " AND ".join(filters + ["c." + part.strip() for part in DENOMINATOR_2.split(" AND ")])
    result = {"journal": journal, "year": year, "stratum": stratum, "dimensions": {}}
    for name in dimensions:
        expression = DECOMPOSITION[name]
        rows = _rows(connection,
            f"SELECT {expression} AS level, SUM(c.abs_category='false') AS k, COUNT(*) AS n, COUNT(DISTINCT c.doi) AS reviews "
            f"FROM citations c JOIN reviews r ON r.journal=c.journal AND r.doi=c.doi WHERE {base} GROUP BY level ORDER BY level",
            params)
        result["dimensions"][name] = [{"level": row["level"], "k": row["k"], "n": row["n"], "reviews": row["reviews"],
                                       "share": row["k"] / row["n"] if row["n"] else None} for row in rows]
    return result


def report(connection: sqlite3.Connection, journals: Sequence[str], *, b: int = DEFAULT_B, seed: int | str = 0,
           ratio_pair: tuple[str, str] | None = None, panels: Sequence[str] = ("year", "stratum", "topic", "country"),
           trend_years: tuple[int, int] | None = None) -> dict[str, Any]:
    """Every predeclared estimand for the given journals."""
    out = {"b": b, "seed": seed, "journals": {}}
    for journal in journals:
        out["journals"][journal] = {
            "headline": headline(connection, journal, b=b, seed=seed),
            "classes": three_classes(connection, journal),
            "panels": {by: panel(connection, journal, by, b=b, seed=seed) for by in panels},
            "trend": trend(connection, journal, b=b, seed=seed, years=trend_years),
            "ladder": sensitivity_ladder(connection, journal),
            "coverage": {by: coverage(connection, journal, by) for by in ("year", "stratum", "route", "provenance")},
        }
    if ratio_pair:
        out["ratio"] = ratio(connection, ratio_pair[0], ratio_pair[1], b=b, seed=seed)
    return out
