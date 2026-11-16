# SPDX-License-Identifier: Apache-2.0
"""Dependency aggregation (``dependency-aggregation@1``): does any co-cited work support an S1 or S2 claim at full text?

For every S1 or S2 anchor locus *a* (claim *c*, the citing sentence, and cited work *w*):

1. the co-cited works are the distinct work keys of the other parent loci of the same citing sentence whose work
   differs from *w* (a work cited twice in the sentence counts once); a co-cited locus whose identity is unresolved
   counts as one unassessable co-citation;
2. a co-cited locus is decided when its full-text ``fu_support`` verdict is assessable and in citation-support@2's
   class map ``supported`` or ``adverse`` classes; a verdict of the class map's ``uncertain`` class (partial-indirect;
   abstain is never assessable) is unsettled support and leaves the locus unassessable for the dependency, as do
   abstract silence and a failed download;
3. a co-cited work supports the claim when one of its decided loci is in the ``supported`` class; it is assessable
   when at least one of its loci is decided. A co-cited work whose own resolved status is one of the protocol's S1
   statuses (it carries a retraction, expression of concern or correction itself) never counts as support: its
   support is recorded (``compromisedSupporting``) but a claim resting only on compromised works is not redundant;
4. the class is ``sole`` when the sentence cites no other work, ``redundant`` when at least one co-cited work without
   such a status supports the claim, ``essential`` when every co-cited work is assessable and none without such a
   status supports it, and ``undetermined`` otherwise;
5. the row records the counts, the anchor's own full-text verdict, every co-cited locus with its verdict and its
   work's status, and the class each of the two other readings would give (``readings``: unsettled support counted as
   decided non-support; support of compromised works counted), so a predeclared sensitivity can use them.

The co-citations come from the parent's loci (every locus of the sentence, also those outside the frame), so a work
the frame leaves out can only make the class undetermined, never essential. Recomputing gives the same rows.
``--scope pilot`` (re)computes the rows of the pilot anchors only and leaves every other row as it is.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .followup_branch import FollowupBranch, require_freeze
from .followup_model import DEPENDENCY_RULE, canonical, locus_id, sentence_digest
from .followup_schema import COMPROMISED

ANCHOR_STRATA = ("S1", "S2")
SUPPORT_INSTRUMENT = "citation-support@2"


@lru_cache(maxsize=1)
def support_classes() -> dict[str, tuple[str, ...]]:
    """The supported, adverse and uncertain verdicts of citation-support@2's class map (support-verdict@2)."""
    from . import instruments
    class_map = instruments.load(SUPPORT_INSTRUMENT).manifest["classMap"]
    return {name: tuple(class_map[name]) for name in ("supported", "adverse", "uncertain")}


def anchors(connection: Any, *, pilot_only: bool = False) -> list[dict[str, Any]]:
    """S1 and S2 anchor loci (``pilot_only``: the anchors of the pilot subsample)."""
    return [dict(row) for row in connection.execute(
        "SELECT DISTINCT f.* FROM fu_frame f JOIN fu_strata s ON s.locus_id=f.locus_id WHERE s.stratum IN "
        f"({','.join('?' for _ in ANCHOR_STRATA)})" + (" AND s.pilot=1" if pilot_only else "") +
        " ORDER BY f.locus_id", ANCHOR_STRATA)]


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


def work_statuses(connection: Any) -> dict[str, str]:
    return dict(connection.execute("SELECT work_key, status FROM fu_work_status").fetchall())


def _class(cocited: int, supporting: int, unassessable: int) -> str:
    if not cocited:
        return "sole"
    if supporting:
        return "redundant"
    if not unassessable:
        return "essential"
    return "undetermined"


