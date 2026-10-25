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
from urllib.parse import parse_qs, unquote, urlsplit
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


def _reference_text(node: ET.Element) -> str:
    """A bibliography label is a separate field; inline citation text is not."""
    parts = [node.text or ""]
    for child in node:
        content = "".join(child.itertext())
        parts.append(" " + content + " " if child.tag == "label" else content)
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


def _numeric_label(label: str) -> str | None:
    """Normalize numeric lookup only; the original printed label stays intact."""
    match = re.fullmatch(r"(?:\[(\d+)\]|(\d+)[.)]?)", label.strip())
    return str(int(next(value for value in match.groups() if value is not None))) if match else None


def _linked_identifiers(link: str) -> dict[str, str]:
    """Read identifiers asserted by resolver links, never by search-result text."""
    try:
        url = urlsplit(link)
        host = (url.hostname or "").lower()
    except ValueError:
        return {}
    if url.scheme not in {"http", "https", ""}:
        return {}
    path = unquote(url.path)
    if host in {"doi.org", "www.doi.org", "dx.doi.org"}:
        doi = path.lstrip("/")
    else:
        match = re.search(r"/doi/(?:abs/|full/|pdf/)?(10\.[^?#]+)", path, re.I)
        doi = match.group(1) if match else ""
    if re.fullmatch(r"10\.\d{4,9}/[^\s<>\"]+", doi, re.I):
        return {"doi": doi}
    if host not in {"pubmed.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"}:
        return {}
    pmc = re.fullmatch(r"/(?:pmc/)?articles/(PMC\d+)/?", path, re.I)
    if pmc:
        return {"pmcid": pmc.group(1).upper()}
    pmid = re.fullmatch(r"/(?:pubmed/)?(\d+)/?", path)
    if pmid and (host == "pubmed.ncbi.nlm.nih.gov" or path.startswith("/pubmed/")):
        return {"pmid": pmid.group(1)}
    query = parse_qs(url.query)
    if path == "/entrez/query.fcgi" and [v.lower() for v in query.get("db", [])] == ["pubmed"]:
        values = query.get("list_uids", [])
        if len(values) == 1 and re.fullmatch(r"\d+", values[0]):
            return {"pmid": values[0]}
    return {}


def _reference_identifiers(node: ET.Element) -> tuple[dict[str, str], list[str]]:
    """Keep only unambiguous identifiers within one bibliography entry."""
    candidates: dict[str, dict[str, str]] = {}

    def add(kind: str, value: str) -> None:
        if kind == "pmc":
            kind = "pmcid"
        value = value.strip()
        if kind == "doi":
            value = _linked_identifiers(value).get("doi", value)
            valid = re.fullmatch(r"10\.\d{4,9}/[^\s<>\"]+", value, re.I)
        elif kind == "pmid":
            valid = re.fullmatch(r"\d+", value)
        elif kind == "pmcid":
            value = value.upper()
            valid = re.fullmatch(r"PMC\d+", value)
        else:
            return
        if valid:
            candidates.setdefault(kind, {}).setdefault(value.casefold(), value)

    for child in node.iter():
        if child.tag == "pub-id":
            add(child.get("pub-id-type", ""), _text(child))
        if child.tag in {"a", "ext-link"}:
            link = child.get("href", "") or child.get("{http://www.w3.org/1999/xlink}href", "")
            for kind, value in _linked_identifiers(link).items():
                add(kind, value)
    return ({kind: next(iter(values.values())) for kind, values in candidates.items() if len(values) == 1},
            [kind for kind, values in candidates.items() if len(values) > 1])


