"""Bounded clients for public scholarly metadata and open-literature services."""

from __future__ import annotations

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
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .confidentiality import EgressPlan, append_egress_event, authorize_egress
from .errors import ProviderError, ReviewError

CROSSREF = "https://api.crossref.org"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DATACITE = "https://api.datacite.org"
OPENCITATIONS_INDEX = "https://api.opencitations.net/index/v2"
MAX_RESULTS = 25
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TRANSIENT_PROVIDER_STATUSES = frozenset({429, 503})
MAX_HTTP_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 5.0


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
    ) -> RawResponse:
        if not url.startswith("https://"):
            raise ProviderError("scholarly providers must use HTTPS")
        filtered = {key: value for key, value in (params or {}).items() if value is not None}
        encoded = urllib.parse.urlencode(filtered, doseq=True)
        target = f"{url}?{encoded}" if encoded else url
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        request_headers.update(headers or {})
        request = urllib.request.Request(target, headers=request_headers, method="GET")
        deadline = self._monotonic() + self.timeout
        for attempt in range(1, self.max_attempts + 1):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ProviderError("provider request exceeded the configured timeout")

            try:
                with urllib.request.urlopen(request, timeout=remaining) as response:
                    response_headers = dict(response.headers.items())
                    status = int(response.status)
                    if status >= 400:
                        detail = response.read(512).decode("utf-8", errors="replace")
                        raw = None
                    else:
                        length = response.headers.get("Content-Length")
                        if length and int(length) > MAX_RESPONSE_BYTES:
                            raise ProviderError(
                                "provider response exceeds the configured size bound"
                            )
                        body = response.read(MAX_RESPONSE_BYTES + 1)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ProviderError(
                                "provider response exceeds the configured size bound"
                            )
                        raw = RawResponse(body, status, response_headers)
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
            except (urllib.error.URLError, TimeoutError) as exc:
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


