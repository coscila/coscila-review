# SPDX-License-Identifier: Apache-2.0
"""Manuscript and cited text are untrusted input to model prompts.

Two kinds of content are removed before any text reaches a judge prompt:

* hidden text: characters or markup that a reader of the document never sees (zero-width,
  bidirectional and other format characters, Unicode tag characters, DOCX runs hidden, white or
  tiny through direct formatting or inherited styles, DOCX fallback content, HTML or XML elements
  hidden by inline style, attribute or the document's own stylesheet, HTML comments and comment
  link definitions in Markdown);
* prompt-injection payloads: sentences that address a model rather than a reader (instruction
  overrides, role reassignment, verdict directives, chat control tokens, smuggled verdict JSON).

Every removal is recorded with its kind, offsets, length and the sha256 of the removed text,
never the text itself, so a report can show that something was removed without repeating it.
``mode="detect"`` records the same findings and leaves the text unchanged, for instruments whose
prompts must stay byte-identical to a pinned reference.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET

from .errors import ValidationError

SANITIZER_VERSION = "sanitize-2"
MODES = ("strip", "detect")

_CHARACTER_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tag-character", re.compile("[\U000e0000-\U000e007f]+")),
    ("bidi-control", re.compile("[‎‏‪-‮⁦-⁩؜]+")),
    ("zero-width", re.compile("[​-‍⁠-⁤﻿᠎]+")),
    ("variation-selector", re.compile("[︀-️\U000e0100-\U000e01ef]+")),
    ("annotation-control", re.compile("[￹-￻]+")),
    ("control-character", re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+")),
    # Every other format character (Unicode category Cf) and the blank fillers that render as nothing.
    ("format-character", re.compile("[\u034f\u0600-\u0605\u06dd\u070f\u0890\u0891\u08e2\u115f\u1160\u180b-\u180d"
                                    "\u180f\u206a-\u206f\u2800\u3164\uffa0\U000110bd\U000110cd\U00013430-\U0001343f"
                                    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a]+")),
    ("private-use", re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]+")),
)
_SOFT_HYPHEN = re.compile("­")
_COVERED = frozenset("\u00ad")
# Lookalike letters, mapped for matching only (never in the text a judge receives).
_CONFUSABLES = {**dict(zip("авекмнорстухіјѕһԁԛԝӏ", "abekmhopctyxijshdqwl")),
                **dict(zip("αβεζηικμνορτυχ", "abezniku" "voptux"))}
_NEWLINES = "\n\r\x0b\x0c\x85\u2028\u2029"

# Each rule names a pattern that addresses a model rather than a reader. Directive verbs count
# only in imperative position (sentence start, after a colon, after "please" or "you must"), so
# "participants were told to ignore the previous instructions" or "studies do not report
# limitations" stay untouched. Matching is case-insensitive and runs on text that has already
# lost its invisible and format characters, on a compatibility-normalised copy (fullwidth letters,
# accents and lookalikes) and on copies whose line breaks are spaces, so "ig​nore", "ｉｇｎｏｒｅ"
# or a keyword split across lines cannot evade it.
_LEAD = (r"(?:(?:^|(?<=[.!?:;\n\"'(\[*>]))\s*(?:(?:please|kindly|now|instead|also|and|then)\s+)?"
         r"|\b(?:please|kindly)\s+|\byou\s+(?:must|should|will|shall|are\s+to)\s+)")
_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction-override", re.compile(
        _LEAD + r"(?:ignore|disregard|forget|override|bypass)\b[^.!?\n]{0,40}?\b(?:all\s+|any\s+|the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|preceding|foregoing|system|original|existing|other)\b[^.!?\n]{0,25}?"
        r"\b(?:instructions?|prompts?|rules?|directions?|guidelines?|context|messages?|constraints?)\b", re.I | re.M)),
    ("new-instructions", re.compile(
        r"\b(?:new|updated|revised|important|hidden|secret|additional)\s+(?:system\s+)?instructions?\s*[:\-]"
        r"|\b(?:system|developer)\s+(?:prompt|message|instructions?)\s*[:\-]", re.I)),
    ("role-reassignment", re.compile(
        r"\byou\s+are\s+now\s+(?:an?|the|my)\b|\byou\s+are\s+(?:an?\s+)?(?:AI|LLM|large\s+language\s+model|"
        r"language\s+model|chatbot|AI\s+assistant)\b|\bfrom\s+now\s+on,?\s+you\b", re.I)),
    ("model-address", re.compile(
        r"\bif\s+you\s+are\s+an?\s+(?:LLM|AI|large\s+language\s+model|language\s+model|assistant|chatbot|"
        r"automated\s+(?:reviewer|judge|system))\b|\bas\s+an?\s+(?:LLM|AI|large\s+language\s+model|language\s+model)"
        r"\s*,?\s+you\b|\b(?:LLM|AI)\s+(?:reviewers?|judges?|graders?|assistants?|readers?)\s*,?\s*(?:must|should|"
        r"need\s+to|are\s+(?:required|instructed)\s+to)\b", re.I)),
    ("verdict-directive", re.compile(
        _LEAD + r"(?:(?:give|write|provide|output|return|produce|assign)\s+(?:a\s+|an\s+|only\s+)?(?:positive|"
        r"favou?rable|glowing|good|high|supportive)\s+(?:review|rating|score|assessment|verdict|evaluation|grade)s?\b"
        r"|(?:rate|mark|classify|score|label|judge|treat)\s+(?:this|the|all|every|each)\s+(?:\w+\s+){0,2}"
        r"(?:paper|manuscript|claim|citation|reference|item|submission|study|source)s?\s+as\b"
        r"|do\s+not\s+(?:mention|highlight|report|flag|criticize|criticise|point\s+out)\s+(?:any\s+)?(?:\w+\s+)?"
        r"(?:weakness(?:es)?|limitations?|negatives?|issues?|problems?|flaws?|errors?)\b)", re.I | re.M)),
    ("control-block", re.compile(r"<\|im_start\|>.*?(?:<\|im_end\|>|$)|\[INST\].*?(?:\[/INST\]|$)"
                                 r"|<<SYS>>.*?(?:<</SYS>>|$)", re.I | re.S)),
    ("control-token", re.compile(
        r"<\|(?:im_start|im_end|system|assistant|user|endoftext|eot_id|start_header_id|end_header_id)\|>"
        r"|\[/?INST\]|<</?SYS>>|</?(?:system|assistant|instructions?|prompt)\s*>"
        r"|#{2,}\s*(?:instruction|system|response|assistant)\b|\bBEGIN\s+(?:SYSTEM\s+)?PROMPT\b"
        r"|^\s*(?:system|assistant)\s*:\s", re.I | re.M)),
    ("verdict-json", re.compile(
        r"[\"'](?:supportVerdict|escalate|escalationReason|verdict)[\"']\s*:\s*|\{\s*[\"']items[\"']\s*:\s*\[", re.I)),
)
_SENTENCE_END = re.compile(r"[.!?。！？．｡]+[\"')\]]*\s+|\n\s*\n")


@dataclass(frozen=True)
class Removal:
    kind: str
    start: int
    end: int
    length: int
    sha256: str
    rule: str = ""
    scope: str = ""

    def record(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value != ""}


@dataclass(frozen=True)
class Sanitized:
    text: str
    removals: tuple[Removal, ...]
    mode: str
    version: str = SANITIZER_VERSION

    @property
    def changed(self) -> bool:
        return bool(self.removals)

    def records(self) -> list[dict[str, Any]]:
        return [removal.record() for removal in self.removals]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The sentence that contains [start, end), in text coordinates."""

    left = 0
    for match in _SENTENCE_END.finditer(text, 0, start):
        left = match.end()
    right_match = _SENTENCE_END.search(text, end)
    right = right_match.end() if right_match else len(text)
    return left, right


