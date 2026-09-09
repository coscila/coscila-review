"""Bounded clients for public scholarly metadata and open-literature services."""

from __future__ import annotations

import html
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

from .confidentiality import EgressPlan, append_egress_event, authorize_egress
from .errors import ProviderError, ReviewError
from .fulltext import MAX_DOCUMENT_BYTES, open_public_https, parse_source_document, validate_public_url

CROSSREF = "https://api.crossref.org"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DATACITE = "https://api.datacite.org"
OPENCITATIONS_INDEX = "https://api.opencitations.net/index/v2"
UNPAYWALL = "https://api.unpaywall.org/v2"
MAX_RESULTS = 25
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TRANSIENT_PROVIDER_STATUSES = frozenset({429, 503})
MAX_HTTP_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 5.0
# Public NLM records, independently checked against their PubMed notice pages.
# https://pubmed.ncbi.nlm.nih.gov/20137807/
# https://pubmed.ncbi.nlm.nih.gov/32336359/
STATUS_POSITIVE_CONTROLS = (
    ("9500320", "RetractionIn", "20137807"),
    ("32336359", "ErratumIn", "35473980"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_doi(value: str) -> str:
    normalized = value.strip()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.IGNORECASE).rstrip(".,;)")
    if not re.fullmatch(r"10\.\d{4,9}/\S+", normalized, flags=re.IGNORECASE):
        raise ReviewError(f"invalid DOI: {value}")
    return normalized.lower()


def bounded_limit(value: int | str | None) -> int:
    try:
        parsed = int(value or 10)
    except (TypeError, ValueError) as exc:
        raise ReviewError("limit must be an integer") from exc
    if parsed < 1:
        raise ReviewError("limit must be positive")
    return min(parsed, MAX_RESULTS)


@dataclass(frozen=True)
class RawResponse:
    body: bytes
    status: int
    headers: Mapping[str, str]
    url: str | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider returned malformed JSON") from exc


class HttpTransport:
    """Small injectable HTTP transport with response and time bounds."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 15.0,
        max_attempts: int = 3,
        max_retry_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.user_agent = user_agent
        parsed_timeout = float(timeout)
        parsed_retry_delay = float(max_retry_delay)
        if not math.isfinite(parsed_timeout) or not math.isfinite(parsed_retry_delay):
            raise ValueError("HTTP transport bounds must be finite")
        self.timeout = min(max(parsed_timeout, 1.0), 30.0)
        self.max_attempts = min(max(int(max_attempts), 1), MAX_HTTP_ATTEMPTS)
        self.max_retry_delay = min(max(parsed_retry_delay, 0.0), MAX_RETRY_DELAY_SECONDS)
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_ncbi_request = 0.0

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, Any]) -> float | None:
        raw_value = headers.get("Retry-After")
        if raw_value is None:
            return None
        value = str(raw_value).strip()
        if value.isdigit():
            return float(value)
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at is None:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())

    def _retry_delay(self, headers: Mapping[str, Any], attempt: int) -> float:
        requested = self._retry_after_seconds(headers)
        if requested is None:
            requested = float(2 ** (attempt - 1))
        return min(requested, self.max_retry_delay)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        document: bool = False,
        authorize_url: Callable[[str], None] | None = None,
    ) -> RawResponse:
        validate_public_url(url)
        if not 1 <= max_response_bytes <= MAX_DOCUMENT_BYTES:
            raise ProviderError("invalid provider response size bound")
        filtered = {key: value for key, value in (params or {}).items() if value is not None}
        encoded = urllib.parse.urlencode(filtered, doseq=True)
        target = f"{url}?{encoded}" if encoded else url
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        request_headers.update(headers or {})
        request = urllib.request.Request(target, headers=request_headers, method="GET")
        deadline = self._monotonic() + self.timeout
        for attempt in range(1, self.max_attempts + 1):
            # Sequential bibliography retrieval must respect NCBI's three
            # requests/second ceiling even when every response is successful.
            # https://www.ncbi.nlm.nih.gov/books/NBK25497/
            if urllib.parse.urlsplit(target).hostname == "eutils.ncbi.nlm.nih.gov":
                delay = max(0.0, self._next_ncbi_request - self._monotonic())
                if self._monotonic() + delay >= deadline:
                    raise ProviderError("provider rate limit exceeds the configured timeout")
                if delay:
                    self._sleep(delay)
                self._next_ncbi_request = self._monotonic() + 0.35
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ProviderError("provider request exceeded the configured timeout")

            try:
                with open_public_https(request, timeout=remaining, allow_cross_origin=document, authorize_url=authorize_url) as response:
                    response_headers = dict(response.headers.items())
                    status = int(response.status)
                    if status >= 400:
                        detail = response.read(512).decode("utf-8", errors="replace")
                        raw = None
                    else:
                        length = response.headers.get("Content-Length")
                        try:
                            declared_length = int(length) if length else None
                        except ValueError as exc:
                            raise ProviderError("provider returned an invalid Content-Length") from exc
                        if declared_length is not None and (declared_length < 0 or declared_length > max_response_bytes):
                            raise ProviderError(
                                "provider response exceeds the configured size bound"
                            )
                        body = response.read(max_response_bytes + 1)
                        if len(body) > max_response_bytes:
                            raise ProviderError(
                                "provider response exceeds the configured size bound"
                            )
                        if declared_length is not None and len(body) != declared_length:
                            raise ProviderError("provider response was truncated")
                        raw = RawResponse(body, status, response_headers, getattr(response, "url", target))
                        detail = ""
            except urllib.error.HTTPError as exc:
                response_headers = dict(exc.headers.items()) if exc.headers else {}
                status = int(exc.code)
                try:
                    detail = exc.read(512).decode("utf-8", errors="replace")
                finally:
                    exc.close()
                raw = None
                cause: BaseException | None = exc
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                raise ProviderError(f"provider request failed: {exc}") from exc
            else:
                cause = None

            if raw is not None:
                if self._monotonic() >= deadline:
                    raise ProviderError("provider request exceeded the configured timeout")
                return raw

            message = f"provider HTTP {status}: {detail}"
            if status not in TRANSIENT_PROVIDER_STATUSES or attempt >= self.max_attempts:
                raise ProviderError(message) from cause

            delay = self._retry_delay(response_headers, attempt)
            remaining = deadline - self._monotonic()
            if remaining <= 0 or delay >= remaining:
                raise ProviderError(
                    f"{message} (retry delay exceeds the configured timeout)"
                ) from cause
            if delay:
                self._sleep(delay)

        raise ProviderError("provider request exhausted its retry budget")

    def get_document(self, url: str, *, authorize_url: Callable[[str], None] | None = None) -> RawResponse:
        return self.get(url, max_response_bytes=MAX_DOCUMENT_BYTES, document=True, authorize_url=authorize_url)


def _first(value: Any, default: Any = None) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return default if value is None else value


def display_text(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(re.sub(r"<[^>]+>", "", html.unescape(str(value))).split())


def _crossref_year(item: Mapping[str, Any]) -> int | None:
    for field in ("published-print", "published-online", "published", "issued", "created"):
        parts = (
            item.get(field, {}).get("date-parts", []) if isinstance(item.get(field), dict) else []
        )
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError, IndexError):
                pass
    return None


def normalize_crossref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": "crossref",
        "doi": item.get("DOI"),
        "title": display_text(_first(item.get("title"))),
        "authors": [
            display_text(" ".join(part for part in (author.get("given"), author.get("family")) if part))
            for author in item.get("author", [])
        ],
        "year": _crossref_year(item),
        "container": display_text(_first(item.get("container-title"))),
        "volume": item.get("volume"),
        "issue": item.get("issue"),
        "pages": item.get("page") or item.get("article-number"),
        "rawMetadata": dict(item),
        "type": item.get("type"),
        "subtype": item.get("subtype"),
        "relation": item.get("relation", {}),
        "updates": item.get("update-to", []),
        "licenses": item.get("license", []),
        "url": item.get("URL"),
        "rawStatusFields": {
            "updateTo": item.get("update-to", []),
            "relation": item.get("relation", {}),
            "type": item.get("type"),
            "subtype": item.get("subtype"),
        },
    }


def normalize_europe_pmc(item: Mapping[str, Any]) -> dict[str, Any]:
    author_text = item.get("authorString") or ""
    return {
        "provider": "europe-pmc",
        "id": item.get("id"),
        "source": item.get("source"),
        "doi": item.get("doi"),
        "pmid": item.get("pmid") or (item.get("id") if item.get("source") == "MED" else None),
        "pmcid": item.get("pmcid"),
        "title": display_text(item.get("title")),
        "authors": [display_text(part) for part in author_text.split(",") if part.strip()],
        "year": int(item["pubYear"]) if str(item.get("pubYear", "")).isdigit() else None,
        "container": display_text(item.get("journalTitle") or item.get("journalInfo", {}).get("journal", {}).get("title")),
        "volume": item.get("journalVolume") or item.get("journalInfo", {}).get("volume"),
        "issue": item.get("issue") or item.get("journalInfo", {}).get("issue"),
        "pages": item.get("pageInfo"),
        "rawMetadata": dict(item),
        "publicationTypes": item.get("pubTypeList", {}).get("pubType", []),
        "isOpenAccess": item.get("isOpenAccess") == "Y" or item.get("isOpenAccess") is True,
        "isRetracted": item.get("isRetracted") == "Y" or item.get("isRetracted") is True,
        "hasReferences": item.get("hasReferences") == "Y" or item.get("hasReferences") is True,
        "hasTextMinedTerms": item.get("hasTextMinedTerms") == "Y",
        "abstract": display_text(item.get("abstractText")),
    }


def normalize_datacite(item: Mapping[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", item)
    return {
        "provider": "datacite",
        "doi": attributes.get("doi") or item.get("id"),
        "titles": attributes.get("titles", []),
        "creators": attributes.get("creators", []),
        "publisher": attributes.get("publisher"),
        "publicationYear": attributes.get("publicationYear"),
        "types": attributes.get("types", {}),
        "relatedIdentifiers": attributes.get("relatedIdentifiers", []),
        "version": attributes.get("version"),
        "state": attributes.get("state"),
        "url": attributes.get("url"),
    }


def parse_pubmed_record(body: bytes, pmid: str) -> dict[str, Any]:
    """Read identity, complete abstract sections, and directed notice links."""

    if len(body) > MAX_RESPONSE_BYTES or re.search(br"<!ENTITY\b", body, re.IGNORECASE):
        raise ProviderError("PubMed XML exceeds bounds or declares entities")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProviderError("PubMed returned malformed structured XML") from exc
    citation = next((node for node in root.findall(".//MedlineCitation") if node.findtext("PMID") == pmid), None)
    if citation is None:
        raise ProviderError(f"PubMed structured response did not contain PMID {pmid}")

    def text(node: ET.Element | None) -> str | None:
        return display_text("".join(node.itertext())) if node is not None else None

    article = citation.find("Article")
    if article is None or not text(article.find("ArticleTitle")):
        raise ProviderError("PubMed structured record lacks an article title")
    year = article.findtext("Journal/JournalIssue/PubDate/Year")
    if not year:
        year_match = re.search(r"\b(?:18|19|20)\d{2}\b", article.findtext("Journal/JournalIssue/PubDate/MedlineDate") or "")
        year = year_match.group() if year_match else None
    abstract_sections = [
        {"label": node.get("Label") or node.get("NlmCategory") or "", "text": text(node)}
        for node in article.findall("Abstract/AbstractText") if text(node)
    ]
    notices = [
        {"refType": node.get("RefType"), "pmid": node.findtext("PMID"), "source": text(node.find("RefSource")), "note": text(node.find("Note"))}
        for node in citation.findall("CommentsCorrectionsList/CommentsCorrections")
    ]
    identifiers = {}
    for parent in root.findall(".//PubmedArticle"):
        if parent.findtext("MedlineCitation/PMID") == pmid:
            identifiers = {node.get("IdType"): text(node) for node in parent.findall("PubmedData/ArticleIdList/ArticleId")}
    return {
        "provider": "pubmed", "pmid": pmid, "doi": identifiers.get("doi"), "pmcid": identifiers.get("pmc"),
        "title": text(article.find("ArticleTitle")),
        "authors": [" ".join(part for part in (node.findtext("ForeName"), node.findtext("LastName"), node.findtext("CollectiveName")) if part) for node in article.findall("AuthorList/Author")],
        "year": int(year) if year and year.isdigit() else None,
        "container": text(article.find("Journal/Title")),
        "volume": article.findtext("Journal/JournalIssue/Volume"), "issue": article.findtext("Journal/JournalIssue/Issue"),
        "pages": article.findtext("Pagination/MedlinePgn"),
        "publicationTypes": [text(node) for node in article.findall("PublicationTypeList/PublicationType")],
        "abstractSections": abstract_sections,
        "abstract": "\n".join((part["label"] + ": " if part["label"] else "") + part["text"] for part in abstract_sections) or None,
        "notices": notices, "noticeRoute": "pubmed-efetch-CommentsCorrectionsList",
        "rawMetadata": {"xml": body.decode("utf-8", errors="replace")},
    }


class ScholarlyClient:
    def __init__(
        self,
        *,
        plan: EgressPlan | None = None,
        workspace: Path | None = None,
        transport: HttpTransport | None = None,
        contact_email: str | None = None,
    ):
        self.plan = plan or EgressPlan()
        self.workspace = workspace
        self.contact_email = contact_email or os.environ.get("OCSL_REVIEW_CONTACT_EMAIL")
        agent = "ocsl-review/0.1 (+https://github.com; scholarly metadata verification)"
        if self.contact_email:
            agent += f" mailto:{self.contact_email}"
        self.transport = transport or HttpTransport(user_agent=agent)
        self._status_probe_cache: dict[str, Any] | None = None
        self._requested_operation: ContextVar[str | None] = ContextVar(
            "ocsl_review_requested_operation",
            default=None,
        )

    @contextmanager
    def _operation_scope(self, operation: str) -> Iterator[None]:
        """Keep provider authorization bound to the public operation requested.

        Several public tools delegate to identifier or status helpers.  The
        outer operation must remain authoritative for both allow-list checks
        and egress-ledger events throughout that call graph.
        """

        if self._requested_operation.get() is not None:
            yield
            return
        token = self._requested_operation.set(operation)
        try:
            yield
        finally:
            self._requested_operation.reset(token)

    def _authorize(self, provider: str, operation: str, arguments: Mapping[str, Any]) -> None:
        effective_operation = self._requested_operation.get() or operation
        event = authorize_egress(
            provider=provider,
            operation=effective_operation,
            arguments=arguments,
            plan=self.plan,
        )
        if self.workspace:
            append_egress_event(self.workspace, event)

    def _envelope(
        self,
        operation: str,
        provider: str,
        records: Any,
        *,
        access: str = "metadata-only",
        limitations: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "provider": provider,
            "retrievedAt": utc_now(),
            "accessLevel": access,
            "records": records,
            "limitations": limitations or [],
        }

    def search_crossref(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        rows = bounded_limit(limit)
        args = {"query": query, "rows": rows}
        self._authorize("crossref", "search_crossref", args)
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": rows,
            "select": "DOI,title,author,published,container-title,volume,issue,page,type,relation,update-to,license,URL",
        }
        if self.contact_email:
            params["mailto"] = self.contact_email
        payload = self.transport.get(f"{CROSSREF}/works", params=params).json()
        items = payload.get("message", {}).get("items", [])[:rows]
        return self._envelope("search_crossref", "crossref", [normalize_crossref(x) for x in items])

    def _crossref_work(self, doi: str, operation: str) -> dict[str, Any]:
        doi = normalize_doi(doi)
        self._authorize("crossref", operation, {"doi": doi})
        encoded = urllib.parse.quote(doi, safe="")
        params = {"mailto": self.contact_email} if self.contact_email else None
        payload = self.transport.get(f"{CROSSREF}/works/{encoded}", params=params).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
            raise ProviderError("Crossref returned no structured work record")
        item = payload["message"]
        try:
            matched = normalize_doi(str(item.get("DOI") or "")) == doi
        except ReviewError:
            matched = False
        if not matched:
            raise ProviderError("Crossref returned an absent or mismatched DOI")
        return normalize_crossref(item)

    def search_pubmed(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        rows = bounded_limit(limit)
        args = {"query": query, "rows": rows}
        self._authorize("pubmed", "search_pubmed", args)
        common: dict[str, Any] = {"db": "pubmed", "retmode": "json", "tool": "ocsl_review"}
        if self.contact_email:
            common["email"] = self.contact_email
        if os.environ.get("NCBI_API_KEY"):
            common["api_key"] = os.environ["NCBI_API_KEY"]
        search = self.transport.get(
            f"{PUBMED}/esearch.fcgi", params={**common, "term": query, "retmax": rows}
        ).json()
        ids = search.get("esearchresult", {}).get("idlist", [])[:rows]
        if not ids:
            return self._envelope("search_pubmed", "pubmed", [])
        summary = (
            self.transport.get(f"{PUBMED}/esummary.fcgi", params={**common, "id": ",".join(ids)})
            .json()
            .get("result", {})
        )
        records = []
        for pmid in ids:
            item = summary.get(pmid, {})
            records.append(
                {
                    "provider": "pubmed",
                    "pmid": pmid,
                    "title": item.get("title"),
                    "authors": [author.get("name") for author in item.get("authors", [])],
                    "published": item.get("pubdate"),
                    "container": item.get("fulljournalname") or item.get("source"),
                    "publicationTypes": item.get("pubtype", []),
                    "identifiers": item.get("articleids", []),
                }
            )
        return self._envelope("search_pubmed", "pubmed", records)

    def _pubmed_summary(self, pmid: str, operation: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{1,10}", pmid.strip()):
            raise ReviewError("invalid PMID")
        pmid = pmid.strip()
        self._authorize("pubmed", operation, {"pmid": pmid})
        params: dict[str, Any] = {
            "db": "pubmed",
            "id": pmid,
            "retmode": "json",
            "tool": "ocsl_review",
        }
        if self.contact_email:
            params["email"] = self.contact_email
        if os.environ.get("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        result = (
            self.transport.get(f"{PUBMED}/esummary.fcgi", params=params).json().get("result", {})
        )
        item = result.get(pmid)
        if not item:
            raise ProviderError(f"PubMed did not resolve PMID {pmid}")
        return {
            "provider": "pubmed",
            "pmid": pmid,
            "title": display_text(item.get("title")),
            "authors": [author.get("name") for author in item.get("authors", [])],
            "published": item.get("pubdate"),
            "year": int(match.group()) if (match := re.search(r"\b(?:18|19|20)\d{2}\b", item.get("pubdate") or "")) else None,
            "container": display_text(item.get("fulljournalname") or item.get("source")),
            "volume": item.get("volume"), "issue": item.get("issue"), "pages": item.get("pages"),
            "doi": next((value.get("value") for value in item.get("articleids", []) if value.get("idtype") == "doi"), None),
            "rawMetadata": dict(item),
            "publicationTypes": item.get("pubtype", []),
            "identifiers": item.get("articleids", []),
        }

    def _pubmed_structured(self, pmid: str, operation: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{1,10}", pmid):
            raise ReviewError("invalid PMID")
        self._authorize("pubmed", operation, {"pmid": pmid})
        params: dict[str, Any] = {"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "ocsl_review"}
        if self.contact_email:
            params["email"] = self.contact_email
        if os.environ.get("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        response = self.transport.get(f"{PUBMED}/efetch.fcgi", params=params, headers={"Accept": "application/xml"})
        return parse_pubmed_record(response.body, pmid)

    def probe_publication_status(self) -> dict[str, Any]:
        """Exercise the exact structured notice route on known-positive records."""

        if self._status_probe_cache is not None:
            return dict(self._status_probe_cache)
        cases = []
        for pmid, relationship, notice in STATUS_POSITIVE_CONTROLS:
            try:
                record = self._pubmed_structured(pmid, "get_publication_status")
                passed = any(item["refType"] == relationship and item["pmid"] == notice for item in record["notices"])
                cases.append({"pmid": pmid, "expectedRelationship": relationship, "expectedNotice": notice, "passed": passed})
            except ProviderError as exc:
                cases.append({"pmid": pmid, "passed": False, "error": str(exc)})
        result = {"route": "pubmed-efetch-CommentsCorrectionsList", "checkedAt": utc_now(), "passed": bool(cases) and all(case["passed"] for case in cases), "cases": cases}
        # A failed probe is retryable on the next source, never a cached success.
        if result["passed"]:
            self._status_probe_cache = result
        return result

    def search_europe_pmc(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        rows = bounded_limit(limit)
        args = {"query": query, "rows": rows}
        self._authorize("europe-pmc", "search_europe_pmc", args)
        payload = self.transport.get(
            f"{EUROPE_PMC}/search",
            params={"query": query, "pageSize": rows, "resultType": "core", "format": "json"},
        ).json()
        records = payload.get("resultList", {}).get("result", [])[:rows]
        return self._envelope(
            "search_europe_pmc", "europe-pmc", [normalize_europe_pmc(item) for item in records]
        )

    def _europe_pmc_lookup(
        self, query: str, operation: str, egress: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        self._authorize("europe-pmc", operation, egress)
        payload = self.transport.get(
            f"{EUROPE_PMC}/search",
            params={"query": query, "pageSize": 5, "resultType": "core", "format": "json"},
        ).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("resultList"), dict):
            raise ProviderError("Europe PMC returned no structured result list")
        items = payload["resultList"].get("result", [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ProviderError("Europe PMC returned malformed result records")
        return [
            normalize_europe_pmc(item)
            for item in items[:5]
        ]

    def resolve_identifier(
        self, *, doi: str | None = None, pmid: str | None = None, pmcid: str | None = None
    ) -> dict[str, Any]:
        supplied = sum(value is not None for value in (doi, pmid, pmcid))
        if supplied != 1:
            raise ReviewError("resolve_identifier requires exactly one of doi, pmid, or pmcid")
        records: list[dict[str, Any]] = []
        failures: list[str] = []
        if doi:
            doi = normalize_doi(doi)
            try:
                records.append(self._crossref_work(doi, "resolve_identifier"))
            except ProviderError as exc:
                failures.append(f"crossref: {exc}")
            try:
                records.extend(
                    self._europe_pmc_lookup(f'DOI:"{doi}"', "resolve_identifier", {"doi": doi})
                )
            except ProviderError as exc:
                failures.append(f"europe-pmc: {exc}")
        elif pmid:
            pmid = pmid.strip()
            if not re.fullmatch(r"\d{1,10}", pmid):
                raise ReviewError("invalid PMID")
            try:
                records.append(self._pubmed_summary(pmid, "resolve_identifier"))
            except ProviderError as exc:
                failures.append(f"pubmed: {exc}")
            try:
                records.extend(
                    self._europe_pmc_lookup(
                        f"EXT_ID:{pmid} AND SRC:MED", "resolve_identifier", {"pmid": pmid}
                    )
                )
            except ProviderError as exc:
                failures.append(f"europe-pmc: {exc}")
        else:
            assert pmcid
            normalized = pmcid.upper()
            if not re.fullmatch(r"PMC\d+", normalized):
                raise ReviewError("invalid PMCID")
            records.extend(
                self._europe_pmc_lookup(
                    f"PMCID:{normalized}", "resolve_identifier", {"pmcid": normalized}
                )
            )
        if not doi:
            # Follow only identifiers linked to the requested record. Keep the
            # originating metadata alongside each independent provider result:
            # disagreement must survive reconciliation, never be overwritten.
            key, expected = ("pmid", pmid) if pmid else ("pmcid", normalized)
            candidates = [record for record in records
                          if str(record.get(key) or "").upper() == expected.upper()]
            if pmcid:
                discovered_pmid = next((str(record["pmid"]).strip() for record in candidates
                                        if record.get("pmid")), None)
                if discovered_pmid:
                    if not re.fullmatch(r"\d{1,10}", discovered_pmid):
                        failures.append("pubmed: source record supplied an invalid PMID")
                    else:
                        try:
                            additional = self._pubmed_summary(discovered_pmid, "resolve_identifier")
                            records.append(additional)
                            candidates.append(additional)
                        except ProviderError as exc:
                            failures.append(f"pubmed: {exc}")
            discovered_doi = next((str(record["doi"]) for record in candidates
                                   if record.get("doi")), None)
            if discovered_doi:
                try:
                    discovered_doi = normalize_doi(discovered_doi)
                except ReviewError:
                    failures.append("crossref: source record supplied an invalid DOI")
                else:
                    try:
                        records.append(self._crossref_work(discovered_doi, "resolve_identifier"))
                    except ProviderError as exc:
                        failures.append(f"crossref: {exc}")
            # At most one request to each of Europe PMC, PubMed, and Crossref;
            # discovered identifiers never recurse through this resolver.
        return self._envelope("resolve_identifier", "multiple", records, limitations=failures)

    def resolve_reference(self, reference: Mapping[str, Any]) -> dict[str, Any]:
        with self._operation_scope("resolve_reference"):
            for key in ("doi", "pmid", "pmcid"):
                if reference.get(key):
                    return self.resolve_identifier(**{key: str(reference[key])})
            title = str(reference.get("title", "")).strip()
            author = str(reference.get("author", "")).strip()
            year = str(reference.get("year", "")).strip()
            if not title:
                raise ReviewError("reference resolution requires an identifier or title")
            egress = {"title": title}
            if author:
                egress["author"] = author
            self._authorize("crossref", "resolve_reference", egress)
            query = " ".join(part for part in (title, author, year) if part)
            params: dict[str, Any] = {"query.bibliographic": query, "rows": 5}
            if self.contact_email:
                params["mailto"] = self.contact_email
            payload = self.transport.get(f"{CROSSREF}/works", params=params).json()
            records = [
                normalize_crossref(item)
                for item in payload.get("message", {}).get("items", [])[:5]
            ]
            return self._envelope("resolve_reference", "crossref", records)

    @staticmethod
    def reconcile_reference_metadata(records: list[Mapping[str, Any]], printed_reference: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Compare already-retrieved records locally; this performs no egress."""

        def canonical_text(value: Any) -> str:
            return " ".join(re.findall(r"\w+", (display_text(value) or "").casefold(), flags=re.UNICODE))

        def canonical_field(field: str, value: Any) -> str:
            if field == "doi":
                try:
                    return normalize_doi(str(value))
                except ReviewError:
                    return f"invalid-doi:{value}"
            if field == "pmcid":
                return str(value).strip().upper().removeprefix("PMC")
            return canonical_text(value)

        providers = {str(record.get("provider")) for record in records if record.get("provider") not in (None, "", "unknown")}
        fields = ("doi", "pmid", "pmcid", "title", "year", "container", "volume", "pages")
        differences = []
        missing = []
        printed = dict(printed_reference or {})
        printed["container"] = printed.get("container") or printed.get("journal")
        printed["pages"] = printed.get("pages") or printed.get("page")
        required = {"title", "year", "container"} | {field for field in ("volume", "pages") if printed.get(field)}
        for field in fields:
            values = {
                canonical_field(field, record.get(field))
                for record in records
                if record.get(field) not in (None, "")
            }
            if len(values) > 1:
                differences.append({"field": field, "values": sorted(values)})
            observed_by = {str(record.get("provider")) for record in records if record.get(field) not in (None, "") and record.get("provider") in providers}
            if field in required and len(observed_by) < 2:
                missing.append({"field": field, "reason": "fewer-than-two-provider-values"})
            if printed.get(field) and values and canonical_field(field, printed[field]) not in values:
                differences.append({"field": field, "printed": canonical_field(field, printed[field]), "values": sorted(values)})
            if printed_reference is not None and field in required and not printed.get(field):
                raw = canonical_text(printed.get("raw"))
                # An exact occurrence in the printed entry is useful; venue
                # abbreviation guesses and title similarity are never repairs.
                if len(values) != 1 or not raw or not any(f" {value} " in f" {raw} " for value in values):
                    missing.append({"field": field, "reason": "printed-field-unconfirmed"})
        agreement = "conflict" if differences else "single-source" if len(providers) < 2 else "incomplete" if missing else "consistent"
        return {
            "agreement": agreement,
            "agreementStrength": "strong" if agreement == "consistent" and printed_reference is not None else "conflict" if agreement == "conflict" else "weak",
            "differences": differences,
            "missingFields": [f"{item['field']}:{item['reason']}" for item in missing],
            "recordsCompared": len(records),
            "providers": sorted(providers), "providersCompared": sorted(providers), "providerCount": len(providers),
        }

    @staticmethod
    def reconcile_identifier_metadata(records: list[Mapping[str, Any]], *, doi: str) -> dict[str, Any]:
        """Reconcile a newly discovered DOI without inventing a printed entry."""

        expected = normalize_doi(doi)
        result = ScholarlyClient.reconcile_reference_metadata(records)
        absent = False
        conflicting = []
        for record in records:
            if not record.get("doi"):
                absent = True
                continue
            try:
                observed = normalize_doi(str(record["doi"]))
            except ReviewError:
                conflicting.append(str(record["doi"]))
                continue
            if observed != expected:
                conflicting.append(observed)
        if conflicting:
            result["differences"].append({"field": "doi", "expected": expected, "values": sorted(set(conflicting))})
            result["agreement"] = "conflict"
            result["agreementStrength"] = "conflict"
        elif absent:
            result["missingFields"].append("doi:identifier-unconfirmed")
            if result["agreement"] == "consistent":
                result["agreement"] = "incomplete"
        elif result["agreement"] == "consistent":
            result["agreementStrength"] = "strong"
        return result

    def get_publication_status(
        self, *, doi: str | None = None, pmid: str | None = None
    ) -> dict[str, Any]:
        if bool(doi) == bool(pmid):
            raise ReviewError("get_publication_status requires exactly one DOI or PMID")
        records: list[dict[str, Any]] = []
        failures: list[str] = []
        if doi:
            doi = normalize_doi(doi)
            try:
                records.append(self._crossref_work(doi, "get_publication_status"))
            except ProviderError as exc:
                failures.append(f"crossref: {exc}")
            try:
                records.extend(
                    self._europe_pmc_lookup(f'DOI:"{doi}"', "get_publication_status", {"doi": doi})
                )
            except ProviderError as exc:
                failures.append(f"europe-pmc: {exc}")
        else:
            assert pmid
            try:
                records.append(self._pubmed_summary(pmid, "get_publication_status"))
            except ProviderError as exc:
                failures.append(f"pubmed: {exc}")
            try:
                records.extend(
                    self._europe_pmc_lookup(
                        f"EXT_ID:{pmid} AND SRC:MED", "get_publication_status", {"pmid": pmid}
                    )
                )
            except ProviderError as exc:
                failures.append(f"europe-pmc: {exc}")
        resolved_pmid = pmid or next((str(record["pmid"]) for record in records if record.get("pmid")), None)
        structured = None
        if resolved_pmid:
            try:
                structured = self._pubmed_structured(resolved_pmid, "get_publication_status")
                records.append(structured)
            except ProviderError as exc:
                failures.append(f"pubmed-structured: {exc}")
        probe = self.probe_publication_status()
        if not probe["passed"]:
            failures.append("Publication-notice surveillance is unproven: known-positive controls failed.")
        signals: list[dict[str, Any]] = []
        for record in records:
            provider = record.get("provider")
            if record.get("isRetracted"):
                signals.append(
                    {
                        "provider": provider,
                        "kind": "retraction",
                        "detail": "record flagged retracted",
                    }
                )
            for update in record.get("updates", []):
                # update-to belongs to the updating notice. It is not proof
                # that this returned work was itself withdrawn.
                signals.append({"provider": provider, "kind": "update-notice", "detail": update})
            for notice in record.get("notices", []):
                kind = {
                    "RetractionIn": "retraction", "RetractedandRepublishedIn": "retraction",
                    "ErratumIn": "correction", "CorrectedandRepublishedIn": "correction",
                    "ExpressionOfConcernIn": "expression-of-concern",
                }.get(notice["refType"], "related-notice")
                signals.append({"provider": provider, "kind": kind, "relationship": notice["refType"], "detail": notice})
            types = " ".join(record.get("publicationTypes", [])).lower()
            for phrase, kind in (
                ("retracted publication", "retraction"),
                ("retraction of publication", "related-notice"),
                ("published erratum", "related-notice"),
            ):
                if phrase in types:
                    signals.append({"provider": provider, "kind": kind, "detail": phrase})
        kinds = {signal["kind"] for signal in signals}
        if any("retract" in kind for kind in kinds):
            state = "retracted"
        elif "expression-of-concern" in kinds:
            state = "expression-of-concern"
        elif any("correct" in kind or "errat" in kind for kind in kinds):
            state = "corrected"
        elif any(
            "preprint" in " ".join(record.get("publicationTypes", [])).lower() for record in records
        ):
            state = "preprint"
        elif structured is not None and probe["passed"] and not failures:
            state = "current"
        else:
            state = "unknown"
        return {
            "operation": "get_publication_status",
            "state": state,
            "signals": signals,
            "providersChecked": sorted({str(record.get("provider")) for record in records}),
            "checkedAt": utc_now(),
            "limitations": failures,
            "surveillance": probe,
            "records": records,
            "warning": "No status service is exhaustive; inspect publisher notices for high-stakes use.",
        }

    def get_corrections_retractions(
        self, *, doi: str | None = None, pmid: str | None = None
    ) -> dict[str, Any]:
        with self._operation_scope("get_corrections_retractions"):
            status = self.get_publication_status(doi=doi, pmid=pmid)
            return {
                "operation": "get_corrections_retractions",
                "status": status["state"],
                "notices": status["signals"],
                "providersChecked": status["providersChecked"],
                "checkedAt": status["checkedAt"],
                "limitations": status["limitations"],
                "surveillance": status["surveillance"],
            }

    def get_related_versions(self, *, doi: str) -> dict[str, Any]:
        record = self._crossref_work(doi, "get_related_versions")
        relation = record.get("relation", {})
        related = []
        for relationship, values in relation.items():
            for value in values if isinstance(values, list) else [values]:
                related.append({"relationship": relationship, "target": value})
        return self._envelope("get_related_versions", "crossref", related)

    def _source_document(self, url: str, identifiers: Mapping[str, str]) -> dict[str, Any]:
        def authorize(destination: str) -> None:
            parsed = validate_public_url(destination)
            self._authorize(str(parsed.hostname), "acquire_full_text", {**identifiers, "source": destination})

        getter = getattr(self.transport, "get_document", None)
        if getter is None:
            # Injectable test/adaptor transports may implement the base get
            # protocol only; the same authorization and parse bounds apply.
            authorize(url)
            response = self.transport.get(url, headers={"Accept": "application/xml, application/pdf"})
        else:
            response = getter(url, authorize_url=authorize)
        if response.status < 200 or response.status >= 300:
            raise ProviderError(f"source download returned HTTP {response.status}")
        media_type = next((value for name, value in response.headers.items() if name.lower() == "content-type"), None)
        # Text/Markdown are permitted for explicit local attachments. A remote
        # access-denied message in text/plain is not a complete paper.
        remote_type = media_type
        if (media_type or "").split(";", 1)[0].strip().lower() in {"text/plain", "text/markdown"}:
            remote_type = None
        document = parse_source_document(response.body, remote_type, response.url or url)
        for key, expected in identifiers.items():
            observed = document["identifiers"].get(key)
            if observed:
                if key == "doi":
                    try:
                        matches = normalize_doi(observed) == normalize_doi(expected)
                    except ReviewError as exc:
                        raise ProviderError("downloaded article contains an invalid source DOI") from exc
                elif key == "pmcid":
                    matches = observed.upper().removeprefix("PMC") == expected.upper().removeprefix("PMC")
                else:
                    matches = observed == expected
                if not matches:
                    raise ProviderError("downloaded article identifies a different source")
        return document

    def acquire_full_text(
        self, *, doi: str | None = None, pmid: str | None = None, pmcid: str | None = None
    ) -> dict[str, Any]:
        """Acquire a complete document for private workspace persistence.

        This internal method returns original bytes. It is deliberately absent
        from MCP's JSON response surface; workspace workflow owns file writes.
        """

        if sum(value is not None for value in (doi, pmid, pmcid)) != 1:
            raise ReviewError("acquire_full_text requires exactly one DOI, PMID, or PMCID")
        identifier = {"doi": normalize_doi(doi)} if doi else {"pmid": pmid} if pmid else {"pmcid": str(pmcid).upper()}
        limitations: list[str] = []
        attempts: list[dict[str, Any]] = []
        documents: list[dict[str, Any]] = []
        abstract_candidates: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        with self._operation_scope("acquire_full_text"):
            try:
                resolved = self.resolve_identifier(**identifier)
                records = resolved["records"]
                limitations.extend(resolved["limitations"])
            except ProviderError as exc:
                limitations.append(f"identity lookup: {exc}")
            key, value = next(iter(identifier.items()))
            epmc = next((record for record in records if record.get("provider") == "europe-pmc" and str(record.get(key) or "").casefold() == str(value).casefold()), None)
            resolved_pmid = pmid or (epmc or {}).get("pmid")
            resolved_doi = identifier.get("doi") or (epmc or {}).get("doi")
            if epmc and epmc.get("abstract"):
                abstract_candidates.append({"provider": "europe-pmc", "text": epmc["abstract"]})
            if resolved_pmid:
                try:
                    structured = self._pubmed_structured(str(resolved_pmid), "acquire_full_text")
                    if structured.get("abstract"):
                        abstract_candidates.append({"provider": "pubmed", "text": structured["abstract"]})
                except ProviderError as exc:
                    limitations.append(f"pubmed abstract: {exc}")
            if epmc and epmc.get("pmcid") and epmc.get("isOpenAccess"):
                url = f"{EUROPE_PMC}/{urllib.parse.quote(str(epmc['pmcid']), safe='')}/fullTextXML"
                try:
                    document = self._source_document(url, identifier)
                    document.update({"provider": "europe-pmc", "version": "repository-copy", "license": None})
                    documents.append(document)
                    attempts.append({"provider": "europe-pmc", "sourceUrl": url, "status": "obtained", "sha256": document["sha256"]})
                except ProviderError as exc:
                    attempts.append({"provider": "europe-pmc", "sourceUrl": url, "status": "failed", "error": str(exc)})
                    limitations.append(f"europe-pmc complete source: {exc}")
            if not documents and resolved_doi:
                if not self.contact_email:
                    limitations.append("Unpaywall requires configured OCSL_REVIEW_CONTACT_EMAIL; no contact identity was inferred.")
                else:
                    normalized = normalize_doi(str(resolved_doi))
                    try:
                        self._authorize("unpaywall", "acquire_full_text", {"doi": normalized})
                        payload = self.transport.get(f"{UNPAYWALL}/{urllib.parse.quote(normalized, safe='')}", params={"email": self.contact_email}).json()
                        if not isinstance(payload, dict) or normalize_doi(str(payload.get("doi") or "")) != normalized:
                            raise ProviderError("Unpaywall returned a different or absent DOI")
                        locations = ([payload["best_oa_location"]] if payload.get("best_oa_location") else []) + list(payload.get("oa_locations") or [])
                        urls: set[str] = set()
                        for location in locations:
                            if not isinstance(location, dict):
                                continue
                            url = location.get("url_for_pdf")
                            if not url:
                                landing = location.get("url_for_landing_page") or location.get("url")
                                if landing:
                                    attempts.append({"provider": "unpaywall", "sourceUrl": str(landing), "status": "landing-page-only"})
                                continue
                            if url in urls:
                                continue
                            if len(urls) >= 10:
                                limitations.append("Open-copy route bound reached after ten distinct document locations.")
                                break
                            urls.add(url)
                            try:
                                document = self._source_document(str(url), {"doi": normalized})
                                document.update({"provider": "unpaywall", "version": location.get("version"), "license": location.get("license")})
                                documents.append(document)
                                attempts.append({"provider": "unpaywall", "sourceUrl": str(url), "status": "obtained", "sha256": document["sha256"]})
                                break
                            except ProviderError as exc:
                                attempts.append({"provider": "unpaywall", "sourceUrl": str(url), "status": "failed", "error": str(exc)})
                        if not documents:
                            limitations.append("No complete readable document obtained from the available Unpaywall locations.")
                    except (ProviderError, ReviewError) as exc:
                        # Policy errors are never caught here: they must stop
                        # the denied route before I/O and remain distinguishable.
                        from .errors import ConfidentialityError
                        if isinstance(exc, ConfidentialityError):
                            raise
                        limitations.append(f"unpaywall: {exc}")
            abstract_record = max(abstract_candidates, key=lambda item: len(item["text"]), default=None)
            abstract = abstract_record["text"] if abstract_record else None
            abstract_complete = bool(abstract and re.search(r"[.!?][\"')\]]*$", abstract.strip()))
            if abstract and not abstract_complete:
                limitations.append("Provider abstract may end mid-sentence; completeness is unproven.")
            for document in documents:
                if document["supplements"]:
                    limitations.append("Supplement links were recorded; external supplement files require separate acquisition and examination.")
                if document["visualAssets"]:
                    limitations.append("XML graphics/media links were recorded; their visual content requires separate acquisition and inspection where relevant.")
            return {
                "operation": "acquire_full_text", "provider": documents[0]["provider"] if documents else "multiple",
                "retrievedAt": utc_now(), "accessLevel": "full-text" if documents else "abstract-only" if abstract else "metadata-only" if records else "inaccessible",
                "abstract": abstract, "abstractComplete": abstract_complete, "abstractProvider": abstract_record["provider"] if abstract_record else None,
                "abstractCandidates": abstract_candidates, "documents": documents, "limitations": limitations, "attempts": attempts,
            }

    def get_abstract_or_open_sections(
        self, *, doi: str | None = None, pmid: str | None = None, pmcid: str | None = None
    ) -> dict[str, Any]:
        with self._operation_scope("get_abstract_or_open_sections"):
            resolved = self.resolve_identifier(doi=doi, pmid=pmid, pmcid=pmcid)
            epmc = next(
                (item for item in resolved["records"] if item.get("provider") == "europe-pmc"),
                None,
            )
            if not epmc:
                return self._envelope(
                    "get_abstract_or_open_sections",
                    "europe-pmc",
                    [],
                    access="metadata-only",
                    limitations=["No Europe PMC record resolved."],
                )
            records: dict[str, Any] = {"abstract": epmc.get("abstract"), "sections": []}
            access = "abstract-only" if epmc.get("abstract") else "metadata-only"
            open_pmcid = epmc.get("pmcid")
            if open_pmcid and epmc.get("isOpenAccess"):
                self._authorize(
                    "europe-pmc",
                    "get_abstract_or_open_sections",
                    {"pmcid": open_pmcid},
                )
                response = self.transport.get(
                    f"{EUROPE_PMC}/{urllib.parse.quote(open_pmcid)}/fullTextXML",
                    headers={"Accept": "application/xml"},
                )
                if re.search(br"<!ENTITY\b", response.body, re.IGNORECASE):
                    raise ProviderError("Europe PMC preview contains entity declarations")
                try:
                    root = ET.fromstring(response.body)
                except ET.ParseError as exc:
                    raise ProviderError("Europe PMC returned malformed full-text XML") from exc
                sections = []
                for section in root.findall(".//sec")[:20]:
                    title = (
                        " ".join("".join(section.find("title").itertext()).split())
                        if section.find("title") is not None
                        else ""
                    )
                    paragraphs = [
                        " ".join("".join(p.itertext()).split()) for p in section.findall("./p")
                    ]
                    text = "\n".join(part for part in paragraphs if part)[:4000]
                    if title or text:
                        sections.append({"title": title, "text": text})
                records["sections"] = sections
                access = "partial-text" if sections else access
            result = self._envelope(
                "get_abstract_or_open_sections",
                "europe-pmc",
                records,
                access=access,
            )
            result["complete"] = False
            result["truncated"] = access == "partial-text"
            if access == "partial-text":
                result["limitations"].append("Preview only: at most twenty sections and 4,000 characters per section; this is not complete full-text evidence.")
            return result

    def _epmc_links(self, kind: str, *, source: str, identifier: str, limit: int) -> dict[str, Any]:
        rows = bounded_limit(limit)
        source = source.upper()
        if source not in {"MED", "PMC", "AGR", "CBA", "CTX", "ETH", "PAT"}:
            raise ReviewError("unsupported Europe PMC source")
        identifier = identifier.strip()
        egress_key = "pmcid" if source == "PMC" else "pmid"
        self._authorize(
            "europe-pmc", f"get_{kind}", {egress_key: identifier, "rows": rows, "source": source}
        )
        payload = self.transport.get(
            f"{EUROPE_PMC}/{source}/{urllib.parse.quote(identifier)}/{kind}",
            params={"pageSize": rows, "format": "json"},
        ).json()
        key = "referenceList" if kind == "references" else "citationList"
        singular = "reference" if kind == "references" else "citation"
        records = payload.get(key, {}).get(singular, [])[:rows]
        result = self._envelope(f"get_{kind}", "europe-pmc", records)
        total = payload.get("hitCount")
        total = total if isinstance(total, int) and not isinstance(total, bool) and total >= len(records) else None
        result.update({"total": total, "complete": total is not None and total == len(records),
                       "truncated": total is None or total > len(records)})
        return result

    def get_references(
        self,
        *,
        doi: str | None = None,
        source: str | None = None,
        identifier: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if source and identifier:
            return self._epmc_links("references", source=source, identifier=identifier, limit=limit)
        if not doi:
            raise ReviewError("get_references requires DOI or Europe PMC source+identifier")
        doi = normalize_doi(doi)
        self._authorize(
            "opencitations", "get_references", {"doi": doi, "rows": bounded_limit(limit)}
        )
        encoded = urllib.parse.quote(f"doi:{doi}", safe=":")
        records = self.transport.get(f"{OPENCITATIONS_INDEX}/references/{encoded}").json()
        return self._bounded_links("get_references", records, limit)

    def get_citations(
        self,
        *,
        doi: str | None = None,
        source: str | None = None,
        identifier: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if source and identifier:
            return self._epmc_links("citations", source=source, identifier=identifier, limit=limit)
        if not doi:
            raise ReviewError("get_citations requires DOI or Europe PMC source+identifier")
        doi = normalize_doi(doi)
        self._authorize(
            "opencitations", "get_citations", {"doi": doi, "rows": bounded_limit(limit)}
        )
        encoded = urllib.parse.quote(f"doi:{doi}", safe=":")
        records = self.transport.get(f"{OPENCITATIONS_INDEX}/citations/{encoded}").json()
        return self._bounded_links("get_citations", records, limit)

    def _bounded_links(self, operation: str, records: Any, limit: int) -> dict[str, Any]:
        if not isinstance(records, list):
            raise ProviderError("citation provider returned a non-list response")
        selected = records[:bounded_limit(limit)]
        result = self._envelope(operation, "opencitations", selected)
        result.update({"total": len(records), "complete": len(selected) == len(records),
                       "truncated": len(selected) < len(records)})
        return result

    def resolve_dataset_or_software(
        self,
        *,
        doi: str | None = None,
        query: str | None = None,
        kind: str = "dataset",
        limit: int = 10,
    ) -> dict[str, Any]:
        if kind not in {"dataset", "software"}:
            raise ReviewError("kind must be dataset or software")
        if bool(doi) == bool(query):
            raise ReviewError("provide exactly one DOI or query")
        if doi:
            doi = normalize_doi(doi)
            self._authorize(
                "datacite",
                "resolve_dataset_or_software",
                {"datasetDoi" if kind == "dataset" else "softwareDoi": doi, "kind": kind},
            )
            encoded = urllib.parse.quote(doi, safe="")
            payload = self.transport.get(f"{DATACITE}/dois/{encoded}").json()
            records = [normalize_datacite(payload.get("data", {}))]
        else:
            rows = bounded_limit(limit)
            assert query is not None
            self._authorize(
                "datacite",
                "resolve_dataset_or_software",
                {"query": query, "rows": rows, "kind": kind},
            )
            resource_type = "dataset" if kind == "dataset" else "software"
            payload = self.transport.get(
                f"{DATACITE}/dois",
                params={"query": query, "page[size]": rows, "resource-type-id": resource_type},
            ).json()
            records = [normalize_datacite(item) for item in payload.get("data", [])[:rows]]
        return self._envelope("resolve_dataset_or_software", "datacite", records)


TOOL_METHODS: dict[str, str] = {
    "resolve_identifier": "resolve_identifier",
    "resolve_reference": "resolve_reference",
    "reconcile_reference_metadata": "reconcile_reference_metadata",
    "search_crossref": "search_crossref",
    "search_pubmed": "search_pubmed",
    "search_europe_pmc": "search_europe_pmc",
    "get_publication_status": "get_publication_status",
    "get_corrections_retractions": "get_corrections_retractions",
    "get_related_versions": "get_related_versions",
    "get_abstract_or_open_sections": "get_abstract_or_open_sections",
    "get_references": "get_references",
    "get_citations": "get_citations",
    "resolve_dataset_or_software": "resolve_dataset_or_software",
}


def call_tool(client: ScholarlyClient, name: str, arguments: Mapping[str, Any]) -> Any:
    method_name = TOOL_METHODS.get(name)
    if not method_name:
        raise ReviewError(f"unknown scholarly operation: {name}")
    method: Callable[..., Any] = getattr(client, method_name)
    with client._operation_scope(name):
        if name == "resolve_reference":
            return method(arguments.get("reference", arguments))
        if name == "reconcile_reference_metadata":
            return method(list(arguments.get("records", [])))
        return method(**dict(arguments))
