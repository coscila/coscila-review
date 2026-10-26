# SPDX-License-Identifier: Apache-2.0
"""Census inputs from byte caches and live routes, with provenance per item.

Review documents, cited-work abstracts and cited-work full texts come first from
byte caches: a pinned ``literature-evidence@2`` projection and a provider
response cache, both read through ``store_client`` (ATTACHed
``mode=ro&immutable=1``; each reader refuses every table outside its allowlist,
so no verdict, score, locus, attempt, summary or bibliography property is ever
read). Cached bytes are accepted only after they hash to the digest their cache
records and the document's own identifiers agree with the requested DOI (PMCID
or PMID where the document carries no DOI); anything else is acquired live
through the configured route, public (Europe PMC fullTextXML, the publisher
page) or entitled (the publisher page under the institution's licence). A
review document must also be the article itself: a page without citation
markers or a bibliography (a paywall or landing page) is refused. Every item
records where its bytes came from: ``cache-hit-public``, ``cache-hit-entitled``,
``live-public``, ``live-entitled`` or ``unavailable``.

Fresh enumeration builds the review manifest from live provider pages, which are
kept with their digests; a stored manifest from an earlier study is compared with
the fresh population afterwards and never imported.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET

from .errors import ProviderError, ReviewError, ValidationError
from .scholarly import EUROPE_PMC, normalize_doi, normalize_europe_pmc
from .scores_db import PROVENANCE as ITEM_PROVENANCE

# The five acquisition classes; ITEM_PROVENANCE adds "supplied": material an input record carried without an
# acquisition record (manifests written before acquisition existed, or an abstract another provider supplied).
PROVENANCE = tuple(label for label in ITEM_PROVENANCE if label != "supplied")
CLASS_OF = {"cache-hit-public": "public", "cache-hit-entitled": "entitled", "live-public": "public",
            "live-entitled": "entitled", "unavailable": "unavailable"}
ROUTE_CLASSES = ("public", "entitled")
DOCUMENT_KINDS = ("jats", "html")
KIND_OF_MEDIA = {"application/xml": "jats", "text/xml": "jats", "application/jats+xml": "jats", "text/html": "html",
                 "application/xhtml+xml": "html"}
SUFFIX = {"jats": ".xml", "html": ".html"}
SEARCH = EUROPE_PMC + "/search"
FULL_TEXT = EUROPE_PMC + "/%s/fullTextXML"
TABLE_TARGET = re.compile(r"tables/(\d+)")
MAX_TABLE_PAGES = 100
ENUMERATION_FORMAT = "ocsl-census-enumeration-v1"
COMPARISON_FORMAT = "ocsl-census-manifest-comparison-v1"
# Requests carry identifiers only: no contact address, no credentials, no cookies.
USER_AGENT = "ocsl-review census inputs"
# Extraction refusals that mean the bytes are not the article's full text (a landing, paywall or abstract page).
NOT_ARTICLE = ("no explicit bibliography targets", "needs an explicit article container", "needs an article body")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# ----------------------------------------------------------------------------- identity
def normal_doi(value: Any) -> str | None:
    try:
        return normalize_doi(str(value)) if value else None
    except ReviewError:
        return None


def normal_pmcid(value: Any) -> str | None:
    match = re.fullmatch(r"(?:PMC)?(\d{1,12})", str(value or "").strip(), re.I)
    return "PMC" + match.group(1) if match else None


def normal_pmid(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{1,10}", text) else None


def identifiers_of(value: Mapping[str, Any]) -> dict[str, str]:
    """DOI, PMID and PMCID of a record, normalised; absent or malformed values are left out."""
    found = {"doi": normal_doi(value.get("doi")), "pmid": normal_pmid(value.get("pmid")),
             "pmcid": normal_pmcid(value.get("pmcid"))}
    return {key: item for key, item in found.items() if item}


def store_identifiers(identifiers: Mapping[str, str]) -> dict[str, str]:
    """The lower-case spellings a corpus store keys works by."""
    return {key: value.lower() for key, value in identifiers.items()}


def check_identity(observed: Mapping[str, str], expected: Mapping[str, str], *,
                   require: Sequence[str] = ("doi",)) -> tuple[bool, str]:
    """(accepted, method or reason): every shared identifier agrees and at least one required one is shared."""
    shared = [key for key in ("doi", "pmcid", "pmid") if observed.get(key) and expected.get(key)]
    if any(observed[key] != expected[key] for key in shared):
        return False, "identity-mismatch"
    if not any(key in shared for key in require):
        return False, "identity-unconfirmed"
    return True, "+".join(shared)


# Review documents: the DOI decides; a JATS document without a DOI can still be confirmed by its PMCID or PMID.
REVIEW_IDENTITY = ("doi", "pmcid", "pmid")


class _HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.canonical: str | None = None

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").strip().lower()
            if name:
                self.meta.setdefault(name, []).append(values.get("content", "").strip())
        elif tag == "link" and "canonical" in values.get("rel", "").lower().split() and values.get("href"):
            self.canonical = self.canonical or values["href"].strip()


def _html_head(body: bytes) -> _HeadParser:
    parser = _HeadParser()
    try:
        parser.feed(body.decode("utf-8"))
        parser.close()
    except (UnicodeError, ValueError) as error:
        raise ValidationError("publisher HTML is not readable UTF-8") from error
    return parser


def _jats_root(body: bytes) -> ET.Element:
    if re.search(br"<!ENTITY\b", body, re.I):
        raise ValidationError("XML declares entities")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ValidationError("XML is malformed") from error
    for node in root.iter():
        if isinstance(node.tag, str):
            node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag == "pmc-articleset":
        articles = root.findall("article")
        if len(articles) != 1:
            raise ValidationError("a PMC article set needs exactly one article")
        root = articles[0]
    return root


def document_identifiers(body: bytes, kind: str) -> dict[str, str]:
    """The identifiers a document declares about itself: JATS article-meta ids, or the HTML citation meta tags."""
    if kind == "jats":
        found: dict[str, str | None] = {}
        for node in _jats_root(body).findall("./front/article-meta/article-id"):
            scheme, text = (node.get("pub-id-type") or "").lower(), "".join(node.itertext()).strip()
            if scheme == "doi":
                found["doi"] = normal_doi(text)
            elif scheme in ("pmcid", "pmc"):
                found["pmcid"] = normal_pmcid(text)
            elif scheme == "pmid":
                found["pmid"] = normal_pmid(text)
        return {key: value for key, value in found.items() if value}
    if kind == "html":
        meta = _html_head(body).meta
        dois = [normal_doi(value) for name in ("citation_doi", "prism.doi") for value in meta.get(name, [])]
        dois += [normal_doi(value) for value in meta.get("dc.identifier", []) if "10." in value]
        distinct = {doi for doi in dois if doi}
        if len(distinct) > 1:
            return {"doi": "conflicting:" + ",".join(sorted(distinct))}
        return {"doi": distinct.pop()} if distinct else {}
    raise ValidationError(f"unsupported document kind: {kind}")


def assess_document(body: bytes, kind: str) -> tuple[str, str | None, list[tuple[int, str]]]:
    """Whether review bytes are the article, and the table pages an HTML article links: (state, reason, links).

    ``ok``; ``not-article`` (no citation markers or bibliography, as on a landing or paywall page); or
    ``extraction-failed`` (the article, but the structured extraction refuses it; preparation records that reason).
    """
    from . import structured
    try:
        value = structured.extract(body, html=kind == "html")
    except ValidationError as error:
        message = str(error)
        return ("not-article" if any(part in message for part in NOT_ARTICLE) else "extraction-failed"), message[:200], []
    if not value["identities"]:
        return "not-article", "no bibliography", []
    if not value["metadata"].get("anchors"):
        return "not-article", "no in-text citation markers", []
    return "ok", None, _linked(value["warnings"]) if kind == "html" else []


def _linked(warnings: Iterable[Mapping[str, Any]]) -> list[tuple[int, str]]:
    """(table number, link target) of tables a publisher renders on pages of their own, as the extraction sees them."""
    found: dict[int, str] = {}
    for warning in warnings:
        if warning.get("code") != "external-table-content-unprovided":
            continue
        for target in warning.get("targets", []):
            match = TABLE_TARGET.search(str(target))
            if match:
                found.setdefault(int(match.group(1)), str(target))
    return sorted(found.items())


def html_base_url(body: bytes) -> str | None:
    """The page's own address (canonical link, og:url or citation_fulltext_html_url), for resolving table links."""
    head = _html_head(body)
    for value in [head.canonical] + head.meta.get("og:url", []) + head.meta.get("citation_fulltext_html_url", []):
        if value and value.startswith("https://"):
            return value
    return None


