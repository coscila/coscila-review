# SPDX-License-Identifier: Apache-2.0
"""claim-match@1: whether a later article restates a review's claim and cites the review for it (validator module).

Pure functions. The BM25 candidate search over the later article's sentences that cite the review is outside the
instrument (``followup_secondgen`` with ``retrieval``); the judge only confirms a candidate. Item input::

    {"id": "<anchor locus>|<sentence id>", "originalClaim": "...", "originalReview": "<review title>",
     "candidateSentence": "... ⟨B7⟩ ...", "candidateMarker": "⟨B7⟩", "candidateSection": "Introduction"}
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping, Sequence

from . import followup_items
from .followup_items import ItemError

INSTRUMENT_ID = "claim-match@1"
TIER = "pair"
MATCHES = ("same-claim", "related-claim", "different-claim")
RELIANCES = ("cites-review-for-claim", "cites-review-otherwise", "unclear")
FIELDS = ("id", "originalClaim", "originalReview", "candidateSentence", "candidateMarker", "candidateSection")
REQUIRED = ("id", "originalClaim", "candidateSentence", "candidateMarker")


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
        if isinstance(item, Mapping) and item.get("candidateMarker") and \
                item["candidateMarker"] not in str(item.get("candidateSentence")):
            raise ItemError("invalid_item", f"item {item.get('id')}: the marker does not occur in the candidate")
    return followup_items.build_messages(instrument().text("systemPrompt"), INSTRUMENT_ID, tier, items, FIELDS, REQUIRED)


def _rules(item: Mapping[str, Any], output: Mapping[str, Any]) -> None:
    if output["match"] == "abstain":
        if not output["abstainReason"].strip():
            raise ItemError("missing_reason", "an abstention needs its reason", retryable=True)
        return
    if output["abstainReason"].strip():
        raise ItemError("invalid_reason", "only an abstention carries an abstainReason", retryable=True)
    followup_items.require_quote(item, output, allowed=("candidateSentence",), field_key=None)


def validate_response(items: Sequence[Mapping[str, Any]], text: Any) -> dict[str, Any]:
    return followup_items.validate_response(items, text, item_schema=item_schema(), rules=_rules)
