# SPDX-License-Identifier: Apache-2.0
"""One abstract provider for every cited work: Europe PMC ``resultType=core``.

Works are looked up 40 DOIs per query; works without a DOI fall back to their
PMID. The raw ``abstractText`` is kept unchanged and its provider recorded, so
the census judge sees one provider's abstracts across journals. Requests carry
identifiers only. A study's byte caches (a literature-evidence@2 store, a
provider response cache) are asked first; a cached Europe PMC record is used
only with its digest verified and its identifiers agreeing. Each work records
where its abstract came from (``abstractProvenance``).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DOIS_PER_QUERY = 40


def http_get(attempts: int = 4, *, opener: Callable[..., Any] | None = None,
             sleep: Callable[[float], None] = time.sleep,
             limit: Any = None) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """GET the search endpoint with bounded retries; None after the last failure.

    Every request, from every thread, waits its turn at ``limit`` (default census_inputs.EUROPE_PMC_LIMIT, the
    census's one Europe PMC rate), and a 429 or 503 answer's Retry-After holds it. Every request names the census
    inputs' User-Agent and asks for JSON; it carries identifiers only (no contact address, no credentials, no
    cookies).
    """
    from .census_inputs import EUROPE_PMC_LIMIT, USER_AGENT
    rate = limit or EUROPE_PMC_LIMIT
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}

    def get(params: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(attempts):
            rate.wait()
            request = urllib.request.Request(SEARCH + "?" + urllib.parse.urlencode(params), headers=headers)
            try:
                with (opener or urllib.request.urlopen)(request, timeout=90) as response:
                    if response.status == 200:
                        return json.loads(response.read())
                    rate.hold(response.status, getattr(response, "headers", None))
            except urllib.error.HTTPError as failure:
                rate.hold(failure.code, failure.headers)
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
                pass
            sleep(3 * (attempt + 1))
        return None
    return get


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DoiRecords:
    """Europe PMC's answers by DOI within one invocation, shared by its threads: the record a lookup selected for a
    DOI (or none) and when it was retrieved. A failed lookup leaves nothing here."""

    def __init__(self):
        self._lock = threading.Lock()
        self._answers: dict[str, tuple[Mapping[str, Any] | None, str]] = {}

    def get(self, doi: str) -> tuple[Mapping[str, Any] | None, str] | None:
        with self._lock:
            return self._answers.get(doi)

    def put(self, doi: str, record: Mapping[str, Any] | None, retrieved_at: str) -> None:
        with self._lock:
            self._answers.setdefault(doi, (record, retrieved_at))


def _doi_outcome(hit: Mapping[str, Any] | None) -> str:
    if hit is None:
        return "europe-pmc-no-hit"
    return "europe-pmc-core" if hit.get("abstractText") else "europe-pmc-no-abstract"


def fetch_abstracts(works: Mapping[str, Mapping[str, Any]], get: Callable[[dict[str, Any]], dict[str, Any] | None], *,
                    memo: DoiRecords | None = None) -> dict[str, dict[str, Any]]:
    """Return {work key: {"abstract", "abstractSource", "abstractRetrievedAt", "record"}} for every work.

    ``record`` is the Europe PMC record the lookup selected (None without a hit). With ``memo`` a DOI an earlier
    lookup of the invocation answered is answered from it (the same record, retrieved when it was), and only the
    other DOIs are asked; an answer that is no result list, or a page that lists fewer records than its hitCount,
    leaves the DOIs it did not list to be asked again.
    """
    out: dict[str, dict[str, Any]] = {}
    with_doi = []
    for key, work in works.items():
        if not work.get("doi"):
            continue
        doi = str(work["doi"]).lower()
        known = memo.get(doi) if memo is not None else None
        if known is None:
            with_doi.append((key, doi))
            continue
        hit, retrieved = known
        out[key] = {"abstract": (hit or {}).get("abstractText"), "abstractSource": _doi_outcome(hit),
                    "abstractRetrievedAt": retrieved, "record": hit}
    for start in range(0, len(with_doi), DOIS_PER_QUERY):
        chunk = with_doi[start:start + DOIS_PER_QUERY]
        query = " OR ".join('DOI:"%s"' % doi.replace('"', "") for _, doi in chunk)
        response = get({"query": query, "format": "json", "resultType": "core", "pageSize": 200})
        hits: dict[str, Mapping[str, Any]] = {}
        results = ((response or {}).get("resultList") or {}).get("result") or []
        for result in results:
            doi = (result.get("doi") or "").lower()
            if doi and doi not in hits:
                hits[doi] = result
        # Kept for the invocation: the records of a result list, and no hit only from a page that holds every hit.
        listed = isinstance(response, dict) and isinstance((response.get("resultList") or {}).get("result"), list)
        complete = listed and not (isinstance(response.get("hitCount"), int) and response["hitCount"] > len(results))
        for key, doi in chunk:
            hit = hits.get(doi)
            source = "europe-pmc-failed" if response is None else _doi_outcome(hit)
            out[key] = {"abstract": (hit or {}).get("abstractText"), "abstractSource": source, "abstractRetrievedAt": _now(),
                        "record": hit}
            if memo is not None and listed and (hit is not None or complete):
                memo.put(doi, hit, out[key]["abstractRetrievedAt"])
    for key, work in works.items():
        if key in out:
            continue
        if not work.get("pmid"):
            out[key] = {"abstract": None, "abstractSource": "no-identifier", "abstractRetrievedAt": _now(), "record": None}
            continue
        response = get({"query": "EXT_ID:%s AND SRC:MED" % work["pmid"], "format": "json", "resultType": "core"})
        results = ((response or {}).get("resultList") or {}).get("result") or []
        hit = results[0] if results else None
        source = ("europe-pmc-failed" if response is None else "europe-pmc-no-hit" if hit is None
                  else "europe-pmc-core-pmid" if hit.get("abstractText") else "europe-pmc-no-abstract")
        out[key] = {"abstract": (hit or {}).get("abstractText"), "abstractSource": source, "abstractRetrievedAt": _now(),
                    "record": hit}
    return out


def looked_up(work: Mapping[str, Any]) -> bool:
    """Whether a work already has a definitive Europe PMC outcome (a failed lookup is not one)."""
    outcome = work.get("europePmcOutcome") or (work.get("abstractSource") if str(work.get("abstractSource", "")).startswith(
        "europe-pmc-") else None)
    return bool(outcome) and outcome != "europe-pmc-failed"


def _text_sha256(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def fill_record(record: Mapping[str, Any], get: Callable[[dict[str, Any]], dict[str, Any] | None], *,
                overwrite: bool = False, cache: Any = None, memo: DoiRecords | None = None) -> dict[str, Any]:
    """A review record whose works carry Europe PMC abstracts, the one provider for every journal.

    Every work not yet looked up (all works with ``overwrite``) is looked up: in the study's byte caches
    first (``cache``: census_inputs.InputCaches), then live. A Europe PMC abstract replaces whatever the work
    carried; where Europe PMC definitively has none, an abstract supplied from another provider is kept as a
    recorded fallback (``abstractSource`` = ``fallback:<provider>``). A failed lookup changes nothing but
    ``europePmcOutcome`` and is retried on the next run. ``abstractProvenance`` records cache-hit-public,
    live-public, supplied (a fallback) or unavailable, with the digest of the text it vouches for. A work derived
    from the review's bibliography (``study works``) also takes its title, venue, year, PMCID and retraction flag
    from the same record (``metadataProvenance``, bound to the digest of those fields). ``memo`` holds the
    invocation's answers by DOI (``fetch_abstracts``).
    """
    from .census_works import apply_metadata
    works = {key: dict(work) for key, work in (record.get("works") or {}).items()}
    wanted = {key: work for key, work in works.items() if overwrite or not looked_up(work)}
    rejected: dict[str, list] = {}
    if cache is not None:
        for key in list(wanted):
            hit, refused = cache.abstract(works[key])
            if refused:
                rejected[key] = refused
            if hit is None:
                continue
            work = works[key]
            work["europePmcOutcome"] = hit["outcome"]
            work["abstractRetrievedAt"] = hit["provenance"].get("retrievedAt")
            work["abstract"], work["abstractSource"] = hit["abstract"], hit["outcome"]
            work["abstractProvenance"] = {**hit["provenance"], "textSha256": _text_sha256(hit["abstract"]),
                                          **({"rejected": refused} if refused else {})}
            if derived(work):
                apply_metadata(work, hit.get("record"), hit["provenance"])
            del wanted[key]
    for key, found in fetch_abstracts(wanted, get, memo=memo).items():
        work, outcome = works[key], found["abstractSource"]
        work["europePmcOutcome"] = outcome
        if outcome == "europe-pmc-failed":
            continue
        work["abstractRetrievedAt"] = found["abstractRetrievedAt"]
        if derived(work):
            apply_metadata(work, found.get("record"), {
                "provenance": "live-public" if found.get("record") is not None else "unavailable",
                "provider": "europe-pmc", "outcome": outcome, "retrievedAt": found["abstractRetrievedAt"]})
        if found["abstract"]:
            work["abstract"], work["abstractSource"] = found["abstract"], outcome
            provenance = {"provenance": "live-public", "provider": "europe-pmc", "retrievedAt": found["abstractRetrievedAt"]}
        elif work.get("abstract"):
            source = str(work.get("abstractSource") or "supplied")
            if not source.startswith(("europe-pmc-", "fallback:")):
                work["abstractSource"] = "fallback:" + source
            provenance = {"provenance": "supplied", "fallback": work["abstractSource"]}
        else:
            work["abstract"], work["abstractSource"] = None, outcome
            provenance = {"provenance": "unavailable", "outcome": outcome}
        work["abstractProvenance"] = {**provenance, "textSha256": _text_sha256(work.get("abstract")),
                                      **({"rejected": rejected[key]} if key in rejected else {})}
    return {**record, "works": works}


def derived(work: Mapping[str, Any]) -> bool:
    """A work ``study works`` derived from the review's bibliography (its metadata comes from the lookup too)."""
    return isinstance(work.get("workProvenance"), dict)
