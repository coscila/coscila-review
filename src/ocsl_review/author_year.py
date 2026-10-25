# SPDX-License-Identifier: Apache-2.0
"""Author-year citations: detection in prose and resolution against the reference list.

Detection recognises parenthetical groups such as "(Smith et al., 2020a; Lee & Park 2019)"
and narrative markers such as "Smith and Jones (2019)" or "Lee's (2018a, b) model".
Resolution binds every cited item to exactly one printed reference by surname(s), year and
year suffix. An item that matches no entry stays unresolved ("cited but not listed"); an item
that matches several entries equally well stays ambiguous. Nothing is guessed: near matches
(a one-year difference, a spelling variant) are reported as hints, never as bindings.

The module is pure and local; it performs no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

RESOLVER_VERSION = "author-year-1"

PARTICLES = frozenset({
    "van", "von", "der", "den", "de", "del", "della", "dello", "dei", "degli", "di", "da",
    "das", "dos", "do", "du", "des", "la", "le", "les", "lo", "ter", "ten", "te", "zu", "zur",
    "af", "av", "al", "el", "bin", "ibn", "ben", "st", "op", "vom",
})
# Words that stand before a year without being an author; they never start an author name.
NON_AUTHOR_WORDS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "spring", "summer", "autumn", "fall", "winter", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "figure", "figures",
    "fig", "figs", "table", "tables", "supplementary", "supplemental", "appendix", "eq",
    "equation", "equations", "section", "sections", "chapter", "box", "panel", "phase",
    "stage", "grade", "wave", "version", "release", "update", "data", "methods", "method",
    "results", "result", "discussion", "introduction", "conclusion", "since", "until", "from",
    "in", "during", "after", "before", "between", "circa", "ca", "approx", "approximately",
    "the", "this", "these", "that", "those", "our", "we", "it", "its", "there", "here", "a",
    "an", "see", "cf", "eg", "ie", "ref", "refs", "reference", "references", "note", "notes",
    "total", "mean", "median", "range", "sd", "se", "iqr", "ci", "hr", "rr", "or", "n", "p",
    "year", "years", "published", "accessed", "retrieved", "updated", "however", "moreover",
    "furthermore", "thus", "therefore", "also", "and", "but", "as", "at", "by", "for", "of",
    "on", "to", "with", "within", "without", "fiscal", "calendar", "baseline", "follow",
    "cohort", "study", "trial", "survey", "census", "report", "guideline", "guidelines",
})
CORPORATE_CONNECTORS = frozenset({"of", "for", "and", "the", "on", "in", "&", "de", "du", "des"})
_TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ı": "i", "ß": "ss",
})
_INVISIBLE = re.compile("[­​-‏⁠-⁤﻿]")
_LETTER = r"[^\W\d_]"
_NAMEWORD = rf"(?:[dDlL]['’])?{_LETTER}+(?:['’\-]{_LETTER}+)*"
_NAMEWORD_TAIL = rf"(?:{_LETTER}['’])?{_LETTER}{{2,}}(?:['’\-]{_LETTER}+)*"
_PARTICLE_PATTERN = (r"(?:[vV]an|[vV]on|[dD]er|[dD]en|[dD]e|[dD]el|[dD]ella|[dD]i|[dD]a|[dD]os"
                     r"|[dD]as|[dD]o|[dD]u|[dD]es|[lL]a|[lL]e|[tT]er|[tT]en|[zZ]u|[aA]f|[aA]l|[eE]l"
                     r"|[bB]in|[iI]bn|St\.?|[vV]om)")
_SURNAME = rf"(?:{_PARTICLE_PATTERN}\s+){{0,3}}{_NAMEWORD}(?:\s+{_NAMEWORD_TAIL}){{0,2}}"
_INITIAL = rf"{_LETTER}{{1,2}}\."
_INITIALS = rf"(?:{_INITIAL}(?:\s*-?\s*{_INITIAL})*|[A-Z]{{1,3}}(?![\w.]))"
_YEAR = r"(?:1[5-9]|20)\d{2}"
_YEAR_TOKEN = re.compile(rf"(?<![\w/.\-]){_YEAR}(?:[a-z]{{1,2}})?(?![\w])")
_SPECIAL = r"in\s+press|in\s+prep(?:aration)?\.?|forthcoming|submitted|unpublished|n\.\s?d\.?|no\s+date"
_SPECIAL_YEAR = re.compile(rf"(?i:{_SPECIAL})(?![\w])")
_ANY_CITED_YEAR = re.compile(rf"(?<![\w/.\-]){_YEAR}(?:[a-z]{{1,2}})?(?![\w])|(?<![\w])(?i:{_SPECIAL})(?![\w])")
_PAREN_GROUP = re.compile(r"\(([^()]{1,600})\)")
_PREFIX = re.compile(
    r"(?:e\.\s?g\.|i\.\s?e\.|cf\.|see(?:\s+also)?|but\s+see|reviewed\s+(?:in|by)|for\s+(?:a\s+)?"
    r"(?:recent\s+)?reviews?(?:\s*,)?(?:\s+see)?|as\s+reviewed\s+(?:in|by)|compare|data\s+from"
    r"|adapted\s+from|modified\s+from|reproduced\s+from|according\s+to)\s*[,:]?\s+",
    re.IGNORECASE,
)
_ET_AL_TAIL = re.compile(
    r"(?:,\s*)?(?:\bet\s*al\b\.?|\s+and\s+(?:colleagues|co-?workers|collaborators|associates))\s*$",
    re.IGNORECASE,
)
_LOCATOR = re.compile(
    r"\s*[,:]\s*(?:p|pp|para|paras|chap|ch|sec|fig|figs|figure|figures|table|tables|tab|suppl"
    r"|supplementary|appendix|eq|eqs|vol|no|box)\.?\s*[\w.\-–]+(?:\s*[-–]\s*[\w.]+)?",
    re.IGNORECASE,
)
_ABBREVIATION = re.compile(r"\s*[\[(]([A-Z][A-Za-z&\-]{1,15})[\])]\s*$")


def clean_text(value: str) -> str:
    """Unicode-normalise printed text: NFC, invisible characters, dashes, quotes and spaces."""

    text = unicodedata.normalize("NFC", str(value or ""))
    text = _INVISIBLE.sub("", text)
    text = (text.replace("–", "-").replace("—", "-").replace("‑", "-")
            .replace("‐", "-").replace("’", "'").replace("‘", "'")
            .replace(" ", " ").replace(" ", " ").replace(" ", " "))
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=65536)
def name_key(value: str) -> str:
    """Comparable surname key: diacritics, case, spacing and punctuation removed."""

    text = unicodedata.normalize("NFKD", str(value or "").translate(_TRANSLITERATE))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return "".join(ch for ch in text if ch.isalpha())


@lru_cache(maxsize=65536)
def core_key(value: str) -> str:
    """The surname key without leading particles ("van der Berg" -> "berg")."""

    tokens = [token for token in re.split(r"\s+", str(value or "").strip()) if token]
    while len(tokens) > 1 and tokens[0].casefold().rstrip(".") in PARTICLES:
        tokens.pop(0)
    return name_key(" ".join(tokens))


@lru_cache(maxsize=16384)
def corporate_key(value: str) -> str:
    return name_key(re.sub(r"^\s*the\s+", "", str(value or ""), flags=re.IGNORECASE))


def _is_capitalized(token: str) -> bool:
    token = token.strip()
    if re.match(r"^[dDlL]['’]\S", token):
        token = token[2:]
    return bool(token) and token[0].isalpha() and token[0].isupper()


def _is_particle(token: str) -> bool:
    return token.casefold().rstrip(".") in PARTICLES


_NAME_TOKEN = re.compile(rf"(?:[dDlL]['’])?{_LETTER}+(?:['’\-.]{_LETTER}*)*\.?")


def _valid_name_unit(unit: str, *, allow_connectors: bool = False) -> bool:
    """A surname (possibly with particles) or, with connectors, an organisation name."""

    tokens = [token for token in re.split(r"\s+", unit.strip()) if token]
    if not tokens or len(tokens) > (10 if allow_connectors else 6):
        return False
    if not _is_capitalized(tokens[-1]):
        return False
    capitals = 0
    for token in tokens:
        if not _NAME_TOKEN.fullmatch(token):
            return False
        if _is_capitalized(token):
            capitals += 1
        elif not (_is_particle(token) or (allow_connectors and token.casefold() in CORPORATE_CONNECTORS)):
            return False
    if tokens[0].casefold().strip(".") in NON_AUTHOR_WORDS and capitals <= 1:
        return False
    return capitals >= 1


def _strip_trailing_initials(unit: str) -> str:
    """"Smith J" and "Smith J.A." keep only the surname."""

    match = re.fullmatch(rf"(.+?)\s+(?:{_INITIALS})", unit.strip())
    if match and _valid_name_unit(match.group(1)):
        return match.group(1)
    return unit.strip()


@dataclass(frozen=True)
class AuthorParse:
    names: tuple[str, ...]
    et_al: bool = False
    corporate: bool = False


@dataclass(frozen=True)
class CitedItem:
    """One cited work inside a marker: a printed author phrase plus one year."""

    text: str
    start: int
    end: int
    names: tuple[str, ...]
    et_al: bool
    year: str | None
    suffix: str
    special: str | None
    alternatives: tuple[AuthorParse, ...] = ()
    locator: str | None = None


@dataclass(frozen=True)
class Marker:
    """A detected citation marker. Offsets address the analysed text exactly."""

    start: int
    end: int
    raw: str
    form: str
    items: tuple[CitedItem, ...]
    author_start: int | None = None
    author_end: int | None = None
    strong: bool = True


def parse_author_zone(zone: str) -> list[AuthorParse]:
    """Candidate parses of a printed author phrase, most specific first."""

    text = zone.strip().strip(",;:").strip()
    if not text or len(text) > 200 or re.search(r"\d", text):
        return []
    et_al = False
    match = _ET_AL_TAIL.search(text)
    if match:
        et_al = True
        text = text[:match.start()].strip().rstrip(",").strip()
    parses: list[AuthorParse] = []
    abbreviation = _ABBREVIATION.search(text)
    if abbreviation:
        text = text[:abbreviation.start()].strip()
    if not text:
        return []
    words = text.split()
    connectors = [word for word in words if word.casefold() in CORPORATE_CONNECTORS]
    if "," not in text and _valid_name_unit(text, allow_connectors=True) and (connectors or len(words) >= 2):
        parses.append(AuthorParse((text,), et_al, True))
    pieces = [piece.strip() for piece in re.split(r"\s*(?:,\s*(?:and|&)\s+|,|;|\s+and\s+|\s*&\s*)\s*", text)]
    names: list[str] = []
    valid = True
    for piece in pieces:
        if not piece:
            valid = False
            break
        if names and re.fullmatch(_INITIALS, piece):
            continue  # "Smith, J." keeps the surname only
        piece = _strip_trailing_initials(piece)
        if not _valid_name_unit(piece):
            valid = False
            break
        names.append(piece)
    if valid and names:
        parse = AuthorParse(tuple(names), et_al, False)
        if parse not in parses:
            parses.append(parse)
    if abbreviation:
        parses.append(AuthorParse((abbreviation.group(1),), et_al, True))
    return parses


def _years_after(text: str, position: int) -> tuple[list[tuple[str | None, str, str | None, int, int]], int]:
    """Read "2020", "2020a, b", "2019, 2020" or "in press" starting exactly at position."""

    years: list[tuple[str | None, str, str | None, int, int]] = []
    token = _YEAR_TOKEN.match(text, position)
    special = None if token else _SPECIAL_YEAR.match(text, position)
    if token:
        years.append((token.group()[:4], token.group()[4:], None, token.start(), token.end()))
        cursor = token.end()
    elif special:
        years.append((None, "", re.sub(r"\s+", " ", special.group().casefold()), special.start(), special.end()))
        cursor = special.end()
    else:
        return [], position
    separator = re.compile(r"\s*,\s*")
    suffix_only = re.compile(r"[a-z]{1,2}(?![\w])")
    while True:
        match = separator.match(text, cursor)
        if not match:
            break
        following = match.end()
        token = _YEAR_TOKEN.match(text, following)
        if token:
            years.append((token.group()[:4], token.group()[4:], None, token.start(), token.end()))
            cursor = token.end()
            continue
        special = _SPECIAL_YEAR.match(text, following)
        if special:
            years.append((None, "", re.sub(r"\s+", " ", special.group().casefold()), special.start(), special.end()))
            cursor = special.end()
            continue
        suffix = suffix_only.match(text, following)
        if suffix and years[-1][0] is not None and years[-1][1]:
            years.append((years[-1][0], suffix.group(), None, suffix.start(), suffix.end()))
            cursor = suffix.end()
            continue
        break
    return years, cursor


def _clean_zone(zone: str) -> tuple[str, int]:
    """Drop lead-in words ("e.g.", "see", "as shown by") before the first author."""

    offset = len(zone) - len(zone.lstrip())
    text = zone.strip()
    while True:
        match = _PREFIX.match(text)
        if not match:
            break
        offset += match.end()
        text = text[match.end():]
    while True:
        match = re.match(r"(\S+)(\s+)", text)
        if not match:
            break
        token = match.group(1)
        later = text[match.end():].split()
        if _is_capitalized(token) and token.casefold().strip(".,:") not in NON_AUTHOR_WORDS:
            break
        if _is_particle(token) and any(_is_capitalized(word) for word in later):
            break
        offset += match.end()
        text = text[match.end():]
    return text.rstrip().rstrip(","), offset


def _primary(parses: Sequence[AuthorParse]) -> AuthorParse:
    return next((parse for parse in parses if not parse.corporate), parses[0])


def _segment_items(segment: str, offset: int, narrative: tuple[AuthorParse, ...] | None = None) -> list[CitedItem]:
    """Cited items of one ";"-separated segment. Offsets are shifted by offset."""

    items: list[CitedItem] = []
    cursor = 0
    pending_narrative = narrative
    while cursor < len(segment):
        if pending_narrative is not None:
            start = cursor + len(segment[cursor:]) - len(segment[cursor:].lstrip())
            years, end = _years_after(segment, start)
            parses = pending_narrative
            zone_start = None
        else:
            found = _ANY_CITED_YEAR.search(segment, cursor)
            if found is None:
                break
            zone, relative = _clean_zone(segment[cursor:found.start()])
            parses = tuple(parse_author_zone(zone))
            if not parses:
                break
            years, end = _years_after(segment, found.start())
            zone_start = cursor + relative
        if not years:
            break
        locator_match = _LOCATOR.match(segment, end)
        locator = locator_match.group().strip(" ,:") if locator_match else None
        primary = _primary(parses)
        for index, (year, suffix, special, year_start, year_end) in enumerate(years):
            item_start = zone_start if index == 0 and zone_start is not None else year_start
            items.append(CitedItem(segment[item_start:year_end], offset + item_start, offset + year_end,
                                   primary.names, primary.et_al, year, suffix, special, tuple(parses), locator))
        cursor = locator_match.end() if locator_match else end
        pending_narrative = None
        following = re.match(r"\s*,\s*", segment[cursor:])
        if not following:
            break
        cursor += following.end()
    return items


_WINDOW = 320
_TOKEN = re.compile(rf"(?:[dDlL]['’])?{_LETTER}+(?:['’\-]{_LETTER}+)*\.?|&|\S")
_NARRATIVE_ET_AL = re.compile(r"(?:,\s*)?\bet\s*al\.?$", re.IGNORECASE)
_NARRATIVE_COLLEAGUES = re.compile(r"\s+and\s+(?:colleagues|co-?workers|collaborators|associates)$", re.IGNORECASE)
_NARRATIVE_AND = re.compile(r"(?:\s+(?:and|&)\s+|\s*&\s*)$")
_NARRATIVE_COMMA = re.compile(r",\s*$")


def _unit_before(text: str, end: int, *, connectors: bool = False) -> tuple[int, str] | None:
    """The maximal capitalised name unit that ends at end (trailing spaces allowed)."""

    window = max(0, end - _WINDOW)
    tokens = list(_TOKEN.finditer(text, window, end))
    if not tokens or text[tokens[-1].end():end].strip():
        return None
    collected: list[re.Match[str]] = []
    capitals = 0
    limit = 10 if connectors else 4
    for token in reversed(tokens):
        value = token.group()
        if collected and text[token.end():collected[-1].start()].strip():
            break
        if _is_capitalized(value) and re.match(rf"(?:[dDlL]['’])?{_LETTER}", value):
            if value.casefold().rstrip(".") in NON_AUTHOR_WORDS or capitals >= limit:
                break
            collected.append(token)
            capitals += 1
        elif collected and _is_particle(value):
            collected.append(token)
        elif connectors and collected and value.casefold() in CORPORATE_CONNECTORS:
            collected.append(token)
        else:
            break
    while collected and not _is_capitalized(collected[-1].group()) and not _is_particle(collected[-1].group()):
        collected.pop()
    if not collected:
        return None
    start = collected[-1].start()
    unit = text[start:collected[0].end()]
    if unit.endswith(".") and not unit.endswith("St."):
        unit = unit[:-1]
    return start, unit


def _narrative_author(text: str, paren_start: int) -> tuple[int, int, list[AuthorParse], bool] | None:
    """Author phrase immediately before "(": "Smith et al.", "Lee and Park", "Lee's"."""

    end = paren_start
    while end > 0 and text[end - 1].isspace():
        end -= 1
    phrase_end = end
    possessive = False
    if text[max(0, end - 2):end] in ("'s", "’s"):
        end -= 2
        possessive = True
    elif text[max(0, end - 2):end] in ("s'", "s’"):
        end -= 1
        possessive = True
    et_al = False
    window = max(0, end - _WINDOW)
    match = _NARRATIVE_ET_AL.search(text, window, end) or _NARRATIVE_COLLEAGUES.search(text, window, end)
    if match:
        et_al = True
        end = match.start()
        while end > 0 and text[end - 1].isspace():
            end -= 1
    last = _unit_before(text, end)
    if last is None:
        return None
    units = [last]
    cursor = last[0]
    connector = _NARRATIVE_AND.search(text, max(0, cursor - 16), cursor)
    if connector and not et_al:
        previous = _unit_before(text, connector.start())
        if previous is not None:
            units.append(previous)
            cursor = previous[0]
            while True:
                comma = _NARRATIVE_COMMA.search(text, max(0, cursor - 8), cursor)
                previous = _unit_before(text, comma.start()) if comma else None
                if previous is None:
                    break
                units.append(previous)
                cursor = previous[0]
    names = [unit for _, unit in reversed(units)]
    parses: list[AuthorParse] = []
    corporate = _unit_before(text, end, connectors=True)
    if corporate and len(corporate[1].split()) >= 2 and _valid_name_unit(corporate[1], allow_connectors=True):
        parses.append(AuthorParse((corporate[1],), et_al, True))
    # Most specific first, then progressively shorter phrases: sentence-initial words such as
    # "Recently Smith" fall away only if the longer phrase matches no reference.
    variants: list[tuple[str, ...]] = [tuple(names[drop:]) for drop in range(len(names))]
    first_tokens = names[0].split()
    variants += [(" ".join(first_tokens[drop:]),) + tuple(names[1:]) for drop in range(1, len(first_tokens))]
    last_tokens = names[-1].split()
    variants += [(" ".join(last_tokens[drop:]),) for drop in range(1, len(last_tokens))]
    for variant in variants:
        if all(_valid_name_unit(name) for name in variant):
            parse = AuthorParse(variant, et_al, False)
            if parse not in parses:
                parses.append(parse)
    if not any(not parse.corporate for parse in parses):
        return None
    strong = et_al or len(names) >= 2 or possessive
    return cursor, phrase_end, parses, strong


def detect(text: str) -> list[Marker]:
    """Find author-year markers in text; offsets refer to text itself."""

    markers: list[Marker] = []
    for group in _PAREN_GROUP.finditer(text):
        content = group.group(1)
        if not _ANY_CITED_YEAR.search(content):
            continue
        base = group.start(1)
        leading = len(content) - len(content.lstrip())
        starts_with_year = bool(_YEAR_TOKEN.match(content, leading) or _SPECIAL_YEAR.match(content, leading))
        items: list[CitedItem] = []
        form = "parenthetical"
        author_start = author_end = None
        strong = True
        position = 0
        for index, segment in enumerate(content.split(";")):
            relative = position
            position += len(segment) + 1
            if index == 0 and starts_with_year:
                author = _narrative_author(text, group.start())
                if author is None:
                    continue
                author_start, author_end, parses, strong = author
                found = _segment_items(segment, base + relative, tuple(parses))
                if not found:
                    continue
                form = "narrative"
                if len(found) > 1 or found[0].suffix:
                    strong = True
                items.extend(found)
            else:
                items.extend(_segment_items(segment, base + relative))
        if items:
            markers.append(Marker(group.start(), group.end(), group.group(), form, tuple(items),
                                  author_start, author_end, strong))
    return markers


@dataclass(frozen=True)
class ParsedReference:
    entry_id: str
    ordinal: int
    names: tuple[str, ...]
    corporate: bool
    aliases: tuple[str, ...]
    et_al: bool
    year: str | None
    suffix: str
    special: str | None
    confidence: str
    raw: str

    @property
    def author_count(self) -> int:
        return len(self.names) + (2 if self.et_al else 0)


_LABEL = re.compile(r"^\s*(?:\[\d+[a-z]?\]|\(\d+\)|\d+[a-z]?[.)]?)\s+(?=\S)")
_YEAR_PAREN = re.compile(rf"\((?P<year>{_YEAR})(?P<suffix>[a-z]{{0,2}})(?:\s*[,/]\s*[^()]{{0,40}})?\)")
_SPECIAL_PAREN = re.compile(rf"\((?P<special>(?i:{_SPECIAL}))\)")
_ADJACENT_YEAR = re.compile(rf"[.,]\s+(?P<year>{_YEAR})(?P<suffix>[a-z]{{0,2}})(?=[.,:;]\s|[.,]?$)")
_VANCOUVER_YEAR = re.compile(rf"(?<![\w/.\-])(?P<year>{_YEAR})(?P<suffix>[a-z]?)\s*(?:[A-Z][a-z]{{2}}\s*\d{{0,2}}\s*)?;\s*\d")
_ANY_YEAR = re.compile(rf"(?<![\w/.\-])(?P<year>{_YEAR})(?P<suffix>[a-z]?)(?![\w])")
_UNIT_SEPARATOR = re.compile(r"\s*(?:,\s*(?:&|and)\s+|,\s*|\s+&\s+|\s+and\s+|\s*(?:\.\.\.|…)\s*)")
_ET_AL_HEAD = re.compile(r"\s*,?\s*(?:et\s*al\b\.?|and\s+others\b)", re.IGNORECASE)
_INVERTED_UNIT = re.compile(rf"\s*(?P<surname>{_SURNAME}),\s*(?P<initials>{_INITIALS})")
_COMPACT_UNIT = re.compile(rf"\s*(?P<surname>{_SURNAME})\s+(?P<initials>[A-Z]{{1,4}})(?![\w])\.?")
_SURNAME_WORDS = rf"(?:{_PARTICLE_PATTERN}\s+){{0,3}}{_NAMEWORD_TAIL}(?:\s+{_NAMEWORD_TAIL}){{0,2}}"
_NATURAL_INITIALS_UNIT = re.compile(rf"\s*(?:{_INITIAL}\s*-?\s*){{1,3}}(?P<surname>{_SURNAME_WORDS})")
_GIVEN = rf"(?:{_LETTER}{{2,}}|{_LETTER}\.)(?:\s+(?:{_LETTER}{{2,}}|{_LETTER}\.)){{0,2}}"
_INVERTED_FULL_UNIT = re.compile(rf"\s*(?P<surname>{_SURNAME}),\s*(?P<given>{_GIVEN})(?=,|\s+&\s|\s+and\s|\s*\(|\.\s|$)")
_CHICAGO_FIRST = re.compile(rf"\s*(?P<surname>{_SURNAME}),\s*(?P<given>{_GIVEN})(?=,|\s+and\s|\.\s|$)")
# Natural order ("Bob B. Jones", "Ana van der Berg"): the surname is the last word with its particles.
_NATURAL_FULL_UNIT = re.compile(
    rf"\s*{_LETTER}{{2,}}(?:\s+(?:{_LETTER}{{2,}}|{_LETTER}\.))*?\s+"
    rf"(?P<surname>(?:{_PARTICLE_PATTERN}\s+){{0,3}}{_NAMEWORD_TAIL})(?=,|\s+and\s|\.|\s*$)")


def _consume(text: str, unit: re.Pattern[str]) -> tuple[list[str], int, bool]:
    names: list[str] = []
    cursor = 0
    while True:
        match = unit.match(text, cursor)
        if not match or not _valid_name_unit(match.group("surname").strip()):
            break
        names.append(match.group("surname").strip())
        cursor = match.end()
        separator = _UNIT_SEPARATOR.match(text, cursor)
        if not separator or not separator.group().strip(" ") or not unit.match(text, separator.end()):
            break
        cursor = separator.end()
    return names, cursor, bool(_ET_AL_HEAD.match(text, cursor))


def _authors(text: str) -> tuple[list[str], bool, str]:
    """Surnames of the printed author list at the start of text, in the best-fitting format."""

    candidates: list[tuple[int, int, list[str], bool, str]] = []
    for rank, (kind, pattern) in enumerate((("inverted", _INVERTED_UNIT), ("compact", _COMPACT_UNIT),
                                            ("natural-initials", _NATURAL_INITIALS_UNIT),
                                            ("inverted-full", _INVERTED_FULL_UNIT))):
        names, _, et_al = _consume(text, pattern)
        if names:
            candidates.append((len(names), -rank, names, et_al, kind))
    chicago = _CHICAGO_FIRST.match(text)
    if (chicago and _valid_name_unit(chicago.group("surname").strip())
            and not re.fullmatch(_INITIALS, chicago.group("given").strip())):
        names = [chicago.group("surname").strip()]
        cursor = chicago.end()
        while True:
            separator = _UNIT_SEPARATOR.match(text, cursor)
            if not separator or not separator.group().strip(" "):
                break
            unit = _NATURAL_FULL_UNIT.match(text, separator.end()) or _NATURAL_INITIALS_UNIT.match(text, separator.end())
            if not unit or not _valid_name_unit(unit.group("surname").strip()):
                break
            names.append(unit.group("surname").strip())
            cursor = unit.end()
        candidates.append((len(names), -3, names, bool(_ET_AL_HEAD.match(text, cursor)), "inverted-first"))
    if not candidates:
        return [], False, ""
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, _, names, et_al, kind = candidates[0]
    return [name[:-1] if name.endswith(".") and not name.endswith("St.") else name for name in names], et_al, kind


def _corporate(zone: str) -> tuple[str, tuple[str, ...]] | None:
    text = zone.strip().rstrip(".,;:").strip()
    aliases = [match.group(1) for match in re.finditer(r"[\(\[]([A-Z][A-Za-z&\-]{1,15})[\)\]]", text)]
    text = re.sub(r"\s*[\(\[][^)\]]{1,20}[\)\]]\s*", " ", text).strip().rstrip(".,;:").strip()
    if not text or len(text) > 160 or re.search(r"\d", text) or not _valid_name_unit(text, allow_connectors=True):
        return None
    words = [word for word in re.split(r"[\s\-]+", text) if word]
    capitals = [word for word in words if word[:1].isupper() and word.casefold() not in CORPORATE_CONNECTORS]
    if len(capitals) >= 2:
        acronym = "".join(word[0] for word in capitals)
        if 2 <= len(acronym) <= 10:
            aliases.append(acronym)
    return text, tuple(dict.fromkeys(aliases))


def parse_reference(raw: str, *, entry_id: str = "", ordinal: int = 0) -> ParsedReference:
    """First-author surnames, author count and year of one printed reference."""

    text = _LABEL.sub("", clean_text(raw), count=1)
    year = suffix = special = None
    position = None
    match = _YEAR_PAREN.search(text)
    special_match = _SPECIAL_PAREN.search(text)
    if special_match and (match is None or special_match.start() < match.start()):
        special = re.sub(r"\s+", " ", special_match.group("special").casefold())
        position = special_match.start()
    elif match is not None:
        year, suffix, position = match.group("year"), match.group("suffix"), match.start()
    else:
        for pattern in (_ADJACENT_YEAR, _VANCOUVER_YEAR, None):
            if pattern is None:
                found = list(_ANY_YEAR.finditer(text))
                match = found[-1] if found else None
            else:
                match = pattern.search(text)
            if match:
                year, suffix, position = match.group("year"), match.group("suffix"), match.start()
                break
    names, et_al, kind = _authors(text)
    corporate = False
    aliases: tuple[str, ...] = ()
    if not names or (len(names) == 1 and kind == "compact" and position is not None
                     and not re.search(_INITIALS, text[:position].replace(names[0], "", 1).strip(" .,()"))):
        zone = text[:position] if position else re.split(r"\.\s", text, maxsplit=1)[0]
        parsed = _corporate(zone)
        if parsed:
            names, aliases, corporate = [parsed[0]], parsed[1], True
    confidence = "high" if names and (year or special) and not corporate else "medium" if names else "low"
    return ParsedReference(entry_id, ordinal, tuple(names), corporate, aliases, et_al, year,
                           suffix or "", special, confidence, raw)


@dataclass(frozen=True)
class Resolution:
    status: str
    reference_id: str | None = None
    candidates: tuple[str, ...] = ()
    method: str = ""
    names: tuple[str, ...] = ()
    et_al: bool = False
    near: tuple[Mapping[str, Any], ...] = ()


def _same_name(cited: str, printed: str) -> str | None:
    if not cited or not printed:
        return None
    if name_key(cited) == name_key(printed):
        return "surname"
    cited_core, printed_core = core_key(cited), core_key(printed)
    if cited_core and cited_core == printed_core and (cited_core != name_key(cited) or printed_core != name_key(printed)):
        return "surname-particle"
    return None


class ReferenceIndex:
    """A parsed reference list with author-year lookup."""

    def __init__(self, entries: Iterable[Mapping[str, Any]]):
        self.references: list[ParsedReference] = []
        for ordinal, entry in enumerate(entries, 1):
            self.references.append(parse_reference(str(entry.get("raw") or ""),
                                                   entry_id=str(entry.get("id") or ordinal), ordinal=ordinal))
        # Same first author and year without printed suffixes: list order implies a, b, c.
        self._inferred: dict[str, str] = {}
        groups: dict[tuple[str, str], list[ParsedReference]] = {}
        for reference in self.references:
            if reference.names and reference.year:
                groups.setdefault((name_key(reference.names[0]), reference.year), []).append(reference)
        for members in groups.values():
            if 1 < len(members) <= 26 and not any(member.suffix for member in members):
                for index, member in enumerate(members):
                    self._inferred[member.entry_id] = "abcdefghijklmnopqrstuvwxyz"[index]
        # First-author keys narrow each lookup to the entries that can match at all.
        self._by_key: dict[str, list[ParsedReference]] = {}
        for reference in self.references:
            if not reference.names:
                continue
            first = reference.names[0]
            keys = {name_key(first), core_key(first)}
            if reference.corporate:
                keys |= {corporate_key(first)} | {name_key(alias) for alias in reference.aliases}
            for key in keys - {""}:
                self._by_key.setdefault(key, []).append(reference)

    def _possible(self, cited: str) -> list[ParsedReference]:
        found: dict[int, ParsedReference] = {}
        for key in {name_key(cited), core_key(cited), corporate_key(cited)} - {""}:
            for reference in self._by_key.get(key, []):
                found[reference.ordinal] = reference
        return [found[ordinal] for ordinal in sorted(found)]

    @staticmethod
    def _first_author(parse: AuthorParse, reference: ParsedReference) -> str | None:
        if not reference.names or not parse.names:
            return None
        cited = parse.names[0]
        if reference.corporate:
            if corporate_key(cited) == corporate_key(reference.names[0]):
                return "corporate"
            if any(name_key(cited) == name_key(alias) for alias in reference.aliases):
                return "corporate-alias"
            return None
        if parse.corporate:
            return None
        return _same_name(cited, reference.names[0])

    def _candidates(self, parse: AuthorParse, item: CitedItem) -> tuple[list[ParsedReference], str]:
        matched: list[tuple[ParsedReference, str]] = []
        for reference in self._possible(parse.names[0]) if parse.names else []:
            how = self._first_author(parse, reference)
            if not how:
                continue
            if item.year is not None:
                if reference.year != item.year:
                    continue
            elif not (item.special and reference.special and name_key(item.special) == name_key(reference.special)):
                continue
            matched.append((reference, how))
        if not matched:
            return [], ""
        method = matched[0][1]
        if item.suffix:
            printed = [pair for pair in matched if pair[0].suffix == item.suffix]
            inferred = [pair for pair in matched if not pair[0].suffix
                        and self._inferred.get(pair[0].entry_id) == item.suffix]
            if printed:
                matched, method = printed, printed[0][1] + "+suffix"
            elif inferred:
                matched, method = inferred, inferred[0][1] + "+suffix-by-order"
            elif len(matched) == 1 and not matched[0][0].suffix:
                method = matched[0][1] + "+suffix-unprinted"
            else:
                return [], ""
        else:
            unsuffixed = [pair for pair in matched if not pair[0].suffix]
            if unsuffixed and len(unsuffixed) < len(matched):
                matched = unsuffixed
        references = [reference for reference, _ in matched]
        if len(parse.names) >= 2:
            # Printed co-authors must match in order; they narrow, never widen.
            references = [reference for reference in references if len(reference.names) >= len(parse.names)
                          and all(_same_name(cited, printed)
                                  for cited, printed in zip(parse.names[1:], reference.names[1:]))]
            if not references:
                return [], ""
            method += "+coauthors"
            if not parse.et_al:
                exact = [reference for reference in references
                         if len(reference.names) == len(parse.names) and not reference.et_al]
                if exact:
                    references = exact
        elif len(references) > 1:
            if parse.et_al:
                preferred = ([reference for reference in references if reference.author_count >= 3]
                             or [reference for reference in references if reference.author_count >= 2])
            else:
                preferred = [reference for reference in references if reference.author_count == 1]
            if preferred:
                references, method = preferred, method + "+author-count"
        return references, method

    def _near(self, parse: AuthorParse, item: CitedItem) -> list[dict[str, Any]]:
        hints: list[dict[str, Any]] = []
        cited = name_key(parse.names[0]) if parse.names else ""
        if not cited:
            return hints
        for reference in self.references:
            if not reference.names:
                continue
            same = bool(self._first_author(parse, reference))
            if same and item.year and reference.year and reference.year != item.year \
                    and abs(int(reference.year) - int(item.year)) <= 1:
                hints.append({"referenceId": reference.entry_id, "hint": "year-differs", "printedYear": reference.year})
            elif not same and item.year and reference.year == item.year and len(cited) >= 4 and not reference.corporate:
                ratio = SequenceMatcher(None, cited, name_key(reference.names[0])).ratio()
                if ratio >= 0.85:
                    hints.append({"referenceId": reference.entry_id, "hint": "surname-variant",
                                  "printedSurname": reference.names[0], "similarity": round(ratio, 3)})
        return hints

    def resolve(self, item: CitedItem) -> Resolution:
        parses = item.alternatives or (AuthorParse(item.names, item.et_al, False),)
        ambiguous: Resolution | None = None
        for parse in parses:
            references, method = self._candidates(parse, item)
            if len(references) == 1:
                return Resolution("resolved", references[0].entry_id, (references[0].entry_id,), method,
                                  parse.names, parse.et_al)
            if len(references) > 1 and ambiguous is None:
                ambiguous = Resolution("ambiguous", None, tuple(reference.entry_id for reference in references),
                                       method, parse.names, parse.et_al)
        if ambiguous is not None:
            return ambiguous
        persons = [parse for parse in parses if not parse.corporate] or list(parses)
        shortest = min(persons, key=lambda parse: (len(parse.names), sum(len(name.split()) for name in parse.names)))
        near: list[dict[str, Any]] = []
        for parse in parses:
            for hint in self._near(parse, item):
                if hint not in near:
                    near.append(hint)
        return Resolution("unresolved", None, (), "", shortest.names, shortest.et_al, tuple(near[:5]))


def marker_record(marker: Marker, resolutions: Sequence[Resolution]) -> dict[str, Any]:
    """JSON-safe description of a marker and the resolution of each cited item."""

    items = []
    for item, resolution in zip(marker.items, resolutions):
        names = list(resolution.names or item.names)
        record: dict[str, Any] = {
            "text": item.text, "start": item.start, "end": item.end,
            "authors": names, "etAl": bool(resolution.et_al if resolution.names else item.et_al),
            "year": item.year, "suffix": item.suffix, "special": item.special,
            "status": resolution.status, "referenceId": resolution.reference_id,
            "candidates": list(resolution.candidates), "method": resolution.method,
        }
        if item.locator:
            record["locator"] = item.locator
        if resolution.near:
            record["nearMatches"] = [dict(hint) for hint in resolution.near]
        items.append(record)
    value: dict[str, Any] = {"form": marker.form, "resolver": RESOLVER_VERSION, "items": items}
    if marker.author_start is not None:
        value["authorStart"] = marker.author_start
        value["authorEnd"] = marker.author_end
    return value


def item_token(item: Mapping[str, Any]) -> str:
    """Printed-style token for one cited item, e.g. "Smith et al. 2020a"."""

    authors = list(item.get("authors") or [])
    text = " and ".join(authors) if len(authors) == 2 else ", ".join(authors)
    if item.get("etAl"):
        text += " et al."
    when = (item.get("year") or "") + (item.get("suffix") or "") if item.get("year") else (item.get("special") or "")
    return f"{text} {when}".strip()


__all__ = [
    "AuthorParse", "CitedItem", "Marker", "ParsedReference", "ReferenceIndex", "Resolution",
    "RESOLVER_VERSION", "clean_text", "core_key", "corporate_key", "detect", "item_token",
    "marker_record", "name_key", "parse_author_zone", "parse_reference",
]