class _Citations:
    """Serialise blocks with one display marker per citation anchor (ranges expanded); shared by every reader."""

    _PAIR = re.compile(r"\x1e\d+\x1f(?:\s*[-–—]\s*\x1e\d+\x1f)+")

    def __init__(self, identities: dict[str, dict[str, Any]], numeric_labels: dict[str, str],
                 targets: dict[ET.Element, list[str]], warnings: list[dict[str, Any]], location, *, skip, in_figure):
        self.identities, self.numeric_labels, self.targets = identities, numeric_labels, targets
        self.warnings, self.location, self.skip, self.in_figure = warnings, location, skip, in_figure
        self.used: dict[ET.Element, int] = {}
        self.markers: list[dict[str, Any]] = []

    def mark(self, node: ET.Element) -> str:
        self.used[node] = self.used.get(node, 0) + 1
        ids = list(self.targets[node])
        printed = _text(node)
        match = _RANGE.fullmatch(printed)
        if match:
            low, high = map(int, match.groups())
            if 0 <= high - low <= 10000:
                ids = [self.numeric_labels.get(str(i), f"<missing-label:{i}>") for i in range(low, high + 1)]
            else:
                self.warnings.append({"code": "invalid-structured-range", "message": printed,
                                      "affectsCompleteness": True})
        self.markers.append({"targets": ids, "sourceMarker": printed, "sourceAnchors": [self.location(node)]})
        return f"\x1e{len(self.markers) - 1}\x1f"

    def serialize(self, node: ET.Element, *, stop_blocks: bool = True) -> str:
        if node in self.targets:
            return self.mark(node)
        if self.skip(node):
            return ""
        parts = [node.text or ""]
        for child in node:
            if not (stop_blocks and child.tag in BLOCKS):
                parts.append(self.serialize(child, stop_blocks=stop_blocks))
            parts.append(child.tail or "")
        return "".join(parts)

    def block(self, node: ET.Element, *, stop_blocks: bool = True) -> dict[str, Any] | None:
        raw = re.sub(r"[ \t\r\n]+", " ", self.serialize(node, stop_blocks=stop_blocks)).strip(" \t\r\n")
        markers, identities, numeric_labels = self.markers, self.identities, self.numeric_labels

        # Consecutive endpoint anchors may share a middle endpoint (1–3–5).
        # Match the whole chain so non-overlapping replacements lose no range.
        def expand(match):
            endpoints = [markers[int(marker.group(1))] for marker in _MARKER.finditer(match.group())]
            if any(len(marker["targets"]) != 1 for marker in endpoints):
                return match.group()
            labels = [_numeric_label(identities.get(marker["targets"][0], {}).get("printedLabel", ""))
                      for marker in endpoints]
            if any(label is None for label in labels):
                return match.group()
            values = [int(label) for label in labels]
            if any(b < a for a, b in zip(values, values[1:])) or values[-1] - values[0] > 10000:
                self.warnings.append({"code": "invalid-structured-range",
                                      "message": "range endpoints are not ordered or bounded", "affectsCompleteness": True})
                return match.group()
            markers.append({"targets": [numeric_labels.get(str(i), f"<missing-label:{i}>")
                                        for i in range(values[0], values[-1] + 1)],
                            "sourceMarker": "–".join(marker["sourceMarker"] for marker in endpoints)})
            markers[-1]["sourceAnchors"] = [anchor for marker in endpoints for anchor in marker["sourceAnchors"]]
            return f"\x1e{len(markers) - 1}\x1f"
        raw = self._PAIR.sub(expand, raw)
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
        if not text.strip():
            return None
        return {"kind": "paragraph", "text": text, "rawText": text,
                "heading": node.tag in {"title", "article-title", "trans-title", "h1", "h2", "h3", "h4"},
                "sourceLocator": self.location(node),
                "caption": node.tag in {"caption", "figcaption"} or self.in_figure(node),
                "explicitCitations": explicit}


def _numeric_labels(identities: dict[str, dict[str, Any]]) -> dict[str, str]:
    numeric_labels: dict[str, str] = {}
    for key, identity in identities.items():
        label = _numeric_label(identity["printedLabel"])
        if label is not None:
            if label in numeric_labels:
                raise ValidationError("structured bibliography has ambiguous numeric labels")
            numeric_labels[label] = key
    return numeric_labels


