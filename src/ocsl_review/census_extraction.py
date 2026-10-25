# SPDX-License-Identifier: Apache-2.0
"""Census inputs from structured review documents.

Extraction itself is ``structured.extract`` (recursive serialisation, range
expansion, tables, legends, boxes and lists, every anchor owned by exactly one
block, explicit bibliography identities). This module turns its blocks into
what the census judge sees for each printed entry:

* every citing unit (a paragraph-like block or a table cell) is rendered with
  one marker per cited target, written ``⟨target⟩``, typographic quotes and
  dashes normalised and whitespace collapsed;
* each marker occurrence yields one locus: the sentence that carries it, the
  section trail, document order, and display flags;
* only loci under the article body count as in-body citations; loci elsewhere
  are recorded as diagnostics, never propagated to other sentences.

It also provides the extraction gates (uncovered anchors, unresolved targets
and JATS-versus-HTML cross-validation), the identity ladder over printed
identifiers, and review metadata (corresponding-author country, topic).
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET

from . import structured
from .errors import ValidationError

MARK = "⟨%s⟩ "
MARK_PATTERN = re.compile(r"⟨([^ ⟩]+)⟩")
SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9(\[“"⟨])')
JATS_DISPLAY = {"table-wrap": "table", "table": "table", "fig": "fig", "boxed-text": "box", "list": "list",
                "supplementary-material": "supp", "abstract": "abstract", "glossary": "glossary", "def-list": "glossary"}
HTML_SKIP_SECTIONS = ("abstract", "references", "author information", "ethics declarations", "peer review",
                      "additional information", "supplementary information", "rights and permissions",
                      "about this article", "acknowledgements", "author contributions", "funding", "change history")
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
TOPIC_MAP_VERSION = "topic-keywords@1"
TOPIC_MAP = (
    ("breast", r"\bbreast\b"),
    ("lung/thoracic", r"\blung\b|nsclc|sclc|mesothelioma|thoracic"),
    ("gastrointestinal", r"colorectal|\bcolon\b|rectal|gastric|esophag|oesophag|pancrea|hepat|\bliver\b|hcc\b|biliary|"
                         r"cholangio|gastrointestinal"),
    ("genitourinary", r"prostate|bladder|urothelial|renal|kidney|testicular|penile"),
    ("gynaecological", r"ovarian|cervical|endometrial|uterine|vulvar|gynaecolog|gynecolog"),
    ("haematological", r"leukemia|leukaemia|lymphoma|myeloma|myelodysplastic|hematolog|haematolog|\bcar[- ]t\b"),
    ("CNS", r"glioma|glioblastoma|\bbrain\b|meningioma|neuro-?oncolog|medulloblastoma"),
    ("skin/melanoma", r"melanoma|\bskin\b|cutaneous|merkel"),
    ("head and neck/thyroid", r"head and neck|thyroid|nasopharyn|oropharyn|oral cancer|laryn"),
    ("sarcoma/bone", r"sarcoma|osteosarcoma|\bbone\b"),
    ("paediatric", r"pediatric|paediatric|childhood|neuroblastoma"),
    ("immunotherapy (cross-cutting)", r"immunotherap|checkpoint|pd-1|pd-l1|ctla|immune"),
    ("radiotherapy/imaging", r"radiother|radiation|imaging|radiomics|pet\b|mri\b"),
    ("genomics/biomarkers", r"genom|mutation|biomarker|liquid biopsy|ctdna|epigenet|microbio"),
)
COUNTRY_NAMES = {
    "united states": "USA", "usa": "USA", "united states of america": "USA", "u.s.a.": "USA", "us": "USA",
    "uk": "United Kingdom", "united kingdom": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "gb": "United Kingdom", "p.r. china": "China", "people's republic of china": "China",
    "pr china": "China", "china": "China", "cn": "China", "korea": "South Korea", "republic of korea": "South Korea",
    "south korea": "South Korea", "kr": "South Korea", "the netherlands": "Netherlands", "netherlands": "Netherlands",
    "nl": "Netherlands", "italy": "Italy", "it": "Italy", "germany": "Germany", "de": "Germany", "japan": "Japan",
    "jp": "Japan", "france": "France", "fr": "France", "spain": "Spain", "es": "Spain", "canada": "Canada",
    "ca": "Canada", "australia": "Australia", "au": "Australia", "india": "India", "taiwan": "Taiwan",
    "brazil": "Brazil", "switzerland": "Switzerland", "ch": "Switzerland", "sweden": "Sweden", "se": "Sweden",
    "belgium": "Belgium", "be": "Belgium", "austria": "Austria", "at": "Austria", "denmark": "Denmark",
    "dk": "Denmark", "greece": "Greece", "turkey": "Turkey", "iran": "Iran", "egypt": "Egypt", "poland": "Poland",
    "israel": "Israel", "singapore": "Singapore", "norway": "Norway", "portugal": "Portugal", "mexico": "Mexico",
    "russia": "Russia", "saudi arabia": "Saudi Arabia", "pakistan": "Pakistan",
}


def clean(value: str | None) -> str:
    """Normalise typographic quotes and dashes, drop soft hyphens, collapse whitespace."""
    if value is None:
        return ""
    for old, new in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                     ("–", "-"), ("—", "-"), ("­", "")):
        value = value.replace(old, new)
    return re.sub(r"\s+", " ", value).strip()


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for match in SENTENCE_SPLIT.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def normalize_label(printed: str | None, ordinal: int) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", clean(printed or "")) or str(ordinal)


def topic(title: str | None) -> str:
    text = (title or "").lower()
    for name, pattern in TOPIC_MAP:
        if re.search(pattern, text):
            return name
    return "other"


def normalize_country(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().strip(".").lower()
    return COUNTRY_NAMES.get(key, value.strip()[:40])


@dataclass
class Locus:
    label: str
    target: str
    sentence: str
    section: str
    unit: int
    offset: int
    in_body: bool
    flags: frozenset = frozenset()
    unit_tag: str = ""

    def passage(self) -> dict[str, str]:
        return {"sentence": self.sentence, "section": self.section}


@dataclass
class Entry:
    ordinal: int
    label: str
    target: str
    printed_label: str
    printed: str
    identifiers: dict[str, str]
    loci: list[Locus] = field(default_factory=list)
    outside_body_loci: int = 0

    @property
    def cited_in_body(self) -> bool:
        return bool(self.loci)

    def passages(self, limit: int | None = None) -> list[dict[str, str]]:
        ordered = sorted(self.loci, key=lambda locus: (locus.unit, locus.offset))
        return [locus.passage() for locus in (ordered if limit is None else ordered[:limit])]


# ----------------------------------------------------------------------------- document trees
def _paths(root: ET.Element) -> tuple[dict[str, ET.Element], dict[ET.Element, ET.Element]]:
    parents = {child: parent for parent in root.iter() for child in parent}
    paths = {root: "/" + root.tag + "[1]"}
    for parent in root.iter():
        counts: dict[str, int] = {}
        for child in parent:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            paths[child] = f"{paths[parent]}/{child.tag}[{counts[child.tag]}]"
    return {path: element for element, path in paths.items()}, parents


def _tree(body: bytes, html: bool) -> ET.Element:
    if html:
        tree = structured._HTMLTree()
        tree.feed(body.decode("utf-8"))
        tree.close()
        return tree.root
    root = ET.fromstring(body)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return root


def _ancestors(element: ET.Element, parents: Mapping[ET.Element, ET.Element]):
    while element in parents:
        element = parents[element]
        yield element


def _jats_context(element, parents) -> tuple[bool, frozenset, str]:
    flags, trail, in_body = set(), [], False
    for ancestor in (element, *_ancestors(element, parents)):
        tag = ancestor.tag
        if tag == "body":
            in_body = True
        if ancestor is not element and tag in JATS_DISPLAY:
            flags.add(JATS_DISPLAY[tag])
        if ancestor is not element and tag == "sec":
            title = ancestor.find("title")
            if title is not None:
                trail.append(clean("".join(title.itertext())))
    return in_body, frozenset(flags), " > ".join(reversed(trail)) or "body"


def _html_context(element, parents, body_root) -> tuple[bool, frozenset, str]:
    flags, section = set(), ""
    chain = list(_ancestors(element, parents))
    for ancestor in chain:
        cls = ancestor.get("class", "")
        tag = ancestor.tag
        if "c-article-table" in cls or tag == "table":
            flags.add("table")
        if "c-article-box" in cls:
            flags.add("box")
        if tag == "figure" and "c-article-table" not in cls:
            flags.add("fig")
        if tag in ("ul", "ol"):
            flags.add("list")
        if not section and "c-article-section" in cls:
            section = ancestor.get("data-title", "")
            if not section:
                heading = next(iter(ancestor.iter("h2")), None)
                section = clean("".join(heading.itertext())) if heading is not None else ""
    skipped = any(a.tag == "section" and a.get("data-title", "").strip().lower() in HTML_SKIP_SECTIONS for a in chain)
    skipped = skipped or any(re.search(r"(?:^|[\s_-])(abstract|references?|bibliography)(?:$|[\s_-])",
                                       a.get("class", "") + " " + a.get("id", ""), re.I) for a in chain)
    inside = body_root is None or element is body_root or body_root in chain
    return inside and not skipped, frozenset(flags), section or "body"


# ----------------------------------------------------------------------------- extraction
def marker_name(target: str, *, html: bool) -> str:
    """Marker text for a target: publisher HTML anchors drop their ``ref-`` id prefix."""
    return target[4:] if html and target.startswith("ref-") and len(target) > 4 else target


def _render(text: str, citations: Sequence[Mapping[str, Any]], *, html: bool = False) -> tuple[str, list[tuple[str, int]]]:
    """Replace display markers by one ⟨target⟩ marker per target; return cleaned text and marker order."""
    pieces, cursor, order = [], 0, []
    for citation in sorted(citations, key=lambda c: c["start"]):
        pieces.append(text[cursor:citation["start"]])
        for target in citation["targets"]:
            pieces.append(MARK % marker_name(target, html=html))
            order.append(target)
        cursor = citation["end"]
    pieces.append(text[cursor:])
    rendered = clean("".join(pieces))
    located, search_from = [], 0
    for target in order:
        token = MARK.rstrip() % marker_name(target, html=html)
        position = rendered.find(token, search_from)
        if position < 0:
            position = rendered.find(token)
        if position < 0:
            continue
        search_from = position + len(token)
        located.append((target, position))
    return rendered, located


def extract_review(body: bytes, *, html: bool = False) -> dict[str, Any]:
    """Printed entries with in-body loci, review metadata and extraction diagnostics."""
    value = structured.extract(body, html=html)
    by_path, parents = _paths(_tree(body, html))
    body_root = None
    if html:
        candidates = [n for n in by_path.values() if "c-article-body" in n.get("class", "")]
        body_root = candidates[0] if candidates else None
    identities = value["identities"]
    entries: dict[str, Entry] = {}
    labels: dict[str, str] = {}
    for target, identity in sorted(identities.items(), key=lambda item: item[1]["ordinal"]):
        label = normalize_label(identity["printedLabel"], identity["ordinal"])
        if label in labels:
            raise ValidationError(f"bibliography labels collide after normalisation: {label}")
        labels[label] = target
        entries[target] = Entry(identity["ordinal"], label, target, identity["printedLabel"], identity["raw"],
                                dict(identity["identifiers"]))
    units = []
    for block in value["blocks"]:
        if block.get("sourceIdentity") or "sourceLocator" not in block:
            continue
        if block.get("kind") == "table":
            for (row, column), citations in sorted(block.get("explicitCellCitations", {}).items()):
                if citations:
                    units.append((block["rows"][row][column], citations, block["sourceLocator"], "td"))
        elif block.get("explicitCitations"):
            units.append((block["text"], block["explicitCitations"], block["sourceLocator"],
                          block["sourceLocator"].get("tag", "")))
    outside = defaultdict(int)
    unresolved_markers = 0
    for index, (text, citations, locator, tag) in enumerate(units):
        element = by_path.get(locator["elementPath"])
        if element is None:
            raise ValidationError("structured locator does not resolve in the document tree")
        in_body, flags, section = (_html_context(element, parents, body_root) if html
                                   else _jats_context(element, parents))
        rendered, located = _render(text, citations, html=html)
        spans = sentence_spans(rendered)
        for target, position in located:
            entry = entries.get(target)
            if entry is None:
                unresolved_markers += 1
                continue
            if not in_body:
                outside[target] += 1
                continue
            sentence = next((rendered[a:b] for a, b in spans if a <= position < b), rendered)
            entry.loci.append(Locus(entry.label, target, sentence, section, index, position, True, flags, tag))
    for target, count in outside.items():
        entries[target].outside_body_loci = count
    metadata = value["metadata"]
    publication = metadata.get("publication", {})
    anchors, owned = metadata.get("anchors", 0), metadata.get("ownedAnchors", 0)
    warnings = value.get("warnings", [])
    diagnostics = {
        "representation": metadata.get("representation"),
        "anchors": anchors, "ownedAnchors": owned, "uncovered": anchors - owned,
        "unresolvedTargets": sum(1 for w in warnings if w.get("code") == "unresolved-structured-target"),
        "unresolvedMarkers": unresolved_markers,
        "warnings": sorted({w.get("code", "") for w in warnings}),
        "entries": len(entries), "citedInBody": sum(1 for e in entries.values() if e.cited_in_body),
        "loci": sum(len(e.loci) for e in entries.values()),
        "outsideBodyLoci": sum(outside.values()),
        "documentSha256": hashlib.sha256(body).hexdigest(),
    }
    title = publication.get("article-title") or publication.get("citation_title") or ""
    return {"title": title, "doi": (publication.get("doi") or publication.get("citation_doi") or "").lower() or None,
            "entries": [entries[t] for t in sorted(entries, key=lambda t: entries[t].ordinal)],
            "diagnostics": diagnostics, "country": corresponding_country(body, html=html), "topic": topic(title),
            "topicMap": TOPIC_MAP_VERSION}


def uncovered_gate(review: Mapping[str, Any]) -> dict[str, Any]:
    """Gate: every anchor owned exactly once and every target resolved."""
    d = review["diagnostics"]
    problems = []
    if d["uncovered"]:
        problems.append(f"{d['uncovered']} anchors not owned exactly once")
    if d["unresolvedTargets"] or d["unresolvedMarkers"]:
        problems.append("unresolved bibliography targets")
    return {"ok": not problems, "uncovered": d["uncovered"], "problems": problems}


def _xval_norm(value: str) -> str:
    value = MARK_PATTERN.sub(" ", value or "").replace("–", "-").replace("—", "-").replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", value.lower())).strip()


def cross_validate(jats_review: Mapping[str, Any], html_review: Mapping[str, Any], *, ratio: float = 0.8,
                   candidates: int = 30) -> dict[str, Any]:
    """Share of JATS loci whose sentence matches an HTML locus of the same label (exact or similarity >= ratio)."""
    html_by_label = defaultdict(list)
    for entry in html_review["entries"]:
        for locus in entry.loci:
            html_by_label[entry.label].append(_xval_norm(locus.sentence))
    jats_loci = [(entry.label, locus.sentence) for entry in jats_review["entries"] for locus in entry.loci]
    matched = 0
    for label, sentence in jats_loci:
        normalised = _xval_norm(sentence)
        pool = html_by_label.get(label, [])[:candidates]
        if any(normalised == other or difflib.SequenceMatcher(None, normalised, other).ratio() >= ratio for other in pool):
            matched += 1
    jats_labels = {e.label for e in jats_review["entries"] if e.loci}
    html_labels = {e.label for e in html_review["entries"] if e.loci}
    union = jats_labels | html_labels
    return {"jatsLoci": len(jats_loci), "htmlLoci": sum(len(v) for v in html_by_label.values()), "matched": matched,
            "share": round(matched / len(jats_loci), 4) if jats_loci else None,
            "labelJaccard": round(len(jats_labels & html_labels) / len(union), 4) if union else None}


def cross_validation_gate(results: Iterable[Mapping[str, Any]], *, minimum_median: float = 0.95) -> dict[str, Any]:
    """Gate over papers whose numbering matches: the median match share must reach ``minimum_median``."""
    shares = sorted(r["share"] for r in results if r.get("share") is not None)
    median = shares[len(shares) // 2] if shares else None
    return {"papers": len(shares), "median": median, "minimumMedian": minimum_median,
            "ok": median is not None and median >= minimum_median}


# ----------------------------------------------------------------------------- identity
def printed_doi(text: str | None) -> str | None:
    """A DOI in printed text, keeping balanced parentheses and trimming trailing punctuation."""
    match = DOI_PATTERN.search(text or "")
    if not match:
        return None
    value = match.group(0)
    while value and (value[-1] in ".,;:'\"]" or (value[-1] == ")" and value.count("(") < value.count(")"))):
        value = value[:-1]
    return value.lower() or None


def _norm_title(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def resolve_identity(entry: Entry, works: Mapping[str, Mapping[str, Any]], *,
                     numbering_verified: bool = False, by_label: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Identity first: markup DOI/PMID, then printed DOI, then title containment, then a verified label.

    ``works`` maps work keys to records with ``doi``, ``pmid`` and ``title``.
    Classes: resolved (identifier and title agree), resolved_unverified
    (identifier only, or label on verified numbering), unresolved.
    """
    by_doi = {str(w.get("doi") or "").lower(): key for key, w in works.items() if w.get("doi")}
    by_pmid = {str(w.get("pmid")): key for key, w in works.items() if w.get("pmid")}
    printed = _norm_title(entry.printed)
    candidates = []
    doi = (entry.identifiers.get("doi") or "").lower()
    if doi and doi in by_doi:
        candidates.append((by_doi[doi], "markup-doi"))
    pmid = entry.identifiers.get("pmid")
    if pmid and pmid in by_pmid:
        candidates.append((by_pmid[pmid], "markup-pmid"))
    if not candidates:
        found = printed_doi(entry.printed)
        if found and found in by_doi:
            candidates.append((by_doi[found], "printed-doi"))
    if not candidates:
        for key, work in works.items():
            title = _norm_title(work.get("title"))
            if len(title) >= 25 and title[:60] in printed:
                candidates.append((key, "title"))
                break
    if not candidates and numbering_verified and by_label and entry.label in by_label:
        candidates.append((by_label[entry.label], "label"))
    if not candidates:
        return {"workKey": None, "identity": "unresolved", "method": None, "titleAgrees": None}
    keys = {key for key, _ in candidates}
    if len(keys) > 1:
        return {"workKey": None, "identity": "unresolved", "method": "conflict", "titleAgrees": None,
                "conflict": sorted(keys)}
    key, method = candidates[0]
    title = _norm_title(works[key].get("title"))
    agrees = bool(title) and title[:40] in printed
    identity = "resolved" if method == "title" or (method != "label" and agrees) else "resolved_unverified"
    return {"workKey": key, "identity": identity, "method": method, "titleAgrees": agrees}


