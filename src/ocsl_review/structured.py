# SPDX-License-Identifier: Apache-2.0
"""Local, independently implemented structured citation extraction.

Publisher markup is data, never code. Original bytes remain the source of
truth; numbered display markers are a local rendering, and original targets
and labels are retained separately. No historical research scripts are reused.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET

from .errors import ValidationError

BLOCKS = {"p", "li", "list-item", "td", "th", "caption", "figcaption", "title",
          "h1", "h2", "h3", "h4", "fn", "disp-quote", "def-item", "attrib",
          "abstract", "article-title", "trans-title", "disp-formula"}
SKIP = {"script", "style", "nav", "noscript", "form"}
_MARKER = re.compile(r"\x1e(\d+)\x1f")
_RANGE = re.compile(r"^(\d+)\s*[-–—]\s*(\d+)$")


class _HTMLTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = ET.SubElement(self.stack[-1], tag, dict((k, v or "") for k, v in attrs))
        if tag not in {"br", "img", "meta", "link", "hr", "input", "source", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data: str) -> None:
        parent = self.stack[-1]
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + data
        else:
            parent.text = (parent.text or "") + data


def _text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def extract(body: bytes, *, html: bool = False) -> dict[str, Any]:
    """Return ingestion blocks, reference identities, and coverage diagnostics."""
    if html:
        try:
            tree = _HTMLTree()
            tree.feed(body.decode("utf-8"))
            tree.close()
            root = tree.root
        except (UnicodeError, ValueError) as exc:
            raise ValidationError("publisher HTML is not readable UTF-8") from exc
    else:
        if re.search(br"<!ENTITY\b", body, re.I):
            raise ValidationError("structured XML contains entity declarations")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ValidationError("structured XML is malformed") from exc
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]
        if root.tag != "article" or root.find("body") is None:
            raise ValidationError("structured XML needs an article body")
    parents = {child: parent for parent in root.iter() for child in parent}
    element_paths = {root: "/" + root.tag + "[1]"}
    for parent in root.iter():
        counts: dict[str, int] = {}
        for child in parent:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            element_paths[child] = f"{element_paths[parent]}/{child.tag}[{counts[child.tag]}]"

    def location(node: ET.Element) -> dict[str, str]:
        return {"elementPath": element_paths[node], "elementId": node.get("id", ""), "tag": node.tag}

    def ancestors(node: ET.Element):
        while node in parents:
            node = parents[node]
            yield node

    def in_references(node: ET.Element) -> bool:
        return any(a.tag in {"ref", "ref-list"} or re.search(
            r"(?:^|[\s_-])(references?|bibliography)(?:$|[\s_-])",
            a.get("class", "") + " " + a.get("id", ""), re.I)
                   for a in (node, *ancestors(node)))

    if html:
        article = next((n for n in root.iter("article")), None)
        if article is None:
            article = next((n for n in root.iter() if n.get("id") in {"main-content", "article-body"}), None)
        if article is None:
            raise ValidationError("publisher HTML needs an explicit article container")
        incoming = {n.get("href", "").rsplit("#", 1)[-1] for n in article.iter("a")
                    if "#" in n.get("href", "")}
        refs = [n for n in root.iter() if n.get("id") and in_references(n)
                and (n.tag == "li" or n.get("id") in incoming)
                and not any(a.tag == "li" and in_references(a) for a in ancestors(n))]
        content_roots = [article]
    else:
        refs = list(root.findall(".//ref-list/ref"))
        # Abstract and back-matter footnotes can contain material citation loci.
        # Walk once from the article and exclude bibliography branches explicitly.
        content_roots = [root]
    identities: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    for number, node in enumerate(refs, 1):
        source_id = node.get("id") or f"unlabelled-{number}"
        if source_id in identities:
            raise ValidationError("structured bibliography contains duplicate target IDs")
        label = _text(node.find("label")) or node.get("data-label", "")
        if not label:
            match = re.match(r"\s*\[?(\d+)\]?[.)]?\s", _text(node))
            label = match.group(1) if match else str(number)
        printed = _text(node)
        identifiers = {n.get("pub-id-type"): _text(n) for n in node.iter("pub-id")
                       if n.get("pub-id-type") in {"doi", "pmid"}}
        for anchor in node.iter("a"):
            link = anchor.get("href", "")
            match = re.search(r"(?:doi\.org/|/doi/)(10\.[^?#]+)", link, re.I)
            if match:
                from urllib.parse import unquote
                identifiers["doi"] = unquote(match.group(1))
        identities[source_id] = {"ordinal": number, "printedLabel": label,
                                 "raw": printed, "identifiers": identifiers}
    if not identities:
        raise ValidationError("structured document has no explicit bibliography targets")
    by_label = {value["printedLabel"]: key for key, value in identities.items()}
    if len(by_label) != len(identities):
        raise ValidationError("structured bibliography has duplicate printed labels")
    targets: dict[ET.Element, list[str]] = {}
    for content in content_roots:
        for node in content.iter():
            if in_references(node) or any(a.tag in SKIP for a in ancestors(node)):
                continue
            if node.tag == "xref" and node.get("ref-type") == "bibr":
                targets[node] = (node.get("rid") or "").split() or ["<missing-rid>"]
            elif html and node.tag == "a" and "#" in node.get("href", ""):
                target = node.get("href", "").rsplit("#", 1)[-1]
                if target in identities or re.search(r"ref|citation|bibr", node.get("class", ""), re.I):
                    targets[node] = [target]
    used: dict[ET.Element, int] = {}
    markers: list[dict[str, Any]] = []

    def mark(node: ET.Element) -> str:
        used[node] = used.get(node, 0) + 1
        ids = list(targets[node])
        printed = _text(node)
        match = _RANGE.fullmatch(printed)
        if match:
            low, high = map(int, match.groups())
            if 0 <= high - low <= 10000:
                ids = [by_label.get(str(i), f"<missing-label:{i}>") for i in range(low, high + 1)]
            else:
                warnings.append({"code": "invalid-structured-range", "message": printed,
                                 "affectsCompleteness": True})
        markers.append({"targets": ids, "sourceMarker": printed, "sourceAnchors": [location(node)]})
        return f"\x1e{len(markers) - 1}\x1f"

    def serialize(node: ET.Element, *, stop_blocks: bool = True) -> str:
        if node in targets:
            return mark(node)
        if node.tag in SKIP or in_references(node):
            return ""
        parts = [node.text or ""]
        for child in node:
            if not (stop_blocks and child.tag in BLOCKS):
                parts.append(serialize(child, stop_blocks=stop_blocks))
            parts.append(child.tail or "")
        return "".join(parts)

    blocks: list[dict[str, Any]] = []

    def emit(node: ET.Element, *, stop_blocks: bool = True) -> None:
        raw = re.sub(r"[ \t\r\n]+", " ", serialize(node, stop_blocks=stop_blocks)).strip(" \t\r\n")
        # A pair of explicit endpoint anchors may print a numeric range.
        pair = re.compile(r"\x1e(\d+)\x1f\s*[-–—]\s*\x1e(\d+)\x1f")
        def expand(match):
            left, right = [markers[int(i)] for i in match.groups()]
            if len(left["targets"]) != 1 or len(right["targets"]) != 1:
                return match.group()
            a, b = [identities.get(m["targets"][0], {}).get("printedLabel", "") for m in (left, right)]
            if not a.isdecimal() or not b.isdecimal() or not 0 <= int(b) - int(a) <= 10000:
                return match.group()
            markers.append({"targets": [by_label.get(str(i), f"<missing-label:{i}>")
                                        for i in range(int(a), int(b) + 1)],
                            "sourceMarker": f"{left['sourceMarker']}–{right['sourceMarker']}"})
            markers[-1]["sourceAnchors"] = left["sourceAnchors"] + right["sourceAnchors"]
            return f"\x1e{len(markers) - 1}\x1f"
        raw = pair.sub(expand, raw)
        pieces, explicit, cursor, length = [], [], 0, 0
        for match in _MARKER.finditer(raw):
            before = raw[cursor:match.start()]
            pieces.append(before); length += len(before)
            marker = markers[int(match.group(1))]
            display = "[" + ",".join(str(identities.get(i, {}).get("ordinal", "?")) for i in marker["targets"]) + "]"
            explicit.append({"start": length, "end": length + len(display), "raw": display,
                             "style": "structured", **marker})
            pieces.append(display); length += len(display); cursor = match.end()
        pieces.append(raw[cursor:])
        text = "".join(pieces)
        if text.strip():
            blocks.append({"kind": "paragraph", "text": text, "rawText": text,
                           "heading": node.tag in {"title", "article-title", "trans-title", "h1", "h2", "h3", "h4"},
                           "sourceLocator": location(node),
                           "caption": node.tag in {"caption", "figcaption"} or any(a.tag in {"fig", "figure"} for a in ancestors(node)),
                           "explicitCitations": explicit})

    def walk(node: ET.Element) -> None:
        if node.tag in SKIP or in_references(node):
            return
        if node.tag == "table":
            rows, cell_citations = [], {}
            for row in node.iter("tr"):
                cells = []
                for cell in row:
                    if cell.tag not in {"td", "th"}:
                        continue
                    before = len(blocks)
                    emit(cell, stop_blocks=False)
                    rendered = blocks.pop() if len(blocks) > before else {"text": "", "explicitCitations": []}
                    cell_citations[(len(rows), len(cells))] = rendered["explicitCitations"]
                    cells.append(rendered["text"])
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append({"kind": "table", "rows": rows, "explicitCellCitations": cell_citations,
                               "sourceLocator": location(node)})
            return
        if node.tag in BLOCKS:
            emit(node)
        elif any(n in targets for n in node.iter()) and not any(
                n is not node and n.tag in BLOCKS for n in node.iter()):
            if not any(n in used for n in node.iter() if n in targets):
                emit(node)
            return
        for child in node:
            walk(child)
    for content in content_roots:
        walk(content)
    missed = [n for n in targets if used.get(n, 0) != 1]
    if missed:
        warnings.append({"code": "structured-anchor-ownership", "message": f"{len(missed)} anchors not owned exactly once",
                         "affectsCompleteness": True})
    blocks.append({"kind": "paragraph", "text": "References", "heading": True})
    for source_id, identity in identities.items():
        blocks.append({"kind": "paragraph", "text": f"{identity['ordinal']}. {identity['raw']}",
                       "sourceIdentity": {"targetId": source_id, "printedLabel": identity["printedLabel"],
                                          "identifiers": identity["identifiers"]}})
    publication = {node.get("pub-id-type"): _text(node) for node in root.findall("./front/article-meta/article-id")
                   if node.get("pub-id-type")}
    for name in ("article-title", "trans-title"):
        title = root.find("./front/article-meta/title-group/" + name)
        if title is not None:
            publication[name] = _text(title)
    for node in root.iter("meta"):
        if node.get("name") in {"citation_doi", "citation_title", "citation_publication_date"}:
            publication[node.get("name")] = node.get("content", "")
    return {"blocks": blocks, "identities": identities, "warnings": warnings,
            "metadata": {"publication": publication, "anchors": len(targets),
                         "ownedAnchors": sum(used.get(n) == 1 for n in targets),
                         "representation": "publisher-html" if html else "jats"}}