def _merge(spans: Iterable[tuple[int, int, str, str]]) -> list[tuple[int, int, str, str]]:
    merged: list[tuple[int, int, str, str]] = []
    for start, end, kind, rule in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous = merged[-1]
            if end > previous[1]:
                merged[-1] = (previous[0], end, previous[2], previous[3])
            continue
        merged.append((start, end, kind, rule))
    return merged


def find_injections(text: str) -> list[tuple[int, int, str]]:
    """(start, end, rule) of each injection pattern match in text."""

    hits: list[tuple[int, int, str]] = []
    for rule, pattern in _INJECTION_RULES:
        for match in pattern.finditer(text):
            hits.append((match.start(), match.end(), rule))
    return sorted(hits)


def _normalized(text: str) -> tuple[str, list[int]]:
    """A matching copy: compatibility-decomposed, without combining marks, casefolded, lookalikes mapped.

    Fullwidth or mathematical letters, accents and Cyrillic or Greek lookalikes cannot then split a keyword.
    Returns the copy and, per copy character, its position in ``text``.
    """
    out: list[str] = []
    index: list[int] = []
    for position, character in enumerate(text):
        if character.isascii():
            out.append(character)
            index.append(position)
        elif character.isspace():
            out.append("\n" if character in _NEWLINES else " ")
            index.append(position)
        else:
            decomposed = unicodedata.normalize("NFKD", character)
            for mapped in "".join(c for c in decomposed if not unicodedata.combining(c)).casefold():
                out.append(_CONFUSABLES.get(mapped, mapped))
                index.append(position)
    return "".join(out), index


