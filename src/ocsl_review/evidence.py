"""Shared acquisition/census contract for unabridged provider abstracts.

Completeness describes the structured provider field, not the whole paper or
proof that a publisher deposited every abstract section. Legacy snippets cannot
be promoted merely because they end in a full stop.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_text(provider: str, raw: dict[str, Any], identifiers: Mapping[str, Any]) -> str | None:
    # Reuse the acquisition parser; importing here avoids an initialization
    # cycle while preserving one definition of structured abstract extraction.
    from .scholarly import display_text, parse_pubmed_record
    from .errors import ProviderError

    if provider == "europe-pmc":
        return display_text(raw.get("abstractText"))
    if provider == "pubmed" and isinstance(raw.get("xml"), str) and identifiers.get("pmid"):
        try:
            return parse_pubmed_record(raw["xml"].encode("utf-8"), str(identifiers["pmid"])).get("abstract")
        except ProviderError:
            return None
    return None


def abstract_candidate(record: Mapping[str, Any], *, source_url: str,
                       retrieved_at: str) -> dict[str, Any]:
    """Construct only from a complete structured record, never a search snippet."""
    text = str(record.get("abstract") or "")
    raw = record.get("rawMetadata")
    provider = record.get("provider")
    identifiers = {key: str(record[key]) for key in ("doi", "pmid", "pmcid") if record.get(key)}
    supported = provider in {"europe-pmc", "pubmed"}
    complete = bool(text.strip() and supported and isinstance(raw, dict) and raw
                    and _record_text(provider, raw, identifiers) == text
                    and not re.search(r"(?:\.{3}|…)\s*$", text))
    return {
        "version": 1, "provider": provider, "text": text, "complete": complete,
        "provenance": {
            "basis": "complete-structured-provider-field",
            "sourceUrl": source_url, "retrievedAt": retrieved_at,
            "identifiers": identifiers,
            "textSha256": _sha(text),
            "recordSha256": _sha(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))),
        },
        # Keep the structured field's original record with its digest. A cached
        # entry can be verified without depending on a remote mutable endpoint.
        "rawMetadata": raw,
    }


def complete_abstract(candidate: Any) -> bool:
    """Validate persisted evidence before accepting it as a complete abstract."""
    if not isinstance(candidate, dict) or candidate.get("version") != 1 or candidate.get("complete") is not True:
        return False
    provenance = candidate.get("provenance")
    raw, text = candidate.get("rawMetadata"), candidate.get("text")
    if (candidate.get("provider") not in {"europe-pmc", "pubmed"}
            or not isinstance(provenance, dict) or not isinstance(raw, dict) or not raw
            or not isinstance(text, str) or not text.strip()
            or re.search(r"(?:\.{3}|…)\s*$", text)):
        return False
    return (provenance.get("basis") == "complete-structured-provider-field"
            and str(provenance.get("sourceUrl", "")).startswith("https://")
            and bool(provenance.get("retrievedAt"))
            and isinstance(provenance.get("identifiers"), dict)
            and bool(provenance.get("identifiers"))
            and _record_text(candidate["provider"], raw, provenance["identifiers"]) == text
            and provenance.get("textSha256") == _sha(text)
            and provenance.get("recordSha256") == _sha(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))))
