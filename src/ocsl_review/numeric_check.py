# SPDX-License-Identifier: Apache-2.0
"""numeric-check@1: the model's parse of a statistic claim (validator module of the bundle).

Pure functions. The judge parses what a citing sentence states (condition, metric, geography, reference year, the
number as written and its unit); the comparison with the statistics tables is deterministic and lives outside the
instrument (``followup_numeric``), so a table source can change without a new instrument. Item input::

    {"id": "<locus id>", "marker": "⟨R3⟩", "citingSentence": "... ⟨R3⟩ ...",
     "citedWork": {"title": "...", "year": 2020}}
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping, Sequence

from . import followup_items
from .followup_items import ItemError

INSTRUMENT_ID = "numeric-check@1"
TIER = "claim"
DECISIONS = ("parsed", "not-epidemiological", "abstain")
METRICS = ("incidence-count", "incidence-rate", "mortality-count", "mortality-rate", "prevalence", "survival", "rank",
           "trend", "other")
FIELDS = ("id", "marker", "citingSentence", "citedWork")
REQUIRED = ("id", "marker", "citingSentence", "citedWork")
PARSED_FIELDS = ("condition", "metric", "geography", "valueText", "unit", "quote")


@lru_cache(maxsize=1)
def instrument():
    from .instruments import load
    return load(INSTRUMENT_ID)


@lru_cache(maxsize=1)
def item_schema() -> dict[str, Any]:
    return json.loads(instrument().text("outputSchema"))["properties"]["items"]["items"]


def item_key(item: Mapping[str, Any]) -> str:
    return item["id"]


def build_messages(items: Sequence[Mapping[str, Any]], tier: str = TIER) -> list[dict[str, str]]:
    if tier != TIER:
        raise ItemError("invalid_tier", f"{INSTRUMENT_ID} has the single tier {TIER}")
    for item in items:
        if isinstance(item, Mapping) and item.get("marker") and item.get("marker") not in str(item.get("citingSentence")):
            raise ItemError("invalid_item", f"item {item.get('id')}: the marker does not occur in the citing sentence")
    return followup_items.build_messages(instrument().text("systemPrompt"), INSTRUMENT_ID, tier, items, FIELDS, REQUIRED)


def _rules(item: Mapping[str, Any], output: Mapping[str, Any]) -> None:
    decision = output["decision"]
    if decision != "parsed":
        if any(output[name] for name in PARSED_FIELDS) or output["year"] is not None:
            raise ItemError("invalid_parse", f"a {decision} item carries no parsed fields", retryable=True)
        if decision == "abstain" and not output["abstainReason"].strip():
            raise ItemError("missing_reason", "an abstention needs its reason", retryable=True)
        return
    if output["abstainReason"].strip():
        raise ItemError("invalid_reason", "only an abstention carries an abstainReason", retryable=True)
    if not output["metric"] or not output["valueText"].strip() or not output["condition"].strip():
        raise ItemError("invalid_parse", "a parsed claim needs condition, metric and valueText", retryable=True)
    sentence = str(item.get("citingSentence", ""))
    if output["valueText"] not in sentence:
        raise ItemError("invalid_value", "valueText is absent from the citing sentence", retryable=True)
    followup_items.require_quote(item, output, allowed=("citingSentence",), field_key=None)
    if output["valueText"] not in output["quote"]:
        raise ItemError("invalid_quote", "the quote must contain valueText", retryable=True)


def validate_response(items: Sequence[Mapping[str, Any]], text: Any) -> dict[str, Any]:
    return followup_items.validate_response(items, text, item_schema=item_schema(), rules=_rules)