def _injection_hits(visible: str) -> list[tuple[int, int, str]]:
    """Injection matches over the visible text, its normalised copy and copies whose line breaks are spaces."""

    copies: list[tuple[str, list[int] | None]] = [(visible, None) if visible.isascii() else _normalized(visible)]
    if any(newline in copies[0][0] for newline in _NEWLINES):
        # A keyword split across lines still reads as one phrase; the copy keeps every position.
        joined = copies[0][0]
        for newline in _NEWLINES:
            joined = joined.replace(newline, " ")
        copies.append((joined, copies[0][1]))
    hits = set()
    for copy, index in copies:
        for start, end, rule in find_injections(copy):
            hits.add((start, end, rule) if index is None else (index[start], index[end - 1] + 1, rule))
    return sorted(hits)


def sanitize_text(text: str, *, mode: str = "strip", scope: str = "") -> Sanitized:
    """Remove (or, in detect mode, only record) hidden characters and injection sentences.

    Offsets in the removal records refer to the input text.
    """

    if mode not in MODES:
        raise ValueError(f"unknown sanitizer mode: {mode}")
    source = str(text or "")
    spans: list[tuple[int, int, str, str]] = []
    for kind, pattern in _CHARACTER_CLASSES:
        spans.extend((match.start(), match.end(), kind, kind) for match in pattern.finditer(source))
    spans.extend((match.start(), match.end(), "soft-hyphen", "soft-hyphen") for match in _SOFT_HYPHEN.finditer(source))
    if not source.isascii():
        # Format characters a newer Unicode version adds are caught by category, not only by the list above.
        covered = {position for start, end, _, _ in spans for position in range(start, end)}
        spans.extend((position, position + 1, "format-character", "format-character") for position, character
                     in enumerate(source) if position not in covered and character not in _COVERED
                     and unicodedata.category(character) == "Cf")
    # Injection matching runs on the text without invisible characters; map offsets back. Most
    # texts have none, and then the text and its offsets are used as they are.
    if spans:
        keep = [True] * len(source)
        for start, end, _, _ in spans:
            for position in range(start, end):
                keep[position] = False
        visible_positions = [position for position, flag in enumerate(keep) if flag]
        visible = "".join(source[position] for position in visible_positions)
    else:
        visible_positions, visible = None, source
    for start, end, rule in _injection_hits(visible):
        while start < end and visible[start].isspace():
            start += 1
        if rule in {"control-token", "control-block"}:
            bounds = (start, end)
        else:
            bounds = _sentence_bounds(visible, start, end)
        if bounds[1] <= bounds[0]:
            continue
        if visible_positions is None:
            original_start, original_end = bounds
        else:
            original_start = visible_positions[bounds[0]]
            original_end = visible_positions[bounds[1] - 1] + 1
        spans.append((original_start, original_end, "prompt-injection", rule))
    removals: list[Removal] = []
    for start, end, kind, rule in _merge(spans):
        removed = source[start:end]
        removals.append(Removal(kind, start, end, len(removed), _digest(removed), rule, scope))
    if mode == "detect" or not removals:
        return Sanitized(source, tuple(removals), mode)
    pieces: list[str] = []
    cursor = 0
    for removal in removals:
        pieces.append(source[cursor:removal.start])
        if removal.kind == "prompt-injection":
            pieces.append(" ")
        cursor = removal.end
    pieces.append(source[cursor:])
    cleaned = re.sub(r"[ \t]{2,}", " ", "".join(pieces))
    cleaned = re.sub(r" +([.,;:!?])", r"\1", cleaned).strip()
    return Sanitized(cleaned, tuple(removals), mode)