def classify(anchor: Mapping[str, Any], loci: list[dict[str, Any]],
             verdicts: Mapping[str, tuple[str | None, int, str | None]], *,
             statuses: Mapping[str, str] | None = None, compromised: Sequence[str] = COMPROMISED) -> dict[str, Any]:
    """The dependency row of one anchor from the parent loci of its sentence, the full-text verdicts and the resolved
    status of every co-cited work (see the module docstring)."""
    classes = support_classes()
    statuses = statuses or {}
    works: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for item in loci:
        if item["locusId"] == anchor["locus_id"]:
            continue
        verdict, assessable, reason = verdicts.get(item["locusId"], (None, 0, "not-judged"))
        uncertain = bool(assessable) and verdict in classes["uncertain"]
        decided = bool(assessable) and not uncertain
        entry = {"locusId": item["locusId"], "label": item["label"], "seq": item["seq"],
                 "verdict": verdict if assessable else None, "assessable": decided,
                 "reason": None if decided else ("unsettled-support" if uncertain else (reason or "not-judged"))}
        if item["workKey"] is None:
            unresolved.append({**entry, "assessable": False, "verdict": None, "reason": "identity-unresolved"})
            continue
        if item["workKey"] == anchor["work_key"]:
            continue
        status = statuses.get(item["workKey"])
        work = works.setdefault(item["workKey"], {"workKey": item["workKey"], "status": status,
                                                  "compromised": status in compromised, "loci": [],
                                                  "assessable": False, "supports": False, "uncertain": False})
        work["loci"].append(entry)
        work["uncertain"] = work["uncertain"] or uncertain
        if decided:
            work["assessable"] = True
            work["supports"] = work["supports"] or verdict in classes["supported"]
    listed = [works[key] for key in sorted(works)]
    for work in listed:
        work["countsAsSupport"] = work["supports"] and not work["compromised"]
    cocited = len(listed) + len(unresolved)
    supporting = sum(1 for work in listed if work["countsAsSupport"])
    compromised_supporting = sum(1 for work in listed if work["supports"] and work["compromised"])
    unassessable = sum(1 for work in listed if not work["assessable"]) + len(unresolved)
    klass = _class(cocited, supporting, unassessable)
    # The other readings: unsettled support as decided non-support; support of compromised works counted.
    settled = [work for work in listed if work["assessable"] or work["uncertain"]]
    uncertain_reading = _class(cocited, supporting, len(listed) - len(settled) + len(unresolved))
    compromised_reading = _class(cocited, supporting + compromised_supporting, unassessable)
    own = verdicts.get(anchor["locus_id"])
    anchor_verdict = own[0] if own and own[1] else None
    detail = {"cocitedWorks": listed, "unresolvedLoci": unresolved, "compromisedSupporting": compromised_supporting,
              "compromisedStatuses": list(compromised),
              "readings": {"unsettledSupportAsNonSupport": uncertain_reading,
                           "compromisedSupportCounts": compromised_reading},
              "anchor": {"verdict": anchor_verdict, "reason": None if own and own[1] else (own[2] if own else
                                                                                           "not-judged")}}
    return {"locus_id": anchor["locus_id"], "claim_id": anchor["claim_id"], "cocited": cocited,
            "cocited_supporting": supporting, "cocited_unassessable": unassessable, "class": klass,
            "anchor_verdict": anchor_verdict, "rule_version": DEPENDENCY_RULE, "detail_json": canonical(detail)}


def _protocol(connection: Any) -> dict[str, Any]:
    row = connection.execute("SELECT value FROM fu_meta WHERE key='protocol'").fetchone()
    return json.loads(row[0]) if row else {}


def compute_dependency(connection: Any, protocol: Mapping[str, Any] | None = None, *,
                       scope: str = "all") -> dict[str, Any]:
    """Write one fu_dependency row per S1 and S2 anchor (idempotent); ``scope="pilot"`` only for the pilot anchors."""
    if scope not in ("all", "pilot"):
        from .errors import ValidationError
        raise ValidationError("--scope is pilot or all")
    protocol = protocol if protocol is not None else _protocol(connection)
    compromised = tuple(((protocol.get("strata") or {}).get("S1") or {}).get("statuses") or COMPROMISED)
    verdicts = full_text_verdicts(connection)
    statuses = work_statuses(connection)
    rows = []
    for anchor in anchors(connection, pilot_only=scope == "pilot"):
        loci = sentence_loci(connection, anchor["journal"], anchor["doi"], anchor["unit"], anchor["sentence_sha256"])
        rows.append(classify(anchor, loci, verdicts, statuses=statuses, compromised=compromised))
    columns = ("locus_id", "claim_id", "cocited", "cocited_supporting", "cocited_unassessable", "class",
               "anchor_verdict", "rule_version", "detail_json")
    connection.executemany(f"INSERT OR REPLACE INTO fu_dependency({','.join(columns)}) VALUES "
                           f"({','.join('?' for _ in columns)})", [tuple(row[key] for key in columns) for row in rows])
    if scope == "all":
        keep = [row["locus_id"] for row in rows]
        if keep:
            connection.execute(f"DELETE FROM fu_dependency WHERE locus_id NOT IN ({','.join('?' for _ in keep)})",
                               keep)
        else:
            connection.execute("DELETE FROM fu_dependency")
    classes: dict[str, int] = {}
    for row in rows:
        classes[row["class"]] = classes.get(row["class"], 0) + 1
    return {"rule": DEPENDENCY_RULE, "scope": scope, "anchors": len(rows), "classes": dict(sorted(classes.items())),
            "withFullTextVerdict": sum(1 for row in rows if row["anchor_verdict"] is not None),
            "compromisedStatuses": list(compromised)}


def cmd_dependency(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    with branch.writing() as connection:
        freeze = require_freeze(connection)
        result = compute_dependency(connection, branch.protocol() or {}, scope=getattr(args, "scope", "all"))
    return {**result, "freeze": freeze["freeze_id"]}
