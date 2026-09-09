"""Local citation grammar shared by ingestion and claim binding.

Detection and bibliography reconciliation are separate: a syntactically valid
marker is not proof that all its sources were recovered. Offsets always address
the original text, including superscript characters and printed punctuation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

MAX_RANGE_LABELS = 10_000
DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2212"
_SUPER = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_TRANSLATE = str.maketrans(_SUPER + DASHES, "0123456789" + "-" * len(DASHES))
_ITEM = re.compile(r"([1-9][0-9]*)(?:\s*-\s*([0-9]+))?")
_SEPARATOR = re.compile(r"\s*(?:[,;](?:\s*and\b)?|\band\b)\s*", re.IGNORECASE)
_BRACKET = re.compile(r"\[[^\[\]\n]*(?:\n[^\[\]\n]*)?\]")
_PAREN = re.compile(r"\([^()\n]*(?:\n[^()\n]*)?\)")
_SUPER_GROUP = re.compile(rf"[{_SUPER}]+(?:\s*(?:[{DASHES},;]|and)\s*[{_SUPER}]+)*")
_NONCITATION_PREFIX = re.compile(
    r"(?:\b(?:fig(?:ure)?s?|tables?|eq(?:uation)?s?|appendix|section|chapter|"
    r"interval|range|ci|coordinates?|vector|array|score|values?)\.?\s*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NumericCitation:
    start: int
    end: int
    raw: str
    tokens: tuple[str, ...]
    issues: tuple[str, ...] = ()
    syntax: str = "bracketed"
    compressed: bool = False


def parse_numeric_group(
    raw: str, *, labels: Iterable[str] | None = None, max_labels: int = MAX_RANGE_LABELS
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Parse the whole group; never salvage digits from malformed expressions.

    A shortened endpoint retains the leading digits of the start and rolls to
    the next matching suffix when necessary (123–5 => 123..125). That convention
    needs corroboration by *every* expanded bibliography label. Missing labels
    remain explicit, even when some members of a regular range resolve.
    """

    value = raw.translate(_TRANSLATE).strip()
    if value[:1] in {"[", "("}:
        expected = "]" if value[0] == "[" else ")"
        if not value.endswith(expected):
            return (), ("unclosed-citation-marker",), False
        value = value[1:-1].strip()
    if not value or len(value) > 4096:
        return (), ("invalid-citation-marker",), False
    tokens: list[str] = []
    compressed = False
    position = 0
    while position < len(value):
        match = _ITEM.match(value, position)
        if match is None:
            return (), ("invalid-citation-marker",), compressed
        low = int(match.group(1))
        endpoint = match.group(2)
        high = low
        if endpoint is not None:
            if len(endpoint) < len(match.group(1)):
                compressed = True
                base = 10 ** len(endpoint)
                high = low // base * base + int(endpoint)
                if high < low:
                    high += base
            else:
                high = int(endpoint)
            if high < low or high < 1:
                return (), ("descending-citation-range",), compressed
        if high - low + 1 > max_labels or len(tokens) + high - low + 1 > max_labels:
            return (), ("citation-range-limit",), compressed
        tokens.extend(str(number) for number in range(low, high + 1))
        position = match.end()
        if position == len(value):
            break
        separator = _SEPARATOR.match(value, position)
        if separator is None or separator.end() == len(value):
            return (), ("invalid-citation-marker",), compressed
        position = separator.end()
    issues: list[str] = []
    if compressed and (labels is None or not set(tokens).issubset(set(labels))):
        issues.append("compressed-citation-range-unconfirmed")
    return tuple(dict.fromkeys(tokens)), tuple(issues), compressed


def numeric_citations(
    text: str,
    *,
    labels: Iterable[str] | None = None,
    superscript_spans: Iterable[tuple[int, int]] = (),
    allow_parentheses: bool = True,
) -> list[NumericCitation]:
    """Detect delimited or structurally raised numeric markers.

    Ordinary bare digits are never candidates. Superscript runs are passed by
    structural readers; only punctuation/``and`` may bridge raised runs.
    """

    candidates: list[tuple[int, int, str]] = []
    for syntax, pattern in (("bracketed", _BRACKET), ("parenthesized", _PAREN)):
        if syntax == "parenthesized" and not allow_parentheses:
            continue
        for match in pattern.finditer(text):
            body = match.group()[1:-1].translate(_TRANSLATE).strip()
            if not re.search(r"[0-9]", body):
                continue
            # Labels with suffixes and malformed separators stay unresolved,
            # but prose, author-years, equations and statistical notation don't
            # become numeric citations just because they contain a digit.
            if re.search(r"[A-Za-z]", re.sub(r"\band\b", "", body, flags=re.I)):
                if not re.fullmatch(r"[0-9a-zA-Z,;\s-]+", body) or re.search(
                    r"[A-Za-z]{2,}", body
                ):
                    continue
            if re.search(r"[^0-9a-zA-Z,;\s-]", body):
                continue
            if _NONCITATION_PREFIX.search(text[max(0, match.start() - 50):match.start()]):
                continue
            if syntax == "parenthesized" and re.fullmatch(r"(?:18|19|20)\d{2}", body):
                continue
            candidates.append((match.start(), match.end(), syntax))
    candidates.extend((m.start(), m.end(), "superscript") for m in _SUPER_GROUP.finditer(text))
    spans = sorted(superscript_spans)
    groups: list[tuple[int, int]] = []
    for start, end in spans:
        if not (0 <= start < end <= len(text)):
            continue
        if groups and re.fullmatch(rf"\s*(?:[{DASHES},;]|and)?\s*", text[groups[-1][1]:start]):
            groups[-1] = (groups[-1][0], max(end, groups[-1][1]))
        else:
            groups.append((start, end))
    candidates.extend((start, end, "superscript") for start, end in groups)
    result: list[NumericCitation] = []
    for start, end, syntax in sorted(candidates, key=lambda item: (item[0], -item[1])):
        if any(start < marker.end and marker.start < end for marker in result):
            continue
        raw = text[start:end]
        if syntax == "superscript":
            if _NONCITATION_PREFIX.search(text[max(0, start - 50):start]):
                continue
            # Mathematical powers, units and chemical notation have no citation
            # delimiter. Keep them out even if a matching reference exists.
            prefix = text[max(0, start - 5):start]
            if re.search(r"(?:[=+*/^]|\b(?:x|y|z|m|cm|mm|kg|s))$", prefix):
                continue
        tokens, issues, compressed = parse_numeric_group(raw, labels=labels)
        result.append(NumericCitation(start, end, raw, tokens, issues, syntax, compressed))
    return result