def residual_findings(value: Any) -> list[dict[str, Any]]:
    """Scan every string leaf of a JSON-like value for content that should have been removed."""

    findings: list[dict[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            check = sanitize_text(node, mode="detect")
            for removal in check.removals:
                if removal.kind != "soft-hyphen":
                    findings.append({"path": path, "kind": removal.kind, "rule": removal.rule})
        elif isinstance(node, Mapping):
            for key, child in node.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(node, (list, tuple)):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(value, "")
    return findings


def assert_prompt_clean(messages: list[Mapping[str, str]]) -> None:
    """Defence in depth: the user message of a judge prompt must carry no residual payload."""

    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            value = content
        findings = residual_findings(value)
        if findings:
            raise ValueError("judge prompt carries unsanitized content: "
                             + ", ".join(sorted({f"{item['kind']}:{item['rule']}" for item in findings})))


# Document-level hidden content -------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DTD = re.compile(br"<!(?:DOCTYPE|ENTITY)\b", re.I)
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_LENGTH = re.compile(r"^(-?\d*\.?\d+)\s*(px|pt|em|rem|%|ex|ch|vw|vh|cm|mm|in|pc)?$")


def _declarations(style: str) -> dict[str, str]:
    """CSS declarations of an inline style or rule body; later declarations win, values lowercased."""
    found: dict[str, str] = {}
    for part in re.sub(r"/\*.*?\*/", " ", style or "", flags=re.S).split(";"):
        name, separator, value = part.partition(":")
        if separator and name.strip():
            found[name.strip().lower()] = re.sub(r"\s*!important\s*$", "", value.strip().lower())
    return found


def _length(value: str) -> tuple[float, str] | None:
    match = _LENGTH.match((value or "").strip())
    return (float(match.group(1)), match.group(2) or "") if match else None


def _whiteish(value: str) -> bool:
    value = (value or "").strip()
    if value in ("white", "#fff", "#ffffff"):
        return True
    match = re.fullmatch(r"#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})", value)
    if match:
        return all(int(channel, 16) >= 0xf0 for channel in match.groups())
    match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+%?)\s*)?\)", value)
    return bool(match) and all(int(channel) >= 240 for channel in match.groups()[:3])


def _transparent(value: str) -> bool:
    value = (value or "").strip()
    if value == "transparent":
        return True
    match = re.fullmatch(r"(?:rgba|hsla)\(.*,\s*([\d.]+)(%?)\s*\)", value)
    return bool(match) and float(match.group(1)) / (100 if match.group(2) else 1) < 0.05


def _hiding(declarations: dict[str, str]) -> str | None:
    """Why a set of CSS declarations hides its element's text from a reader, or None."""
    d = declarations
    if d.get("display") == "none" or d.get("visibility") in ("hidden", "collapse"):
        return "display"
    opacity = _length(d.get("opacity", ""))
    if opacity is not None and opacity[0] < 0.05:
        return "opacity"
    size = _length(d.get("font-size", ""))
    if size is not None and size[0] < {"px": 2, "pt": 1.5, "em": 0.15, "rem": 0.15, "%": 15, "": 2}.get(size[1], 0.1):
        return "font-size"
    color = d.get("color", "")
    background = d.get("background-color", d.get("background", ""))
    if _transparent(color) or (_whiteish(color) and (not background or _whiteish(background) or _transparent(background))):
        return "color"
    if d.get("position") in ("absolute", "fixed"):
        for side in ("left", "top", "right", "bottom"):
            offset = _length(d.get(side, ""))
            if offset is not None and offset[0] <= -999:
                return "off-screen"
    indent = _length(d.get("text-indent", ""))
    if indent is not None and indent[0] <= -999:
        return "off-screen"
    clip = d.get("clip", "").replace(" ", "")
    if clip.startswith("rect(0") or clip in ("rect(1px,1px,1px,1px)", "rect(0,0,0,0)"):
        return "clip"
    clip_path = d.get("clip-path", "").replace(" ", "")
    if re.fullmatch(r"inset\((?:[5-9]\d|100)%.*\)|circle\(0.*\)", clip_path):
        return "clip"
    if re.search(r"\bscale[xy]?\(\s*0(?:\.0+)?\s*[,)]", d.get("transform", "")):
        return "transform"
    if d.get("overflow", d.get("overflow-x", d.get("overflow-y", ""))) in ("hidden", "clip"):
        for name in ("height", "max-height", "width", "max-width"):
            box = _length(d.get(name, ""))
            if box is not None and box[0] <= 1 and box[1] in ("", "px"):
                return "zero-box"
    return None


def _style_hidden(style: str) -> bool:
    return _hiding(_declarations(style)) is not None


_COMPOUND = re.compile(r"(?P<tag>\*|[a-zA-Z][\w-]*)?(?P<rest>(?:[.#][\w-]+|\[[\w-]+(?:[~|^$*]?=(?:\"[^\"]*\"|'[^']*'|[^\]]*))?\])*)")
_SIMPLE = re.compile(r"([.#])([\w-]+)|\[([\w-]+)(?:([~|^$*]?=)(?:\"([^\"]*)\"|'([^']*)'|([^\]]*)))?\]")


