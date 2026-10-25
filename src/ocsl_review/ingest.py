"""Local manuscript ingestion with stable, auditable location identifiers.

The module deliberately has no network behavior.  PDF text extraction is the
only subprocess boundary and is constrained by an input-size limit, a timeout,
and an extracted-output limit.  DOCX files are Open Packaging Convention ZIP
archives, so their text and simple structure can be read with the standard
library.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import statistics
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import author_year as _author_year
from .errors import ReviewError
from .citations import DASHES, numeric_citations, parse_numeric_group

MANUSCRIPT_SCHEMA_VERSION = "1.0.0"
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 32 * 1024 * 1024
DEFAULT_PDF_TIMEOUT_SECONDS = 30.0

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

_KNOWN_SECTION_TITLES = {
    "abstract",
    "summary",
    "introduction",
    "background",
    "methods",
    "materials and methods",
    "methodology",
    "results",
    "findings",
    "discussion",
    "conclusion",
    "conclusions",
    "limitations",
    "acknowledgements",
    "acknowledgments",
    "references",
    "bibliography",
    "works cited",
    "supplementary methods",
    "supplementary information",
}
_BIBLIOGRAPHY_TITLES = {"references", "bibliography", "works cited"}
_FIGURE_CAPTION = re.compile(
    r"^\s*(?:figure|fig\.)\s*([A-Za-z]?\d+[A-Za-z]?)\s*[:.\-]?\s*(.*)$", re.IGNORECASE
)
_NUMBERED_REFERENCE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)]?)\s+(.+)$")
_BULLET_REFERENCE = re.compile(r"^\s*[-*+\u2022]\s+(?=\S)")
_YEARLIKE_NUMERIC = re.compile(
    r"[\(\[]?\s*(?:1[5-9]|20)\d{2}[a-z]{0,2}(?:\s*[,;]\s*(?:(?:1[5-9]|20)\d{2}[a-z]{0,2}|[a-z]{1,2}))*\s*[\)\]]?"
)
_MARKDOWN_CITATION = re.compile(r"\[(?:@[A-Za-z0-9][A-Za-z0-9_.:+/-]*(?:\s*;\s*)?)+\]")
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s<>\"]+", re.IGNORECASE)
_PMID = re.compile(r"\bPMID\s*:?\s*(\d{5,9})\b", re.IGNORECASE)
_ARXIV = re.compile(r"\barXiv\s*:?\s*([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?\b", re.IGNORECASE)

_IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "orcid": re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b", re.IGNORECASE),
    "phone": re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]?){2,4}\d{2,4}(?!\w)"),
    "doi": _DOI,
    "trial_registration": re.compile(r"\b(?:NCT\d{8}|ISRCTN\d{8}|ACTRN\d{14})\b", re.IGNORECASE),
    "grant_identifier": re.compile(
        r"\b(?:R\d{2}|U\d{2}|P\d{2}|ERC|NSF|NSFC)[-/ ][A-Z0-9][A-Z0-9-]{4,}\b",
        re.IGNORECASE,
    ),
    "url": re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE),
}


class IngestionError(ReviewError):
    """The supplied manuscript could not be ingested without guessing."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        stat = path.stat()
    except OSError as exc:
        raise IngestionError(f"cannot inspect manuscript: {path.name}: {exc}") from exc
    if not path.is_file():
        raise IngestionError(f"manuscript is not a regular file: {path}")
    if stat.st_size > limit:
        raise IngestionError(
            f"manuscript exceeds the configured input limit ({stat.st_size} > {limit} bytes)"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IngestionError(f"cannot read manuscript: {path.name}: {exc}") from exc


def _decode_utf8(data: bytes, warnings: list[dict[str, Any]]) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        warnings.append(
            {
                "code": "text-decoding-loss",
                "message": f"invalid UTF-8 bytes were replaced near byte {exc.start}",
            }
        )
        return data.decode("utf-8-sig", errors="replace")


def _is_heading(line: str, *, markdown: bool) -> tuple[bool, str, int | None]:
    stripped = line.strip()
    if markdown:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        if match:
            return True, match.group(2).strip(), len(match.group(1))
    normalized = re.sub(r"\s+", " ", stripped).strip(" :.\t").casefold()
    if normalized in _KNOWN_SECTION_TITLES:
        return True, stripped.strip(" :.\t"), None
    if 1 <= len(stripped) <= 80 and stripped.isupper() and any(ch.isalpha() for ch in stripped):
        return True, stripped.title(), None
    return False, stripped, None


def _looks_like_markdown_table(lines: Sequence[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    delimiter = lines[index + 1].strip()
    if "|" not in header or "|" not in delimiter:
        return False
    cells = [cell.strip() for cell in delimiter.strip("|").split("|")]
    return len(cells) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _markdown_table_rows(lines: Sequence[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for offset, line in enumerate(lines):
        if offset == 1:
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _text_blocks(text: str, *, markdown: bool) -> list[dict[str, Any]]:
    """Convert decoded text to ordered paragraph/table blocks.

    Form-feed boundaries emitted by ``pdftotext`` are retained as page
    numbers.  Text and Markdown inputs use page ``None``.
    """

    blocks: list[dict[str, Any]] = []
    pages = text.replace("\r\n", "\n").replace("\r", "\n").split("\f")
    has_page_boundaries = len(pages) > 1
    bibliography_mode = False
    for page_offset, page_text in enumerate(pages):
        page = page_offset + 1 if has_page_boundaries else None
        lines = page_text.split("\n")
        paragraph: list[str] = []

        def flush(current_page: int | None = page) -> None:
            nonlocal paragraph
            if not paragraph:
                return
            raw = "\n".join(paragraph)
            value = " ".join(part.strip() for part in paragraph if part.strip()).strip()
            paragraph = []
            if value:
                blocks.append({"kind": "paragraph", "text": value, "rawText": raw, "page": current_page})

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            heading, heading_text, level = _is_heading(line, markdown=markdown)
            if heading:
                flush()
                blocks.append(
                    {
                        "kind": "paragraph",
                        "text": heading_text,
                        "page": page,
                        "heading": True,
                        "headingLevel": level,
                    }
                )
                bibliography_mode = heading_text.casefold() in _BIBLIOGRAPHY_TITLES
                i += 1
                continue
            if markdown and _looks_like_markdown_table(lines, i):
                flush()
                table_lines = [lines[i], lines[i + 1]]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    table_lines.append(lines[i])
                    i += 1
                blocks.append(
                    {
                        "kind": "table",
                        "rows": _markdown_table_rows(table_lines),
                        "page": page,
                    }
                )
                continue
            if not stripped:
                flush()
                i += 1
                continue
            if bibliography_mode and _BULLET_REFERENCE.match(stripped):
                # A bulleted item always starts the next reference entry.
                flush()
                paragraph.append(_BULLET_REFERENCE.sub("", stripped, count=1))
                i += 1
                continue
            if bibliography_mode and (_NUMBERED_REFERENCE.match(stripped) or paragraph):
                if _NUMBERED_REFERENCE.match(stripped):
                    flush()
                    paragraph.append(stripped)
                else:
                    paragraph.append(stripped)
                # A numbered item always starts the next entry; otherwise blank
                # lines remain the unnumbered-entry boundary.
                i += 1
                continue
            if _FIGURE_CAPTION.match(stripped):
                flush()
                blocks.append(
                    {"kind": "paragraph", "text": stripped, "page": page, "caption": True}
                )
                i += 1
                continue
            paragraph.append(stripped)
            i += 1
        flush()
    return blocks


def _docx_paragraph(element: ET.Element) -> tuple[str, str | None, list[str], list[tuple[int, int]]]:
    parts: list[str] = []
    raised: list[tuple[int, int]] = []
    offset = 0
    for run in element.iter(f"{{{_W}}}r"):
        run_parts = []
        for node in run.iter():
            if node.tag == f"{{{_W}}}t":
                run_parts.append(node.text or "")
            elif node.tag == f"{{{_W}}}tab":
                run_parts.append("\t")
            elif node.tag in (f"{{{_W}}}br", f"{{{_W}}}cr"):
                run_parts.append("\n")
        value = "".join(run_parts)
        alignment = run.find(f"./{{{_W}}}rPr/{{{_W}}}vertAlign")
        if alignment is not None and alignment.get(f"{{{_W}}}val") == "superscript" and value:
            raised.append((offset, offset + len(value)))
        parts.append(value)
        offset += len(value)
    style: str | None = None
    style_node = element.find(f"./{{{_W}}}pPr/{{{_W}}}pStyle")
    if style_node is not None:
        style = style_node.attrib.get(f"{{{_W}}}val")
    descriptions: list[str] = []
    for node in element.iter(f"{{{_WP}}}docPr"):
        description = (
            node.attrib.get("descr") or node.attrib.get("title") or node.attrib.get("name")
        )
        if description:
            descriptions.append(description.strip())
    value = "".join(parts)
    leading = len(value) - len(value.lstrip())
    stripped = value.strip()
    spans = [
        (max(0, start - leading), min(len(stripped), end - leading))
        for start, end in raised
        if start - leading < len(stripped) and end > leading
    ]
    return stripped, style, descriptions, spans


def _docx_table(element: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in element.findall(f"./{{{_W}}}tr"):
        values: list[str] = []
        for cell in row.findall(f"./{{{_W}}}tc"):
            pieces: list[str] = []
            for paragraph in cell.findall(f".//{{{_W}}}p"):
                value, _, _, _ = _docx_paragraph(paragraph)
                if value:
                    pieces.append(value)
            values.append("\n".join(pieces))
        rows.append(values)
    return rows


def _docx_blocks(
    data: bytes, *, max_extracted_bytes: int, warnings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise IngestionError("DOCX package is missing word/document.xml") from exc
            if info.file_size > max_extracted_bytes:
                raise IngestionError("DOCX document XML exceeds the configured extraction limit")
            xml = archive.read(info)
            content_parts = {
                name
                for name in archive.namelist()
                if re.fullmatch(
                    r"word/(?:footnotes|endnotes|comments|header\d+|footer\d+)\.xml", name
                )
            }
            if content_parts:
                warnings.append(
                    {
                        "code": "docx-secondary-stories-not-ingested",
                        "message": "content outside the main DOCX story was detected: "
                        + ", ".join(sorted(content_parts)),
                        "affectsCompleteness": True,
                    }
                )
    except zipfile.BadZipFile as exc:
        raise IngestionError("DOCX is not a valid ZIP package") from exc
    if re.search(rb"<!(?:DOCTYPE|ENTITY)\b", xml, re.I):
        # Office Open XML needs no DTD; refusing it keeps the parser and the hidden-text check in agreement.
        raise IngestionError("DOCX document XML declares a DTD or entities; the document is refused")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise IngestionError(f"DOCX document XML is malformed: {exc}") from exc
    body = root.find(f".//{{{_W}}}body")
    if body is None:
        raise IngestionError("DOCX document has no body")
    # Word renders the mc:Choice branch of alternate content; mc:Fallback text is never shown to a reader.
    compatibility = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    dropped = 0
    for alternate in list(root.iter(f"{{{compatibility}}}AlternateContent")):
        for fallback in list(alternate.findall(f"{{{compatibility}}}Fallback")):
            dropped += bool("".join(node.text or "" for node in fallback.iter(f"{{{_W}}}t")).strip())
            alternate.remove(fallback)
    if dropped:
        warnings.append({"code": "docx-fallback-content-ignored",
                         "message": f"{dropped} alternate-content fallback blocks with text were not ingested",
                         "affectsCompleteness": False})
    blocks: list[dict[str, Any]] = []
    if root.findall(".//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath"):
        warnings.append(
            {
                "code": "docx-equations-not-linearized",
                "message": "one or more equations could not be represented as sentence text",
                "affectsCompleteness": True,
            }
        )
    if root.findall(f".//{{{_W}}}ins") or root.findall(f".//{{{_W}}}del"):
        warnings.append(
            {
                "code": "docx-unresolved-revisions",
                "message": "tracked insertions or deletions are present in the DOCX",
                "affectsCompleteness": True,
            }
        )
    for child in body:
        if child.tag == f"{{{_W}}}p":
            text, style, descriptions, superscripts = _docx_paragraph(child)
            heading = bool(style and (style.casefold().startswith("heading") or style == "Title"))
            caption = bool(style and style.casefold() == "caption") and bool(
                _FIGURE_CAPTION.match(text)
            )
            for description in descriptions:
                blocks.append(
                    {
                        "kind": "figure",
                        "description": description,
                        "page": None,
                        "caption": None,
                    }
                )
            if text:
                blocks.append(
                    {
                        "kind": "paragraph",
                        "text": text,
                        "page": None,
                        "heading": heading,
                        "headingLevel": _heading_level(style),
                        "caption": caption,
                        "superscriptSpans": superscripts,
                    }
                )
        elif child.tag == f"{{{_W}}}tbl":
            cell_spans: dict[tuple[int, int], list[tuple[int, int]]] = {}
            for row_index, row in enumerate(child.findall(f"./{{{_W}}}tr")):
                for column, cell in enumerate(row.findall(f"./{{{_W}}}tc")):
                    offset = 0
                    for paragraph in cell.findall(f".//{{{_W}}}p"):
                        value, _, _, spans = _docx_paragraph(paragraph)
                        cell_spans.setdefault((row_index, column), []).extend(
                            (start + offset, end + offset) for start, end in spans
                        )
                        if value:
                            offset += len(value) + 1
            blocks.append({
                "kind": "table", "rows": _docx_table(child), "page": None,
                "cellSuperscriptSpans": cell_spans,
            })
    return blocks


def _heading_level(style: str | None) -> int | None:
    if not style:
        return None
    match = re.search(r"(\d+)$", style)
    return int(match.group(1)) if match else None


def _pdf_blocks(
    path: Path,
    *,
    executable: str | os.PathLike[str],
    timeout: float,
    max_extracted_bytes: int,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if timeout <= 0:
        raise IngestionError("PDF extraction timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="ocsl-review-pdf-") as temporary:
        output = Path(temporary) / "manuscript.xhtml"
        argv = [
            os.fspath(executable),
            "-bbox-layout",
            "-enc",
            "UTF-8",
            os.fspath(path),
            os.fspath(output),
        ]
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise IngestionError(f"PDF extractor is not available: {executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise IngestionError(f"PDF extraction exceeded {timeout:g} seconds") from exc
        except OSError as exc:
            raise IngestionError(f"could not start PDF extractor: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            detail = detail[-800:] if detail else "no diagnostic output"
            raise IngestionError(f"PDF extraction failed with status {result.returncode}: {detail}")
        if not output.is_file():
            raise IngestionError("PDF extractor returned success without producing text")
        if output.stat().st_size > max_extracted_bytes:
            raise IngestionError("PDF text exceeds the configured extraction limit")
        data = output.read_bytes()
        if not data.strip():
            raise IngestionError("PDF contains no extractable text; OCR is required")
        decoding_warnings: list[dict[str, Any]] = []
        text = _decode_utf8(data, decoding_warnings)
        if decoding_warnings:
            raise IngestionError("PDF extractor emitted invalid UTF-8")
        return _bbox_blocks(text, warnings)


def _bbox_blocks(text: str, warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read Poppler word geometry, retaining columns and locally raised runs.

    Poppler's flow order supplies paragraphs and columns. A raised run can be
    emitted as its own line/block; it is attached only to a nearby baseline with
    a preceding word, never by concatenating all numeric spans on a page.
    """

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise IngestionError("PDF extractor did not produce valid bbox-layout XML") from exc
    pages = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "page"]
    if not pages:
        raise IngestionError("PDF bbox extraction contains no pages")
    all_lines: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, start=1):
        try:
            width, height = float(page.attrib["width"]), float(page.attrib["height"])
            if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                raise ValueError("invalid page dimensions")
            block_number = 0
            for node in page.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag == "block":
                    block_number += 1
                if tag != "line":
                    continue
                words = []
                for word in node:
                    if word.tag.rsplit("}", 1)[-1] != "word":
                        continue
                    box = [float(word.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax")]
                    if not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]:
                        raise ValueError("invalid word dimensions")
                    value = "".join(word.itertext())
                    if value:
                        words.append({"text": value, "box": box, "raised": False})
                if words:
                    all_lines.append({
                        "page": page_number, "block": block_number, "words": words,
                        "width": width, "height": height,
                    })
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestionError("PDF bbox extraction contains invalid geometry") from exc
    if not all_lines:
        raise IngestionError("PDF contains no extractable words; OCR is required")
    observed_pages = {line["page"] for line in all_lines}
    if len(observed_pages) != len(pages):
        warnings.append({
            "code": "pdf-pages-without-text", "message": "one or more PDF pages contain no words",
            "pages": sorted(set(range(1, len(pages) + 1)) - observed_pages),
            "affectsCompleteness": True,
        })

    # Repeated page chrome is recognized by text AND position. No blanket page
    # band is removed: a reference continuing near a page edge remains intact.
    chrome_pages: dict[str, set[int]] = {}
    for line in all_lines:
        words = line["words"]
        near_edge = min(w["box"][1] for w in words) < line["height"] * .07 or max(
            w["box"][3] for w in words
        ) > line["height"] * .93
        raw = " ".join(w["text"] for w in words)
        structural_content = (
            _NUMBERED_REFERENCE.match(raw) or _FIGURE_CAPTION.match(raw)
            or re.match(r"^Table\s+\d+\b", raw, re.I)
            or _is_heading(raw, markdown=False)[0]
        )
        if near_edge and not structural_content:
            key = re.sub(r"\d+", "#", raw).casefold()
            chrome_pages.setdefault(key, set()).add(line["page"])
            line["chromeKey"] = key
    retained = [
        line for line in all_lines
        if len(chrome_pages.get(line.get("chromeKey", ""), set())) < 2
    ]
    removed = len(all_lines) - len(retained)
    if removed:
        warnings.append({"code": "pdf-repeated-chrome-removed", "message": "repeated page chrome removed", "lines": removed})
    all_lines = retained
    # A new section beginning in the left column applies across the page.
    # Poppler can emit the complete left flow (including References) before the
    # right body column; partition by those section boundaries before retaining
    # the flow order within each band.
    section_bands: dict[int, list[float]] = {}
    for line in all_lines:
        value = " ".join(word["text"] for word in line["words"])
        if _is_heading(value, markdown=False)[0] and min(w["box"][0] for w in line["words"]) < line["width"] / 3:
            section_bands.setdefault(line["page"], []).append(min(w["box"][1] for w in line["words"]))
    all_lines.sort(key=lambda line: (
        line["page"], sum(y <= min(w["box"][1] for w in line["words"]) for y in section_bands.get(line["page"], []))
    ))
    lines_by_page: dict[int, list[dict[str, Any]]] = {}
    for line in all_lines:
        lines_by_page.setdefault(line["page"], []).append(line)
    numeric_word = re.compile(rf"^[0-9⁰¹²³⁴⁵⁶⁷⁸⁹,;{re.escape(DASHES)}]+$")
    for line in all_lines:
        for word in list(line["words"]):
            if not numeric_word.fullmatch(word["text"]) or not re.search(
                r"[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]", word["text"]
            ):
                continue
            x0, y0, x1, y1 = word["box"]
            word_height = y1 - y0
            neighbors = []
            for other in lines_by_page[line["page"]]:
                for body_word in other["words"]:
                    if not re.search(r"[A-Za-z]{2}", body_word["text"]) or body_word.get("raised"):
                        continue
                    bx0, by0, bx1, by1 = body_word["box"]
                    bh = by1 - by0
                    gap = x0 - bx1
                    if -.2 * bh <= gap <= 2.5 * bh and .1 * bh < by1 - y1 < .9 * bh and word_height < .9 * bh:
                        neighbors.append((abs(gap), other, body_word))
            if neighbors:
                _, target, _ = min(neighbors, key=lambda item: item[0])
                word["raised"] = True
                if target is not line:
                    line["words"].remove(word)
                    target["words"].append(word)
    # Join the rest of a raised digit/list/range run using local geometry. A
    # baseline-sized dash may bridge smaller digit words without swallowing an
    # adjacent column or treating ordinary small table text as a superscript.
    while True:
        joined = False
        for line in all_lines:
            for word in list(line["words"]):
                if word["raised"] or not (numeric_word.fullmatch(word["text"]) or word["text"].casefold() == "and"):
                    continue
                x0, y0, x1, y1 = word["box"]
                matches = []
                for target in lines_by_page[line["page"]]:
                    for prior in target["words"]:
                        if not prior["raised"]:
                            continue
                        px0, py0, px1, py1 = prior["box"]
                        h = max(y1 - y0, py1 - py0)
                        if -.15 * h <= x0 - px1 <= .8 * h and abs(y0 - py0) < .6 * h:
                            matches.append((abs(x0 - px1), target))
                if matches:
                    _, target = min(matches, key=lambda item: item[0])
                    word["raised"] = True
                    joined = True
                    if target is not line:
                        line["words"].remove(word)
                        target["words"].append(word)
        if not joined:
            break

    table_blocks, table_lines = _bbox_tables(all_lines, warnings)
    blocks: list[dict[str, Any]] = []
    current_key = None
    for line in all_lines:
        if id(line) in table_blocks:
            blocks.append(table_blocks[id(line)])
            current_key = None
        if id(line) in table_lines:
            continue
        words = sorted(line["words"], key=lambda item: item["box"][0])
        if not words:
            continue
        value = ""
        spans = []
        positions = []
        prior = None
        for word in words:
            if value:
                gap = word["box"][0] - prior["box"][2]
                height = word["box"][3] - word["box"][1]
                # Keep token adjacency from geometry. Do not insert a space
                # inside a superscript range split across font runs.
                if not (word["raised"] and prior["raised"] and gap < .3 * height):
                    value += " "
            start = len(value)
            value += word["text"]
            if word["raised"]:
                spans.append((start, len(value)))
            positions.append({"start": start, "end": len(value), "bbox": word["box"]})
            prior = word
        heading, title, level = _is_heading(value, markdown=False)
        key = (line["page"], line["block"])
        separate = heading or _NUMBERED_REFERENCE.match(value) or _FIGURE_CAPTION.match(value)
        if blocks and current_key == key and not separate and not blocks[-1].get("heading"):
            block = blocks[-1]
            offset = len(block["text"]) + 1
            block["text"] += " " + value
            block["superscriptSpans"].extend((a + offset, b + offset) for a, b in spans)
            block["wordPositions"].extend(
                {**item, "start": item["start"] + offset, "end": item["end"] + offset}
                for item in positions
            )
        else:
            blocks.append({
                "kind": "paragraph", "text": title if heading else value,
                "page": line["page"], "heading": heading, "headingLevel": level,
                "superscriptSpans": spans, "wordPositions": positions,
            })
        current_key = key
    # Word geometry cannot recover arbitrary ruled/merged PDF tables. Keep the
    # text, and block completeness where a table needs a structural re-ingest.
    table_captions = sum(
        bool(re.match(r"^Table\s+\d+\b", b.get("text", ""), re.I)) for b in blocks
    )
    if table_captions > len(table_blocks):
        warnings.append({
            "code": "pdf-table-structure-unresolved",
            "message": "PDF table text requires row/cell confirmation or a structural source",
            "affectsCompleteness": True,
        })
    return blocks


def _bbox_tables(
    lines: list[dict[str, Any]], warnings: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Recover aligned tables with an explicit reference column.

    Column starts come from separated header cells, and rows from shared text
    baselines. We require reference-column grammar and never guess a merged-cell
    or unlabelled table's row-to-source assignment.
    """

    tables: dict[int, dict[str, Any]] = {}
    consumed: set[int] = set()
    header_word = re.compile(r"(?:refs?|references?|citations?)\.?", re.I)
    for header_line in lines:
        if id(header_line) in consumed or not any(header_word.fullmatch(w["text"]) for w in header_line["words"]):
            continue
        page_lines = [line for line in lines if line["page"] == header_line["page"] and line["words"]]
        header_y = statistics.median(w["box"][3] for w in header_line["words"])
        height = statistics.median(w["box"][3] - w["box"][1] for w in header_line["words"])
        same_row = [line for line in page_lines if abs(statistics.median(w["box"][3] for w in line["words"]) - header_y) < .35 * height]
        words = sorted([w for line in same_row for w in line["words"]], key=lambda w: w["box"][0])
        groups: list[list[dict[str, Any]]] = []
        for word in words:
            if not groups or word["box"][0] - groups[-1][-1]["box"][2] > 1.5 * height:
                groups.append([word])
            else:
                groups[-1].append(word)
        headers = [" ".join(w["text"] for w in group) for group in groups]
        reference_columns = [i for i, value in enumerate(headers) if header_word.fullmatch(value)]
        if len(groups) < 2 or len(reference_columns) != 1:
            continue
        reference_column = reference_columns[0]
        starts = [group[0]["box"][0] for group in groups]
        boundaries = [(groups[i][-1]["box"][2] + starts[i + 1]) / 2 for i in range(len(groups) - 1)]
        candidates = sorted(
            [line for line in page_lines if min(w["box"][1] for w in line["words"]) > header_y],
            key=lambda line: (min(w["box"][1] for w in line["words"]), min(w["box"][0] for w in line["words"])),
        )
        visual_rows: list[list[dict[str, Any]]] = []
        last_y = header_y
        for line in candidates:
            y = statistics.median(w["box"][3] for w in line["words"])
            content = " ".join(w["text"] for w in line["words"])
            if y - last_y > 3 * height or _is_heading(content, markdown=False)[0] or re.match(r"^(?:Table|Figure)\s+\d+", content, re.I):
                break
            if visual_rows and abs(y - last_y) < .4 * height:
                visual_rows[-1].append(line)
            else:
                visual_rows.append([line])
                last_y = y
        rows = [headers]
        accepted_lines = list(same_row)
        uncertain = False
        for visual in visual_rows:
            row_words = sorted([word for line in visual for word in line["words"]], key=lambda word: word["box"][0])
            cells: list[list[str]] = [[] for _ in headers]
            if any(word["box"][0] < starts[0] - height for word in row_words):
                break
            for word in row_words:
                column = sum(word["box"][0] >= boundary for boundary in boundaries)
                cells[column].append(word["text"])
            values = [" ".join(cell) for cell in cells]
            reference_text = values[reference_column]
            tokens, issues, _ = parse_numeric_group(reference_text)
            dangling = len(rows) > 1 and re.search(
                r"(?:and|[,;" + re.escape(DASHES) + r"])\s*$",
                rows[-1][reference_column],
            )
            if reference_text and tokens and not (set(issues) - {"compressed-citation-range-unconfirmed"}):
                # Reference-only continuation after a dangling separator is the
                # same printed row, rather than a second citation occurrence.
                continuation = not values[0] if reference_column else not any(values[1:])
                if dangling and continuation:
                    for column, value in enumerate(values):
                        if value:
                            rows[-1][column] += " " + value
                else:
                    rows.append(values)
                accepted_lines.extend(visual)
            elif not reference_text and len(rows) > 1 and not values[0]:
                for column, value in enumerate(values):
                    if value:
                        rows[-1][column] += " " + value
                accepted_lines.extend(visual)
            elif reference_text and re.fullmatch(r"[\d,;\s" + re.escape(DASHES) + r"]+(?:and)?", reference_text):
                rows.append(values)
                accepted_lines.extend(visual)
            else:
                uncertain = True
                break
        if len(rows) < 2:
            continue
        if any(
            set(parse_numeric_group(row[reference_column])[1])
            - {"compressed-citation-range-unconfirmed"}
            for row in rows[1:]
        ):
            uncertain = True
        tables[id(header_line)] = {"kind": "table", "rows": rows, "page": header_line["page"]}
        consumed.update(id(line) for line in accepted_lines)
        if uncertain:
            warnings.append({
                "code": "pdf-table-row-ambiguity", "message": "table row boundaries or reference cells require confirmation",
                "page": header_line["page"], "affectsCompleteness": True,
            })
    return tables, consumed


def _sentence_spans(
    text: str, citation_spans: Iterable[tuple[int, int]] = (),
) -> Iterator[tuple[int, int, str]]:
    if not text.strip():
        return
    markers = sorted(citation_spans)
    start = 0
    for match in re.finditer(r"(?:[.!?][\]\)\"']*|[。！？])", text):
        if match.start() < start or any(a <= match.start() < b for a, b in markers):
            continue
        if match.group().startswith(".") and re.search(
            r"\b(?:fig|eq|dr|prof|vs|al|e\.g|i\.e)$", text[:match.start()], re.I
        ):
            continue
        end = match.end()
        for marker_start, marker_end in markers:
            if marker_start >= end and not text[end:marker_start].strip():
                end = marker_end
                if end < len(text) and text[end] in ".!?":
                    end += 1
        if end < len(text) and not text[end].isspace():
            continue
        value = text[start:end].strip()
        if value:
            value_start = text.find(value, start, end)
            yield value_start, value_start + len(value), value
        start = end
    value = text[start:].strip()
    if value:
        value_start = text.find(value, start)
        yield value_start, value_start + len(value), value


def _citation_matches(
    text: str, *, superscript_spans: Iterable[tuple[int, int]] = (),
) -> list[tuple[int, int, str, str, Any]]:
    """Citation markers as (start, end, raw, style, author-year marker or None)."""

    candidates: list[tuple[int, int, str, str, Any]] = [
        (marker.start, marker.end, marker.raw, "numeric", None)
        for marker in numeric_citations(text, superscript_spans=superscript_spans)
    ]
    for match in _MARKDOWN_CITATION.finditer(text):
        candidates.append((match.start(), match.end(), match.group(0), "markdown-key", None))
    author_year = [
        (marker.start, marker.end, marker.raw, "author-year", marker)
        for marker in _author_year.detect(text)
    ]
    # "Smith (2019, 2020)" is an author-year marker, not two numeric labels.
    candidates = [
        candidate for candidate in candidates
        if not (candidate[3] == "numeric" and _YEARLIKE_NUMERIC.fullmatch(candidate[2].strip())
                and any(start <= candidate[0] and candidate[1] <= end for start, end, *_ in author_year))
    ]
    candidates.extend(author_year)
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str, str, Any]] = []
    for candidate in candidates:
        if any(candidate[0] < prior[1] and prior[0] < candidate[1] for prior in accepted):
            continue
        accepted.append(candidate)
    return accepted


def _citation_tokens(raw: str, style: str) -> list[str]:
    if style == "markdown-key":
        return re.findall(r"@([A-Za-z0-9][A-Za-z0-9_.:+/-]*)", raw)
    if style == "numeric":
        return list(parse_numeric_group(raw)[0])
    return [part.strip() for part in raw.strip("()").split(";") if part.strip()]


def _reference_identifiers(text: str) -> dict[str, list[str]]:
    dois = [match.group(0).rstrip(".,;)") for match in _DOI.finditer(text)]
    pmids = [match.group(1) for match in _PMID.finditer(text)]
    arxiv = [match.group(1) for match in _ARXIV.finditer(text)]
    return {"doi": dois, "pmid": pmids, "arxiv": arxiv}


def _make_locator(
    *,
    section_id: str,
    paragraph_id: str | None = None,
    sentence_number: int | None = None,
    table_id: str | None = None,
    figure_id: str | None = None,
    page: int | None = None,
    character_start: int | None = None,
    character_end: int | None = None,
    row: int | None = None,
    column: int | None = None,
) -> dict[str, Any]:
    locator: dict[str, Any] = {"sectionId": section_id, "page": page}
    if paragraph_id is not None:
        locator["paragraphId"] = paragraph_id
    if sentence_number is not None:
        locator["sentenceNumber"] = sentence_number
    if table_id is not None:
        locator["tableId"] = table_id
    if figure_id is not None:
        locator["figureId"] = figure_id
    if character_start is not None:
        locator["characterStart"] = character_start
    if character_end is not None:
        locator["characterEnd"] = character_end
    if row is not None:
        locator["row"] = row
    if column is not None:
        locator["column"] = column
    return locator


def _assemble(
    *,
    source: Path,
    source_data: bytes,
    source_format: str,
    parser: str,
    blocks: Sequence[dict[str, Any]],
    confidential: bool,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    paragraphs: list[dict[str, Any]] = []
    sentences: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    bibliography: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    bibliography_mode = False

    def create_section(title: str, heading_paragraph_id: str | None = None) -> dict[str, Any]:
        item = {
            "id": f"sec-{len(sections) + 1:03d}",
            "ordinal": len(sections) + 1,
            "title": title,
            "headingParagraphId": heading_paragraph_id,
            "paragraphIds": [],
            "tableIds": [],
            "figureIds": [],
        }
        sections.append(item)
        return item

    def need_section() -> dict[str, Any]:
        nonlocal current_section
        if current_section is None:
            current_section = create_section("Front matter")
        return current_section

    for block in blocks:
        kind = block["kind"]
        if kind == "paragraph":
            text = block.get("text", "").strip()
            if not text:
                continue
            paragraph_id = f"p-{len(paragraphs) + 1:06d}"
            is_heading = bool(block.get("heading"))
            if not is_heading and not block.get("sourceIdentity") and not block.get("sourceLocator"):
                detected, heading_text, level = _is_heading(text, markdown=False)
                if detected:
                    is_heading = True
                    text = heading_text
                    block = {**block, "headingLevel": block.get("headingLevel") or level}
            if is_heading:
                current_section = create_section(text, paragraph_id)
                bibliography_mode = text.casefold() in _BIBLIOGRAPHY_TITLES
            section = need_section()
            paragraph_kind = "heading" if is_heading else "body"
            if block.get("caption") or _FIGURE_CAPTION.match(text):
                paragraph_kind = "figure-caption"
            if bibliography_mode and not is_heading:
                paragraph_kind = "bibliography-entry"
            paragraph = {
                "id": paragraph_id,
                "ordinal": len(paragraphs) + 1,
                "sectionId": section["id"],
                "kind": paragraph_kind,
                "text": text,
                "rawText": block.get("rawText", block.get("text", "")),
                "locator": _make_locator(
                    section_id=section["id"], paragraph_id=paragraph_id, page=block.get("page")
                ),
                "sentenceIds": [],
            }
            paragraphs.append(paragraph)
            if block.get("sourceLocator"):
                paragraph["sourceLocator"] = block["sourceLocator"]
            section["paragraphIds"].append(paragraph_id)
            if paragraph_kind == "figure-caption":
                caption_match = _FIGURE_CAPTION.match(text)
                if (
                    figures
                    and figures[-1]["sectionId"] == section["id"]
                    and figures[-1]["caption"] is None
                ):
                    figures[-1]["caption"] = text
                    figures[-1]["label"] = caption_match.group(1) if caption_match else None
                    figures[-1]["locator"]["paragraphId"] = paragraph_id
                else:
                    figure_id = f"fig-{len(figures) + 1:04d}"
                    figure = {
                        "id": figure_id,
                        "ordinal": len(figures) + 1,
                        "sectionId": section["id"],
                        "caption": text,
                        "label": caption_match.group(1) if caption_match else None,
                        "description": None,
                        "locator": _make_locator(
                            section_id=section["id"],
                            paragraph_id=paragraph_id,
                            figure_id=figure_id,
                            page=block.get("page"),
                        ),
                    }
                    figures.append(figure)
                    section["figureIds"].append(figure_id)
            if paragraph_kind == "bibliography-entry":
                match = _NUMBERED_REFERENCE.match(text)
                if (
                    match is None and bibliography and bibliography[-1]["label"]
                    and bibliography[-1]["locator"]["sectionId"] == section["id"]
                ):
                    # Numbered references may continue in another flow or on
                    # the next page. Preserve every contributing paragraph
                    # instead of inventing an additional unlabelled reference.
                    reference = bibliography[-1]
                    reference["raw"] += "\n" + text
                    reference["paragraphIds"].append(paragraph_id)
                    reference["identifiers"] = _reference_identifiers(reference["raw"])
                else:
                    bibliography.append({
                        "id": f"ref-{len(bibliography) + 1:05d}",
                        "ordinal": len(bibliography) + 1,
                        "label": (match.group(1) or match.group(2)) if match else None,
                        "raw": match.group(3) if match else text,
                        "paragraphId": paragraph_id,
                        "paragraphIds": [paragraph_id],
                        "identifiers": _reference_identifiers(text),
                        "locator": paragraph["locator"],
                    })
                    if block.get("sourceIdentity"):
                        bibliography[-1]["sourceIdentity"] = block["sourceIdentity"]
                        bibliography[-1]["identifiers"].update({key: [value] for key, value in block["sourceIdentity"]["identifiers"].items()})
            if not is_heading:
                if paragraph_kind == "bibliography-entry":
                    paragraph_citations = []
                elif "explicitCitations" in block:
                    paragraph_citations = [(c["start"], c["end"], c["raw"], c["style"], None)
                                           for c in block["explicitCitations"]]
                else:
                    paragraph_citations = _citation_matches(text, superscript_spans=block.get("superscriptSpans", []))
                for sentence_number, (start, end, sentence_text) in enumerate(
                    _sentence_spans(text, [(a, b) for a, b, *_ in paragraph_citations]), start=1
                ):
                    sentence_id = f"s-{len(sentences) + 1:07d}"
                    locator = _make_locator(
                        section_id=section["id"],
                        paragraph_id=paragraph_id,
                        sentence_number=sentence_number,
                        page=block.get("page"),
                        character_start=start,
                        character_end=end,
                    )
                    sentence = {
                        "id": sentence_id,
                        "ordinal": len(sentences) + 1,
                        "paragraphId": paragraph_id,
                        "sectionId": section["id"],
                        "text": sentence_text,
                        "locator": locator,
                        "citationClusterIds": [],
                    }
                    sentences.append(sentence)
                    paragraph["sentenceIds"].append(sentence_id)
                    sentence_citations = [
                        (a - start, b - start, raw, style, marker)
                        for a, b, raw, style, marker in paragraph_citations if start <= a and b <= end
                    ]
                    for local_start, local_end, raw, style, marker in sentence_citations:
                        citation_id = f"cite-{len(citations) + 1:06d}"
                        citation = {
                            "id": citation_id,
                            "ordinal": len(citations) + 1,
                            "style": style,
                            "raw": raw,
                            "tokens": _citation_tokens(raw, style),
                            "locusType": "figure-caption" if paragraph_kind == "figure-caption" else "prose",
                            "sentenceId": sentence_id,
                            "paragraphId": paragraph_id,
                            "locator": _make_locator(
                                section_id=section["id"],
                                paragraph_id=paragraph_id,
                                sentence_number=sentence_number,
                                page=block.get("page"),
                                character_start=start + local_start,
                                character_end=start + local_end,
                            ),
                        }
                        if style == "structured":
                            original = next(c for c in block["explicitCitations"] if c["start"] == start + local_start)
                            citation["sourceTargets"] = original["targets"]
                            citation["sourceMarker"] = original["sourceMarker"]
                            citation["sourceAnchors"] = original["sourceAnchors"]
                        if marker is not None:
                            citation["_authorYearMarker"] = marker
                        positions = [
                            word["bbox"] for word in block.get("wordPositions", [])
                            if word["start"] < start + local_end and start + local_start < word["end"]
                        ]
                        if positions:
                            citation["boundingBoxes"] = positions
                        citations.append(citation)
                        sentence["citationClusterIds"].append(citation_id)
        elif kind == "table":
            section = need_section()
            table_id = f"tbl-{len(tables) + 1:04d}"
            rows = [[str(cell) for cell in row] for row in block.get("rows", [])]
            table = {
                "id": table_id,
                "ordinal": len(tables) + 1,
                "sectionId": section["id"],
                "rows": rows,
                "rowCount": len(rows),
                "columnCount": max((len(row) for row in rows), default=0),
                "locator": _make_locator(
                    section_id=section["id"], table_id=table_id, page=block.get("page")
                ),
                "citationClusterIds": [],
            }
            tables.append(table)
            if block.get("sourceLocator"):
                table["sourceLocator"] = block["sourceLocator"]
            section["tableIds"].append(table_id)
            for row_index, row in enumerate(rows):
                for column, cell in enumerate(row):
                    if "explicitCellCitations" in block:
                        matches = [(c["start"], c["end"], c["raw"], c["style"], None)
                                   for c in block["explicitCellCitations"].get((row_index, column), [])]
                    else:
                        matches = _citation_matches(cell, superscript_spans=block.get("cellSuperscriptSpans", {}).get((row_index, column), []))
                    # A labelled reference column may print undecorated lists.
                    if row_index and column < len(rows[0]) and re.fullmatch(
                        r"(?:refs?|references?|citations?)\.?", rows[0][column].strip(), re.I
                    ) and not matches and re.search(r"\d", cell):
                        matches = [(0, len(cell), cell, "numeric", None)]
                    for start, end, raw, style, marker in matches:
                        citation_id = f"cite-{len(citations) + 1:06d}"
                        citations.append({
                            "id": citation_id, "ordinal": len(citations) + 1,
                            "style": style, "raw": raw, "tokens": _citation_tokens(raw, style),
                            "locusType": "table-cell", "sentenceId": None, "paragraphId": None,
                            "locator": _make_locator(
                                section_id=section["id"], table_id=table_id, page=block.get("page"),
                                row=row_index + 1, column=column + 1,
                                character_start=start, character_end=end,
                            ),
                        })
                        if style == "structured":
                            original = next(c for c in block["explicitCellCitations"][(row_index, column)] if c["start"] == start)
                            citations[-1]["sourceTargets"] = original["targets"]
                            citations[-1]["sourceMarker"] = original["sourceMarker"]
                            citations[-1]["sourceAnchors"] = original["sourceAnchors"]
                        if marker is not None:
                            citations[-1]["_authorYearMarker"] = marker
                        table["citationClusterIds"].append(citation_id)
        elif kind == "figure":
            section = need_section()
            figure_id = f"fig-{len(figures) + 1:04d}"
            figures.append(
                {
                    "id": figure_id,
                    "ordinal": len(figures) + 1,
                    "sectionId": section["id"],
                    "caption": block.get("caption"),
                    "label": None,
                    "description": block.get("description"),
                    "locator": _make_locator(
                        section_id=section["id"], figure_id=figure_id, page=block.get("page")
                    ),
                }
            )
            section["figureIds"].append(figure_id)

    by_label: dict[str, list[str]] = {}
    for reference in bibliography:
        if reference["label"]:
            by_label.setdefault(reference["label"], []).append(reference["id"])
    duplicates = sorted(label for label, entries in by_label.items() if len(entries) > 1)
    if duplicates:
        warnings.append({
            "code": "duplicate-bibliography-labels", "message": "printed reference labels are not unique",
            "labels": duplicates, "affectsCompleteness": True,
        })
    labels = set(by_label)
    numeric_labels = sorted(int(label) for label in labels if label.isdecimal())
    if numeric_labels and numeric_labels[-1] <= 100_000:
        gaps = sorted(set(range(1, numeric_labels[-1] + 1)) - set(numeric_labels))
        if gaps:
            warnings.append({
                "code": "bibliography-numbering-gaps",
                "message": "reference numbering has gaps; confirm manuscript numbering and extraction",
                "labels": gaps, "affectsCompleteness": True,
            })
    reference_index: _author_year.ReferenceIndex | None = None
    author_year_warnings: list[dict[str, Any]] = []
    for citation in citations:
        issues: list[str] = []
        if citation["style"] == "structured":
            identities = {r["sourceIdentity"]["targetId"]: r["id"] for r in bibliography if r.get("sourceIdentity")}
            citation["tokens"] = citation["sourceTargets"]
            citation["referenceIds"] = [identities[t] for t in citation["sourceTargets"] if t in identities]
            citation["unresolvedMarkers"] = [t for t in citation["sourceTargets"] if t not in identities]
            citation["bindingStatus"] = "ambiguous" if citation["unresolvedMarkers"] else "exact"
            if citation["unresolvedMarkers"]:
                warnings.append({"code": "unresolved-structured-citation", "message": "explicit source target is absent from bibliography",
                                 "citationId": citation["id"], "affectsCompleteness": True})
        elif citation["style"] == "numeric":
            tokens, parse_issues, _ = parse_numeric_group(citation["raw"], labels=labels)
            citation["tokens"] = list(tokens)
            issues.extend(parse_issues)
            unresolved = [token for token in tokens if len(by_label.get(token, [])) != 1]
            if not tokens or parse_issues:
                unresolved.append(citation["raw"])
            citation["referenceIds"] = [
                by_label[token][0] for token in tokens if len(by_label.get(token, [])) == 1
            ]
            citation["unresolvedMarkers"] = list(dict.fromkeys(unresolved))
            citation["bindingStatus"] = "exact" if not unresolved else "ambiguous"
            if unresolved:
                warnings.append({
                    "code": "unresolved-numeric-citation", "message": "citation requires label or syntax reconciliation",
                    "citationId": citation["id"], "markers": unresolved, "issues": issues,
                    "affectsCompleteness": True,
                })
        elif citation["style"] == "author-year" and citation.get("_authorYearMarker") is not None:
            marker = citation.pop("_authorYearMarker")
            if reference_index is None:
                reference_index = _author_year.ReferenceIndex(bibliography)
            resolutions = [reference_index.resolve(item) for item in marker.items]
            record = _author_year.marker_record(marker, resolutions)
            tokens = [_author_year.item_token(item) for item in record["items"]]
            resolved = list(dict.fromkeys(item.reference_id for item in resolutions
                                          if item.status == "resolved" and item.reference_id))
            unresolved = [token for token, item in zip(tokens, resolutions) if item.status != "resolved"]
            citation["authorYear"] = record
            citation["tokens"] = tokens
            citation["referenceIds"] = resolved
            citation["unresolvedMarkers"] = list(dict.fromkeys(unresolved))
            if not unresolved:
                citation["bindingStatus"] = "exact"
            elif resolved or any(item.status == "ambiguous" for item in resolutions):
                citation["bindingStatus"] = "ambiguous"
            else:
                citation["bindingStatus"] = "unresolved"
            if not marker.strong and not resolved:
                # A bare "Name (2020)" that matches no reference is ordinary prose, not a citation.
                citation["_drop"] = True
            elif unresolved:
                author_year_warnings.append(citation)
        else:
            citation.pop("_authorYearMarker", None)
            citation["referenceIds"] = []
            citation["unresolvedMarkers"] = list(citation["tokens"])
            citation["bindingStatus"] = "unresolved"
        citation["parseIssues"] = issues
    if any(citation.get("_drop") for citation in citations):
        kept = [citation for citation in citations if not citation.get("_drop")]
        renamed = {citation["id"]: f"cite-{ordinal:06d}" for ordinal, citation in enumerate(kept, 1)}
        for ordinal, citation in enumerate(kept, 1):
            citation["id"], citation["ordinal"] = renamed[citation["id"]], ordinal
        for container in [*sentences, *tables]:
            container["citationClusterIds"] = [renamed[item] for item in container["citationClusterIds"] if item in renamed]
        for warning in warnings:
            if warning.get("citationId") in renamed:
                warning["citationId"] = renamed[warning["citationId"]]
        citations[:] = kept
    for citation in author_year_warnings:
        warnings.append({
            "code": "unresolved-author-year-citation",
            "message": "author-year citation does not match exactly one reference-list entry",
            "citationId": citation["id"], "markers": citation["unresolvedMarkers"],
            "affectsCompleteness": False,
        })

    complete = not any(bool(item.get("affectsCompleteness")) for item in warnings)
    if not paragraphs:
        complete = False
        warnings.append({"code": "no-prose", "message": "no manuscript paragraphs were extracted"})
    replacement_count = sum(item["text"].count("\ufffd") for item in paragraphs)
    if replacement_count:
        complete = False
        warnings.append(
            {
                "code": "replacement-characters",
                "message": f"extracted text contains {replacement_count} replacement characters",
            }
        )
    if citations and not bibliography:
        complete = False
        warnings.append(
            {
                "code": "bibliography-not-extracted",
                "message": "citation markers were found but no bibliography entries were extracted",
            }
        )
    bibliography_heading = any(
        section["title"].casefold() in _BIBLIOGRAPHY_TITLES for section in sections
    )
    if bibliography_heading and not bibliography:
        complete = False
        warnings.append(
            {
                "code": "empty-bibliography-section",
                "message": "a bibliography heading was found without extractable entries",
            }
        )
    page_values = [
        item["locator"].get("page")
        for item in [*paragraphs, *tables, *figures]
        if item["locator"].get("page") is not None
    ]
    manuscript_id = f"ms-{_sha256(source_data)[:20]}"
    return {
        "schemaVersion": MANUSCRIPT_SCHEMA_VERSION,
        "manuscriptId": manuscript_id,
        "confidential": bool(confidential),
        "source": {
            "fileName": source.name,
            "format": source_format,
            "byteLength": len(source_data),
            "sha256": _sha256(source_data),
        },
        "sections": sections,
        "paragraphs": paragraphs,
        "sentences": sentences,
        "tables": tables,
        "figures": figures,
        "citationClusters": citations,
        "bibliographyEntries": bibliography,
        "ingestion": {
            "complete": complete,
            "status": "complete" if complete else "partial",
            "parser": parser,
            "warnings": warnings,
            "errors": [],
            "coverage": {
                "inputBytes": len(source_data),
                "extractedCharacters": sum(len(item["text"]) for item in paragraphs),
                "pagesObserved": max(page_values, default=0),
                "sections": len(sections),
                "paragraphs": len(paragraphs),
                "sentences": len(sentences),
                "tables": len(tables),
                "figures": len(figures),
                "citationClusters": len(citations),
                "bibliographyEntries": len(bibliography),
                "numericMarkersDetected": sum(item["style"] == "numeric" for item in citations),
                "numericMarkersReconciled": sum(item["style"] == "numeric" and item["bindingStatus"] == "exact" for item in citations),
                "citationReferenceLinks": sum(len(item["referenceIds"]) for item in citations),
                "distinctCitedReferences": len({reference for item in citations for reference in item["referenceIds"]}),
                "tableCitationLoci": sum(item["locusType"] == "table-cell" for item in citations),
            },
        },
    }


def ingest_manuscript(
    path: str | os.PathLike[str],
    *,
    confidential: bool = True,
    pdftotext: str | os.PathLike[str] = "pdftotext",
    pdf_timeout: float = DEFAULT_PDF_TIMEOUT_SECONDS,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> dict[str, Any]:
    """Ingest TXT, Markdown, DOCX, or PDF into the manuscript schema.

    No manuscript content is logged or sent over the network.  Errors are
    explicit rather than returning an apparently complete empty document.
    """

    source = Path(path).expanduser().resolve()
    if max_input_bytes <= 0 or max_extracted_bytes <= 0:
        raise IngestionError("ingestion byte limits must be positive")
    source_data = _read_bounded(source, max_input_bytes)
    suffix = source.suffix.casefold()
    warnings: list[dict[str, Any]] = []
    if suffix in {".txt", ".md", ".markdown"}:
        text = _decode_utf8(source_data, warnings)
        blocks = _text_blocks(text, markdown=suffix != ".txt")
        source_format = "markdown" if suffix != ".txt" else "text"
        parser = "ocsl-text-1"
    elif suffix == ".docx":
        blocks = _docx_blocks(
            source_data, max_extracted_bytes=max_extracted_bytes, warnings=warnings
        )
        source_format = "docx"
        parser = "ocsl-docx-opc-1"
    elif suffix in {".xml", ".jats", ".html", ".htm"}:
        from .structured import extract
        structured = extract(source_data, html=suffix in {".html", ".htm"})
        blocks = structured["blocks"]
        warnings.extend(structured["warnings"])
        source_format = "publisher-html" if suffix in {".html", ".htm"} else "jats"
        parser = "ocsl-structured-citations-1"
    elif suffix == ".pdf":
        blocks = _pdf_blocks(
            source,
            executable=pdftotext,
            timeout=pdf_timeout,
            max_extracted_bytes=max_extracted_bytes,
            warnings=warnings,
        )
        source_format = "pdf"
        parser = "pdftotext-bbox-layout-2"
    else:
        raise IngestionError(
            f"unsupported manuscript format {source.suffix or '<none>'}; expected txt, md, docx, pdf, jats/xml, or publisher html"
        )
    result = _assemble(
        source=source,
        source_data=source_data,
        source_format=source_format,
        parser=parser,
        blocks=blocks,
        confidential=confidential,
        warnings=warnings,
    )
    if suffix in {".xml", ".jats", ".html", ".htm"}:
        result["structuredSource"] = structured["metadata"]
    return result


def ingest(path: str | os.PathLike[str], **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for callers that prefer a short verb."""

    return ingest_manuscript(path, **kwargs)


def _knowledge_files(
    inputs: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
) -> Iterator[Path]:
    if isinstance(inputs, (str, os.PathLike)):
        values: Iterable[str | os.PathLike[str]] = [inputs]
    else:
        values = inputs
    accepted = {".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml"}
    seen: set[Path] = set()
    for value in values:
        root = Path(value).expanduser().resolve()
        candidates = [root] if root.is_file() else sorted(root.rglob("*")) if root.is_dir() else []
        for candidate in candidates:
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or candidate.suffix.casefold() not in accepted
            ):
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield resolved


def audit_identifier_leaks(
    inputs: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    *,
    allowed_kinds: Iterable[str] = (),
    max_file_bytes: int = 8 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Find identifier-shaped content in maintainable knowledge inputs.

    Findings contain a one-way fingerprint rather than the matched value, so
    the audit report does not become a second disclosure channel.
    """

    allowed = set(allowed_kinds)
    unknown = allowed - set(_IDENTIFIER_PATTERNS)
    if unknown:
        raise IngestionError(
            f"unknown identifier kinds in allow-list: {', '.join(sorted(unknown))}"
        )
    if max_file_bytes <= 0:
        raise IngestionError("knowledge audit byte limit must be positive")
    findings: list[dict[str, Any]] = []
    for path in _knowledge_files(inputs):
        data = _read_bounded(path, max_file_bytes)
        if b"\x00" in data:
            raise IngestionError(f"knowledge audit refuses binary content: {path}")
        text = data.decode("utf-8-sig", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in _IDENTIFIER_PATTERNS.items():
                if kind in allowed:
                    continue
                for match in pattern.finditer(line):
                    normalized = match.group(0).casefold().encode("utf-8")
                    findings.append(
                        {
                            "path": str(path),
                            "line": line_number,
                            "kind": kind,
                            "fingerprint": hashlib.sha256(normalized).hexdigest()[:16],
                        }
                    )
    return findings


def require_no_identifier_leaks(
    inputs: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    **kwargs: Any,
) -> None:
    findings = audit_identifier_leaks(inputs, **kwargs)
    if findings:
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding["kind"]] = counts.get(finding["kind"], 0) + 1
        summary = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
        raise IngestionError(f"identifier-leak audit failed: {summary}")


__all__ = [
    "IngestionError",
    "audit_identifier_leaks",
    "ingest",
    "ingest_manuscript",
    "require_no_identifier_leaks",
]
