# SPDX-License-Identifier: Apache-2.0
"""citation-function@1: how a review uses a cited work at one locus (validator module of the bundle).

Pure functions. Item input (one per parent census locus)::

    {"id": "<locus id>", "marker": "⟨R12⟩", "citingSentence": "... ⟨R12⟩ ...", "contextBefore": "...",
     "contextAfter": "...", "section": "Results > Subgroups", "citedWork": {"title": "...", "year": 2019}}

Only these fields reach the judge: never the cited work's status, the stratum, support verdicts, identifiers or the
journal. ``provenance`` (public or entitled) travels with an item for routing and is never sent.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping, Sequence

from . import followup_items
from .followup_items import ItemError

INSTRUMENT_ID = "citation-function@1"
TIER = "locus"
CLASSES = ("relies", "background", "critical", "notes-retraction")
FIELDS = ("id", "marker", "citingSentence", "contextBefore", "contextAfter", "section", "citedWork")
REQUIRED = ("id", "marker", "citingSentence", "citedWork")
QUOTE_FIELDS = {"relies": ("citingSentence",), "background": ("citingSentence",), "critical": ("citingSentence",),
                "notes-retraction": ("citingSentence", "contextBefore", "contextAfter")}


@lru_cache(maxsize=1)
def instrument():
    from .instruments import load
    return load(INSTRUMENT_ID)


@lru_cache(maxsize=1)
def item_schema() -> dict[str, Any]:
    return json.loads(instrument().text("outputSchema"))["properties"]["items"]["items"]


def item_key(item: Mapping[str, Any]) -> str:
    return item["id"]


def build_item(key: str, *, marker: str, citing_sentence: str, context_before: str = "", context_after: str = "",
               section: str = "", title: str = "", year: Any = None, provenance: str) -> dict[str, Any]:
    """One item with the declared fields only (and its provenance, which routes it and is never sent)."""
    if provenance not in ("public", "entitled"):
        raise ItemError("invalid_item", f"item {key}: provenance must be public or entitled")
    cited: dict[str, Any] = {"title": title or ""}
    if isinstance(year, int) and not isinstance(year, bool):
        cited["year"] = year
    elif isinstance(year, str) and year.strip().isdigit():
        cited["year"] = int(year.strip())
    return {"id": key, "marker": marker, "citingSentence": citing_sentence, "contextBefore": context_before or "",
            "contextAfter": context_after or "", "section": section or "", "citedWork": cited,
            "provenance": provenance}


def build_messages(items: Sequence[Mapping[str, Any]], tier: str = TIER) -> list[dict[str, str]]:
    if tier != TIER:
        raise ItemError("invalid_tier", f"{INSTRUMENT_ID} has the single tier {TIER}")
    for item in items:
        if isinstance(item, Mapping) and item.get("marker") and item.get("marker") not in str(item.get("citingSentence")):
            raise ItemError("invalid_item", f"item {item.get('id')}: the marker does not occur in the citing sentence")
    return followup_items.build_messages(instrument().text("systemPrompt"), INSTRUMENT_ID, tier, items, FIELDS, REQUIRED)


def _rules(item: Mapping[str, Any], output: Mapping[str, Any]) -> None:
    function = output["function"]
    if function == "abstain":
        if not output["abstainReason"].strip():
            raise ItemError("missing_reason", "an abstention needs its reason", retryable=True)
        return
    if output["abstainReason"].strip():
        raise ItemError("invalid_reason", "only an abstention carries an abstainReason", retryable=True)
    followup_items.require_quote(item, output, allowed=QUOTE_FIELDS[function])


def validate_response(items: Sequence[Mapping[str, Any]], text: Any) -> dict[str, Any]:
    return followup_items.validate_response(items, text, item_schema=item_schema(), rules=_rules)


def row(output: Mapping[str, Any]) -> dict[str, Any]:
    """fu_function columns of an accepted output (an abstention is unassessable with reason ``abstained``)."""
    if output["function"] == "abstain":
        return {"class": None, "assessable": 0, "reason": "abstained", "quote": None, "quote_field": None}
    return {"class": output["function"], "assessable": 1, "reason": None, "quote": output["quote"],
            "quote_field": output["quoteField"]}