def _parse_selector(selector: str) -> list[tuple[str, tuple]] | None:
    """[(combinator, (tag, ids, classes, attributes)), ...] for descendant/child chains of simple selectors."""
    tokens = re.split(r"\s*(>)\s*|\s+", selector.strip())
    chain, combinator = [], " "
    for token in tokens:
        if token is None or token == "":
            continue
        if token == ">":
            combinator = ">"
            continue
        match = _COMPOUND.fullmatch(token)
        if not match:
            return None             # pseudo-classes, siblings and other selectors are not evaluated
        tag = (match.group("tag") or "*").lower()
        ids, classes, attributes = [], [], []
        for simple in _SIMPLE.finditer(match.group("rest") or ""):
            if simple.group(1) == "#":
                ids.append(simple.group(2))
            elif simple.group(1) == ".":
                classes.append(simple.group(2))
            else:
                value = next((v for v in simple.group(5, 6, 7) if v is not None), None)
                attributes.append((simple.group(3).lower(), simple.group(4), value))
        chain.append((combinator, (tag, tuple(ids), tuple(classes), tuple(attributes))))
        combinator = " "
    return chain or None


def _compound_matches(compound: tuple, tag: str, attributes: dict[str, str]) -> bool:
    name, ids, classes, required = compound
    if name != "*" and name != tag:
        return False
    if any(attributes.get("id") != identifier for identifier in ids):
        return False
    present = set(attributes.get("class", "").split())
    if any(item not in present for item in classes):
        return False
    for attribute, operator, value in required:
        actual = attributes.get(attribute)
        if actual is None or (operator == "=" and actual != value) or (operator == "~=" and value not in actual.split()) \
                or (operator == "^=" and not actual.startswith(value or "")) \
                or (operator == "$=" and not actual.endswith(value or "")) or (operator == "*=" and (value or "") not in actual):
            return False
    return True


def _chain_matches(chain: list, stack: list[tuple[str, dict[str, str]]], tag: str, attributes: dict[str, str]) -> bool:
    if not _compound_matches(chain[-1][1], tag, attributes):
        return False

    def ancestors_match(position: int, depth: int) -> bool:
        if position < 0:
            return True
        combinator = chain[position + 1][0]
        compound = chain[position][1]
        if combinator == ">":
            return depth >= 0 and _compound_matches(compound, *stack[depth]) and ancestors_match(position - 1, depth - 1)
        return any(_compound_matches(compound, *stack[level]) and ancestors_match(position - 1, level - 1)
                   for level in range(depth, -1, -1))
    return ancestors_match(len(chain) - 2, len(stack) - 1)


def _stylesheet(text: str) -> tuple[list[tuple[list, str]], list[str]]:
    """Hiding rules of the document's own <style> blocks and the selectors that cannot be evaluated."""
    rules, unevaluated = [], []
    for block in re.findall(r"<style\b[^>]*>(.*?)</style\s*>", text, flags=re.S | re.I):
        css = re.sub(r"/\*.*?\*/", " ", block, flags=re.S)
        css = re.sub(r"@(?:media|supports)[^{]*\{", " ", css)          # evaluate conditional groups unconditionally
        for selectors, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
            reason = _hiding(_declarations(body))
            if reason is None:
                continue
            for selector in selectors.split(","):
                chain = _parse_selector(selector)
                if chain is None:
                    unevaluated.append(selector.strip()[:80])
                else:
                    rules.append((chain, reason))
    return rules, unevaluated


def _document_removal(kind: str, text: str, rule: str, position: int) -> dict[str, Any]:
    return {"kind": kind, "rule": rule, "position": position, "length": len(text), "sha256": _digest(text),
            "scope": "manuscript-document"}