# ----------------------------------------------------------------------------- byte caches
@dataclass
class Candidate:
    """One cached copy of a document, not yet accepted."""

    kind: str
    klass: str
    cache: str
    sha256: str
    load: Callable[[], bytes]
    detail: dict[str, Any]
    base_url: str | None = None
    tables: list[tuple[int, str]] = field(default_factory=list)   # (number, sha256) of table pages the cache holds

    def describe(self) -> dict[str, Any]:
        return {"cache": self.cache, "sha256": self.sha256, "kind": self.kind,
                **{k: v for k, v in self.detail.items() if k in ("source", "documentId", "requestKey")}}


class InputCaches:
    """The byte caches of a census study: a literature-evidence@2 store and a provider response cache (either optional).

    Store documents count as public when a public repository holds them (Europe PMC); retained captures and
    cached publisher pages take the class of the journal's route, entitled when the journal has none.
    """

    def __init__(self, store=None, provider_cache=None):
        self.store = store
        self.provider_cache = provider_cache

    @classmethod
    def open(cls, *, store: str | Path | None = None, store_root: str | None = None,
             provider_cache: str | Path | None = None) -> "InputCaches | None":
        from .store_client import EvidenceStore, ProviderCache
        if store is None and provider_cache is None:
            if store_root:
                raise ValidationError("--store-root pins a store; give --store as well")
            return None
        opened_store = EvidenceStore(Path(store).absolute(), expected_root=store_root) if store else None
        try:
            opened_cache = ProviderCache(Path(provider_cache).absolute()) if provider_cache else None
        except BaseException:
            if opened_store is not None:
                opened_store.close()
            raise
        return cls(opened_store, opened_cache)

    def describe(self) -> dict[str, Any]:
        return {"store": self.store.version if self.store else None,
                "providerCache": self.provider_cache.version if self.provider_cache else None}

    def version_of(self, cache: str | None) -> str | None:
        """The version string a cache hit records: the store's Merkle root or the provider cache's digest."""
        if cache == "store" and self.store is not None:
            return self.store.version["merkleRoot"]
        if cache == "provider-cache" and self.provider_cache is not None:
            return self.provider_cache.version["sha256"]
        return None

    def close(self) -> None:
        for cache in (self.store, self.provider_cache):
            if cache is not None:
                cache.close()

    # ------------------------------------------------------------ review documents
    def store_review_candidates(self, doi: str, identifiers: Mapping[str, str], cache_class: str,
                                rejected: list[dict[str, Any]]) -> list[Candidate]:
        from .store_client import ByteCacheError
        store = self.store
        if store is None:
            return []
        version = store.version["merkleRoot"]
        found = []
        for row in store.retained_captures(doi):
            kind = row["route"] if row["route"] in DOCUMENT_KINDS else None
            if kind is None:
                rejected.append({"cache": "store", "source": "retained-capture", "sha256": row["capture_sha256"],
                                 "reason": "unsupported-route", "route": row["route"]})
                continue
            found.append(Candidate(kind, cache_class, "store", row["capture_sha256"],
                                   lambda digest=row["capture_sha256"]: store.read(digest),
                                   {"source": "retained-capture", "cacheVersion": version, "mediaType": row["media_type"],
                                    "capturedAt": row["captured_at"]}))
        work_id, reason = store.find_work(store_identifiers(identifiers))
        if work_id is None:
            if reason != "not-in-store":
                rejected.append({"cache": "store", "source": "store-document", "reason": reason})
            return found
        for document in store.documents(work_id, "full-text"):
            provider = document["provider"]
            if provider not in ("europe-pmc", "publisher"):
                continue
            try:
                metadata = json.loads(document["metadata_json"] or "{}")
            except ValueError:
                metadata = {}
            raw, tables = metadata.get("rawArtifactSha256") if isinstance(metadata, dict) else None, []
            if provider == "publisher":
                # The publisher page as served (before linked tables were inlined) and its table pages.
                try:
                    stored = json.loads(store.read(document["artifact_sha256"]))
                except ByteCacheError as error:
                    rejected.append({"cache": "store", "source": "store-document", "documentId": document["document_id"],
                                     "reason": "sha256-mismatch", "detail": str(error)[:200]})
                    continue
                except (ValueError, UnicodeError):
                    rejected.append({"cache": "store", "source": "store-document", "documentId": document["document_id"],
                                     "reason": "unreadable"})
                    continue
                if not isinstance(stored, dict):
                    continue
                raw = stored.get("originalArtifactSha256") or raw
                components = stored.get("components") if isinstance(stored.get("components"), list) else []
                for component in (item for item in components[1:] if isinstance(item, dict)):
                    match = TABLE_TARGET.search(str(component.get("sourceUrl", "")))
                    if match and store.artifact(str(component.get("sha256", ""))):
                        tables.append((int(match.group(1)), str(component["sha256"])))
            artifact = store.artifact(str(raw or ""))
            kind = KIND_OF_MEDIA.get(str((artifact or {}).get("media_type", "")).split(";")[0].strip().lower())
            if artifact is None or kind is None:
                continue
            source_url = str(document["source_url"] or "")
            found.append(Candidate(kind, "public" if provider == "europe-pmc" else cache_class, "store", raw,
                                   lambda digest=raw: store.read(digest),
                                   {"source": "store-document", "cacheVersion": version, "documentId": document["document_id"],
                                    "provider": provider, "sourceUrl": source_url, "capturedAt": document["created_at"]},
                                   base_url=source_url if source_url.startswith("https://") else None, tables=tables))
        return found

    def provider_review_candidates(self, doi: str, identifiers: Mapping[str, str], cache_class: str,
                                   rejected: list[dict[str, Any]]) -> list[Candidate]:
        from .store_client import ByteCacheError
        cache = self.provider_cache
        if cache is None:
            return []
        requests = []
        if identifiers.get("pmcid"):
            requests.append(("jats", "public", "europe-pmc", FULL_TEXT % identifiers["pmcid"]))
        requests.append(("html", cache_class, "publisher", "https://doi.org/" + urllib.parse.quote(doi, safe="/()")))
        found = []
        for kind, klass, provider, url in requests:
            try:
                response = cache.get(url, document=True)
            except ByteCacheError as error:
                rejected.append({"cache": "provider-cache", "source": "provider-response", "url": url,
                                 "reason": "sha256-mismatch", "detail": str(error)[:200]})
                continue
            if response is None:
                continue
            found.append(Candidate(kind, klass, "provider-cache", response.sha256, lambda body=response.body: body,
                                   {"source": "provider-response", "cacheVersion": cache.version["sha256"],
                                    "requestKey": response.key, "provider": provider, "sourceUrl": response.url,
                                    "retrievedAt": response.retrieved_at},
                                   base_url=response.url if response.url.startswith("https://") else None))
        return found

    def table_page(self, url: str) -> tuple[bytes, dict[str, Any]] | None:
        """A linked table page from the provider cache (bytes verified)."""
        from .store_client import ByteCacheError
        if self.provider_cache is None:
            return None
        try:
            response = self.provider_cache.get(url, document=True)
        except ByteCacheError:
            return None
        if response is None:
            return None
        return response.body, {"source": "provider-cache", "sha256": response.sha256, "requestKey": response.key,
                               "cacheVersion": self.provider_cache.version["sha256"]}

    # ----------------------------------------------------------------- abstracts
    def abstract(self, work: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """A cached Europe PMC core record with an abstract for a cited work: (hit, rejected candidates).

        The request is the one the live lookup would make (by DOI, else by PMID). In a cached provider response
        the first record carrying the requested identifier decides, as it does live; the hit carries that record's
        raw ``abstractText`` unchanged, the outcome class the live lookup would give and its provenance. A record
        without an abstract is no hit: the live lookup decides that case.
        """
        from .store_client import ByteCacheError
        wanted = identifiers_of(work)
        rejected: list[dict[str, Any]] = []
        scheme = "doi" if wanted.get("doi") else "pmid" if wanted.get("pmid") else None
        if scheme is None:
            return None, rejected
        outcome = "europe-pmc-core" if scheme == "doi" else "europe-pmc-core-pmid"
        if self.provider_cache is not None:
            query = f'DOI:"{wanted["doi"]}"' if scheme == "doi" else f"EXT_ID:{wanted['pmid']} AND SRC:MED"
            try:
                response = self.provider_cache.get(SEARCH, params={"query": query, "pageSize": 5, "resultType": "core",
                                                                   "format": "json"})
            except ByteCacheError as error:
                response = None
                rejected.append({"cache": "provider-cache", "reason": "sha256-mismatch", "detail": str(error)[:200]})
            if response is not None:
                hit = self._provider_abstract(response, wanted, scheme, outcome, rejected)
                if hit is not None:
                    return hit, rejected
        if self.store is not None:
            hit = self._store_abstract(wanted, scheme, outcome, rejected)
            if hit is not None:
                return hit, rejected
        return None, rejected

    def _provider_abstract(self, response, wanted, scheme, outcome, rejected):
        try:
            payload = json.loads(response.body)
            results = payload.get("resultList", {}).get("result") or []
        except (ValueError, AttributeError):
            rejected.append({"cache": "provider-cache", "reason": "unreadable", "requestKey": response.key})
            return None
        for index, raw in enumerate(results if isinstance(results, list) else []):
            if not isinstance(raw, dict) or identifiers_of(normalize_europe_pmc(raw)).get(scheme) != wanted[scheme]:
                continue
            accepted, method = _record_identity(raw, wanted, scheme)
            pointer = f"/resultList/result/{index}"
            if not accepted:
                rejected.append({"cache": "provider-cache", "reason": method or "identity-unconfirmed",
                                 "requestKey": response.key, "pointer": pointer})
                return None
            if not raw.get("abstractText"):
                return None
            return {"abstract": raw["abstractText"], "outcome": outcome, "provenance": {
                "provenance": "cache-hit-public", "cache": "provider-cache",
                "cacheVersion": self.provider_cache.version["sha256"], "sourceSha256": response.sha256,
                "requestKey": response.key, "pointer": pointer, "identity": method, "provider": "europe-pmc",
                "sourceUrl": response.url, "retrievedAt": response.retrieved_at}}
        return None

    def _store_abstract(self, wanted, scheme, outcome, rejected):
        from .evidence import complete_abstract
        from .store_client import ByteCacheError
        store = self.store
        work_id, reason = store.find_work(store_identifiers(wanted))
        if work_id is None:
            if reason != "not-in-store":
                rejected.append({"cache": "store", "reason": reason})
            return None
        for document in store.documents(work_id, "abstract"):
            if document["provider"] != "europe-pmc":
                continue      # legacy normalised columns and other providers are never abstracts of record
            try:
                body = store.read(document["artifact_sha256"])
                value = json.loads(body)
                metadata = json.loads(document["metadata_json"] or "{}")
            except ByteCacheError as error:
                rejected.append({"cache": "store", "documentId": document["document_id"], "reason": "sha256-mismatch",
                                 "detail": str(error)[:200]})
                continue
            except (ValueError, UnicodeError):
                rejected.append({"cache": "store", "documentId": document["document_id"], "reason": "unreadable"})
                continue
            candidate = metadata.get("completeAbstractCandidate") if isinstance(metadata, dict) else None
            if isinstance(candidate, dict):
                # A retained provider response: the candidate's original record must be one of its results.
                results = (value.get("resultList") or {}).get("result") if isinstance(value, dict) else None
                if not isinstance(results, list) or candidate.get("rawMetadata") not in results:
                    rejected.append({"cache": "store", "documentId": document["document_id"], "reason": "record-not-in-response"})
                    continue
            elif isinstance(value, dict) and isinstance(value.get("provenance"), dict):
                candidate = value["provenance"]          # an acquisition: the candidate the study selected
            else:
                continue
            if candidate.get("provider") != "europe-pmc" or not complete_abstract(candidate):
                rejected.append({"cache": "store", "documentId": document["document_id"], "reason": "incomplete-provider-record"})
                continue
            raw = candidate["rawMetadata"]
            accepted, method = _record_identity(raw, wanted, scheme)
            if not accepted:
                rejected.append({"cache": "store", "documentId": document["document_id"],
                                 "reason": method or "identity-unconfirmed"})
                continue
            if not raw.get("abstractText"):
                continue
            provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
            return {"abstract": raw["abstractText"], "outcome": outcome, "provenance": {
                "provenance": "cache-hit-public", "cache": "store", "cacheVersion": store.version["merkleRoot"],
                "sourceSha256": document["artifact_sha256"], "documentId": document["document_id"], "identity": method,
                "provider": "europe-pmc", "sourceUrl": provenance.get("sourceUrl"),
                "retrievedAt": provenance.get("retrievedAt")}}
        return None

    # ----------------------------------------------------------------- full text
    def full_text(self, work: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Cached Europe PMC fullTextXML of a cited work (bytes verified, PMCID or DOI confirmed): (hit, rejected)."""
        from .store_client import ByteCacheError
        wanted = identifiers_of(work)
        rejected: list[dict[str, Any]] = []
        pmcid = wanted.get("pmcid")
        if not pmcid:
            return None, rejected
        candidates: list[Candidate] = []
        if self.provider_cache is not None:
            url = FULL_TEXT % pmcid
            try:
                response = self.provider_cache.get(url, document=True)
            except ByteCacheError as error:
                response = None
                rejected.append({"cache": "provider-cache", "url": url, "reason": "sha256-mismatch", "detail": str(error)[:200]})
            if response is not None:
                candidates.append(Candidate("jats", "public", "provider-cache", response.sha256,
                                            lambda body=response.body: body,
                                            {"cacheVersion": self.provider_cache.version["sha256"], "requestKey": response.key,
                                             "sourceUrl": response.url, "retrievedAt": response.retrieved_at}))
        if self.store is not None:
            work_id, reason = self.store.find_work(store_identifiers(wanted))
            if work_id is None and reason != "not-in-store":
                rejected.append({"cache": "store", "reason": reason})
            for document in self.store.documents(work_id, "full-text") if work_id else []:
                source_url = str(document["source_url"] or "")
                europe_pmc = document["provider"] == "europe-pmc" or (
                    document["provider"] == "legacy-original-source" and source_url.startswith(EUROPE_PMC)
                    and source_url.endswith("/fullTextXML"))
                if not europe_pmc:
                    continue
                try:
                    metadata = json.loads(document["metadata_json"] or "{}")
                except ValueError:
                    metadata = {}
                raw = (metadata.get("rawArtifactSha256") if isinstance(metadata, dict) else None) \
                    if document["provider"] == "europe-pmc" else document["artifact_sha256"]
                artifact = self.store.artifact(str(raw or ""))
                if artifact is None or KIND_OF_MEDIA.get(str(artifact["media_type"]).split(";")[0].strip().lower()) != "jats":
                    continue
                candidates.append(Candidate("jats", "public", "store", raw, lambda digest=raw: self.store.read(digest),
                                            {"cacheVersion": self.store.version["merkleRoot"],
                                             "documentId": document["document_id"], "sourceUrl": source_url,
                                             "retrievedAt": document["created_at"]}))
        for candidate in candidates:
            try:
                body = candidate.load()
            except ByteCacheError as error:
                rejected.append({**candidate.describe(), "reason": "sha256-mismatch", "detail": str(error)[:200]})
                continue
            if sha256_bytes(body) != candidate.sha256:
                rejected.append({**candidate.describe(), "reason": "sha256-mismatch"})
                continue
            try:
                observed = document_identifiers(body, "jats")
            except ValidationError:
                rejected.append({**candidate.describe(), "reason": "unreadable"})
                continue
            accepted, method = check_identity(observed, wanted, require=("pmcid", "doi"))
            if not accepted:
                rejected.append({**candidate.describe(), "reason": method})
                continue
            return {"body": body, "provenance": {
                "provenance": "cache-hit-public", "cache": candidate.cache, "sourceSha256": candidate.sha256,
                "identity": method, "provider": "europe-pmc-fulltextxml", **candidate.detail}}, rejected
        return None, rejected


def _record_identity(raw: Any, wanted: Mapping[str, str], scheme: str) -> tuple[bool, str | None]:
    """Whether a Europe PMC record is the requested work: same identifier for the scheme, no conflicting other one."""
    if not isinstance(raw, dict):
        return False, None
    observed = identifiers_of(normalize_europe_pmc(raw))
    if observed.get(scheme) != wanted.get(scheme):
        return False, None
    return check_identity(observed, wanted, require=(scheme,))


# ----------------------------------------------------------------------------- live routes
@dataclass
class Route:
    """A journal's acquisition route: its licence class and the document kinds it reads, in order of preference."""

    klass: str
    order: tuple[str, ...] = DOCUMENT_KINDS

    def rank(self, kind: str) -> int | None:
        return self.order.index(kind) if kind in self.order else None


def parse_routes(values: Iterable[str] | None) -> dict[str, Route]:
    """``JOURNAL=public`` or ``JOURNAL=entitled``, optionally ``:html,jats`` for the document kinds read, in order.

    A route listing one kind reads only that kind, from the caches and live.
    """
    routes: dict[str, Route] = {}
    for value in values or []:
        journal, separator, spec = str(value).rpartition("=")
        if not separator or not journal.strip():
            raise ValidationError(f"a route is JOURNAL=public|entitled[:jats,html], not {value!r}")
        klass, _, order = spec.partition(":")
        if klass not in ROUTE_CLASSES:
            raise ValidationError(f"route class must be public or entitled, not {klass!r}")
        kinds = tuple(kind.strip() for kind in order.split(",") if kind.strip()) if order else DOCUMENT_KINDS
        if not kinds or any(kind not in DOCUMENT_KINDS for kind in kinds) or len(set(kinds)) != len(kinds):
            raise ValidationError("the document order lists jats and html, each at most once")
        if journal.strip() in routes:
            raise ValidationError(f"route for {journal.strip()} given twice")
        routes[journal.strip()] = Route(klass, kinds)
    return routes


def page_fetcher(*, spacing: float = 1.0, attempts: int = 3, backoff: Sequence[float] = (2.0, 5.0, 10.0),
                 timeout: float = 90.0, opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep):
    """GET a page over public HTTPS: ``(body, final URL)``, a reason string for a definitive miss, or RetryableFetch.

    The default opener (``fulltext.open_public_https``) reaches public hosts only, follows a bounded number of
    redirects and never sends cookies, proxies' credentials or contact addresses. 404, 410 and other client
    errors are definitive (``http-403`` for a page the licence does not open); throttling, server errors,
    timeouts and dropped connections are retried with backoff and then raise ``RetryableFetch``.
    """
    from .census_runner import RetryableFetch
    from .fulltext import MAX_DOCUMENT_BYTES, open_public_https
    state = threading.local()

    def fetch(url: str):
        error = "no attempt"
        for attempt in range(max(1, attempts)):
            wait = getattr(state, "last", 0.0) + spacing - time.monotonic()
            if wait > 0:
                sleep(wait)
            state.last = time.monotonic()
            request = urllib.request.Request(url, headers={"Accept": "text/html", "User-Agent": USER_AGENT})
            try:
                with (opener or open_public_https)(request, timeout=timeout, allow_cross_origin=True) as response:
                    status = int(getattr(response, "status", 200))
                    if status == 200:
                        body = response.read(MAX_DOCUMENT_BYTES + 1)
                        if len(body) > MAX_DOCUMENT_BYTES:
                            return "too-large"
                        return body, str(getattr(response, "url", url) or url)
                if status in (404, 410) or (400 <= status < 500 and status not in (408, 425, 429)):
                    return f"http-{status}"
                error = f"HTTP {status}"
            except urllib.error.HTTPError as failure:
                if failure.code in (404, 410) or (400 <= failure.code < 500 and failure.code not in (408, 425, 429)):
                    return f"http-{failure.code}"
                error = f"HTTP {failure.code}"
            except ProviderError as failure:
                message = str(failure)
                # Policy refusals (a non-public destination, a credential in the URL) are definitive; lookups and
                # connections that failed are transient.
                if re.search(r"not public|non-public|require|credential|bound", message, re.I) or not re.search(
                        r"timeout|timed out|connection|lookup failed|temporar", message, re.I):
                    return "refused: " + message[:120]
                error = message[:120]
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as failure:
                error = type(failure).__name__
            if attempt + 1 < attempts:
                sleep(backoff[min(attempt, len(backoff) - 1)])
        raise RetryableFetch(f"{url}: {error}")
    return fetch


@dataclass
class LiveSources:
    """Live acquisition paths: Europe PMC fullTextXML by PMCID and the publisher page through doi.org."""

    europe_pmc: Callable[[str], bytes | None] | None = None
    publisher: Callable[[str], Any] | None = None

    @classmethod
    def default(cls) -> "LiveSources":
        from .study_cli import europe_pmc_fetcher
        return cls(europe_pmc=europe_pmc_fetcher(), publisher=page_fetcher())

    def attempts(self, route: Route, doi: str, identifiers: Mapping[str, str]) -> list[tuple[str, str, str, str, Callable]]:
        """(kind, class, provider, url, fetch) for the kinds the route reads, in its order; a fetch returns (body, url)
        or a reason string."""
        items = []
        if self.europe_pmc is not None and identifiers.get("pmcid"):
            url = FULL_TEXT % identifiers["pmcid"]

            def europe_pmc(pmcid=identifiers["pmcid"], url=url):
                body = self.europe_pmc(pmcid)
                return (body, url) if body else "not-available"
            items.append(("jats", "public", "europe-pmc", url, europe_pmc))
        if self.publisher is not None:
            url = "https://doi.org/" + urllib.parse.quote(doi, safe="/()")
            items.append(("html", route.klass, "publisher", url, lambda url=url: self.publisher(url)))
        return sorted((item for item in items if route.rank(item[0]) is not None), key=lambda item: route.rank(item[0]))


@dataclass
class Acquisition:
    state: str                                 # acquired | unavailable | retry
    provenance: str | None = None
    body: bytes | None = None
    kind: str | None = None
    tables: list[tuple[int, bytes]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


class ReviewAcquirer:
    """Review documents: byte caches first, then the journal's live route; never a document whose identity is unconfirmed.

    A candidate must hash to the digest its cache records (and to the digest the record pins, if any), declare the
    review's own DOI (or, for JATS without a DOI, its PMCID or PMID) and be the article: a landing or paywall page
    is refused. An article whose structured extraction fails is kept only when no other candidate extracts.
    """

    def __init__(self, caches: InputCaches | None = None, *, routes: Mapping[str, Route] | None = None,
                 live: LiveSources | None = None):
        self.caches = caches
        self.routes = dict(routes or {})
        self.live = live

    def _check(self, candidate: Candidate, expected: Mapping[str, str], pinned: str | None):
        from .store_client import ByteCacheError
        try:
            body = candidate.load()
        except ByteCacheError as error:
            return None, {"reason": "sha256-mismatch", "detail": str(error)[:200]}
        if sha256_bytes(body) != candidate.sha256:
            return None, {"reason": "sha256-mismatch", "detail": "bytes differ from the digest the cache records"}
        if pinned and candidate.sha256 != pinned:
            return None, {"reason": "sha256-mismatch", "detail": "differs from the document digest the record pins"}
        return self._identity(body, candidate.kind, expected)

    @staticmethod
    def _identity(body: bytes, kind: str, expected: Mapping[str, str]):
        try:
            observed = document_identifiers(body, kind)
        except ValidationError as error:
            return None, {"reason": "unreadable", "detail": str(error)[:200]}
        accepted, method = check_identity(observed, expected, require=REVIEW_IDENTITY)
        if not accepted:
            return None, {"reason": method, "observed": observed}
        return body, {"identity": method}

    def _tables(self, links: Sequence[tuple[int, str]], base_url: str | None, stored: Sequence[tuple[int, str]],
                route: Route | None, expected: Mapping[str, str]) -> tuple[list[tuple[int, bytes]], list[dict[str, Any]]]:
        """Linked table pages: from the store, the provider cache, then live on the journal's route; else missing.

        A table page that declares a DOI must declare the review's DOI.
        """
        from .store_client import ByteCacheError
        pages, notes = [], []
        held = dict(stored)
        if len(links) > MAX_TABLE_PAGES:
            # Pathological pages keep their article; the extraction then lists the tables as missing.
            return [], [{"source": "missing", "reason": f"more than {MAX_TABLE_PAGES} linked table pages"}]

        def belongs(page: bytes) -> str | None:
            try:
                declared = document_identifiers(page, "html").get("doi")
            except ValidationError:
                return "unreadable"
            return "identity-mismatch" if declared and declared != expected.get("doi") else None
        for number, target in links:
            if number in held and self.caches is not None and self.caches.store is not None:
                try:
                    page = self.caches.store.read(held[number])
                except ByteCacheError:
                    page = None
                if page is not None and belongs(page) is None:
                    pages.append((number, page))
                    notes.append({"number": number, "source": "store", "sha256": held[number],
                                  "cacheVersion": self.caches.version_of("store")})
                    continue
            url = urllib.parse.urljoin(base_url, target) if base_url else None
            if url is None:
                notes.append({"number": number, "source": "missing", "reason": "no page address to resolve the link"})
                continue
            cached = self.caches.table_page(url) if self.caches is not None else None
            if cached is not None and belongs(cached[0]) is None:
                pages.append((number, cached[0]))
                notes.append({"number": number, "url": url, **cached[1]})
                continue
            if route is None or self.live is None or self.live.publisher is None:
                notes.append({"number": number, "url": url, "source": "missing", "reason": "not cached; no live route"})
                continue
            got = self.live.publisher(url)          # RetryableFetch propagates: the review is acquired again later
            if isinstance(got, str):
                notes.append({"number": number, "url": url, "source": "missing", "reason": got})
                continue
            problem = belongs(got[0])
            if problem:
                notes.append({"number": number, "url": url, "source": "missing", "reason": problem})
                continue
            pages.append((number, got[0]))
            notes.append({"number": number, "url": url, "source": "live", "sha256": sha256_bytes(got[0])})
        return pages, notes

    def acquire(self, record: Mapping[str, Any]) -> Acquisition:
        from .census_runner import RetryableFetch
        journal, doi = record.get("journal"), normal_doi(record.get("doi"))
        if not journal or not doi:
            return Acquisition("unavailable", "unavailable", detail={"reasons": [{"reason": "record-without-journal-or-doi"}]})
        expected = {"doi": doi, **{k: v for k, v in identifiers_of(record).items() if k != "doi"}}
        pinned = str(record.get("documentSha256") or "") or None
        route = self.routes.get(journal)
        cache_class = route.klass if route else "entitled"
        reads = route or Route("entitled")
        rejected: list[dict[str, Any]] = []
        fallbacks: list[tuple[str, str, bytes, list, str | None, list, dict[str, Any]]] = []
        candidates: list[Candidate] = []
        if self.caches is not None:
            candidates += self.caches.store_review_candidates(doi, expected, cache_class, rejected)
            candidates += self.caches.provider_review_candidates(doi, expected, cache_class, rejected)
        for candidate in list(candidates):
            if reads.rank(candidate.kind) is None:
                rejected.append({**candidate.describe(), "reason": "kind-not-read-by-route"})
                candidates.remove(candidate)
        candidates.sort(key=lambda c: reads.rank(c.kind))
        try:
            for candidate in candidates:
                body, outcome = self._check(candidate, expected, pinned)
                if body is None:
                    rejected.append({**candidate.describe(), **outcome})
                    continue
                state, reason, links = assess_document(body, candidate.kind)
                detail = {"cache": candidate.cache, "sourceSha256": candidate.sha256, "identity": outcome["identity"],
                          "kind": candidate.kind, **candidate.detail}
                if state == "not-article":
                    rejected.append({**candidate.describe(), "reason": "not-the-article", "detail": reason})
                    continue
                if state == "extraction-failed":
                    fallbacks.append((f"cache-hit-{candidate.klass}", candidate.kind, body, links, candidate.base_url,
                                      candidate.tables, {**detail, "extraction": reason}))
                    continue
                return self._accepted(f"cache-hit-{candidate.klass}", candidate.kind, body, links, candidate.base_url,
                                      candidate.tables, route, expected, detail, rejected)
            transient = []
            if route is not None and self.live is not None:
                for kind, klass, provider, url, fetch in self.live.attempts(route, doi, expected):
                    try:
                        got = fetch()
                    except RetryableFetch as error:
                        transient.append({"provider": provider, "url": url, "reason": "transient", "detail": str(error)[:200]})
                        continue
                    if isinstance(got, str):
                        rejected.append({"provider": provider, "url": url, "reason": got})
                        continue
                    body, final_url = got
                    if pinned and sha256_bytes(body) != pinned:
                        rejected.append({"provider": provider, "url": url, "reason": "sha256-mismatch",
                                         "detail": "differs from the document digest the record pins"})
                        continue
                    body, outcome = self._identity(body, kind, expected)
                    if body is None:
                        rejected.append({"provider": provider, "url": url, **outcome})
                        continue
                    state, reason, links = assess_document(body, kind)
                    base = final_url if str(final_url).startswith("https://") else (
                        html_base_url(body) if kind == "html" else None)
                    detail = {"cache": None, "sourceSha256": sha256_bytes(body), "identity": outcome["identity"],
                              "kind": kind, "provider": provider, "sourceUrl": final_url, "retrievedAt": utc_now()}
                    if state == "not-article":
                        rejected.append({"provider": provider, "url": url, "reason": "not-the-article", "detail": reason})
                        continue
                    if state == "extraction-failed":
                        fallbacks.append((f"live-{klass}", kind, body, links, base, [], {**detail, "extraction": reason}))
                        continue
                    return self._accepted(f"live-{klass}", kind, body, links, base, [], route, expected, detail, rejected)
            if transient:
                return Acquisition("retry", detail={"reasons": rejected + transient})
            if fallbacks:
                return self._accepted(*fallbacks[0][:6], route, expected, fallbacks[0][6], rejected)
        except RetryableFetch as error:
            return Acquisition("retry", detail={"reasons": rejected + [{"reason": "transient", "detail": str(error)[:200]}]})
        if route is None:
            rejected.append({"reason": "no-live-route"})
        elif self.live is None:
            rejected.append({"reason": "live-acquisition-off"})
        return Acquisition("unavailable", "unavailable", detail={"reasons": rejected})

    def _accepted(self, label: str, kind: str, body: bytes, links: Sequence[tuple[int, str]], base_url: str | None,
                  stored_tables: Sequence[tuple[int, str]], route: Route | None, expected: Mapping[str, str],
                  detail: dict[str, Any], rejected: list[dict[str, Any]]) -> Acquisition:
        tables, notes = self._tables(links, base_url, stored_tables, route, expected) if links else ([], [])
        return Acquisition("acquired", label, body, kind, tables,
                           {**detail, "route": route.klass if route else None, "tables": notes, "rejected": rejected})


def _write_content(directory: Path, body: bytes, suffix: str) -> str:
    """Content-addressed file in ``directory``: written once, verified when it already exists."""
    digest = sha256_bytes(body)
    target = directory / (digest + suffix)
    if target.is_symlink():
        raise ValidationError(f"{target.name}: acquired documents must not be symlinks")
    if target.exists():
        if sha256_bytes(target.read_bytes()) != digest:
            raise ValidationError(f"{target.name}: stored document differs from its digest")
        return target.name
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".incoming-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target.name


def acquired_record(record: Mapping[str, Any], acquisition: Acquisition, documents: Path) -> dict[str, Any]:
    """The record with its acquired document (paths relative to the manifest) and its acquisition provenance."""
    out = {key: value for key, value in record.items() if key not in ("document", "acquisition")}
    if acquisition.state == "retry":
        out.pop("provenanceClass", None)
        out["acquisition"] = {"state": "retry", "attemptedAt": utc_now(), **acquisition.detail}
        return out
    if acquisition.state != "acquired":
        out["provenanceClass"] = "unavailable"
        out["acquisition"] = {"provenance": "unavailable", "acquiredAt": utc_now(), **acquisition.detail}
        return out
    documents.mkdir(parents=True, exist_ok=True)
    name = _write_content(documents, acquisition.body, SUFFIX[acquisition.kind])
    document = {"path": f"{documents.name}/{name}", "kind": acquisition.kind, "sha256": sha256_bytes(acquisition.body)}
    if acquisition.tables:
        document["tables"] = [{"number": number, "path": f"{documents.name}/{_write_content(documents, page, '.html')}",
                               "sha256": sha256_bytes(page)} for number, page in acquisition.tables]
    out["document"] = document
    out["provenanceClass"] = CLASS_OF[acquisition.provenance]
    out["acquisition"] = {"provenance": acquisition.provenance, "acquiredAt": utc_now(), **acquisition.detail}
    return out


def needs_acquisition(record: Mapping[str, Any], *, retry_unavailable: bool = False) -> bool:
    acquisition = record.get("acquisition")
    if not isinstance(acquisition, dict):
        return not record.get("document")      # supplied documents pass through unchanged
    if acquisition.get("state") == "retry":
        return True
    return retry_unavailable and acquisition.get("provenance") == "unavailable"


def acquire_records(source: Path, output: Path, acquirer: ReviewAcquirer, *, retry_unavailable: bool = False
                    ) -> dict[str, Any]:
    """Stream review records through the acquirer into ``<output>.partial``; resume it; rename when complete.

    Documents are written content-addressed into ``documents/`` beside the output, so every manifest of the
    study should live in that directory. Records with a supplied document pass through unchanged; records
    whose acquisition failed transiently are acquired again when the command runs on its own output.
    """
    from .census_runner import iter_records
    source, output = Path(source), Path(output)
    if output.resolve() == source.resolve():
        raise ValidationError("write acquired records to a new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    documents = output.parent / "documents"
    partial = Path(str(output) + ".partial")
    done = _resume_partial(partial)
    counts: dict[str, int] = {"records": 0, "resumed": len(done), "acquired": 0, "passedThrough": 0}
    provenance: dict[str, int] = {}
    with partial.open("a", encoding="utf-8") as handle:
        for index, record in enumerate(iter_records(source)):
            if index < len(done):
                if (record.get("journal"), record.get("doi")) != done[index]:
                    raise ValidationError(f"{partial} does not continue this input (record {index + 1} differs)")
                counts["records"] += 1
                continue
            if needs_acquisition(record, retry_unavailable=retry_unavailable):
                result = acquired_record(record, acquirer.acquire(record), documents)
                counts["acquired"] += 1
            else:
                result = dict(record)
                counts["passedThrough"] += 1
            label = (result.get("acquisition") or {}).get("provenance") or (
                "retry" if (result.get("acquisition") or {}).get("state") == "retry" else "supplied")
            provenance[label] = provenance.get(label, 0) + 1
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            counts["records"] += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, output)
    return {**counts, "output": str(output), "documents": str(documents), "provenance": dict(sorted(provenance.items())),
            "next": "run study acquire again on the output for reviews whose acquisition failed transiently"
            if provenance.get("retry") else None}


def _resume_partial(partial: Path) -> list[tuple[Any, Any]]:
    """(journal, doi) of the complete lines of an interrupted output; a cut-off tail is dropped."""
    done: list[tuple[Any, Any]] = []
    if not partial.exists():
        return done
    kept = 0
    with partial.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            try:
                value = json.loads(raw)
            except ValueError:
                break
            done.append((value.get("journal"), value.get("doi")))
            kept += len(raw)
    with partial.open("rb+") as handle:
        handle.truncate(kept)
    return done


# ----------------------------------------------------------------------------- provenance records
def review_provenance(record: Mapping[str, Any], document_sha256: str | None) -> tuple[str, dict[str, Any]]:
    """(label, acquisition detail) for a review as the input record states it."""
    acquisition = record.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("provenance"):
        label = acquisition["provenance"]
        if label not in PROVENANCE:
            raise ValidationError(f"unknown acquisition provenance: {label}")
        if (label == "unavailable") != (document_sha256 is None):
            raise ValidationError("the acquisition record and the document disagree")
        if document_sha256 is not None and acquisition.get("sourceSha256") != document_sha256:
            raise ValidationError("document differs from the acquired bytes")
        if record.get("provenanceClass") and record["provenanceClass"] != CLASS_OF[label]:
            raise ValidationError("provenance class differs from the acquisition provenance")
        return label, dict(acquisition)
    return ("unavailable" if document_sha256 is None else "supplied"), {}


def verify_claim(record: Mapping[str, Any], document: bytes | None, caches: InputCaches | None) -> tuple[bool | None, str]:
    """Re-check a review's cache-hit claim against the pinned caches: (verified, reason).

    True: the claimed cache version is the one given and it holds exactly these bytes for this review.
    None: nothing to verify (live, supplied, unavailable) or the claimed cache was not given. False: refuted.
    """
    acquisition = record.get("acquisition") or {}
    label = acquisition.get("provenance")
    if label not in ("cache-hit-public", "cache-hit-entitled") or document is None:
        return None, "not a cache hit"
    digest, doi = sha256_bytes(document), normal_doi(record.get("doi"))
    if acquisition.get("sourceSha256") != digest:
        return False, "document differs from the bytes the cache served"
    cache = acquisition.get("cache")
    if cache == "store":
        if caches is None or caches.store is None:
            return None, "store not given"
        if acquisition.get("cacheVersion") != caches.store.version["merkleRoot"]:
            return False, "acquired from a different store version"
        rejected: list[dict[str, Any]] = []
        expected = {"doi": doi, **{k: v for k, v in identifiers_of(record).items() if k != "doi"}}
        held = {candidate.sha256 for candidate in caches.store_review_candidates(doi, expected, "entitled", rejected)}
        return (digest in held), ("store holds these bytes for this review" if digest in held
                                  else "store holds no such capture for this review")
    if cache == "provider-cache":
        if caches is None or caches.provider_cache is None:
            return None, "provider cache not given"
        if acquisition.get("cacheVersion") != caches.provider_cache.version["sha256"]:
            return False, "acquired from a different provider cache"
        from .store_client import ByteCacheError
        try:
            response = caches.provider_cache.response(str(acquisition.get("requestKey") or ""))
        except ByteCacheError:
            response = None
        ok = response is not None and response.sha256 == digest
        return ok, "provider cache holds these bytes" if ok else "provider cache holds no such response"
    return False, "unknown cache"


def verify_item_claim(detail: Mapping[str, Any], caches: InputCaches | None) -> bool | None:
    """Whether a cited-work cache hit names the cache version the study pinned (None without that cache)."""
    if detail.get("provenance") not in ("cache-hit-public", "cache-hit-entitled"):
        return None
    pinned = caches.version_of(detail.get("cache")) if caches is not None else None
    if pinned is None:
        return None
    return detail.get("cacheVersion") == pinned


# ----------------------------------------------------------------------------- fresh enumeration
def search_pages(*, spacing: float = 0.4, attempts: int = 4, opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> Callable[[Mapping[str, Any]], bytes]:
    """GET Europe PMC search pages; the raw bytes, or ProviderError after the last attempt (an enumeration never guesses)."""
    state = threading.local()

    def get(params: Mapping[str, Any]) -> bytes:
        error = "no attempt"
        for attempt in range(max(1, attempts)):
            wait = getattr(state, "last", 0.0) + spacing - time.monotonic()
            if wait > 0:
                sleep(wait)
            state.last = time.monotonic()
            request = urllib.request.Request(SEARCH + "?" + urllib.parse.urlencode(dict(params)),
                                             headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            try:
                with (opener or urllib.request.urlopen)(request, timeout=90) as response:
                    if getattr(response, "status", 200) == 200:
                        return response.read()
                    error = f"HTTP {response.status}"
            except urllib.error.HTTPError as failure:
                error = f"HTTP {failure.code}"
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as failure:
                error = type(failure).__name__
            if attempt + 1 < attempts:
                sleep(3 * (attempt + 1))
        raise ProviderError(f"Europe PMC search failed: {error}")
    return get


def _spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("groups"), list) or not value["groups"]:
        raise ValidationError("an enumeration spec lists groups: [{journal, query, stratum or strata}]")
    if value.get("provider", "europe-pmc") != "europe-pmc":
        raise ValidationError("enumeration uses the Europe PMC search provider")
    window = value.get("window")
    if window is not None:
        if not isinstance(window, dict) or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(window.get(k, "")))
                                                    for k in ("from", "until")):
            raise ValidationError("the enumeration window needs from and until dates (YYYY-MM-DD)")
        if not re.fullmatch(r"[A-Z_]{1,40}", str(window.get("field", "FIRST_PDATE"))):
            raise ValidationError("invalid enumeration window field")
    for group in value["groups"]:
        if not isinstance(group, dict) or not str(group.get("journal", "")).strip() or not str(group.get("query", "")).strip():
            raise ValidationError("every enumeration group needs a journal and a query")
        if "strata" in group and not isinstance(group["strata"], dict):
            raise ValidationError("group strata map publication types to stratum labels")
    if value.get("resultType", "core") not in ("core", "lite"):
        raise ValidationError("resultType is core or lite")
    page_size = value.get("pageSize", 1000)
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
        raise ValidationError("pageSize is an integer from 1 to 1000")
    return dict(value)


def effective_query(spec: Mapping[str, Any], group: Mapping[str, Any]) -> str:
    window = spec.get("window")
    query = str(group["query"]).strip()
    if window:
        query = f"({query}) AND {window.get('field', 'FIRST_PDATE')}:[{window['from']} TO {window['until']}]"
    return query


def _stratum(group: Mapping[str, Any], publication_types: Sequence[str]) -> str | None:
    if group.get("strata"):
        for kind in publication_types:
            if kind in group["strata"]:
                return str(group["strata"][kind])
        return group.get("stratum")
    return group.get("stratum")


def enumerated_records(spec: Mapping[str, Any], index: int, page: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Review records of one raw search page (journal and stratum from the group): (records, page summary)."""
    group = spec["groups"][index]
    try:
        payload = json.loads(page)
        rows = payload["resultList"]["result"]
        total = payload["hitCount"]
    except (ValueError, KeyError, TypeError) as error:
        raise ValidationError("enumeration page is not a Europe PMC result list") from error
    if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
        raise ValidationError("enumeration provider omitted a valid hit count or result list")
    records, without = [], []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = normalize_europe_pmc(raw)
        doi = normal_doi(item.get("doi"))
        if doi is None:
            without.append(str(raw.get("id") or ""))
            continue
        types = [str(t) for t in item.get("publicationTypes") or []]
        first = str(raw.get("firstPublicationDate") or "")
        year = int(first[:4]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", first) else item.get("year")
        record = {"journal": str(group["journal"]).strip(), "doi": doi, "pmid": normal_pmid(item.get("pmid")),
                  "pmcid": normal_pmcid(item.get("pmcid")), "enumeratedTitle": item.get("title"), "year": year,
                  "firstOnline": first or None, "stratum": _stratum(group, types), "publicationTypes": types,
                  "retracted": bool(item.get("isRetracted")), "enumeration": {"group": index, "page": sha256_bytes(page)}}
        records.append({key: value for key, value in record.items() if value is not None})
    return records, {"hitCount": total, "rows": len(rows), "withoutDoi": without,
                     "nextCursorMark": payload.get("nextCursorMark")}


def enumerate_reviews(spec_value: Mapping[str, Any], output: Path, get: Callable[[Mapping[str, Any]], bytes],
                      *, code_revision: str | None = None) -> dict[str, Any]:
    """Live enumeration through Europe PMC search pages (cursorMark): manifest, kept pages and a receipt.

    Every page is written content-addressed into ``<output>.pages/``; the population must be exhausted
    (the declared hit count reached and stable across pages). A DOI enumerated twice is kept once; records
    without a DOI are counted and listed by provider id. The receipt is written last.
    """
    spec = _spec(spec_value)
    output = Path(output)
    receipt_path = Path(str(output) + ".receipt.json")
    if output.exists() or receipt_path.exists():
        raise ValidationError("enumeration output already exists; enumerate into a new path")
    output.parent.mkdir(parents=True, exist_ok=True)
    pages_dir = Path(str(output) + ".pages")
    pages_dir.mkdir(exist_ok=True)
    seen: dict[tuple[str, str], int] = {}
    manifest: list[dict[str, Any]] = []
    groups = []
    for index, group in enumerate(spec["groups"]):
        query = effective_query(spec, group)
        cursor, total, rows_seen, pages, duplicates, without = "*", None, 0, [], 0, []
        while True:
            params = {"query": query, "cursorMark": cursor, "pageSize": spec.get("pageSize", 1000),
                      "resultType": spec.get("resultType", "core"), "format": "json"}
            body = get(params)
            records, summary = enumerated_records(spec, index, body)
            if total is not None and summary["hitCount"] != total:
                raise ValidationError("enumeration population changed during paging; enumerate again")
            total = summary["hitCount"]
            name = _write_content(pages_dir, body, ".json")
            pages.append({"sha256": name[:-5], "rows": summary["rows"], "cursorMark": cursor})
            for record in records:
                key = (record["journal"], record["doi"])
                if key in seen:
                    duplicates += 1
                    continue
                seen[key] = len(manifest)
                manifest.append(record)
            without += summary["withoutDoi"]
            rows_seen += summary["rows"]
            following = summary["nextCursorMark"]
            if rows_seen >= total:
                if rows_seen != total:
                    raise ValidationError("enumeration returned more records than its declared population")
                break
            if not summary["rows"] or not following or following == cursor:
                raise ValidationError("enumeration stopped before its declared population was exhausted")
            cursor = following
        groups.append({"index": index, "journal": str(group["journal"]).strip(), "stratum": group.get("stratum"),
                       "strata": group.get("strata"), "query": query, "hitCount": total, "rows": rows_seen,
                       "duplicates": duplicates, "withoutDoi": len(without), "withoutDoiIds": without, "pages": pages})
    temporary = Path(str(output) + ".incoming")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in manifest:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    reviews = [[record["journal"], record["doi"]] for record in manifest]
    receipt = {"format": ENUMERATION_FORMAT, "provider": "europe-pmc", "endpoint": SEARCH, "enumeratedAt": utc_now(),
               "codeRevision": code_revision, "spec": spec, "groups": groups, "reviews": reviews,
               "reviewsSha256": hashlib.sha256(canonical(reviews).encode()).hexdigest(),
               "manifest": {"path": output.name, "sha256": sha256_bytes(output.read_bytes()), "records": len(manifest)},
               "pages": pages_dir.name}
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return {"output": str(output), "receipt": str(receipt_path), "records": len(manifest),
            "groups": [{k: g[k] for k in ("journal", "stratum", "hitCount", "duplicates", "withoutDoi")} for g in groups]}


@dataclass
class Enumeration:
    sha256: str
    receipt: dict[str, Any]
    reviews: dict[tuple[str, str], dict[str, Any]]
    pages: frozenset[str] = frozenset()


def load_enumeration(path: Path) -> Enumeration:
    """A verified enumeration receipt: every kept page hashes to its digest and re-derives the receipt's population."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        receipt = json.loads(raw)
    except ValueError as error:
        raise ValidationError("enumeration receipt is not JSON") from error
    if not isinstance(receipt, dict) or receipt.get("format") != ENUMERATION_FORMAT:
        raise ValidationError("not a census enumeration receipt")
    spec = _spec(receipt.get("spec"))
    pages_dir = path.parent / str(receipt.get("pages", ""))
    if not receipt.get("pages") or "/" in str(receipt["pages"]) or not pages_dir.is_dir() or pages_dir.is_symlink():
        raise ValidationError("the enumeration's kept pages are missing")
    derived: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[list[str]] = []
    digests: set[str] = set()
    for group in receipt.get("groups") or []:
        index = group.get("index")
        if not isinstance(index, int) or not 0 <= index < len(spec["groups"]):
            raise ValidationError("enumeration receipt names an unknown group")
        if group.get("query") != effective_query(spec, spec["groups"][index]):
            raise ValidationError("enumeration receipt query differs from its spec")
        rows = 0
        for page in group.get("pages") or []:
            digest = str(page.get("sha256", ""))
            target = pages_dir / (digest + ".json")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or target.is_symlink() or not target.is_file():
                raise ValidationError("an enumeration page is missing")
            body = target.read_bytes()
            if sha256_bytes(body) != digest:
                raise ValidationError("an enumeration page differs from its digest")
            digests.add(digest)
            records, summary = enumerated_records(spec, index, body)
            rows += summary["rows"]
            for record in records:
                key = (record["journal"], record["doi"])
                if key not in derived:
                    derived[key] = record
                    order.append([record["journal"], record["doi"]])
        if rows != group.get("rows") or rows != group.get("hitCount"):
            raise ValidationError("enumeration pages do not add up to the declared population")
    if order != receipt.get("reviews"):
        raise ValidationError("enumeration receipt population differs from its kept pages")
    return Enumeration(sha256_bytes(raw), receipt, derived, frozenset(digests))


ENUMERATED_FIELDS = ("stratum", "year", "firstOnline")


def enumeration_mismatch(record: Mapping[str, Any], enumeration: Enumeration) -> str | None:
    """Why a record does not descend from the pinned enumeration (None when it does).

    The record must be an enumerated review, name a kept page of this enumeration, and carry the population
    attributes (stratum, year, first online) the enumeration gave it.
    """
    key = (record.get("journal"), normal_doi(record.get("doi")))
    enumerated = enumeration.reviews.get(key)
    if enumerated is None:
        return "not in the verified fresh enumeration; reviews are never imported from elsewhere"
    origin = record.get("enumeration") if isinstance(record.get("enumeration"), dict) else {}
    if origin.get("page") not in enumeration.pages:
        return "the record names no kept page of this enumeration"
    for name in ENUMERATED_FIELDS:
        if record.get(name) != enumerated.get(name):
            return f"{name} differs from the enumeration"
    return None


# ----------------------------------------------------------------------------- manifest comparison
def load_stored_manifest(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Review identities of a stored manifest (JSON lines, JSON, CSV/TSV with a doi column, or one DOI per line).

    Only DOI, journal and stratum are read; nothing else of the stored manifest is used.
    """
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise ValidationError("stored manifest is not UTF-8 text") from error
    suffix = path.suffix.lower()
    rows: Any
    try:
        if suffix == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        elif suffix == ".json":
            value = json.loads(text)
            rows = value.get("reviews") if isinstance(value, dict) else value
        elif suffix in (".csv", ".tsv"):
            rows = list(csv.DictReader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ","))
        else:
            rows = [{"doi": line.strip()} for line in text.splitlines() if line.strip() and not line.startswith("#")]
    except (ValueError, csv.Error) as error:
        raise ValidationError(f"stored manifest is not readable as {suffix or 'text'}") from error
    if not isinstance(rows, list):
        raise ValidationError("stored manifest holds no review records")
    records, invalid = {}, 0
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        fields = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
        doi = normal_doi(fields.get("doi"))
        if doi is None:
            invalid += 1
            continue
        journal = str(fields.get("journal") or "").strip() or None
        records.setdefault(doi, {"doi": doi, "journal": journal,
                                 "stratum": str(fields.get("stratum") or "").strip() or None})
    return list(records.values()), {"path": path.name, "sha256": sha256_bytes(raw), "records": len(records),
                                    "invalid": invalid}


def _journal_key(value: str | None) -> str:
    return " ".join(str(value or "").casefold().split())


def compare_manifests(fresh: Sequence[Mapping[str, Any]], stored: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts of reviews in both manifests, only in the fresh one and only in the stored one, by journal and stratum.

    The fresh side keys journals and strata; a stored-only review keeps its own journal when it names one of
    the fresh journals, else it is unattributed. Nothing of the stored manifest enters the study.
    """
    fresh_by_doi = {str(item["doi"]): item for item in fresh}
    stored_by_doi = {str(item["doi"]): item for item in stored}
    names = {_journal_key(item["journal"]): item["journal"] for item in fresh}
    cells: dict[tuple[str, str], dict[str, int]] = {}

    def bump(journal: str, stratum: str | None, field_name: str) -> None:
        for key in ((journal, "*"), (journal, stratum or "(none)")):
            cell = cells.setdefault(key, {"fresh": 0, "stored": 0, "inBoth": 0, "freshOnly": 0, "storedOnly": 0})
            cell[field_name] += 1
    for doi, item in fresh_by_doi.items():
        bump(item["journal"], item.get("stratum"), "fresh")
        bump(item["journal"], item.get("stratum"), "inBoth" if doi in stored_by_doi else "freshOnly")
    for doi, item in stored_by_doi.items():
        if doi in fresh_by_doi:
            journal, stratum = fresh_by_doi[doi]["journal"], fresh_by_doi[doi].get("stratum")
        else:
            journal = names.get(_journal_key(item.get("journal")), "(unattributed)")
            stratum = item.get("stratum")
        bump(journal, stratum, "stored")
        if doi not in fresh_by_doi:
            bump(journal, stratum, "storedOnly")
    totals = {"fresh": len(fresh_by_doi), "stored": len(stored_by_doi),
              "inBoth": len(fresh_by_doi.keys() & stored_by_doi.keys()),
              "freshOnly": len(fresh_by_doi.keys() - stored_by_doi.keys()),
              "storedOnly": len(stored_by_doi.keys() - fresh_by_doi.keys())}
    by_journal: dict[str, Any] = {}
    for (journal, stratum), cell in sorted(cells.items()):
        entry = by_journal.setdefault(journal, {"strata": {}})
        if stratum == "*":
            entry.update(cell)
        else:
            entry["strata"][stratum] = cell
    return {"totals": totals, "byJournal": by_journal,
            "freshOnly": sorted(fresh_by_doi.keys() - stored_by_doi.keys()),
            "storedOnly": sorted(stored_by_doi.keys() - fresh_by_doi.keys()),
            "cells": [{"journal": j, "stratum": s, **c} for (j, s), c in sorted(cells.items())]}
