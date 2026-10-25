# SPDX-License-Identifier: Apache-2.0
"""Manuscript and cited text are untrusted input to model prompts.

Two kinds of content are removed before any text reaches a judge prompt:

* hidden text: characters or markup that a reader of the document never sees (zero-width and
  bidirectional controls, Unicode tag characters, DOCX runs marked vanish/white/tiny, HTML or
  XML elements hidden by style or attribute, HTML comments in Markdown);
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
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|$)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|color\s*:\s*(?:#f{3}(?:f{3})?\b|white\b|rgba?\(\s*255\s*,\s*255\s*,\s*255\s*[,)])"
    r"|(?:height|width|max-height)\s*:\s*0(?:px)?\s*;[^\"']*overflow\s*:\s*hidden|clip\s*:\s*rect\(\s*0",
    re.I,
)


def _style_hidden(style: str) -> bool:
    return bool(_HIDDEN_STYLE.search(style or ""))


def _document_removal(kind: str, text: str, rule: str, position: int) -> dict[str, Any]:
    return {"kind": kind, "rule": rule, "position": position, "length": len(text), "sha256": _digest(text),
            "scope": "manuscript-document"}


def _hiding_properties(properties: ET.Element | None) -> str | None:
    if properties is None:
        return None
    for tag in ("vanish", "specVanish", "webHidden"):
        node = properties.find(f"{{{_W}}}{tag}")
        if node is not None and node.get(f"{{{_W}}}val", "true").lower() not in {"0", "false", "off"}:
            return tag
    return None


def _docx_hidden_styles(archive: zipfile.ZipFile) -> tuple[set[str], set[str]]:
    """Character and paragraph style ids whose run properties hide text."""

    try:
        styles = archive.read("word/styles.xml")
    except KeyError:
        return set(), set()
    if _DTD.search(styles):
        raise ValidationError("DOCX word/styles.xml declares a DTD or entities; the document is refused")
    try:
        root = ET.fromstring(styles)
    except ET.ParseError:
        return set(), set()
    character, paragraph = set(), set()
    for style in root.iter(f"{{{_W}}}style"):
        if _hiding_properties(style.find(f"{{{_W}}}rPr")):
            identifier = style.get(f"{{{_W}}}styleId", "")
            (paragraph if style.get(f"{{{_W}}}type") == "paragraph" else character).add(identifier)
    return character, paragraph


def _docx_hidden_reason(run: ET.Element, hidden_styles: set[str] = frozenset()) -> str | None:
    properties = run.find(f"{{{_W}}}rPr")
    if properties is None:
        return None
    style = properties.find(f"{{{_W}}}rStyle")
    if style is not None and style.get(f"{{{_W}}}val", "") in hidden_styles:
        return "hidden-style"
    for tag in ("vanish", "specVanish", "webHidden"):
        node = properties.find(f"{{{_W}}}{tag}")
        if node is not None and node.get(f"{{{_W}}}val", "true").lower() not in {"0", "false", "off"}:
            return tag
    color = properties.find(f"{{{_W}}}color")
    shading = properties.find(f"{{{_W}}}shd")
    highlight = properties.find(f"{{{_W}}}highlight")
    if color is not None and color.get(f"{{{_W}}}val", "").upper() in {"FFFFFF", "FEFEFE"}:
        fill = (shading.get(f"{{{_W}}}fill", "") if shading is not None else "").upper()
        if highlight is None and fill in {"", "AUTO", "FFFFFF"}:
            return "white-text"
    size = properties.find(f"{{{_W}}}sz")
    if size is not None:
        try:
            if int(size.get(f"{{{_W}}}val", "24")) < 4:
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
    parents = {child: parent for parent in root.iter() for child in parent}
    character_styles, paragraph_styles = _docx_hidden_styles(archive)
    hidden_paragraphs = set()
    for paragraph in root.iter(f"{{{_W}}}p"):
        style = paragraph.find(f"{{{_W}}}pPr/{{{_W}}}pStyle")
        if style is not None and style.get(f"{{{_W}}}val", "") in paragraph_styles:
            hidden_paragraphs.update(paragraph.iter(f"{{{_W}}}r"))
    position = 0
    for run in list(root.iter(f"{{{_W}}}r")):
        reason = "hidden-paragraph-style" if run in hidden_paragraphs else _docx_hidden_reason(run, character_styles)
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
    """Re-emit HTML without hidden subtrees and comments; record what was dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.removals: list[dict[str, Any]] = []
        self.hidden_depth = 0
        self.hidden_tag: list[str] = []
        self.hidden_text: list[str] = []
        self.hidden_rule = ""
        self.position = 0

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


def _sanitize_html(data: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, []
    parser = _HiddenFilter()
    parser.feed(text)
    parser.close()
    if not parser.removals:
        return data, []
    return "".join(parser.output).encode("utf-8"), parser.removals


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
    r"<(?P<tag>span|div|p|font|small)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>", re.S | re.I)


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
        if (style and _style_hidden(style.group(1))) or re.search(r"\bhidden\b", attributes, re.I):
            removals.append(_document_removal("hidden-markup", match.group("body"), "markdown-hidden-html", match.start()))
            return ""
        return match.group()

    cleaned = _MARKDOWN_COMMENT.sub(drop_comment, text)
    cleaned = _MARKDOWN_HIDDEN_SPAN.sub(drop_hidden, cleaned)
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
        cleaned, removals = _sanitize_html(data)
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
