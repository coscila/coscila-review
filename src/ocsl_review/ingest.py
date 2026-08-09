"""Local manuscript ingestion with stable, auditable location identifiers.

The module deliberately has no network behavior.  PDF text extraction is the
only subprocess boundary and is constrained by an input-size limit, a timeout,
and an extracted-output limit.  DOCX files are Open Packaging Convention ZIP
archives, so their text and simple structure can be read with the standard
library.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .errors import ReviewError

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
_NUMBERED_REFERENCE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s+(.+)$")
_NUMERIC_CITATION = re.compile(
    r"\[(?:\s*\d+[A-Za-z]?(?:\s*[-\u2013]\s*\d+[A-Za-z]?)?\s*(?:[,;]\s*)?)+\]"
)
_MARKDOWN_CITATION = re.compile(r"\[(?:@[A-Za-z0-9][A-Za-z0-9_.:+/-]*(?:\s*;\s*)?)+\]")
_AUTHOR_YEAR_CITATION = re.compile(r"\((?=[^()]{1,240}\b(?:19|20)\d{2}[a-z]?\b)[^()]{1,240}\)")
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
            value = " ".join(part.strip() for part in paragraph if part.strip()).strip()
            paragraph = []
            if value:
                blocks.append({"kind": "paragraph", "text": value, "page": current_page})

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


def _docx_paragraph(element: ET.Element) -> tuple[str, str | None, list[str]]:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{_W}}}t":
            parts.append(node.text or "")
        elif node.tag == f"{{{_W}}}tab":
            parts.append("\t")
        elif node.tag in (f"{{{_W}}}br", f"{{{_W}}}cr"):
            parts.append("\n")
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
    return "".join(parts).strip(), style, descriptions


def _docx_table(element: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in element.findall(f"./{{{_W}}}tr"):
        values: list[str] = []
        for cell in row.findall(f"./{{{_W}}}tc"):
            pieces: list[str] = []
            for paragraph in cell.findall(f".//{{{_W}}}p"):
                value, _, _ = _docx_paragraph(paragraph)
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
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise IngestionError(f"DOCX document XML is malformed: {exc}") from exc
    body = root.find(f".//{{{_W}}}body")
    if body is None:
        raise IngestionError("DOCX document has no body")
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
            text, style, descriptions = _docx_paragraph(child)
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
                    }
                )
        elif child.tag == f"{{{_W}}}tbl":
            blocks.append({"kind": "table", "rows": _docx_table(child), "page": None})
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
) -> list[dict[str, Any]]:
    if timeout <= 0:
        raise IngestionError("PDF extraction timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="ocsl-review-pdf-") as temporary:
        output = Path(temporary) / "manuscript.txt"
        argv = [
            os.fspath(executable),
            "-layout",
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
        warnings: list[dict[str, Any]] = []
        text = _decode_utf8(data, warnings)
        if warnings:
            raise IngestionError("PDF extractor emitted invalid UTF-8")
        return _text_blocks(text, markdown=False)


def _sentence_spans(text: str) -> Iterator[tuple[int, int, str]]:
    if not text.strip():
        return
    start = 0
    for match in re.finditer(r"(?:[.!?][\]\)\"']*|[。！？])\s+", text):
        end = match.end()
        value = text[start:end].strip()
        if value:
            value_start = text.find(value, start, end)
            yield value_start, value_start + len(value), value
        start = end
    value = text[start:].strip()
    if value:
        value_start = text.find(value, start)
        yield value_start, value_start + len(value), value


def _citation_matches(text: str) -> list[tuple[int, int, str, str]]:
    candidates: list[tuple[int, int, str, str]] = []
    for style, pattern in (
        ("markdown-key", _MARKDOWN_CITATION),
        ("numeric", _NUMERIC_CITATION),
        ("author-year", _AUTHOR_YEAR_CITATION),
    ):
        for match in pattern.finditer(text):
            raw = match.group(0)
            if style == "author-year":
                before_year = re.split(r"(?:19|20)\d{2}", raw, maxsplit=1)[0]
                if not re.search(r"[A-Za-z]{2,}", before_year):
                    continue
            candidates.append((match.start(), match.end(), raw, style))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str, str]] = []
    for candidate in candidates:
        if any(candidate[0] < prior[1] and prior[0] < candidate[1] for prior in accepted):
            continue
        accepted.append(candidate)
    return accepted


def _citation_tokens(raw: str, style: str) -> list[str]:
    if style == "markdown-key":
        return re.findall(r"@([A-Za-z0-9][A-Za-z0-9_.:+/-]*)", raw)
    if style == "numeric":
        tokens: list[str] = []
        for part in re.findall(r"\d+[A-Za-z]?(?:\s*[-\u2013]\s*\d+[A-Za-z]?)?", raw):
            if re.fullmatch(r"\d+\s*[-\u2013]\s*\d+", part):
                low, high = (int(value) for value in re.split(r"[-\u2013]", part))
                if low <= high and high - low <= 100:
                    tokens.extend(str(value) for value in range(low, high + 1))
                else:
                    tokens.append(part.replace(" ", ""))
            else:
                tokens.append(part.replace(" ", ""))
        return tokens
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
            text = re.sub(r"[ \t]+", " ", block.get("text", "")).strip()
            if not text:
                continue
            paragraph_id = f"p-{len(paragraphs) + 1:06d}"
            is_heading = bool(block.get("heading"))
            if not is_heading:
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
                "locator": _make_locator(
                    section_id=section["id"], paragraph_id=paragraph_id, page=block.get("page")
                ),
                "sentenceIds": [],
            }
            paragraphs.append(paragraph)
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
                bibliography.append(
                    {
                        "id": f"ref-{len(bibliography) + 1:05d}",
                        "ordinal": len(bibliography) + 1,
                        "label": (match.group(1) or match.group(2)) if match else None,
                        "raw": match.group(3) if match else text,
                        "paragraphId": paragraph_id,
                        "identifiers": _reference_identifiers(text),
                        "locator": paragraph["locator"],
                    }
                )
            if not is_heading:
                for sentence_number, (start, end, sentence_text) in enumerate(
                    _sentence_spans(text), start=1
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
                    sentence_citations = (
                        []
                        if paragraph_kind == "bibliography-entry"
                        else _citation_matches(sentence_text)
                    )
                    for local_start, local_end, raw, style in sentence_citations:
                        citation_id = f"cite-{len(citations) + 1:06d}"
                        citation = {
                            "id": citation_id,
                            "ordinal": len(citations) + 1,
                            "style": style,
                            "raw": raw,
                            "tokens": _citation_tokens(raw, style),
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
            }
            tables.append(table)
            section["tableIds"].append(table_id)
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
    elif suffix == ".pdf":
        blocks = _pdf_blocks(
            source,
            executable=pdftotext,
            timeout=pdf_timeout,
            max_extracted_bytes=max_extracted_bytes,
        )
        source_format = "pdf"
        parser = "pdftotext-layout-1"
    else:
        raise IngestionError(
            f"unsupported manuscript format {source.suffix or '<none>'}; expected txt, md, docx, or pdf"
        )
    return _assemble(
        source=source,
        source_data=source_data,
        source_format=source_format,
        parser=parser,
        blocks=blocks,
        confidential=confidential,
        warnings=warnings,
    )


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