class _DocxStyles:
    """Run properties of a WordprocessingML document with style inheritance (basedOn) and document defaults."""

    PROPERTIES = ("vanish", "specVanish", "webHidden", "color", "sz", "shd", "highlight")

    def __init__(self, archive: zipfile.ZipFile):
        self.styles: dict[str, tuple[str, str | None, ET.Element | None]] = {}
        self.defaults: ET.Element | None = None
        self.default_paragraph: str | None = None
        try:
            data = archive.read("word/styles.xml")
        except KeyError:
            return
        if _DTD.search(data):
            raise ValidationError("DOCX word/styles.xml declares a DTD or entities; the document is refused")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise ValidationError("DOCX word/styles.xml is malformed; its hidden-text rules cannot be evaluated") from error
        self.defaults = root.find(f"{{{_W}}}docDefaults/{{{_W}}}rPrDefault/{{{_W}}}rPr")
        for style in root.iter(f"{{{_W}}}style"):
            identifier = style.get(f"{{{_W}}}styleId", "")
            based = style.find(f"{{{_W}}}basedOn")
            self.styles[identifier] = (style.get(f"{{{_W}}}type", ""), based.get(f"{{{_W}}}val") if based is not None else None,
                                       style.find(f"{{{_W}}}rPr"))
            if style.get(f"{{{_W}}}type") == "paragraph" and style.get(f"{{{_W}}}default") in ("1", "true", "on"):
                self.default_paragraph = identifier
            # Conditional table-style formatting can hide text in a way this check does not resolve.
            for conditional in style.iter(f"{{{_W}}}tblStylePr"):
                if conditional.find(f".//{{{_W}}}vanish") is not None:
                    raise ValidationError("DOCX table style hides text conditionally; the hidden-text check cannot "
                                          "evaluate it, so the document is refused")

    def chain(self, identifier: str | None) -> list[ET.Element]:
        found, seen = [], set()
        while identifier and identifier not in seen and identifier in self.styles:
            seen.add(identifier)
            _, based, properties = self.styles[identifier]
            if properties is not None:
                found.append(properties)
            identifier = based
        return found

    def effective(self, run: ET.Element, paragraph: ET.Element | None) -> dict[str, tuple[ET.Element, str]]:
        """Each property's element and where it came from: direct, character style, paragraph style or defaults."""
        direct = run.find(f"{{{_W}}}rPr")
        character = direct.find(f"{{{_W}}}rStyle").get(f"{{{_W}}}val") if direct is not None and \
            direct.find(f"{{{_W}}}rStyle") is not None else None
        paragraph_style = None
        if paragraph is not None:
            node = paragraph.find(f"{{{_W}}}pPr/{{{_W}}}pStyle")
            paragraph_style = node.get(f"{{{_W}}}val") if node is not None else self.default_paragraph
        layers = ([(direct, "direct")] if direct is not None else []) \
            + [(properties, "style") for properties in self.chain(character)] \
            + [(properties, "paragraph-style") for properties in self.chain(paragraph_style)] \
            + ([(self.defaults, "default-style")] if self.defaults is not None else [])
        found: dict[str, tuple[ET.Element, str]] = {}
        for properties, origin in layers:
            for name in self.PROPERTIES:
                if name not in found:
                    node = properties.find(f"{{{_W}}}{name}")
                    if node is not None:
                        found[name] = (node, origin)
        return found

    def hidden_reason(self, run: ET.Element, paragraph: ET.Element | None) -> str | None:
        found = self.effective(run, paragraph)
        suffix = {"direct": "", "style": "hidden-style", "paragraph-style": "hidden-paragraph-style",
                  "default-style": "hidden-default-style"}
        for name in ("vanish", "specVanish", "webHidden"):
            if name in found and found[name][0].get(f"{{{_W}}}val", "true").lower() not in {"0", "false", "off"}:
                return name if found[name][1] == "direct" else suffix[found[name][1]]
        if "color" in found:
            node = found["color"][0]
            value = node.get(f"{{{_W}}}val", "").upper()
            theme = node.get(f"{{{_W}}}themeColor", "").lower()
            white = theme in ("background1", "bg1", "light1") or (
                re.fullmatch(r"[0-9A-F]{6}", value) is not None and all(int(value[i:i + 2], 16) >= 0xf0 for i in (0, 2, 4)))
            highlight = found.get("highlight", (None, ""))[0]
            shading = found.get("shd", (None, ""))[0]
            fill = (shading.get(f"{{{_W}}}fill", "") if shading is not None else "").upper()
            if white and (highlight is None or highlight.get(f"{{{_W}}}val", "none") == "none") \
                    and fill in {"", "AUTO", "FFFFFF"}:
                return "white-text"
        if "sz" in found:
            try:
                if int(found["sz"][0].get(f"{{{_W}}}val", "24")) <= 4:
                    return "tiny-text"
            except ValueError:
                pass
        return None


