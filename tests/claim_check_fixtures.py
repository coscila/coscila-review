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


# Claim-check fixtures -------------------------------------------------------------------------

ATTRIBUTION_WORKS = [
    {"workId": "work-x", "identifiers": {"doi": "10.5555/attr.x"}, "title": "Drug X and all-cause mortality in adults",
     "year": 2019, "units": [{"unitId": "work-x#abstract", "kind": "abstract",
                              "text": "In a randomised trial of 2,000 adults, drug X reduced all-cause mortality. Drug X was well tolerated."}]},
    {"workId": "work-y", "identifiers": {"doi": "10.5555/attr.y"}, "title": "HMG-CoA reductase inhibition by statins",
     "year": 2015, "units": [{"unitId": "work-y#abstract", "kind": "abstract",
                              "text": "Statins inhibit HMG-CoA reductase, the rate-limiting enzyme of cholesterol synthesis, which lowers LDL cholesterol."}]},
    {"workId": "work-m", "identifiers": {"doi": "10.5555/attr.m"}, "title": "Metformin and hepatic glucose output",
     "year": 2012, "units": [{"unitId": "work-m#abstract", "kind": "abstract",
                              "text": "Metformin reduces hepatic glucose output in people with type 2 diabetes."}]},
    {"workId": "work-c", "identifiers": {"doi": "10.5555/attr.c"}, "title": "Statins in the primary prevention of cardiovascular events",
     "year": 2016, "units": [{"unitId": "work-c#abstract", "kind": "abstract",
                              "text": "Statin therapy reduced cardiovascular events in primary prevention."},
                             {"unitId": "work-c#p1", "kind": "full-text",
                              "text": "Among 10,000 participants without vascular disease, statins reduced major cardiovascular events."}]},
]
REFERENCES = {
    "x": "Rivers, A. (2019). Drug X and all-cause mortality in adults. Journal of Example Biology, 3, 1-9. doi:10.5555/attr.x",
    "y": "Stone, B. (2015). HMG-CoA reductase inhibition by statins. Example Methods, 7, 10-19. doi:10.5555/attr.y",
    "m": "Lake, C. (2012). Metformin and hepatic glucose output. Synthetic Reports, 2, 5-8. doi:10.5555/attr.m",
    "c": "Moss, D. (2016). Statins in the primary prevention of cardiovascular events. Example Methods, 9, 20-29. doi:10.5555/attr.c",
}
# A1: two sentences, each citing a different source. A2: a clause supported by Y inside a
# sentence supported by X (cited at the end). A3: uncited sentences framed by two X citations.
A1 = ["Drug X reduced all-cause mortality in adults [1].", "Metformin reduces hepatic glucose output [2]."]
A2 = ["Statins lower LDL cholesterol, an effect mediated by HMG-CoA reductase inhibition [3], and reduce cardiovascular "
      "events in primary prevention [4]."]
A3 = ["Drug X reduced mortality in the trial [1].", "Our results suggest a similar benefit in older patients.",
      "These results indicate that dosing matters.",
      "Metformin is the most widely prescribed oral antidiabetic drug worldwide.", "Drug X was also well tolerated [1]."]
ORDERED_REFERENCES = ["x", "m", "y", "c"]


def attribution_markdown() -> str:
    lines = ["# Introduction", "", " ".join(A1), "", " ".join(A2), "", "## Discussion", "", " ".join(A3), "", "## References", ""]
    lines += [f"{number}. {REFERENCES[key]}" for number, key in enumerate(ORDERED_REFERENCES, 1)]
    return "\n".join(lines) + "\n"


def attribution_docx() -> bytes:
    paragraphs: list[str | tuple[str, str]] = [("Heading1", "Introduction"), " ".join(A1), " ".join(A2),
                                               ("Heading1", "Discussion"), " ".join(A3), ("Heading1", "References")]
    paragraphs += [f"{number}. {REFERENCES[key]}" for number, key in enumerate(ORDERED_REFERENCES, 1)]
    return docx_bytes(paragraphs)


def attribution_jats() -> str:
    def body(sentences: list[str]) -> str:
        text = " ".join(sentences)
        return re.sub(r"\[(\d)\]", lambda m: f'<xref ref-type="bibr" rid="r{m.group(1)}">{m.group(1)}</xref>', text)
    refs = "".join(f'<ref id="r{number}"><label>{number}</label><mixed-citation>{REFERENCES[key]}</mixed-citation></ref>'
                   for number, key in enumerate(ORDERED_REFERENCES, 1))
    return ('<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta><title-group><article-title>'
            'Synthetic attribution fixture</article-title></title-group></article-meta></front><body>'
            f'<sec><title>Introduction</title><p>{body(A1)}</p><p>{body(A2)}</p></sec>'
            f'<sec><title>Discussion</title><p>{body(A3)}</p></sec></body><back><ref-list>{refs}</ref-list></back></article>')


def write_store(path: Path, works: list[dict[str, Any]] | None = None) -> Path:
    path.write_text("\n".join(json.dumps(work) for work in (works or ATTRIBUTION_WORKS)) + "\n", encoding="utf-8")
    return path