# ----------------------------------------------------------------------------- review metadata
def corresponding_country(body: bytes, *, html: bool = False) -> dict[str, Any]:
    """Corresponding-author country (else first affiliation); descriptive only."""
    try:
        root = _tree(body, html)
    except (ET.ParseError, UnicodeError, ValueError):
        return {"country": None, "source": None}
    if html:
        for node in root.iter("meta"):
            if node.get("name") == "citation_author_institution" and node.get("content"):
                match = re.search(r",\s*([A-Z][A-Za-z .]+?)\s*$", node.get("content").strip())
                if match:
                    return {"country": normalize_country(match.group(1)), "source": "html-meta-first-institution"}
        return {"country": None, "source": None}
    affiliations = {a.get("id"): a for a in root.iter("aff")}

    def country(aff):
        node = aff.find(".//country")
        if node is not None and "".join(node.itertext()).strip():
            return "".join(node.itertext()).strip()
        text = "".join(aff.itertext()).strip()
        match = re.search(r",\s*([A-Z][A-Za-z .]+?)\s*$", text)
        return match.group(1).strip() if match else None

    for contrib in root.iter("contrib"):
        corresponding = contrib.get("corresp") == "yes" or any(
            x.get("ref-type") == "corresp" for x in contrib.iter("xref"))
        if not corresponding:
            continue
        for xref in contrib.iter("xref"):
            if xref.get("ref-type") == "aff":
                aff = affiliations.get(xref.get("rid"))
                if aff is not None and country(aff):
                    return {"country": normalize_country(country(aff)), "source": "jats-corresponding-affiliation"}
    for aff in affiliations.values():
        if country(aff):
            return {"country": normalize_country(country(aff)), "source": "jats-first-affiliation"}
    return {"country": None, "source": None}
