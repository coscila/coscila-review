# SPDX-License-Identifier: Apache-2.0
"""Frame, strata, inclusion probabilities and the protocol freeze of a follow-up audit (``followup strata``).

The frame holds one row per locus of the parent census in the protocol's journals and years: one marker occurrence
of one bibliography entry, with its cited work, the citing sentence (``claim_id``: loci of one sentence share it),
the review's year, first-online date, topic and article-type stratum, the provenance class of the citing text, the
entry's identity class and in-body flag and the parent's categories. A locus is *eligible* when its entry is cited in
the body (when the protocol says so), its identity class is one the protocol lists and it names a cited work.

Strata (every parameter comes from the protocol; nothing here has a default):

- S1: eligible loci whose cited work's resolved status (``fu_work_status``) is one of the protocol's statuses.
- S2: eligible loci citing a work the hotspot test selected (``fu_hotspots.selected``); excluded families stay out.
- S3: eligible loci citing a work of the protocol's family (versioned title rules); built only when the protocol
  records verified table licensing, otherwise dropped and recorded as dropped.
- co-cited: for every S1 and S2 locus, the other eligible loci of its citing sentence that cite another work.
- S0: eligible loci in none of S1, S2 or the family whose work status is ``none``, sampled with the protocol seed and
  frequency-matched to S1 over the protocol's cells.

S1, S2, S3 and co-cited are censuses (inclusion probability 1); S0 records its realised probability per cell
(selected / eligible). The pilot is a seeded share of S1 per status plus the protocol's count of S0 loci, each with
its pilot probability. ``--freeze`` writes the protocol digest, a digest over the ordered membership rows, the status
snapshot, the parent digest (recomputed), the protocol's estimands and the sizes into ``fu_protocol_freeze``; the
strata cannot be built again after a freeze.

Seeded selections rank keys by the sha256 of the protocol seed, a scope and the key, so a selection depends only on
the seed, the scope and the set of keys (never on row order or the Python version).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import scores_db
from .errors import ValidationError
from .followup_branch import FollowupBranch, now
from .followup_model import canonical, claim_id, digest, locus_id, sentence_digest
from .followup_schema import PROVENANCE, STRATA

RULE = "strata-builder@1"
CELL_FIELDS = {"journal": "journal", "year": "review_year", "topic": "topic"}
ALL = "all"


# ----------------------------------------------------------------------------- protocol and scope
def protocol_of(branch: FollowupBranch) -> dict[str, Any]:
    protocol = branch.protocol()
    if not protocol:
        raise ValidationError("the follow-up branch has no protocol; make the branch with followup init --protocol")
    return protocol


def scope(protocol: Mapping[str, Any], reviews: str = "r") -> tuple[str, list[Any]]:
    """SQL condition (on the parent's reviews table under alias ``reviews``) for the protocol's journals and years."""
    journals = list(protocol["journals"])
    clause = f"{reviews}.journal IN ({','.join('?' for _ in journals)})"
    params: list[Any] = list(journals)
    years = protocol.get("years")
    if years:
        clause += f" AND {reviews}.year BETWEEN ? AND ?"
        params.extend((int(years["from"]), int(years["to"])))
    return clause, params


def journal_slugs(journals: Iterable[str]) -> dict[str, str]:
    """Key-safe slugs of the protocol's journals (letters, digits and hyphens); refuses collisions."""
    slugs: dict[str, str] = {}
    for journal in journals:
        slug = re.sub(r"[^a-z0-9]+", "-", journal.lower()).strip("-") or "journal"
        if slug == ALL or slug in slugs.values():
            raise ValidationError(f"journal names give clashing estimate keys: {journal!r}")
        slugs[journal] = slug
    return slugs


def exact(value: Any) -> Fraction:
    """A protocol number as an exact fraction (0.1 is one tenth, not its binary approximation)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"not a number: {value!r}")
    return Fraction(value) if isinstance(value, int) else Fraction(repr(float(value)))


# ----------------------------------------------------------------------------- families (title rules)
def family_patterns(protocol: Mapping[str, Any], family: str) -> list[re.Pattern[str]]:
    """Compiled title rules of a family the protocol defines (strata.S3.family with its familyRules)."""
    s3 = protocol["strata"].get("S3")
    if not s3 or s3.get("family") != family:
        raise ValidationError(f"the protocol names the family {family!r} but defines no title rules for it")
    patterns = []
    for text in s3["familyRules"]["titlePatterns"]:
        try:
            patterns.append(re.compile(text, re.IGNORECASE))
        except re.error as error:
            raise ValidationError(f"family title rule {text!r} is not a regular expression: {error}") from error
    return patterns


def title_matches(patterns: Sequence[re.Pattern[str]], title: str | None) -> bool:
    folded = " ".join((title or "").split())
    return bool(folded) and any(pattern.search(folded) for pattern in patterns)


def excluded_patterns(protocol: Mapping[str, Any]) -> list[re.Pattern[str]]:
    """Title rules of the families the hotspot test excludes (strata.S2.excludeFamilies)."""
    return [pattern for family in protocol["strata"]["S2"]["excludeFamilies"]
            for pattern in family_patterns(protocol, family)]


def s3_family(protocol: Mapping[str, Any]) -> list[re.Pattern[str]] | None:
    s3 = protocol["strata"].get("S3")
    return family_patterns(protocol, s3["family"]) if s3 else None


def family_works(connection: sqlite3.Connection, patterns: Sequence[re.Pattern[str]]) -> set[str]:
    """Parent works whose title (``works.title``, else an entry's ``cited_title``) a family's rules match."""
    if not patterns:
        return set()
    matched, untitled = set(), set()
    for key, title in connection.execute("SELECT work_key, title FROM census.works"):
        if title and title.strip():
            if title_matches(patterns, title):
                matched.add(key)
        else:
            untitled.add(key)
    for key, title in connection.execute("SELECT DISTINCT cited_work_key, cited_title FROM census.citations "
                                         "WHERE cited_work_key IS NOT NULL AND cited_title IS NOT NULL"):
        if key in untitled and title_matches(patterns, title):
            matched.add(key)
    return matched


# ----------------------------------------------------------------------------- seeded selection
def seeded_order(seed: Any, scope_parts: Sequence[Any], keys: Iterable[str]) -> list[str]:
    """Keys ordered by sha256(seed, scope, key): a uniform random permutation fixed by the seed and the scope."""
    prefix = canonical([seed, *scope_parts]) + "\x1f"
    return sorted(set(keys), key=lambda key: (hashlib.sha256((prefix + key).encode("utf-8")).hexdigest(), key))


def seeded_sample(seed: Any, scope_parts: Sequence[Any], keys: Iterable[str], n: int) -> list[str]:
    """A simple random sample of ``n`` keys without replacement, fixed by the seed and the scope."""
    return seeded_order(seed, scope_parts, keys)[:max(0, int(n))]


def share_count(share: Any, n: int) -> int:
    """Units a share of ``n`` selects: the ceiling of share x n (so a non-empty group is never left out)."""
    return min(n, math.ceil(exact(share) * n))


REPLICATE_RULE = ("within each replicate group of the units in scope (the unit's first stratum in the order S1, S2, "
                  "S3, S0, co-cited, and within S1 its work's status), the first ceil(share x n) of the group's n "
                  "units, ranked by sha256 of the protocol seed, 'replicate', the instrument id, the group and the "
                  "unit key (strata.replicate.share)")
REPLICATE_ORDER = ("S1", "S2", "S3", "S0", "co-cited")


def replicate_group(strata: Iterable[str], status: str | None) -> str:
    """The replicate group of a unit: its first stratum in REPLICATE_ORDER, S1 split by its work's status."""
    members = set(strata)
    for stratum in REPLICATE_ORDER:
        if stratum in members:
            return f"S1:{status or 'unknown'}" if stratum == "S1" else stratum
    return "unstratified"


def replicate_groups(connection: sqlite3.Connection, loci: Iterable[str]) -> dict[str, str]:
    """The replicate group of every given frame locus (``replicate_group``)."""
    wanted = set(loci)
    strata: dict[str, set[str]] = defaultdict(set)
    statuses: dict[str, str | None] = {}
    for locus, stratum, status in connection.execute(
            "SELECT s.locus_id, s.stratum, w.status FROM fu_strata s JOIN fu_frame f ON f.locus_id=s.locus_id "
            "LEFT JOIN fu_work_status w ON w.work_key=f.work_key"):
        if locus in wanted:
            strata[locus].add(stratum)
            statuses[locus] = status
    return {locus: replicate_group(strata.get(locus, ()), statuses.get(locus)) for locus in wanted}


def replicate_keys(protocol: Mapping[str, Any], keys: Iterable[str], *, scope_parts: Sequence[Any] = (),
                   groups: Mapping[str, str] | None = None) -> list[str]:
    """The blind-replicate subsample of ``keys`` (protocol seed and strata.replicate.share), in the given order.

    ``groups`` maps a key to its replicate group (``replicate_groups``); the draw is made within each group, so every
    stratum and every S1 status present gets its share of replicate units (a non-empty group at least one). Judging
    commands call this with ``--variant replicate``; the selection depends on the seed, the scope, the groups and the
    set of keys only, so every command and every rerun draws the same units.
    """
    replicate = (protocol.get("strata") or {}).get("replicate")
    if not replicate:
        raise ValidationError("the protocol declares no replicate share (strata.replicate.share)")
    ordered = list(dict.fromkeys(keys))
    by_group: dict[str, list[str]] = defaultdict(list)
    for key in ordered:
        by_group[(groups or {}).get(key, "unstratified")].append(key)
    chosen: set[str] = set()
    for group, members in sorted(by_group.items()):
        chosen.update(seeded_sample(protocol["seed"], ("replicate", *scope_parts, group), members,
                                    share_count(replicate["share"], len(members))))
    return [key for key in ordered if key in chosen]


# ----------------------------------------------------------------------------- frame
FRAME_COLUMNS = ("locus_id", "journal", "doi", "label", "seq", "work_key", "claim_id", "sentence_sha256", "unit",
                 "section", "review_year", "review_first_online", "topic", "review_stratum", "provenance_class",
                 "identity", "cited_inbody", "census_abs_category", "census_ft_category")


def frame_rows(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> Iterator[tuple[Any, ...]]:
    """One row (FRAME_COLUMNS) per parent locus in the protocol's journals and years, streamed in document order."""
    clause, params = scope(protocol)
    cursor = connection.cursor()
    cursor.row_factory = None
    for (journal, doi, label, seq, sentence, section, unit, work_key, identity, inbody, abs_category, ft_category,
         year, first_online, topic, stratum, provenance) in cursor.execute(f"""
        SELECT l.journal, l.doi, l.label, l.seq, l.sentence, l.section, l.unit, c.cited_work_key, c.identity,
               c.cited_inbody, c.abs_category, c.ft_category, r.year, r.first_online, r.topic, r.stratum,
               r.provenance_class
        FROM census.loci l
        JOIN census.citations c ON c.journal=l.journal AND c.doi=l.doi AND c.label=l.label
        JOIN census.reviews r ON r.journal=l.journal AND r.doi=l.doi
        WHERE {clause} ORDER BY l.journal, l.doi, l.unit, l.seq, l.label""", params):
        sentence_sha = sentence_digest(sentence or "")
        yield (locus_id(journal, doi, label, seq), journal, doi, label, int(seq), work_key,
               claim_id(journal, doi, unit, sentence_sha), sentence_sha, int(unit), section, year, first_online, topic,
               stratum, provenance if provenance in PROVENANCE else "unavailable", identity, int(inbody or 0),
               abs_category, ft_category)


def eligible_sql(protocol: Mapping[str, Any], alias: str = "f") -> tuple[str, list[Any]]:
    """SQL condition for eligible frame loci: a cited work, a listed identity class, in the body when required."""
    s1 = protocol["strata"]["S1"]
    identities = list(s1["identities"])
    clause = (f"{alias}.work_key IS NOT NULL AND {alias}.identity IN ({','.join('?' for _ in identities)})"
              + (f" AND {alias}.cited_inbody=1" if s1["inBodyOnly"] else ""))
    return clause, identities


def is_eligible(row: Mapping[str, Any], protocol: Mapping[str, Any]) -> bool:
    s1 = protocol["strata"]["S1"]
    return bool(row["work_key"]) and row["identity"] in s1["identities"] and (
        not s1["inBodyOnly"] or int(row["cited_inbody"] or 0) == 1)


def _cell_key(cell: Sequence[Any]) -> str:
    return canonical(list(cell))


def allocate(size: int, weights: Mapping[Any, int], capacity: Mapping[Any, int]) -> tuple[dict[Any, int], int]:
    """Frequency matching: ``size`` units over cells in proportion to ``weights`` (largest remainders, ties in cell
    order), each cell capped by its ``capacity``; what the caps leave over goes to weighted cells in cell order.

    Without any weight (no unit to match) the allocation is proportional to the capacities. Returns the allocation
    and the shortfall (units no cell could take).
    """
    order = sorted(set(weights) | set(capacity), key=_cell_key)
    basis = {cell: int(weights.get(cell, 0)) for cell in order if int(weights.get(cell, 0)) > 0}
    if not basis:
        basis = {cell: int(capacity.get(cell, 0)) for cell in order if int(capacity.get(cell, 0)) > 0}
    allocation = {cell: 0 for cell in order}
    total = sum(basis.values())
    if size <= 0 or not total:
        return allocation, max(0, size)
    quotas = {cell: Fraction(size * weight, total) for cell, weight in basis.items()}
    for cell, quota in quotas.items():
        allocation[cell] = math.floor(quota)
    remainder = size - sum(allocation.values())
    position = {cell: index for index, cell in enumerate(order)}
    for cell in sorted(basis, key=lambda c: (-(quotas[c] - math.floor(quotas[c])), position[c]))[:remainder]:
        allocation[cell] += 1
    leftover = 0
    for cell in order:
        cap = int(capacity.get(cell, 0))
        if allocation[cell] > cap:
            leftover += allocation[cell] - cap
            allocation[cell] = cap
    for cell in order:
        if leftover <= 0:
            break
        if cell in basis:
            take = min(int(capacity.get(cell, 0)) - allocation[cell], leftover)
            allocation[cell] += take
            leftover -= take
    return allocation, leftover


def stratum_units(connection: sqlite3.Connection, stratum: str, *, pilot_only: bool = False) -> list[sqlite3.Row]:
    """Frame rows of one stratum with their membership (inclusion and pilot probabilities, cell)."""
    if stratum not in STRATA:
        raise ValidationError(f"unknown stratum: {stratum}")
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(
        "SELECT f.*, s.inclusion_probability, s.selection, s.cell, s.pilot, s.pilot_probability FROM fu_strata s "
        "JOIN fu_frame f ON f.locus_id=s.locus_id WHERE s.stratum=?" + (" AND s.pilot=1" if pilot_only else "") +
        " ORDER BY f.journal, f.doi, f.unit, f.seq, f.label", (stratum,)).fetchall()


def frozen(connection: sqlite3.Connection) -> bool:
    return connection.execute("SELECT COUNT(*) FROM fu_protocol_freeze").fetchone()[0] > 0


def _register_rank(connection: sqlite3.Connection, seed: Any) -> None:
    """fu_s0_rank(key, cell values...): the seeded rank of seeded_order with scope ("S0", cell), inside SQL."""
    def rank(key: str, *cell: Any) -> str:
        return hashlib.sha256((canonical([seed, "S0", list(cell)]) + "\x1f" + key).encode("utf-8")).hexdigest()
    connection.create_function("fu_s0_rank", -1, rank, deterministic=True)


def _temp_keys(connection: sqlite3.Connection, name: str, keys: Iterable[str]) -> None:
    connection.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name}(key TEXT PRIMARY KEY)")
    connection.execute(f"DELETE FROM temp.{name}")
    connection.executemany(f"INSERT OR IGNORE INTO temp.{name}(key) VALUES(?)", ((key,) for key in keys))


# ----------------------------------------------------------------------------- build
def build(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the frame, strata, co-cited anchors and sizes from the parent, the status rows and the hotspots."""
    if frozen(connection):
        raise ValidationError("the follow-up strata are frozen; they cannot be built again")
    strata = protocol["strata"]
    seed = protocol["seed"]
    clause, params = scope(protocol)
    missing = connection.execute(f"""
        SELECT COUNT(DISTINCT c.cited_work_key) FROM census.loci l
        JOIN census.citations c ON c.journal=l.journal AND c.doi=l.doi AND c.label=l.label
        JOIN census.reviews r ON r.journal=l.journal AND r.doi=l.doi
        WHERE {clause} AND c.cited_work_key IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM fu_work_status s WHERE s.work_key=c.cited_work_key)""", params).fetchone()[0]
    if missing:
        raise ValidationError(f"{missing} cited works of the frame have no status row; run followup status-resolve "
                              "before followup strata")
    if not connection.execute("SELECT COUNT(*) FROM fu_hotspots").fetchone()[0]:
        from .followup_stats import hotspot_entries
        if hotspot_entries(connection, protocol, limit=1).fetchone():
            raise ValidationError("the hotspot test has not run; run followup hotspots before followup strata")
    family = s3_family(protocol)
    excluded_keys = family_works(connection, excluded_patterns(protocol))
    family_keys = family_works(connection, family or [])
    s3_built = bool(family is not None and strata["S3"]["licensingVerified"])

    connection.execute("DELETE FROM fu_cocited")
    connection.execute("DELETE FROM fu_strata")
    connection.execute("DELETE FROM fu_stratum_sizes")
    connection.execute("DELETE FROM fu_frame")
    connection.executemany(f"INSERT INTO fu_frame({','.join(FRAME_COLUMNS)}) "
                           f"VALUES({','.join('?' * len(FRAME_COLUMNS))})", frame_rows(connection, protocol))
    frame_count = connection.execute("SELECT COUNT(*) FROM fu_frame").fetchone()[0]
    if not frame_count:
        raise ValidationError("the protocol's journals and years select no locus of the parent census")
    eligible, eligible_params = eligible_sql(protocol)
    eligible_count = connection.execute(f"SELECT COUNT(*) FROM fu_frame f WHERE {eligible}",
                                        eligible_params).fetchone()[0]
    _temp_keys(connection, "fu_excluded_works", excluded_keys)
    _temp_keys(connection, "fu_family_works", family_keys)
    _register_rank(connection, seed)
    match_on = list(strata["S0"]["matchOn"])
    cells_sql = ", ".join(f"f.{CELL_FIELDS[name]}" for name in match_on)

    statuses = list(strata["S1"]["statuses"])
    s1 = [tuple(row) for row in connection.execute(
        f"SELECT f.locus_id, f.journal, s.status, {cells_sql} FROM fu_frame f JOIN fu_work_status s "
        f"ON s.work_key=f.work_key WHERE {eligible} AND s.status IN ({','.join('?' for _ in statuses)}) "
        "ORDER BY f.locus_id", [*eligible_params, *statuses])]
    s2 = [row[0] for row in connection.execute(
        f"SELECT f.locus_id FROM fu_frame f JOIN fu_hotspots h ON h.work_key=f.work_key AND h.selected=1 "
        f"WHERE {eligible} AND f.work_key NOT IN (SELECT key FROM temp.fu_excluded_works) ORDER BY f.locus_id",
        eligible_params)]
    family_loci = [row[0] for row in connection.execute(
        f"SELECT f.locus_id FROM fu_frame f WHERE {eligible} AND f.work_key IN (SELECT key FROM temp.fu_family_works) "
        "ORDER BY f.locus_id", eligible_params)]
    s3 = family_loci if s3_built else []
    dropped = []
    if family is not None and not s3_built:
        dropped.append({"stratum": "S3", "reason": "table licensing is not verified (strata.S3.licensingVerified)",
                        "eligible": len(family_loci)})

    _temp_keys(connection, "fu_anchors", [row[0] for row in s1] + s2)
    cocited_pairs = [tuple(row) for row in connection.execute(
        f"SELECT a.locus_id, f.locus_id, a.claim_id FROM fu_frame a JOIN fu_frame f ON f.claim_id=a.claim_id "
        f"AND f.locus_id!=a.locus_id AND f.work_key!=a.work_key WHERE a.locus_id IN (SELECT key FROM temp.fu_anchors) "
        f"AND {eligible} ORDER BY a.locus_id, f.locus_id", eligible_params)]
    cocited_ids = sorted({pair[1] for pair in cocited_pairs})

    pool = (f"FROM fu_frame f JOIN fu_work_status s ON s.work_key=f.work_key WHERE {eligible} AND s.status='none' "
            "AND f.work_key NOT IN (SELECT work_key FROM fu_hotspots WHERE selected=1) "
            "AND f.work_key NOT IN (SELECT key FROM temp.fu_family_works)")
    capacity = {tuple(row[:-1]): row[-1] for row in connection.execute(
        f"SELECT {cells_sql}, COUNT(*) {pool} GROUP BY {cells_sql}", eligible_params)}
    pool_count = sum(capacity.values())
    allocation, shortfall = allocate(int(strata["S0"]["size"]), Counter(tuple(row[3:]) for row in s1), capacity)
    # One ordered pass over the pool: within each cell, loci in seeded order; the first allocated ones are sampled.
    s0: dict[str, tuple[float, str]] = {}
    taken: Counter[tuple] = Counter()
    if any(allocation.values()):
        for row in connection.execute(
                f"SELECT {cells_sql}, f.locus_id, fu_s0_rank(f.locus_id, {cells_sql}) AS seeded {pool} "
                f"ORDER BY {cells_sql}, seeded, f.locus_id", eligible_params):
            cell, unit = tuple(row[:len(match_on)]), row[len(match_on)]
            if taken[cell] < allocation.get(cell, 0):
                taken[cell] += 1
                s0[unit] = (allocation[cell] / capacity[cell], canonical(dict(zip(match_on, cell))))

    pilot_s1: dict[str, float] = {}
    pilot_s0: dict[str, float] = {}
    pilot = strata.get("pilot")
    if pilot:
        by_status: dict[str, list[str]] = defaultdict(list)
        for row in s1:
            by_status[row[2]].append(row[0])
        for status in sorted(by_status):
            units = by_status[status]
            count = share_count(pilot["s1Share"], len(units))
            for unit in seeded_sample(seed, ("pilot", "S1", status), units, count):
                pilot_s1[unit] = count / len(units)
        count = min(len(s0), int(pilot["s0Count"]))
        for unit in seeded_sample(seed, ("pilot", "S0"), s0, count):
            pilot_s0[unit] = s0[unit][0] * count / len(s0)

    membership = [("S1", row[0], 1.0, "census", None, int(row[0] in pilot_s1), pilot_s1.get(row[0])) for row in s1]
    membership.extend(("S2", unit, 1.0, "census", None, 0, None) for unit in s2)
    membership.extend(("S3", unit, 1.0, "census", None, 0, None) for unit in s3)
    membership.extend(("co-cited", unit, 1.0, "census", None, 0, None) for unit in cocited_ids)
    membership.extend(("S0", unit, probability, "sampled", label, int(unit in pilot_s0), pilot_s0.get(unit))
                      for unit, (probability, label) in sorted(s0.items()))
    connection.executemany("INSERT INTO fu_strata(stratum, locus_id, inclusion_probability, selection, cell, pilot, "
                           "pilot_probability) VALUES(?,?,?,?,?,?,?)", membership)
    connection.executemany("INSERT INTO fu_cocited(anchor_locus_id, locus_id, claim_id) VALUES(?,?,?)", cocited_pairs)

    sizes = []
    journals = list(protocol["journals"])
    pool_by_journal = dict(connection.execute(f"SELECT f.journal, COUNT(*) {pool} GROUP BY f.journal",
                                              eligible_params).fetchall())
    journal_of = dict(connection.execute(
        "SELECT f.locus_id, f.journal FROM fu_frame f JOIN fu_strata s ON s.locus_id=f.locus_id").fetchall())
    journal_of.update(connection.execute(
        f"SELECT f.locus_id, f.journal FROM fu_frame f WHERE {eligible} AND f.work_key IN "
        "(SELECT key FROM temp.fu_family_works)", eligible_params).fetchall())
    members = {"S1": [row[0] for row in s1], "S2": s2, "S3": s3, "co-cited": cocited_ids, "S0": sorted(s0)}
    eligible_units = {"S1": members["S1"], "S2": s2, "S3": family_loci, "co-cited": cocited_ids}
    pilots = {"S1": set(pilot_s1), "S0": set(pilot_s0)}

    def count(units: Iterable[str], journal: str) -> int:
        return sum(1 for unit in units if journal == "*" or journal_of[unit] == journal)
    for stratum in STRATA:
        if stratum == "S3" and family is None:
            continue
        for journal in journals + ["*"]:
            if stratum == "S0":
                available = pool_count if journal == "*" else int(pool_by_journal.get(journal, 0))
            else:
                available = count(eligible_units[stratum], journal)
            sizes.append((stratum, journal, available, count(members[stratum], journal),
                          count(pilots.get(stratum, ()), journal)))
    connection.executemany("INSERT INTO fu_stratum_sizes(stratum, journal, eligible, selected, pilot) "
                           "VALUES(?,?,?,?,?)", sizes)
    return {"rule": RULE, "frame": frame_count, "eligible": eligible_count,
            "strata": {stratum: len(units) for stratum, units in members.items()},
            "cocitedPairs": len(cocited_pairs), "dropped": dropped,
            "s0": {"size": int(strata["S0"]["size"]), "selected": len(s0), "eligible": pool_count,
                   "shortfall": shortfall, "cells": len([cell for cell, take in allocation.items() if take]),
                   "matchOn": match_on},
            "pilot": {"S1": len(pilot_s1), "S0": len(pilot_s0)},
            "familyWorks": len(family_keys), "excludedFamilyWorks": len(excluded_keys)}


# ----------------------------------------------------------------------------- freeze
def strata_digest(connection: sqlite3.Connection) -> str:
    """sha256 over the ordered membership rows (strata and co-cited anchors)."""
    value = hashlib.sha256()
    for row in connection.execute("SELECT stratum, locus_id, inclusion_probability, selection, cell, pilot, "
                                  "pilot_probability FROM fu_strata ORDER BY stratum, locus_id"):
        value.update(canonical(["strata", *row]).encode("utf-8") + b"\n")
    for row in connection.execute("SELECT anchor_locus_id, locus_id, claim_id FROM fu_cocited "
                                  "ORDER BY anchor_locus_id, locus_id"):
        value.update(canonical(["co-cited", *row]).encode("utf-8") + b"\n")
    return value.hexdigest()


def sizes_record(connection: sqlite3.Connection) -> dict[str, dict[str, dict[str, int]]]:
    sizes: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for row in connection.execute("SELECT stratum, journal, eligible, selected, pilot FROM fu_stratum_sizes "
                                  "ORDER BY stratum, journal"):
        sizes[row[0]][row[1]] = {"eligible": row[2], "selected": row[3], "pilot": row[4]}
    return dict(sizes)


def freeze(connection: sqlite3.Connection, branch: FollowupBranch, protocol: Mapping[str, Any],
           built: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the strata: protocol digest, strata digest, status snapshot, parent digest, estimands and sizes."""
    meta = dict(connection.execute("SELECT key, value FROM fu_meta").fetchall())
    parent_sha = scores_db.file_sha256(branch.parent)
    if parent_sha != meta.get("parentSha256"):
        raise ValidationError("the parent census digest changed since the branch was made; the strata are not frozen")
    snapshots = sorted({row[0] for row in connection.execute(
        "SELECT DISTINCT snapshot_id FROM fu_work_status WHERE work_key IN "
        "(SELECT work_key FROM fu_frame WHERE work_key IS NOT NULL)")})
    snapshot = snapshots[0] if len(snapshots) == 1 else canonical(snapshots)
    strata_sha = strata_digest(connection)
    sizes = {"rule": RULE, "frame": built["frame"], "eligible": built["eligible"], "strata": sizes_record(connection),
             "dropped": list(built["dropped"]), "s0": dict(built["s0"])}
    freeze_id = "freeze-" + digest(meta.get("protocolSha256"), strata_sha, snapshot, parent_sha)[:32]
    frozen_at = now()
    connection.execute("INSERT INTO fu_protocol_freeze VALUES(?,?,?,?,?,?,?,?)",
                       (freeze_id, meta.get("protocolSha256"), strata_sha, snapshot, parent_sha,
                        canonical(list(protocol["estimands"])), canonical(sizes), frozen_at))
    return {"freezeId": freeze_id, "protocolSha256": meta.get("protocolSha256"), "strataSha256": strata_sha,
            "statusSnapshot": snapshot, "parentSha256": parent_sha, "frozenAt": frozen_at}


def cmd_strata(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = protocol_of(branch)
    with branch.writing() as connection:
        if getattr(args, "freeze", False) and frozen(connection):
            # A freeze asked again (the same command run again after its result was lost) reports the freeze the
            # branch holds; the strata are never built again.
            row = connection.execute("SELECT * FROM fu_protocol_freeze ORDER BY frozen_at DESC LIMIT 1").fetchone()
            return {"database": str(branch.path), **json.loads(row["sizes_json"]), "alreadyFrozen": True,
                    "freeze": {"freezeId": row["freeze_id"], "protocolSha256": row["protocol_sha256"],
                               "strataSha256": row["strata_sha256"], "statusSnapshot": row["status_snapshot"],
                               "parentSha256": row["parent_sha256"], "frozenAt": row["frozen_at"]}}
        built = build(connection, protocol)
        result = {"database": str(branch.path), **built}
        if getattr(args, "freeze", False):
            result["freeze"] = freeze(connection, branch, protocol, built)
    return result