_STOP = {"the", "and", "of", "in", "a", "an", "to", "was", "were", "is", "are", "by", "with", "that", "which", "also"}


def _content(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9-]+", text.lower()) if word not in _STOP and len(word) > 2}


class QuoteJudge:
    """Deterministic judge for tests: quotes the best-overlapping unit verbatim, never invents text.

    "increases" in a claim against "reduced" in the evidence yields "contradicted"; no overlap
    yields an explicit abstention. ``drop_first`` omits one item from the first response to
    exercise the repair pass.
    """

    name, model, location, host = "quote-judge", "synthetic-judge-1", "on-prem", "127.0.0.1"

    def __init__(self, *, drop_first: bool = False):
        self.calls: list[list[dict[str, str]]] = []
        self.drop_first = drop_first

    def complete(self, messages, *, settings):
        from ocsl_review.claim_check import JudgeResponse
        self.calls.append(messages)
        payload = json.loads(messages[1]["content"])
        items = []
        for item in payload["items"]:
            claim = _content(item["claim"])
            best = max(item["evidenceUnits"], key=lambda unit: len(claim & _content(unit["text"])))
            overlap = claim & _content(best["text"])
            quote = best["text"].split(". ")[0][:150]
            if "increases" in item["claim"].lower() and "reduced" in best["text"].lower():
                verdict, escalate, reason = "contradicted", False, ""
            elif len(overlap) >= 2:
                verdict, escalate, reason = "direct-support", False, ""
            else:
                verdict, escalate, reason = "abstain", True, "the supplied evidence does not address the claim"
            items.append({"id": item["id"], "supportVerdict": verdict,
                          "rationale": f"{len(overlap)} shared content words with {best['id']}.",
                          "evidence": [] if verdict == "abstain" else [{"unitId": best["id"], "quote": quote}],
                          "escalate": escalate, "escalationReason": reason})
        if self.drop_first and len(self.calls) == 1 and len(items) > 1:
            items = items[1:]
        return JudgeResponse(json.dumps({"items": items}), self.model, {"prompt_tokens": len(messages[1]["content"]) // 4})


SURNAME_POOL = ["Rivers", "Stone", "Lake", "Moss", "Field", "Brook", "Hill", "Vale", "Marsh", "Glen", "Heath", "Ford",
                "Wood", "Shaw", "Dale", "Fenn", "Holt", "Lane", "Reed", "Wells"]
TOPICS = ["statin", "metformin", "aspirin", "insulin", "vitamin", "exercise", "sleep", "smoking", "alcohol", "diet",
          "sodium", "coffee", "obesity", "lipid", "glucose", "kidney", "liver", "bone", "immune", "tumour"]
OUTCOMES = ["mortality", "incidence", "survival", "progression", "remission", "admission", "fracture", "relapse"]


def large_fixture(loci: int = 300, references: int = 60) -> tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """A synthetic manuscript with `loci` (sentence, reference) citation loci, its store and Crossref records."""

    works, records, entries = [], {}, []
    for number in range(1, references + 1):
        topic, outcome = TOPICS[number % len(TOPICS)], OUTCOMES[number % len(OUTCOMES)]
        surname = SURNAME_POOL[number % len(SURNAME_POOL)]
        doi = f"10.5555/perf.{number}"
        title = f"{topic.capitalize()} exposure and {outcome} in cohort {number} of adults"
        year = 1995 + number % 30
        units = [{"unitId": f"perf-{number}#abstract", "kind": "abstract",
                  "text": f"In cohort {number}, {topic} exposure was associated with lower {outcome} among adults."}]
        for paragraph in range(1, 7):
            units.append({"unitId": f"perf-{number}#p{paragraph}", "kind": "full-text",
                          "text": f"Paragraph {paragraph}: the {topic} analysis of cohort {number} measured {outcome} "
                                  f"with adjustment for age, sex and {TOPICS[(number + paragraph) % len(TOPICS)]} intake." * 3})
        works.append({"workId": f"perf-{number}", "identifiers": {"doi": doi}, "title": title, "year": year,
                      "units": units})
        records[doi] = crossref_message(doi, title, [("Ann", surname)], year, volume=str(number), page=f"{number}-{number + 9}")
        entries.append(f"{number}. {surname}, A. ({year}). {title}. Journal of Example Biology, {number}, {number}-{number + 9}. "
                       f"doi:{doi}")
    sentences = []
    for index in range(loci // 2):
        first = index % references + 1
        second = (index * 7 + 3) % references + 1
        if second == first:
            second = second % references + 1
        topic, outcome = TOPICS[first % len(TOPICS)], OUTCOMES[first % len(OUTCOMES)]
        sentences.append(f"In cohort {first}, {topic} exposure was associated with lower {outcome} among adults [{first}, {second}].")
    paragraphs = [" ".join(sentences[start:start + 6]) for start in range(0, len(sentences), 6)]
    markdown = "# Introduction\n\n" + "\n\n".join(paragraphs) + "\n\n## References\n\n" + "\n".join(entries) + "\n"
    return markdown, works, records
