# SPDX-License-Identifier: Apache-2.0
"""Dependency aggregation (``dependency-aggregation@1``): does any co-cited work support an S1 or S2 claim at full text?

For every S1 or S2 anchor locus *a* (claim *c*, the citing sentence, and cited work *w*):

1. the co-cited works are the distinct work keys of the other parent loci of the same citing sentence whose work
   differs from *w* (a work cited twice in the sentence counts once); a co-cited locus whose identity is unresolved
   counts as one unassessable co-citation;
2. a co-cited work supports the claim when one of its loci has a full-text ``fu_support`` verdict of direct-support
   or scope-caveat; it is unassessable when none of its loci has an assessable full-text verdict;
3. the class is ``sole`` when the sentence cites no other work, ``redundant`` when at least one co-cited work
   supports the claim, ``essential`` when every co-cited work is assessable and none supports it, and
   ``undetermined`` otherwise: abstract silence and failed downloads never count as "not supporting";
4. the row records the counts, the anchor's own full-text verdict and every co-cited locus with its verdict.

The co-citations come from the parent's loci (every locus of the sentence, also those outside the frame), so a work
the frame leaves out can only make the class undetermined, never essential. Recomputing gives the same rows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .followup_branch import FollowupBranch, require_freeze
from .followup_model import DEPENDENCY_RULE, canonical, locus_id, sentence_digest
from .followup_schema import SUPPORTING

ANCHOR_STRATA = ("S1", "S2")


def anchors(connection: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT DISTINCT f.* FROM fu_frame f JOIN fu_strata s ON s.locus_id=f.locus_id WHERE s.stratum IN "
        f"({','.join('?' for _ in ANCHOR_STRATA)}) ORDER BY f.locus_id", ANCHOR_STRATA)]


def sentence_loci(connection: Any, journal: str, doi: str, unit: int, sentence_sha256: str) -> list[dict[str, Any]]:
    """Every parent locus of one citing sentence with its cited work and identity class."""
    rows = connection.execute(
        "SELECT l.label, l.seq, l.sentence, c.cited_work_key, c.identity FROM census.loci l JOIN census.citations c "
        "ON c.journal=l.journal AND c.doi=l.doi AND c.label=l.label WHERE l.journal=? AND l.doi=? AND l.unit=? "
        "ORDER BY l.label, l.seq", (journal, doi, int(unit))).fetchall()
    return [{"locusId": locus_id(journal, doi, row[0], row[1]), "label": row[0], "seq": row[1], "workKey": row[3],
             "identity": row[4]} for row in rows if sentence_digest(row[2] or "") == sentence_sha256]


def full_text_verdicts(connection: Any) -> dict[str, tuple[str | None, int, str | None]]:
    return {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
        "SELECT locus_id, verdict, assessable, reason FROM fu_support WHERE tier='full-text'")}


def classify(anchor: Mapping[str, Any], loci: list[dict[str, Any]],
             verdicts: Mapping[str, tuple[str | None, int, str | None]]) -> dict[str, Any]:
    """The dependency row of one anchor from the parent loci of its sentence and the full-text verdicts."""
    works: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for item in loci:
        if item["locusId"] == anchor["locus_id"]:
            continue
        verdict, assessable, reason = verdicts.get(item["locusId"], (None, 0, "not-judged"))
        entry = {"locusId": item["locusId"], "label": item["label"], "seq": item["seq"],
                 "verdict": verdict if assessable else None, "assessable": bool(assessable),
                 "reason": None if assessable else (reason or "not-judged")}
        if item["workKey"] is None:
            unresolved.append({**entry, "assessable": False, "verdict": None, "reason": "identity-unresolved"})
            continue
        if item["workKey"] == anchor["work_key"]:
            continue
        work = works.setdefault(item["workKey"], {"workKey": item["workKey"], "loci": [], "assessable": False,
                                                  "supports": False})
        work["loci"].append(entry)
        if assessable:
            work["assessable"] = True
            work["supports"] = work["supports"] or verdict in SUPPORTING
    listed = [works[key] for key in sorted(works)]
    cocited = len(listed) + len(unresolved)
    supporting = sum(1 for work in listed if work["supports"])
    unassessable = sum(1 for work in listed if not work["assessable"]) + len(unresolved)
    if not cocited:
        klass = "sole"
    elif supporting:
        klass = "redundant"
    elif not unassessable:
        klass = "essential"
    else:
        klass = "undetermined"
    own = verdicts.get(anchor["locus_id"])
    anchor_verdict = own[0] if own and own[1] else None
    detail = {"cocitedWorks": listed, "unresolvedLoci": unresolved,
              "anchor": {"verdict": anchor_verdict, "reason": None if own and own[1] else (own[2] if own else
                                                                                           "not-judged")}}
    return {"locus_id": anchor["locus_id"], "claim_id": anchor["claim_id"], "cocited": cocited,
            "cocited_supporting": supporting, "cocited_unassessable": unassessable, "class": klass,
            "anchor_verdict": anchor_verdict, "rule_version": DEPENDENCY_RULE, "detail_json": canonical(detail)}


def compute_dependency(connection: Any) -> dict[str, Any]:
    """Write one fu_dependency row per S1 and S2 anchor (idempotent)."""
    verdicts = full_text_verdicts(connection)
    rows = []
    for anchor in anchors(connection):
        loci = sentence_loci(connection, anchor["journal"], anchor["doi"], anchor["unit"], anchor["sentence_sha256"])
        rows.append(classify(anchor, loci, verdicts))
    columns = ("locus_id", "claim_id", "cocited", "cocited_supporting", "cocited_unassessable", "class",
               "anchor_verdict", "rule_version", "detail_json")
    connection.executemany(f"INSERT OR REPLACE INTO fu_dependency({','.join(columns)}) VALUES "
                           f"({','.join('?' for _ in columns)})", [tuple(row[key] for key in columns) for row in rows])
    keep = [row["locus_id"] for row in rows]
    if keep:
        connection.execute(f"DELETE FROM fu_dependency WHERE locus_id NOT IN ({','.join('?' for _ in keep)})", keep)
    else:
        connection.execute("DELETE FROM fu_dependency")
    classes: dict[str, int] = {}
    for row in rows:
        classes[row["class"]] = classes.get(row["class"], 0) + 1
    return {"rule": DEPENDENCY_RULE, "anchors": len(rows), "classes": dict(sorted(classes.items())),
            "withFullTextVerdict": sum(1 for row in rows if row["anchor_verdict"] is not None)}


def cmd_dependency(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    with branch.writing() as connection:
        freeze = require_freeze(connection)
        result = compute_dependency(connection)
    return {**result, "freeze": freeze["freeze_id"]}
