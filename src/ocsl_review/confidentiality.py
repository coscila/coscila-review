"""Confidential-manuscript egress policy enforced before provider I/O."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ConfidentialityError, ValidationError
from .util import atomic_json

IDENTIFIER_FIELDS = {
    "doi",
    "pmid",
    "pmcid",
    "issn",
    "isbn",
    "orcid",
    "datasetDoi",
    "softwareDoi",
}
QUERY_FIELDS = {"query", "sanitizedQuery"}
SAFE_CONTROL_FIELDS = {
    "cursor",
    "format",
    "fromYear",
    "kind",
    "limit",
    "page",
    "provider",
    "resultType",
    "rows",
    "sort",
    "source",
    "toYear",
}
SENSITIVE_KEY_FRAGMENTS = {
    "abstract",
    "author",
    "caption",
    "figure",
    "manuscript",
    "note",
    "paragraph",
    "path",
    "review",
    "sentence",
    "table",
    "title",
    "upload",
}
IDENTIFIER_PATTERNS = {
    "doi": re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE),
    "datasetDoi": re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE),
    "softwareDoi": re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE),
    "pmid": re.compile(r"^\d{1,10}$"),
    "pmcid": re.compile(r"^PMC\d+$", re.IGNORECASE),
    "orcid": re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.IGNORECASE),
}


@dataclass(frozen=True)
class EgressPlan:
    confidential: bool = True
    allow_sanitized_queries: bool = True
    max_query_terms: int = 12
    approved_operations: frozenset[str] = field(default_factory=frozenset)
    approved_fields: frozenset[str] = field(default_factory=frozenset)
    shared_cache: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EgressPlan:
        known = {
            "confidential",
            "allowSanitizedQueries",
            "maxQueryTerms",
            "approvedOperations",
            "approvedFields",
            "sharedCache",
        }
        extra = set(value) - known
        if extra:
            raise ValidationError(f"unknown egress-plan fields: {sorted(extra)}")
        for field_name in ("confidential", "allowSanitizedQueries", "sharedCache"):
            if field_name in value and not isinstance(value[field_name], bool):
                raise ValidationError(f"egress-plan {field_name} must be boolean")
        maximum = value.get("maxQueryTerms", 12)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 30:
            raise ValidationError("egress-plan maxQueryTerms must be an integer from 1 to 30")
        approved: dict[str, frozenset[str]] = {}
        for source_name, target_name in (
            ("approvedOperations", "operations"),
            ("approvedFields", "fields"),
        ):
            items = value.get(source_name, [])
            if (
                not isinstance(items, list)
                or not all(isinstance(item, str) and item for item in items)
                or len(items) != len(set(items))
            ):
                raise ValidationError(f"egress-plan {source_name} must contain unique strings")
            approved[target_name] = frozenset(items)
        confidential = value.get("confidential", True)
        shared = value.get("sharedCache", False)
        if confidential and shared:
            raise ValidationError("confidential reviews cannot use a shared provider cache")
        return cls(
            confidential=confidential,
            allow_sanitized_queries=value.get("allowSanitizedQueries", True),
            max_query_terms=maximum,
            approved_operations=approved["operations"],
            approved_fields=approved["fields"],
            shared_cache=shared,
        )

    @classmethod
    def load(cls, path: Path | None) -> EgressPlan:
        if path is None:
            return cls()
        return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    flattened: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten(child, name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flattened.extend(_flatten(child, f"{prefix}[{index}]"))
    else:
        flattened.append((prefix, value))
    return flattened


def _query_terms(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", value)


def _validate_sanitized_query(value: str, maximum: int) -> None:
    if "\n" in value or "\r" in value or len(value) > 240:
        raise ConfidentialityError("search query resembles manuscript prose")
    terms = _query_terms(value)
    if not terms or len(terms) > maximum:
        raise ConfidentialityError(f"search query must contain 1-{maximum} minimized terms")
    if any(len(term) > 50 for term in terms):
        raise ConfidentialityError("search query contains an unusually identifying token")
    if re.search(r"[.!?]\s+[A-Z]", value) or re.search(r"[\"“”].{20,}[\"“”]", value):
        raise ConfidentialityError(
            "quoted or sentence-like manuscript text is not a sanitized query"
        )


def authorize_egress(
    *,
    provider: str,
    operation: str,
    arguments: Mapping[str, Any],
    plan: EgressPlan,
) -> dict[str, Any]:
    """Reject confidential content before any network client is constructed."""

    if plan.approved_operations and operation not in plan.approved_operations:
        raise ConfidentialityError(f"operation is outside the approved egress plan: {operation}")
    if not plan.confidential:
        return _audit_event(provider, operation, arguments, decision="allowed-nonconfidential")

    allowed = IDENTIFIER_FIELDS | SAFE_CONTROL_FIELDS | plan.approved_fields
    if plan.allow_sanitized_queries:
        allowed |= QUERY_FIELDS
    for path, value in _flatten(arguments):
        leaf = re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1])
        lowered = leaf.lower()
        if (
            any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)
            and leaf not in plan.approved_fields
        ):
            raise ConfidentialityError(f"confidential field cannot leave the workspace: {path}")
        if leaf not in allowed and value not in (None, "", False):
            raise ConfidentialityError(f"field is not approved for confidential egress: {path}")
        if value in (None, "", False):
            continue
        if leaf in QUERY_FIELDS:
            if not isinstance(value, str):
                raise ConfidentialityError(f"query field must be text: {path}")
            _validate_sanitized_query(value, plan.max_query_terms)
        if leaf in IDENTIFIER_PATTERNS and (
            not isinstance(value, str) or not IDENTIFIER_PATTERNS[leaf].match(value.strip())
        ):
            raise ConfidentialityError(f"invalid {leaf} identifier")
    return _audit_event(provider, operation, arguments, decision="allowed-minimized")


def _audit_event(
    provider: str, operation: str, arguments: Mapping[str, Any], *, decision: str
) -> dict[str, Any]:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "provider": provider,
        "operation": operation,
        "decision": decision,
        "argumentNames": sorted(arguments),
        "payloadSha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def append_egress_event(workspace: Path, event: Mapping[str, Any]) -> None:
    """Persist only hashes and field names; never persist confidential payloads."""

    path = workspace / ".ocsl-review" / "egress-events.json"
    events: list[dict[str, Any]] = []
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            events = loaded
    events.append(dict(event))
    atomic_json(path, events, mode=0o600)
