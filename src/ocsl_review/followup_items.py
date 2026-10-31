# SPDX-License-Identifier: Apache-2.0
"""Builder and validator shared by the follow-up instruments (citation-function@1, numeric-check@1, claim-match@1).

Pure functions, no I/O. An instrument module names the item fields its judge sees and the rules its outputs must meet;
this module turns them into messages and checks a response: one JSON object ``{"items": [...]}``, exactly one output
per input id, each output valid against the bundle's output schema, each quote an exact substring of the input field it
names. It is the builder module of those bundles, so its digest is part of each of their instrument digests.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable, Mapping, Sequence

import jsonschema

from .errors import ValidationError

PROTOCOL = "ocsl-followup-items-v1"
MAX_ITEMS = 40
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


class ItemError(ValidationError):
    """A classified rejection of one output item; ``retryable`` items go to the repair pass."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


def check_items(items: Sequence[Mapping[str, Any]], fields: Sequence[str], required: Sequence[str]) -> None:
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_ITEMS:
        raise ItemError("invalid_batch", f"a call needs 1 to {MAX_ITEMS} items")
    ids = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ItemError("invalid_item", "every item needs a non-empty string id")
        missing = [name for name in required if item.get(name) in (None, "")]
        if missing:
            raise ItemError("invalid_item", f"item {item['id']} lacks {', '.join(missing)}")
        unknown = sorted(set(item) - set(fields) - {"provenance"})
        if unknown:
            raise ItemError("invalid_item", f"item {item['id']} carries fields the instrument does not send: "
                            + ", ".join(unknown))
        ids.append(item["id"])
    if len(set(ids)) != len(ids):
        raise ItemError("invalid_batch", "item ids must be unique within a call")


def build_messages(system_prompt: str, instrument_id: str, tier: str, items: Sequence[Mapping[str, Any]],
                   fields: Sequence[str], required: Sequence[str] = ("id",)) -> list[dict[str, str]]:
    """System prompt unchanged; one user message with the items' declared fields as canonical JSON."""
    check_items(items, fields, required)
    payload = {"protocol": PROTOCOL, "instrument": instrument_id, "tier": tier,
               "items": [{name: item[name] for name in fields if name in item} for item in items]}
    return [{"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)}]


def parse_response(text: Any) -> list[Any]:
    """The ``items`` array of a response: one JSON object, optionally inside one fenced ``json`` block."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        raise ItemError("invalid_json", "response is not text", retryable=True)
    match = _FENCE.match(text)
    try:
        value = json.loads(match.group(1) if match else text)
    except ValueError as error:
        raise ItemError("invalid_json", "response is not JSON", retryable=True) from error
    if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list):
        raise ItemError("invalid_schema", "response must be one object with an items array", retryable=True)
    return value["items"]


def require_quote(item: Mapping[str, Any], output: Mapping[str, Any], *, allowed: Sequence[str],
                  quote_key: str = "quote", field_key: str | None = "quoteField") -> None:
    """The quote is non-empty and occurs exactly as written in the input field it names (or the only allowed one)."""
    quote = output.get(quote_key)
    if not isinstance(quote, str) or not quote.strip():
        raise ItemError("missing_quote", "a substantive output needs an exact quote", retryable=True)
    field = output.get(field_key) if field_key else allowed[0]
    if field not in allowed:
        raise ItemError("invalid_quote", f"the quote must come from {', '.join(allowed)}", retryable=True)
    source = item.get(field)
    if not isinstance(source, str) or quote not in source:
        raise ItemError("invalid_quote", f"the quote is absent from {field}", retryable=True)


def validate_response(items: Sequence[Mapping[str, Any]], text: Any, *, item_schema: Mapping[str, Any],
                      rules: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """Accepted outputs by id, rejected ids with their codes, unexpected ids and unidentified outputs.

    A response that cannot be parsed rejects every item as retryable; one invalid output never rejects the others.
    """
    by_id = {item["id"]: item for item in items}
    try:
        outputs = parse_response(text)
    except ItemError as error:
        return {"accepted": {}, "rejected": {key: {"code": error.code, "message": str(error), "retryable": True}
                                             for key in by_id},
                "unexpected": [], "unidentifiedItems": 0}
    validator = jsonschema.Draft202012Validator(item_schema)
    counts = Counter(output.get("id") for output in outputs if isinstance(output, dict) and isinstance(output.get("id"), str))
    accepted: dict[str, Any] = {}
    rejected: dict[str, Any] = {}
    for key, item in by_id.items():
        if counts[key] != 1:
            rejected[key] = {"code": "missing_item" if not counts[key] else "duplicate_item",
                             "message": "expected exactly one output for this item", "retryable": True}
            continue
        output = next(o for o in outputs if isinstance(o, dict) and o.get("id") == key)
        error = next(iter(sorted(validator.iter_errors(output), key=lambda e: list(map(str, e.absolute_path)))), None)
        try:
            if error is not None:
                raise ItemError("invalid_schema", error.message, retryable=True)
            if rules is not None:
                rules(item, output)
        except ItemError as failure:
            rejected[key] = {"code": failure.code, "message": str(failure), "retryable": failure.retryable}
            continue
        accepted[key] = dict(output)
    return {"accepted": accepted, "rejected": rejected,
            "unexpected": sorted(key for key in counts if key not in by_id),
            "unidentifiedItems": sum(1 for o in outputs if not isinstance(o, dict) or not isinstance(o.get("id"), str))}