def _sanitize_docx(data: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    removals: list[dict[str, Any]] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        document = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        return data, removals
    if _DTD.search(document):
        # Office Open XML never needs a DTD; an entity could hide what the hidden-text check would see.
        raise ValidationError("DOCX word/document.xml declares a DTD or entities; the document is refused")
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return data, removals
    styles = _DocxStyles(archive)
    parents = {child: parent for parent in root.iter() for child in parent}
    position = 0
    # Word renders the mc:Choice branch of alternate content; the fallback is never shown to a reader.
    for alternate in list(root.iter(f"{{{_MC}}}AlternateContent")):
        for fallback in list(alternate.findall(f"{{{_MC}}}Fallback")):
            text = "".join(node.text or "" for node in fallback.iter(f"{{{_W}}}t"))
            if text.strip():
                removals.append(_document_removal("hidden-markup", text, "docx-mc-fallback", position))
            alternate.remove(fallback)
            position += 1
    parents = {child: parent for parent in root.iter() for child in parent}
    for run in list(root.iter(f"{{{_W}}}r")):
        paragraph = parents.get(run)
        while paragraph is not None and paragraph.tag != f"{{{_W}}}p":
            paragraph = parents.get(paragraph)
        reason = styles.hidden_reason(run, paragraph)
        text = "".join(node.text or "" for node in run.iter(f"{{{_W}}}t"))
        if reason and text:
            removals.append(_document_removal("hidden-markup", text, f"docx-{reason}", position))
            parents[run].remove(run)
        position += 1
    if not removals:
        return data, removals
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9]+)="([^"]+)"', document):
        try:
            ET.register_namespace(prefix.decode(), uri.decode())
        except ValueError:
            continue
    rewritten = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in archive.infolist():
            target.writestr(info, rewritten if info.filename == "word/document.xml" else archive.read(info))
    return output.getvalue(), removals


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIPPED_BY_READERS = {"script", "style", "noscript"}


class _HiddenFilter(HTMLParser):
    """Re-emit HTML without hidden subtrees and comments; record what was dropped.

    Hiding is read from inline styles, the hidden and aria-hidden attributes, templates and the document's
    own stylesheet rules (descendant and child chains of tag, class, id and attribute selectors).
    """

    def __init__(self, rules: list[tuple[list, str]] = ()) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.removals: list[dict[str, Any]] = []
        self.hidden_depth = 0
        self.hidden_tag: list[str] = []
        self.hidden_text: list[str] = []
        self.hidden_rule = ""
        self.position = 0
        self.rules = list(rules)
        self.stack: list[tuple[str, dict[str, str]]] = []

    def _hidden(self, tag: str, attributes: list[tuple[str, str | None]]) -> str | None:
        values = {key.lower(): (value or "") for key, value in attributes}
        if tag in _SKIPPED_BY_READERS:
            return None  # every extractor skips these already; not a finding
        if tag == "template":
            return "html-template"
        if "hidden" in values:
            return "html-hidden-attribute"
        if values.get("aria-hidden", "").lower() == "true" and tag not in {"svg", "i", "img"}:
            return "html-aria-hidden"
        if _style_hidden(values.get("style", "")):
            return "html-hidden-style"
        if any(_chain_matches(chain, self.stack, tag, values) for chain, _ in self.rules):
            return "html-hidden-stylesheet"
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden_depth:
            if tag not in _VOID:
                self.hidden_depth += 1
            return
        rule = self._hidden(tag, attrs)
        if rule and tag not in _VOID:
            self.hidden_depth, self.hidden_rule, self.hidden_text = 1, rule, []
            return
        if tag not in _VOID:
            self.stack.append((tag, {key.lower(): (value or "") for key, value in attrs}))
        self.output.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.hidden_depth:
            self.output.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_depth:
            self.hidden_depth -= 1
            if not self.hidden_depth:
                text = "".join(self.hidden_text)
                if text.strip():
                    self.removals.append(_document_removal("hidden-markup", text, self.hidden_rule, self.position))
                self.position += 1
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            self.hidden_text.append(data)
        else:
            self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        (self.hidden_text if self.hidden_depth else self.output).append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        (self.hidden_text if self.hidden_depth else self.output).append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if data.strip():
            self.removals.append(_document_removal("hidden-markup", data, "html-comment", self.position))
        self.position += 1

    def handle_decl(self, decl: str) -> None:
        if not self.hidden_depth:
            self.output.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        return


def _sanitize_html(data: bytes) -> tuple[bytes, list[dict[str, Any]], list[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, [], []
    rules, unevaluated = _stylesheet(text)
    if unevaluated:
        raise ValidationError("the document's stylesheet hides content with selectors the hidden-text check cannot "
                              "evaluate (" + "; ".join(sorted(set(unevaluated))[:5]) + "); the document is refused")
    limitations = []
    if re.search(r"<link\b[^>]*\brel\s*=\s*[\"']?stylesheet", text, re.I):
        limitations.append("html-external-stylesheet: stylesheets outside the document are not read, so text they "
                           "hide is not detected; inline styles and the document's own <style> rules are checked")
    parser = _HiddenFilter(rules)
    parser.feed(text)
    parser.close()
    if not parser.removals:
        return data, [], limitations
    return "".join(parser.output).encode("utf-8"), parser.removals, limitations


def _sanitize_xml(data: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    if re.search(br"<!ENTITY\b", data, re.I):
        return data, []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data, []
    removals: list[dict[str, Any]] = []
    parents = {child: parent for parent in root.iter() for child in parent}
    position = 0
    for node in list(root.iter()):
        style = node.get("style", "")
        specific = (node.get("specific-use", "") + " " + node.get("content-type", "")).lower()
        if _style_hidden(style) or re.search(r"\bhidden\b", specific) or node.get("hidden") is not None:
            text = "".join(node.itertext())
            parent = parents.get(node)
            if parent is not None and text.strip():
                removals.append(_document_removal("hidden-markup", text, "xml-hidden-element", position))
                tail = node.tail or ""
                index = list(parent).index(node)
                parent.remove(node)
                if tail:
                    if index:
                        previous = parent[index - 1]
                        previous.tail = (previous.tail or "") + tail
                    else:
                        parent.text = (parent.text or "") + tail
        position += 1
    if not removals:
        return data, []
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9]+)="([^"]+)"', data):
        try:
            ET.register_namespace(prefix.decode(), uri.decode())
        except ValueError:
            continue
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removals