def _first(value: Any, default: Any = None) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return default if value is None else value


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
        "title": _first(item.get("title")),
        "authors": [
            " ".join(part for part in (author.get("given"), author.get("family")) if part)
            for author in item.get("author", [])
        ],
        "year": _crossref_year(item),
        "container": _first(item.get("container-title")),
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
        "pmid": item.get("pmid"),
        "pmcid": item.get("pmcid"),
        "title": item.get("title"),
        "authors": [part.strip() for part in author_text.split(",") if part.strip()],
        "year": int(item["pubYear"]) if str(item.get("pubYear", "")).isdigit() else None,
        "container": item.get("journalTitle"),
        "publicationTypes": item.get("pubTypeList", {}).get("pubType", []),
        "isOpenAccess": item.get("isOpenAccess") == "Y" or item.get("isOpenAccess") is True,
        "isRetracted": item.get("isRetracted") == "Y" or item.get("isRetracted") is True,
        "hasReferences": item.get("hasReferences") == "Y" or item.get("hasReferences") is True,
        "hasTextMinedTerms": item.get("hasTextMinedTerms") == "Y",
        "abstract": item.get("abstractText"),
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

    def _authorize(self, provider: str, operation: str, arguments: Mapping[str, Any]) -> None:
        event = authorize_egress(
            provider=provider, operation=operation, arguments=arguments, plan=self.plan
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
            "select": "DOI,title,author,published,container-title,type,relation,update-to,license,URL",
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
        return normalize_crossref(payload.get("message", {}))

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
            "title": item.get("title"),
            "authors": [author.get("name") for author in item.get("authors", [])],
            "published": item.get("pubdate"),
            "container": item.get("fulljournalname") or item.get("source"),
            "publicationTypes": item.get("pubtype", []),
            "identifiers": item.get("articleids", []),
        }

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
        return [
            normalize_europe_pmc(item)
            for item in payload.get("resultList", {}).get("result", [])[:5]
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
            records.append(self._pubmed_summary(pmid, "resolve_identifier"))
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
        return self._envelope("resolve_identifier", "multiple", records, limitations=failures)

    def resolve_reference(self, reference: Mapping[str, Any]) -> dict[str, Any]:
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
            normalize_crossref(item) for item in payload.get("message", {}).get("items", [])[:5]
        ]
        return self._envelope("resolve_reference", "crossref", records)

    @staticmethod
    def reconcile_reference_metadata(records: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Compare already-retrieved records locally; this performs no egress."""

        if len(records) < 2:
            return {
                "agreement": "single-source",
                "differences": [],
                "recordsCompared": len(records),
            }

        def canonical_text(value: Any) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

        fields = ("doi", "title", "year", "container")
        differences = []
        for field in fields:
            values = {
                canonical_text(record.get(field))
                for record in records
                if record.get(field) not in (None, "")
            }
            if len(values) > 1:
                differences.append({"field": field, "values": sorted(values)})
        return {
            "agreement": "conflict" if differences else "consistent",
            "differences": differences,
            "recordsCompared": len(records),
            "providers": sorted({str(record.get("provider", "unknown")) for record in records}),
        }

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
            records.append(self._pubmed_summary(pmid, "get_publication_status"))
            try:
                records.extend(
                    self._europe_pmc_lookup(
                        f"EXT_ID:{pmid} AND SRC:MED", "get_publication_status", {"pmid": pmid}
                    )
                )
            except ProviderError as exc:
                failures.append(f"europe-pmc: {exc}")
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
                kind = str(update.get("type", "update")).lower()
                signals.append({"provider": provider, "kind": kind, "detail": update})
            types = " ".join(record.get("publicationTypes", [])).lower()
            for phrase, kind in (
                ("retracted publication", "retraction"),
                ("retraction of publication", "retraction"),
                ("published erratum", "correction"),
                ("expression of concern", "expression-of-concern"),
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
        elif records:
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
            "warning": "No status service is exhaustive; inspect publisher notices for high-stakes use.",
        }

    def get_corrections_retractions(
        self, *, doi: str | None = None, pmid: str | None = None
    ) -> dict[str, Any]:
        status = self.get_publication_status(doi=doi, pmid=pmid)
        return {
            "operation": "get_corrections_retractions",
            "status": status["state"],
            "notices": status["signals"],
            "providersChecked": status["providersChecked"],
            "checkedAt": status["checkedAt"],
            "limitations": status["limitations"],
        }

    def get_related_versions(self, *, doi: str) -> dict[str, Any]:
        record = self._crossref_work(doi, "get_related_versions")
        relation = record.get("relation", {})
        related = []
        for relationship, values in relation.items():
            for value in values if isinstance(values, list) else [values]:
                related.append({"relationship": relationship, "target": value})
        return self._envelope("get_related_versions", "crossref", related)

    def get_abstract_or_open_sections(
        self, *, doi: str | None = None, pmid: str | None = None, pmcid: str | None = None
    ) -> dict[str, Any]:
        resolved = self.resolve_identifier(doi=doi, pmid=pmid, pmcid=pmcid)
        epmc = next(
            (item for item in resolved["records"] if item.get("provider") == "europe-pmc"), None
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
            self._authorize("europe-pmc", "get_abstract_or_open_sections", {"pmcid": open_pmcid})
            response = self.transport.get(
                f"{EUROPE_PMC}/{urllib.parse.quote(open_pmcid)}/fullTextXML",
                headers={"Accept": "application/xml"},
            )
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
            access = "full-text"
        return self._envelope("get_abstract_or_open_sections", "europe-pmc", records, access=access)

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
        return self._envelope(f"get_{kind}", "europe-pmc", records)

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
        return self._envelope("get_references", "opencitations", records[: bounded_limit(limit)])

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
        return self._envelope("get_citations", "opencitations", records[: bounded_limit(limit)])

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
    if name == "resolve_reference":
        return method(arguments.get("reference", arguments))
    if name == "reconcile_reference_metadata":
        return method(list(arguments.get("records", [])))
    return method(**dict(arguments))
