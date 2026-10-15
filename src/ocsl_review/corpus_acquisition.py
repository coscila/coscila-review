# SPDX-License-Identifier: Apache-2.0
"""Corpus-scoped scholarly acquisition with durable, shared response reuse.

All requests still pass through the supplied ScholarlyClient's authorization and
transport. The cache is a private study input snapshot, not a global public cache.
Failures are recorded separately and never become permanent negative responses.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .errors import ProviderError, ReviewError, ValidationError
from .evidence import abstract_candidate, complete_abstract
from .fulltext import MAX_DOCUMENT_BYTES, validate_public_url
from .scholarly import EUROPE_PMC, PUBMED, MAX_RESPONSE_BYTES, RawResponse, ScholarlyClient, normalize_doi, utc_now
from .structured import _HTMLTree, extract


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def failure_kind(error: BaseException) -> dict[str, Any]:
    """Classify transport outcomes without conflating missing and inaccessible."""
    text = str(error)
    found = re.search(r"HTTP\s+(\d{3})", text)
    status = int(found.group(1)) if found else None
    if status == 429:
        kind, retryable = "rate-limited", True
    elif status in {408, 425} or (status is not None and status >= 500):
        kind, retryable = "provider-transient", True
    elif status in {401, 403, 405}:
        kind, retryable = "access-blocked", False
    elif status in {404, 410}:
        kind, retryable = "not-found", False
    elif re.search(r"timeout|timed out|connection|request failed|temporar|DNS", text, re.I):
        kind, retryable = "network-transient", True
    else:
        kind, retryable = "invalid-source", False
    return {"outcome": kind, "retryable": retryable, "httpStatus": status}


class CacheIntegrityError(RuntimeError):
    """Fatal local corruption; must not be swallowed as remote unavailability."""


def _credential_name(name):
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return (compact in {"apikey", "xapikey", "authorization", "proxyauthorization", "cookie", "setcookie",
                        "token", "accesstoken", "refreshtoken", "secret", "clientsecret", "password",
                        "credential", "credentials", "signature", "sig"}
            or compact.startswith("xamz") and any(word in compact for word in ("credential", "token", "signature")))


def _safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    query = urllib.parse.urlencode([(key, "[redacted]" if _credential_name(key) else value)
                                   for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)])
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _request_metadata(url, params=None, headers=None, document=False):
    parsed = urllib.parse.urlsplit(url)
    private = {"query": [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if _credential_name(k)],
               "params": {k: v for k, v in (params or {}).items() if v is not None and _credential_name(k)},
               "headers": {k.lower(): v for k, v in (headers or {}).items() if _credential_name(k)},
               "userinfo": parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""}
    request = {"url": _safe_url(url),
               "params": {k: "[redacted]" if _credential_name(k) else v for k, v in (params or {}).items() if v is not None},
               "headers": {k.lower(): v for k, v in (headers or {}).items() if not _credential_name(k)},
               "document": document}
    if any(private.values()):
        request["authorizationScopeSha256"] = hashlib.sha256(_json(private).encode()).hexdigest()
    return request, private


def _private_values(private):
    values = [str(v) for _, v in private["query"]]
    values += [str(v) for v in private["params"].values()]
    for name, value in private["headers"].items():
        value = str(value)
        values.append(value)
        if value.lower().startswith(("bearer ", "basic ")):
            values.append(value.split(" ", 1)[1])
        if name in {"cookie", "set-cookie"}:
            values.extend(part.split("=", 1)[1].strip() for part in value.split(";") if "=" in part)
    values.append(private["userinfo"])
    return sorted(set(values), key=len, reverse=True)


def _safe_provider_error(error, private):
    message = str(error)
    for value in _private_values(private):
        if value:
            for spelling in (value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)):
                message = message.replace(spelling, "[redacted]")
    return ProviderError(message)


class CorpusResponseCache:
    """SQLite-coordinated requests and rate reservations, shared across clients.

    Raw bytes are content-addressed files; SQLite contains metadata and leases.
    A killed requester loses its five-minute lease instead of blocking forever.
    """

    def __init__(self, path: Path, transport: Any, rates: Mapping[str, float] | None = None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.path.parent / (self.path.stem + "-artifacts")
        self.artifacts.mkdir(exist_ok=True)
        self.transport = transport
        self.rates = {"www.ebi.ac.uk": 3.0, "eutils.ncbi.nlm.nih.gov": 2.8,
                      "api.crossref.org": 5.0, "*": 1.0}
        self.rates.update(rates or {})
        if any(isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0
               for v in self.rates.values()):
            raise ValueError("provider request rates must be positive finite numbers")
        self._capture: ContextVar[tuple[list[dict[str, Any]], dict[str, str]] | None] = ContextVar(
            "corpus_response_capture", default=None)
        with self._db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS responses(
                key TEXT PRIMARY KEY, request TEXT NOT NULL, sha256 TEXT NOT NULL,
                status INTEGER NOT NULL, headers TEXT NOT NULL, url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, derived_from TEXT);
              CREATE TABLE IF NOT EXISTS request_leases(
                key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS provider_slots(host TEXT PRIMARY KEY, next_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS failures(
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, outcome TEXT NOT NULL,
                retryable INTEGER NOT NULL, http_status INTEGER, recorded_at TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=60)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=60000")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def capture(self):
        existing = self._capture.get()
        if existing is not None:
            yield existing[0]
            return
        records: list[dict[str, Any]] = []
        token = self._capture.set((records, {}))
        try:
            yield records
        finally:
            self._capture.reset(token)

    @staticmethod
    def request_key(url: str, params=None, headers=None, document=False) -> tuple[str, str]:
        request = _json(_request_metadata(url, params, headers, document)[0])
        return hashlib.sha256(request.encode()).hexdigest(), request

    def _artifact(self, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        target = self.artifacts / digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.artifacts, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise CacheIntegrityError("corpus cache source artifact failed integrity verification")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return digest

    def _read(self, key: str, maximum: int, authorize_url=None) -> RawResponse | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM responses WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if authorize_url:
            authorize_url(row["url"])
        if not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]):
            raise CacheIntegrityError("invalid cached artifact digest")
        path = self.artifacts / row["sha256"]
        if path.is_symlink() or not path.is_file():
            raise CacheIntegrityError("cached response is missing or unsafe")
        if path.stat().st_size > maximum:
            raise ProviderError("cached response exceeds the requested size bound")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise CacheIntegrityError("corpus cache response hash mismatch")
        active = self._capture.get()
        if active is not None:
            record = {"artifactPath": str(path), "sha256": row["sha256"],
                      "retrievedAt": row["retrieved_at"], "sourceUrl": row["url"],
                      "httpStatus": row["status"], "mediaType": json.loads(row["headers"]).get("content-type"),
                      "request": json.loads(row["request"]), "derivedFrom": row["derived_from"]}
            if record not in active[0]:
                active[0].append(record)
            if row["derived_from"]:
                # Include the original provider payload, not just its local projection.
                self._read(row["derived_from"], MAX_RESPONSE_BYTES)
        return RawResponse(body, row["status"], json.loads(row["headers"]), row["url"])

    def store(self, url: str, response: RawResponse, *, params=None, headers=None,
              document=False, derived_from: str | None = None) -> str:
        key, request = self.request_key(url, params, headers, document)
        private = _request_metadata(url, params, headers, document)[1]
        for value in _private_values(private):
            if value and any(spelling.encode() in response.body for spelling in
                             (value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value))):
                raise ProviderError("provider response echoes request credentials; response was not cached")
        digest = self._artifact(response.body)
        kept = {k.lower(): v for k, v in response.headers.items()
                if k.lower() in {"content-type", "etag", "last-modified", "content-length"}}
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO responses VALUES(?,?,?,?,?,?,?,?)",
                       (key, request, digest, response.status, _json(kept), _safe_url(response.url or url),
                        utc_now(), derived_from))
        return key

    def _reserve(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        interval = 1.0 / float(self.rates.get(host, self.rates["*"]))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next_at FROM provider_slots WHERE host=?", (host,)).fetchone()
            now = time.time()
            start = max(now, row[0] if row else now)
            db.execute("INSERT OR REPLACE INTO provider_slots VALUES(?,?)", (host, start + interval))
        delay = start - time.time()
        if delay > 0:
            time.sleep(delay)

    def get(self, url: str, *, params=None, headers=None, max_response_bytes=MAX_RESPONSE_BYTES,
            document=False, authorize_url=None) -> RawResponse:
        validate_public_url(url)
        if not 1 <= max_response_bytes <= MAX_DOCUMENT_BYTES:
            raise ProviderError("invalid provider response size bound")
        if authorize_url:
            authorize_url(url)
        key, _ = self.request_key(url, params, headers, document)
        active = self._capture.get()
        if active is not None and key in active[1]:
            raise ProviderError(active[1][key])
        owner = str(uuid.uuid4())
        while True:
            cached = self._read(key, max_response_bytes, authorize_url)
            if cached is not None:
                return cached
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM request_leases WHERE expires < ?", (time.time(),))
                got = db.execute("INSERT OR IGNORE INTO request_leases VALUES(?,?,?)",
                                 (key, owner, time.time() + 300)).rowcount
            if got:
                break
            time.sleep(0.025)
        try:
            # A preceding requester may have committed between our read and lease.
            cached = self._read(key, max_response_bytes, authorize_url)
            if cached is not None:
                return cached
            self._reserve(url)
            if document and hasattr(self.transport, "get_document"):
                response = self.transport.get_document(url, authorize_url=authorize_url)
            else:
                # Existing injectable transports implement this narrow protocol.
                response = self.transport.get(url, params=params, headers=headers)
            if not 200 <= response.status < 300:
                raise ProviderError(f"provider HTTP {response.status}")
            if len(response.body) > max_response_bytes:
                raise ProviderError("provider response exceeds the configured size bound")
            if authorize_url and response.url:
                authorize_url(response.url)
            self.store(url, response, params=params, headers=headers, document=document)
            return self._read(key, max_response_bytes, authorize_url)
        except ProviderError as exc:
            safe = _safe_provider_error(exc, _request_metadata(url, params, headers, document)[1])
            kind = failure_kind(safe)
            with self._db() as db:
                db.execute("INSERT INTO failures(key,outcome,retryable,http_status,recorded_at) VALUES(?,?,?,?,?)",
                           (key, kind["outcome"], int(kind["retryable"]), kind["httpStatus"], utc_now()))
            if active is not None:
                active[1][key] = str(safe)
            raise safe from None
        finally:
            with self._db() as db:
                db.execute("DELETE FROM request_leases WHERE key=? AND owner=?", (key, owner))

    def get_document(self, url: str, *, authorize_url=None) -> RawResponse:
        return self.get(url, document=True, max_response_bytes=MAX_DOCUMENT_BYTES, authorize_url=authorize_url)


class CorpusAcquirer:
    def __init__(self, cache_db: Path, client: ScholarlyClient, *,
                 requests_per_second: Mapping[str, float] | None = None):
        self.client = copy.copy(client)
        self.cache = CorpusResponseCache(cache_db, client.transport, requests_per_second)
        self.client.transport = self.cache

    @staticmethod
    def _identifier(identifiers: Mapping[str, str]) -> dict[str, str]:
        for key in ("doi", "pmid", "pmcid"):
            if identifiers.get(key):
                value = str(identifiers[key]).strip()
                if key == "doi":
                    value = normalize_doi(value)
                elif key == "pmcid":
                    value = value.upper()
                if key == "pmid" and not re.fullmatch(r"\d{1,10}", value):
                    raise ReviewError("invalid PMID")
                if key == "pmcid" and not re.fullmatch(r"PMC\d+", value):
                    raise ReviewError("invalid PMCID")
                return {key: value}
        raise ReviewError("corpus acquisition requires DOI, PMID or PMCID")

    def prefetch_dois(self, dois: Iterable[str]) -> dict[str, Any]:
        values = sorted({normalize_doi(str(doi)) for doi in dois})
        primed = batches = 0
        failures = []
        with self.cache.capture() as provenance:
            for offset in range(0, len(values), 50):
                group = values[offset:offset + 50]
                # Authorize every identifier individually; a batch is not a broader
                # query/field permission than the existing identifier operation.
                for doi in group:
                    self.client._authorize("europe-pmc", "resolve_identifier", {"doi": doi})
                params = {"query": "DOI:(" + " OR ".join('"' + doi + '"' for doi in group) + ")",
                          "pageSize": 1000, "resultType": "core", "format": "json"}
                try:
                    response = self.cache.get(f"{EUROPE_PMC}/search", params=params)
                    payload = response.json()
                    items = payload["resultList"]["result"]
                    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                        raise ProviderError("Europe PMC batch returned malformed results")
                    complete = int(payload.get("hitCount", len(items))) <= len(items)
                    origin, _ = self.cache.request_key(f"{EUROPE_PMC}/search", params)
                    for doi in group:
                        matched = []
                        for item in items:
                            try:
                                if normalize_doi(str(item.get("doi", ""))) == doi:
                                    matched.append(item)
                            except ReviewError:
                                continue
                        if not matched and not complete:
                            continue
                        lookup = {"query": f'DOI:"{doi}"', "pageSize": 5, "resultType": "core", "format": "json"}
                        projection = RawResponse(_json({"resultList": {"result": matched[:5]}}).encode(),
                                                 200, {"Content-Type": "application/json"}, response.url)
                        self.cache.store(f"{EUROPE_PMC}/search", projection, params=lookup, derived_from=origin)
                        primed += 1
                    batches += 1
                except (ProviderError, KeyError, TypeError, ValueError) as exc:
                    failures.append({**failure_kind(exc), "dois": group, "error": str(exc)})
        return {"requested": len(values), "batches": batches, "primed": primed,
                "failures": failures, "cacheProvenance": provenance}

    def _download(self, url: str, identifier: Mapping[str, str]) -> RawResponse:
        def authorize(destination):
            parsed = validate_public_url(destination)
            self.client._authorize(str(parsed.hostname), "acquire_full_text",
                                   {**identifier, "source": destination})
        return self.cache.get_document(url, authorize_url=authorize)

    @staticmethod
    def _html_identity(root: ET.Element, expected: Mapping[str, str], *, required: bool) -> None:
        dois = {normalize_doi(node.get("content", "")) for node in root.iter("meta")
                if node.get("name", "").lower() == "citation_doi" and node.get("content")}
        if expected.get("doi") and (dois != {normalize_doi(expected["doi"])} if dois else required):
            raise ProviderError("publisher HTML has absent or conflicting article DOI metadata")

    def _publisher_document(self, doi: str) -> dict[str, Any]:
        identifier = {"doi": doi}
        response = self._download("https://doi.org/" + urllib.parse.quote(doi, safe="/()"), identifier)
        tree = _HTMLTree()
        tree.feed(response.body.decode("utf-8")); tree.close()
        self._html_identity(tree.root, identifier, required=True)
        parsed = extract(response.body, html=True)
        targets = {url for warning in parsed["warnings"]
                   if warning["code"] == "external-table-content-unprovided"
                   for url in warning["targets"]}
        components = [{"sourceUrl": response.url, "sha256": hashlib.sha256(response.body).hexdigest()}]
        if len(targets) > 100:
            raise ProviderError("publisher article exceeds the linked-table acquisition bound")
        for target in sorted(targets):
            url = urllib.parse.urljoin(response.url, target)
            table_response = self._download(url, identifier)
            table_tree = _HTMLTree()
            table_tree.feed(table_response.body.decode("utf-8")); table_tree.close()
            self._html_identity(table_tree.root, identifier, required=False)
            tables = list(table_tree.root.iter("table"))
            if not tables or not any(table.find(".//tr") is not None for table in tables):
                raise ProviderError("linked publisher table has no readable table rows")
            parents = {child: parent for parent in tree.root.iter() for child in parent}
            anchors = [a for a in tree.root.iter("a") if a.get("href") == target]
            for index, anchor in enumerate(anchors):
                if index == 0:
                    for table in tables:
                        parents[anchor].append(copy.deepcopy(table))
                anchor.set("data-source-url", url)
                anchor.attrib.pop("href", None)
            components.append({"sourceUrl": table_response.url or url,
                               "sha256": hashlib.sha256(table_response.body).hexdigest()})
        body = ET.tostring(tree.root, encoding="utf-8", method="html") if targets else response.body
        if len(body) > MAX_DOCUMENT_BYTES:
            raise ProviderError("combined publisher article exceeds the document size bound")
        parsed = extract(body, html=True)
        if any(warning.get("affectsCompleteness") for warning in parsed["warnings"]):
            raise ProviderError("publisher article has incomplete structured citation coverage")
        blocks = []
        for item in parsed["blocks"]:
            text = item.get("text") or "\n".join(" | ".join(row) for row in item.get("rows", []))
            if text.strip():
                blocks.append({"id": f"block-{len(blocks) + 1:06d}", "kind": item["kind"],
                               "locator": _json(item.get("sourceLocator", {})), "text": text})
        substantive = [item for item in parsed["blocks"] if not item.get("sourceIdentity")
                       and not item.get("heading") and (item.get("text") or item.get("rows"))]
        if not substantive or not parsed["identities"] or not parsed["metadata"].get("anchors"):
            raise ProviderError("publisher page lacks a readable body and bibliography")
        return {"body": body, "mediaType": "text/html", "sourceUrl": response.url,
                "sha256": hashlib.sha256(body).hexdigest(), "complete": True,
                "text": "\n\n".join(block["text"] for block in blocks), "blocks": blocks,
                "supplements": [], "visualAssets": [], "identifiers": identifier,
                "provider": "publisher", "version": "publisher-html", "license": None,
                "originalBody": response.body, "originalSha256": hashlib.sha256(response.body).hexdigest(),
                "components": components,
                "transformation": "linked-tables-inlined-v1" if targets else None}

    def _abstract_only(self, identifier: Mapping[str, str], resolution: dict[str, Any]) -> dict[str, Any]:
        """Reuse full provider fields without downloading every cited paper."""
        candidates = []
        limitations = list(resolution.get("limitations", []))
        key, value = next(iter(identifier.items()))
        epmc = next((r for r in resolution["records"] if r.get("provider") == "europe-pmc"
                     and str(r.get(key) or "").casefold() == value.casefold()), None)
        if epmc and epmc.get("abstract"):
            query = f'DOI:"{value}"' if key == "doi" else f"EXT_ID:{value} AND SRC:MED" if key == "pmid" else f"PMCID:{value}"
            candidates.append(abstract_candidate(epmc, source_url=f"{EUROPE_PMC}/search?" + urllib.parse.urlencode(
                {"query": query, "pageSize": 5, "resultType": "core", "format": "json"}), retrieved_at=utc_now()))
        # PubMed is a fallback for absent/truncated fields, not an additional
        # full-text request. Status may already have cached its structured XML.
        pmid = identifier.get("pmid") or (epmc or {}).get("pmid")
        doi = identifier.get("doi") or (epmc or {}).get("doi")
        if pmid and not any(complete_abstract(c) for c in candidates):
            try:
                record = self.client._pubmed_structured(str(pmid), "acquire_full_text")
                if doi and record.get("doi") and normalize_doi(record["doi"]) != normalize_doi(doi):
                    limitations.append("PubMed abstract DOI conflicts with the requested work; abstract excluded.")
                elif record.get("abstract"):
                    candidates.append(abstract_candidate(record, source_url=f"{PUBMED}/efetch.fcgi?" + urllib.parse.urlencode(
                        {"db": "pubmed", "id": pmid, "retmode": "xml"}), retrieved_at=utc_now()))
            except ProviderError as exc:
                limitations.append(f"pubmed abstract: {exc}")
        selected = max(candidates, key=lambda c: (complete_abstract(c), len(c["text"])), default=None)
        complete = complete_abstract(selected)
        if selected and not complete:
            limitations.append("Complete structured provider abstract provenance is unavailable or indicates truncation.")
        return {"operation": "acquire_abstract", "provider": selected["provider"] if selected else "multiple",
                "retrievedAt": utc_now(), "accessLevel": "abstract-only" if selected else "metadata-only",
                "abstract": selected["text"] if selected else None, "abstractComplete": complete,
                "abstractProvider": selected["provider"] if selected else None, "abstractCandidates": candidates,
                "documents": [], "limitations": limitations, "attempts": []}

    def _indexed_resolution(self, identifier: Mapping[str, str], expected_title: str | None = None):
        """Use a complete exact index record; disagreement takes the broad path.

        The lookup still authorizes cache access. Printed-reference title binding
        remains the caller's responsibility; this is not independent identity or
        exhaustive publication-notice surveillance.
        """
        key, value = next(iter(identifier.items()))
        query = f'DOI:"{value}"' if key == "doi" else f"EXT_ID:{value} AND SRC:MED" if key == "pmid" else f"PMCID:{value}"
        try:
            records = self.client._europe_pmc_lookup(query, "resolve_identifier", identifier)
        except ProviderError:
            return None
        def normalized(text):
            return " ".join(re.findall(r"\w+", str(text or "").casefold()))
        def matches(record):
            try:
                observed = record.get(key)
                return (normalize_doi(str(observed)) == value if key == "doi"
                        else str(observed or "").casefold() == value.casefold())
            except ReviewError:
                return False
        if not records or not all(matches(record) for record in records):
            return None
        titles = {normalized(record.get("title")) for record in records}
        if len(titles) != 1 or not next(iter(titles)) or (expected_title and normalized(expected_title) not in titles):
            return None
        for linked in ("doi", "pmid", "pmcid"):
            values = {str(record[linked]).casefold() for record in records if record.get(linked)}
            if len(values) > 1:
                return None
        source = f"{EUROPE_PMC}/search?" + urllib.parse.urlencode(
            {"query": query, "pageSize": 5, "resultType": "core", "format": "json"})
        candidates = [(record, abstract_candidate(record, source_url=source, retrieved_at=utc_now()))
                      for record in records]
        complete = [(record, candidate) for record, candidate in candidates if complete_abstract(candidate)]
        if not complete or len({candidate["text"] for _, candidate in complete}) != 1:
            return None
        selected = complete[0][0]
        ordered = [{**record, key: value} for record in [selected] + [record for record in records if record is not selected]]
        result = self.client._envelope("resolve_identifier", "europe-pmc", ordered, limitations=[
            "Identity uses an exact identifier and consistent title in Europe PMC; independent provider verification was not requested."])
        result["method"] = "exact-index-identifier-and-title"
        return result

    @staticmethod
    def _indexed_status(records):
        flags, signals = [], []
        for record in records:
            raw = record.get("rawMetadata", {})
            value = raw.get("isRetracted")
            known = isinstance(value, bool) or value in ("Y", "N")
            retracted = value is True or value == "Y"
            types = record.get("publicationTypes") or []
            flags.append({"id": record.get("id"), "source": record.get("source"),
                          "isRetractedKnown": known, "isRetracted": retracted if known else None,
                          "publicationTypes": types})
            if retracted or any(str(t).casefold() == "retracted publication" for t in types):
                signals.append({"provider": "europe-pmc", "kind": "retraction", "detail": "provider index flags this work"})
        return {"operation": "get_publication_status", "state": "retracted" if signals else "unknown",
                "coverage": "provider-index-only", "providersChecked": ["europe-pmc"],
                "checkedAt": utc_now(), "signals": signals, "providerFlags": flags,
                "independentSurveillancePerformed": False,
                "limitations": ["Europe PMC index flags only; a missing or negative flag does not establish that a work is current or unretracted."]}

    def _indexed_full_text(self, identifier, resolution, result):
        record = resolution["records"][0]
        pmcid = record.get("pmcid")
        if not pmcid or not re.fullmatch(r"PMC\d+", str(pmcid).upper()):
            return False
        url = f"{EUROPE_PMC}/{urllib.parse.quote(str(pmcid).upper(), safe='')}/fullTextXML"
        linked = {key: str(record[key]) for key in ("doi", "pmid", "pmcid") if record.get(key)}
        linked.update(identifier)
        try:
            document = self.client._source_document(url, linked)
        except ProviderError as exc:
            result["attempts"].append({"provider": "europe-pmc", "sourceUrl": url, "status": "failed", "error": str(exc)})
            return False
        document.update(provider="europe-pmc", version="repository-copy", license=None)
        result.update(operation="acquire_full_text", provider="europe-pmc", accessLevel="full-text", documents=[document])
        result["attempts"].append({"provider": "europe-pmc", "sourceUrl": url, "status": "obtained", "sha256": document["sha256"]})
        if document.get("supplements"):
            result["limitations"].append("Supplement links were recorded; external supplement files require separate acquisition and examination.")
        if document.get("visualAssets"):
            result["limitations"].append("XML graphics/media links were recorded; their visual content requires separate acquisition and inspection where relevant.")
        return True

    def fetch_work(self, identifiers: Mapping[str, str], *, include_full_text: bool = False,
                   verify_independently: bool = False) -> dict[str, Any]:
        identifier = self._identifier(identifiers)
        with self.cache.capture() as provenance:
            resolution = None if verify_independently else self._indexed_resolution(identifier, identifiers.get("title"))
            fast = self._abstract_only(identifier, resolution) if resolution else None
            if fast is not None and (not include_full_text or self._indexed_full_text(identifier, resolution, fast)):
                fast.update(identityResolution=resolution,
                            identityReconciliation=self.client.reconcile_reference_metadata(resolution["records"], identifier),
                            publicationStatus=self._indexed_status(resolution["records"]))
                result = fast
            else:
                result = self._fully_resolved_work(identifier, include_full_text)
                if fast:
                    result["attempts"] = fast["attempts"] + result.get("attempts", [])
        result["cacheProvenance"] = provenance
        self._outcome(result)
        return result

    def _fully_resolved_work(self, identifier, include_full_text):
        resolution = self.client.resolve_identifier(**identifier)
        records = resolution["records"]
        reconciliation = self.client.reconcile_reference_metadata(records, identifier)
        status_key = "doi" if identifier.get("doi") else "pmid" if identifier.get("pmid") else None
        status = self.client.get_publication_status(**identifier) if status_key else None
        result = self.client.acquire_full_text(**identifier) if include_full_text else self._abstract_only(identifier, resolution)
        result.update(identityResolution=resolution, identityReconciliation=reconciliation,
                      publicationStatus=status)
        return result

    @staticmethod
    def _outcome(result: dict[str, Any]) -> None:
        errors = [str(a.get("error")) for a in result.get("attempts", []) if a.get("error")]
        errors += list(result.get("limitations", []))
        classifications = [failure_kind(ProviderError(error)) for error in errors]
        result["retryable"] = any(item["retryable"] for item in classifications)
        result["outcome"] = ("available" if result.get("documents") else "abstract-only"
                             if result.get("abstract") else "retryable" if result["retryable"] else "inaccessible")

    def fetch_review(self, identifiers: Mapping[str, str]) -> dict[str, Any]:
        identifier = self._identifier(identifiers)
        with self.cache.capture() as provenance:
            result = self.fetch_work(identifiers, include_full_text=True)
            structured = []
            for document in result["documents"]:
                if document["mediaType"] != "application/xml":
                    continue
                try:
                    extracted = extract(document["body"])
                    if extracted["identities"] and not any(w.get("affectsCompleteness") for w in extracted["warnings"]):
                        structured.append(document)
                except ValidationError:
                    continue
            records = result["identityResolution"]["records"]
            key, value = next(iter(identifier.items()))
            matched = [r for r in records if str(r.get(key) or "").casefold() == value.casefold()]
            doi = identifier.get("doi") or next((str(r["doi"]) for r in matched if r.get("doi")), None)
            pmcid = identifier.get("pmcid") or next((str(r["pmcid"]) for r in matched if r.get("pmcid")), None)
            if not structured and pmcid:
                try:
                    # Public fullTextXML can expose author manuscripts even when
                    # metadata's isOpenAccess flag is false.
                    url = f"{EUROPE_PMC}/{urllib.parse.quote(str(pmcid), safe='')}/fullTextXML"
                    doc = self.client._source_document(url, identifier)
                    extracted = extract(doc["body"])
                    if not extracted["identities"] or any(w.get("affectsCompleteness") for w in extracted["warnings"]):
                        raise ProviderError("repository review has incomplete structured citation coverage")
                    doc.update(provider="europe-pmc", version="repository-copy", license=None)
                    structured.append(doc)
                except (ProviderError, ValidationError) as exc:
                    result["attempts"].append({"provider": "europe-pmc", "status": "failed",
                                               "error": str(exc), **failure_kind(exc)})
            if not structured and doi:
                try:
                    structured.append(self._publisher_document(normalize_doi(doi)))
                except (ProviderError, ValidationError, UnicodeError) as exc:
                    result["attempts"].append({"provider": "publisher", "status": "failed",
                                               "error": str(exc), **failure_kind(exc)})
            result["documents"] = structured
            result["document"] = structured[0] if structured else None
            result["accessLevel"] = "full-text" if structured else "inaccessible"
            self._outcome(result)
            result["outcome"] = "available" if structured else "retryable" if result["retryable"] else "inaccessible"
        result["cacheProvenance"] = provenance
        return result
