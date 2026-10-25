# SPDX-License-Identifier: Apache-2.0
"""Offline fixtures for the claim checker: synthetic manuscripts, stores, providers and judges.

No fixture calls a network service or a model endpoint. Provider fakes answer from canned,
synthetic records and record every request so that tests can inspect exactly what would have
left the machine.
"""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from ocsl_review.scholarly import RawResponse

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
POSITIVE_CONTROLS = {"9500320": ("RetractionIn", "20137807"), "32336359": ("ErratumIn", "35473980")}


def crossref_message(doi: str, title: str, authors: list[tuple[str, str]], year: int, *, volume: str = "",
                     page: str = "", container: str = "Journal of Example Biology",
                     updated_by: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "DOI": doi, "title": [title], "author": [{"given": given, "family": family} for given, family in authors],
        "published": {"date-parts": [[year]]}, "container-title": [container], "type": "journal-article",
        "volume": volume, "page": page, "update-to": [], "relation": {},
    }
    if updated_by:
        message["updated-by"] = updated_by
    return message


def _pubmed_xml(pmid: str, relationship: str | None = None, notice: str | None = None) -> bytes:
    comments = ""
    if relationship:
        comments = (f'<CommentsCorrectionsList><CommentsCorrections RefType="{relationship}"><RefSource>Notice</RefSource>'
                    f"<PMID>{notice}</PMID></CommentsCorrections></CommentsCorrectionsList>")
    return (f"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><Journal><JournalIssue>"
            f"<PubDate><Year>2001</Year></PubDate></JournalIssue><Title>Control Journal</Title></Journal>"
            f"<ArticleTitle>Control record {pmid}</ArticleTitle></Article>{comments}</MedlineCitation><PubmedData>"
            f'<ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>'
            f"</PubmedArticleSet>").encode()


class OfflineScholarlyTransport:
    """Answers the ScholarlyClient's requests from synthetic records and records every call."""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None, *, europe_pmc: dict[str, dict[str, Any]] | None = None):
        self.records = {key.lower(): value for key, value in (records or {}).items()}
        self.europe_pmc = {key.lower(): value for key, value in (europe_pmc or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
            **_: Any) -> RawResponse:
        self.calls.append({"url": url, "params": dict(params or {})})
        if "api.crossref.org/works/" in url:
            doi = urllib.parse.unquote(url.split("/works/", 1)[1]).lower()
            if doi not in self.records:
                from ocsl_review.errors import ProviderError
                raise ProviderError("provider HTTP 404: not found")
            return RawResponse(json.dumps({"message": self.records[doi]}).encode(), 200, {})
        if "europepmc" in url:
            query = str((params or {}).get("query", ""))
            match = re.search(r'DOI:"([^"]+)"', query)
            result = []
            if match and match.group(1).lower() in self.europe_pmc:
                result = [self.europe_pmc[match.group(1).lower()]]
            return RawResponse(json.dumps({"resultList": {"result": result}}).encode(), 200, {})
        if "efetch.fcgi" in url:
            pmid = str((params or {}).get("id", ""))
            relationship, notice = POSITIVE_CONTROLS.get(pmid, (None, None))
            return RawResponse(_pubmed_xml(pmid, relationship, notice), 200, {})
        if "esummary.fcgi" in url:
            pmid = str((params or {}).get("id", ""))
            return RawResponse(json.dumps({"result": {pmid: {"title": "Summary", "authors": [], "pubdate": "2001",
                                                             "source": "J", "pubtype": [], "articleids": []}}}).encode(), 200, {})
        return RawResponse(b"{}", 200, {})


class FakeRegistry:
    """(method, url) -> (status, headers, body) from a table of DOI outcomes; records every call."""

    def __init__(self, outcomes: dict[str, tuple[int, int, int]] | None = None, default: tuple[int, int, int] = (200, 302, 200)):
        self.outcomes = {key.lower(): value for key, value in (outcomes or {}).items()}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((method, url))
        for prefix, index in (("https://doi.org/api/handles/", 0), ("https://doi.org/", 1), ("https://api.crossref.org/works/", 2)):
            if url.startswith(prefix):
                doi = urllib.parse.unquote(url[len(prefix):]).lower()
                status = self.outcomes.get(doi, self.default)[index]
                if index == 0:
                    code = 1 if status == 200 else 100
                    return status, {}, json.dumps({"responseCode": code, "handle": doi}).encode()
                headers = {"Location": "https://example.org/article"} if status in (301, 302, 303) else {}
                return status, headers, b""
        return 400, {}, b""


def retraction_watch_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    fields = ["Record ID", "Title", "Subject", "Institution", "Journal", "Country", "Author", "URLS", "ArticleType",
              "RetractionDate", "RetractionDOI", "RetractionPubMedID", "OriginalPaperDate", "OriginalPaperDOI",
              "OriginalPaperPubMedID", "RetractionNature", "Reason", "Paywalled", "Notes"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def docx_bytes(paragraphs: list[str | tuple[str, str]]) -> bytes:
    """Minimal DOCX; a (style, text) tuple sets a paragraph style such as Heading1."""

    body = []
    for paragraph in paragraphs:
        style, text = paragraph if isinstance(paragraph, tuple) else (None, paragraph)
        properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body.append(f'<w:p>{properties}<w:r><w:t xml:space="preserve">{escaped}</w:t></w:r></w:p>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>'
                + "".join(body) + "<w:sectPr/></w:body></w:document>")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", document)
    return output.getvalue()