def extract_table_page(body: bytes, identities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Citing units of a separately rendered table page, resolved against the article's bibliography.

    Some publisher article pages carry only a table's caption and a link; the table body is a page of its
    own. Every ``td``/``th`` cell of its tables, its figure captions and its table-footer paragraphs are
    citing units; a link counts as a citation when its fragment (``...#ref-CR12``) names a bibliography
    target of the article page. Each anchor is owned by exactly one unit.
    """
    try:
        tree = _HTMLTree()
        tree.feed(body.decode("utf-8"))
        tree.close()
    except (UnicodeError, ValueError) as exc:
        raise ValidationError("table page HTML is not readable UTF-8") from exc
    root = tree.root
    parents = {child: parent for parent in root.iter() for child in parent}
    element_paths = {root: "/" + root.tag + "[1]"}
    for parent in root.iter():
        counts: dict[str, int] = {}
        for child in parent:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            element_paths[child] = f"{element_paths[parent]}/{child.tag}[{counts[child.tag]}]"

    def ancestors(node: ET.Element):
        while node in parents:
            node = parents[node]
            yield node

    def location(node: ET.Element) -> dict[str, str]:
        return {"elementPath": element_paths[node], "elementId": node.get("id", ""), "tag": node.tag}

    def unit(node: ET.Element) -> bool:
        if any(a.tag in SKIP for a in ancestors(node)):
            return False
        if node.tag in {"td", "th"}:
            return any(a.tag == "table" for a in ancestors(node))
        if node.tag == "figcaption":
            return True
        return node.tag == "p" and any("c-article-table-footer" in a.get("class", "") for a in ancestors(node))

    candidates = [node for node in root.iter() if unit(node)]
    chosen = set(candidates)
    cells = [node for node in candidates if not any(a in chosen for a in ancestors(node))]
    targets: dict[ET.Element, list[str]] = {}
    for cell in cells:
        for node in cell.iter("a"):
            href = node.get("href", "")
            if "#" in href and href.rsplit("#", 1)[-1] in identities:
                targets[node] = [href.rsplit("#", 1)[-1]]
    warnings: list[dict[str, Any]] = []
    citations = _Citations(identities, _numeric_labels(identities), targets, warnings, location,
                           skip=lambda node: node.tag in SKIP, in_figure=lambda node: False)
    blocks = []
    for cell in cells:
        block = citations.block(cell, stop_blocks=False)
        if block is not None and block["explicitCitations"]:
            blocks.append(block)
    missed = [n for n in targets if citations.used.get(n, 0) != 1]
    if missed:
        warnings.append({"code": "structured-anchor-ownership", "message": f"{len(missed)} anchors not owned exactly once",
                         "affectsCompleteness": True})
    unresolved = {target for marker in citations.markers for target in marker["targets"] if target not in identities}
    if unresolved:
        warnings.append({"code": "unresolved-structured-target", "message": f"{len(unresolved)} bibliography targets unresolved",
                         "affectsCompleteness": True})
    return {"blocks": blocks, "warnings": warnings,
            "metadata": {"anchors": len(targets), "ownedAnchors": sum(citations.used.get(n) == 1 for n in targets),
                         "units": len(cells), "representation": "publisher-html-table-page"}}


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
        document_root = root
        if root.tag == "pmc-articleset":
            articles = list(root.findall("article"))
            if len(articles) != 1:
                raise ValidationError("PMC article set needs exactly one article")
            root = articles[0]
        if root.tag != "article" or root.find("body") is None:
            raise ValidationError("structured XML needs an article body")
    document_root = root if html else document_root
    parents = {child: parent for parent in document_root.iter() for child in parent}
    element_paths = {document_root: "/" + document_root.tag + "[1]"}
    for parent in document_root.iter():
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
                and (n.tag == "li" or n.get("id") in incoming
                     or (n.tag == "p" and n.get("id", "").startswith("ref-")))
                and not any(a.tag == "li" and a.get("id") and in_references(a)
                            for a in ancestors(n))]
        content_roots = [article]
    else:
        refs = list(root.findall(".//ref-list/ref"))
        # Abstract and back-matter footnotes can contain material citation loci.
        # Walk once from the article and exclude bibliography branches explicitly.
        content_roots = [root]
    identities: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    reference_nodes = set(refs)
    for number, node in enumerate(refs, 1):
        source_id = node.get("id") or f"unlabelled-{number}"
        if source_id in identities:
            raise ValidationError("structured bibliography contains duplicate target IDs")
        label = _text(node.find("label")) or node.get("data-label", "") or node.get("data-counter", "")
        if not label and html:
            label = next((a.get("data-counter", "") for a in ancestors(node)
                          if a.tag == "li" and a.get("data-counter")), "")
        if not label:
            match = re.match(r"\s*\[?(\d+)\]?[.)]?\s", _text(node))
            label = match.group(1) if match else str(number)
        printed = _reference_text(node) if node.tag == "ref" else _text(node)
        identifier_scope = node
        if html and node.tag != "li":
            # Publishers commonly put the target on a paragraph and its DOI /
            # PubMed links in a sibling paragraph. Never cross an entry boundary
            # or borrow links from a list item that owns multiple references.
            item = next((a for a in ancestors(node) if a.tag == "li"), None)
            if item is not None and reference_nodes.intersection(item.iter()) == {node}:
                identifier_scope = item
        identifiers, conflicts = _reference_identifiers(identifier_scope)
        if conflicts:
            warnings.append({"code": "conflicting-reference-identifiers", "targetId": source_id,
                             "message": "multiple explicit identifiers for " + ", ".join(sorted(conflicts)),
                             "affectsCompleteness": True})
        identities[source_id] = {"ordinal": number, "printedLabel": label,
                                 "raw": printed, "identifiers": identifiers}
    if not identities:
        raise ValidationError("structured document has no explicit bibliography targets")
    by_label = {value["printedLabel"]: key for key, value in identities.items()}
    if len(by_label) != len(identities):
        raise ValidationError("structured bibliography has duplicate printed labels")
    numeric_labels: dict[str, str] = {}
    for key, identity in identities.items():
        label = _numeric_label(identity["printedLabel"])
        if label is not None:
            if label in numeric_labels:
                raise ValidationError("structured bibliography has ambiguous numeric labels")
            numeric_labels[label] = key
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
    citations = _Citations(identities, numeric_labels, targets, warnings, location,
                           skip=lambda node: node.tag in SKIP or in_references(node),
                           in_figure=lambda node: any(a.tag in {"fig", "figure"} for a in ancestors(node)))
    used, markers = citations.used, citations.markers
    blocks: list[dict[str, Any]] = []

    def emit(node: ET.Element, *, stop_blocks: bool = True) -> None:
        block = citations.block(node, stop_blocks=stop_blocks)
        if block is not None:
            blocks.append(block)

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
    if html:
        external_tables = set()
        for anchor in article.iter("a"):
            link = anchor.get("href", "")
            if in_references(anchor) or not re.search(r"(?:^|/)tables/\d+(?:[/?#]|$)", link):
                continue
            containers = [node for node in ancestors(anchor) if node.tag in {"figure", "fig", "table-wrap"}]
            if any(any(table.find(".//tr") is not None for table in container.iter("table"))
                   for container in containers):
                continue
            external_tables.add(link)
        if external_tables:
            warnings.append({"code": "external-table-content-unprovided",
                             "message": f"{len(external_tables)} linked tables are absent from supplied HTML",
                             "targets": sorted(external_tables), "affectsCompleteness": True})
    missed = [n for n in targets if used.get(n, 0) != 1]
    if missed:
        warnings.append({"code": "structured-anchor-ownership", "message": f"{len(missed)} anchors not owned exactly once",
                         "affectsCompleteness": True})
    unresolved = {target for marker in markers for target in marker["targets"] if target not in identities}
    if unresolved:
        warnings.append({"code": "unresolved-structured-target", "message": f"{len(unresolved)} bibliography targets unresolved",
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