_MARKDOWN_COMMENT = re.compile(r"<!--.*?-->", re.S)
_MARKDOWN_HIDDEN_SPAN = re.compile(
    r"<(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)\s*>", re.S | re.I)
# A link reference definition is never rendered; "[//]: # (text)" and "[comment]: <> (text)" are comment idioms.
_MARKDOWN_REFERENCE_COMMENT = re.compile(
    r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(?:#|<>|<#>)[ \t]*(?:\([^\n]*\)|\"[^\n]*\"|'[^\n]*')[ \t]*$")


def _sanitize_markdown(data: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data, []
    removals: list[dict[str, Any]] = []

    def drop_comment(match: re.Match[str]) -> str:
        removals.append(_document_removal("hidden-markup", match.group(), "markdown-html-comment", match.start()))
        return ""

    def drop_hidden(match: re.Match[str]) -> str:
        attributes = match.group("attrs")
        style = re.search(r"style\s*=\s*[\"']([^\"']*)[\"']", attributes, re.I)
        if (style and _style_hidden(style.group(1))) or re.search(r"(?:^|\s)hidden(?:\s|=|$)", attributes, re.I) \
                or re.search(r"aria-hidden\s*=\s*[\"']?true", attributes, re.I) or match.group("tag").lower() == "template":
            removals.append(_document_removal("hidden-markup", match.group("body"), "markdown-hidden-html", match.start()))
            return ""
        return match.group()

    def drop_reference(match: re.Match[str]) -> str:
        removals.append(_document_removal("hidden-markup", match.group(), "markdown-reference-comment", match.start()))
        return ""

    cleaned = _MARKDOWN_COMMENT.sub(drop_comment, text)
    cleaned = _MARKDOWN_HIDDEN_SPAN.sub(drop_hidden, cleaned)
    cleaned = _MARKDOWN_REFERENCE_COMMENT.sub(drop_reference, cleaned)
    characters = sanitize_text(cleaned, mode="detect")
    invisible = [removal for removal in characters.removals if removal.kind != "prompt-injection"]
    if invisible:
        pieces, cursor = [], 0
        for removal in invisible:
            pieces.append(cleaned[cursor:removal.start])
            cursor = removal.end
            removals.append({"kind": removal.kind, "rule": removal.rule, "position": removal.start,
                             "length": removal.length, "sha256": removal.sha256, "scope": "manuscript-document"})
        pieces.append(cleaned[cursor:])
        cleaned = "".join(pieces)
    if not removals:
        return data, []
    return cleaned.encode("utf-8"), removals


def sanitize_document(data: bytes, suffix: str) -> tuple[bytes, list[dict[str, Any]], list[str]]:
    """Remove hidden document content before ingestion.

    Returns the bytes to ingest (the original bytes when nothing was hidden), the removal
    records and the known limitations for this format.
    """

    suffix = suffix.lower()
    limitations: list[str] = []
    if suffix == ".docx":
        cleaned, removals = _sanitize_docx(data)
    elif suffix in {".html", ".htm"}:
        cleaned, removals, limitations = _sanitize_html(data)
    elif suffix in {".xml", ".jats"}:
        cleaned, removals = _sanitize_xml(data)
    elif suffix in {".md", ".markdown", ".txt"}:
        cleaned, removals = _sanitize_markdown(data)
    else:
        cleaned, removals = data, []
        if suffix == ".pdf":
            limitations.append("pdf-hidden-text: white, tiny or render-mode-3 text in a PDF is not detectable "
                               "from extracted text; only character-level and injection checks apply")
    return cleaned, removals, limitations


__all__ = [
    "MODES", "Removal", "SANITIZER_VERSION", "Sanitized", "assert_prompt_clean", "find_injections",
    "residual_findings", "sanitize_document", "sanitize_text",
]
