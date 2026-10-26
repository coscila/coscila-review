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


def http_get(spacing: float = 0.4, attempts: int = 4) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """GET the search endpoint with per-thread spacing and bounded retries; None after the last failure."""
    state = threading.local()

    def get(params: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(attempts):
            wait = getattr(state, "last", 0.0) + spacing - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            state.last = time.monotonic()
            try:
                with urllib.request.urlopen(SEARCH + "?" + urllib.parse.urlencode(params), timeout=90) as response:
                    if response.status == 200:
                        return json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(3 * (attempt + 1))
        return None
    return get


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_abstracts(works: Mapping[str, Mapping[str, Any]], get: Callable[[dict[str, Any]], dict[str, Any] | None]
                    ) -> dict[str, dict[str, Any]]:
    """Return {work key: {"abstract", "abstractSource", "abstractRetrievedAt", "record"}} for every work.

    ``record`` is the Europe PMC record the lookup selected (None without a hit).
    """
    out: dict[str, dict[str, Any]] = {}
    with_doi = [(key, str(work["doi"]).lower()) for key, work in works.items() if work.get("doi")]
    for start in range(0, len(with_doi), DOIS_PER_QUERY):
        chunk = with_doi[start:start + DOIS_PER_QUERY]
        query = " OR ".join('DOI:"%s"' % doi.replace('"', "") for _, doi in chunk)
        response = get({"query": query, "format": "json", "resultType": "core", "pageSize": 200})
        hits: dict[str, Mapping[str, Any]] = {}
        for result in ((response or {}).get("resultList") or {}).get("result") or []:
            doi = (result.get("doi") or "").lower()
            if doi and doi not in hits:
                hits[doi] = result
        for key, doi in chunk:
            hit = hits.get(doi)
            if response is None:
                source = "europe-pmc-failed"
            elif hit is None:
                source = "europe-pmc-no-hit"
            else:
                source = "europe-pmc-core" if hit.get("abstractText") else "europe-pmc-no-abstract"
            out[key] = {"abstract": (hit or {}).get("abstractText"), "abstractSource": source, "abstractRetrievedAt": _now(),
                        "record": hit}
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
                overwrite: bool = False, cache: Any = None) -> dict[str, Any]:
    """A review record whose works carry Europe PMC abstracts, the one provider for every journal.

    Every work not yet looked up (all works with ``overwrite``) is looked up: in the study's byte caches
    first (``cache``: census_inputs.InputCaches), then live. A Europe PMC abstract replaces whatever the work
    carried; where Europe PMC definitively has none, an abstract supplied from another provider is kept as a
    recorded fallback (``abstractSource`` = ``fallback:<provider>``). A failed lookup changes nothing but
    ``europePmcOutcome`` and is retried on the next run. ``abstractProvenance`` records cache-hit-public,
    live-public, supplied (a fallback) or unavailable, with the digest of the text it vouches for. A work derived
    from the review's bibliography (``study works``) also takes its title, venue, year, PMCID and retraction flag
    from the same record (``metadataProvenance``, bound to the digest of those fields).
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
    for key, found in fetch_abstracts(wanted, get).items():
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
