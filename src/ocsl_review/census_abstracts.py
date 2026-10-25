# SPDX-License-Identifier: Apache-2.0
"""One abstract provider for every cited work: Europe PMC ``resultType=core``.

Works are looked up 40 DOIs per query; works without a DOI fall back to their
PMID. The raw ``abstractText`` is kept unchanged and its provider recorded, so
the census judge sees one provider's abstracts across journals. Requests carry
identifiers only.
"""
from __future__ import annotations

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
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(3 * (attempt + 1))
        return None
    return get


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_abstracts(works: Mapping[str, Mapping[str, Any]], get: Callable[[dict[str, Any]], dict[str, Any] | None]
                    ) -> dict[str, dict[str, Any]]:
    """Return {work key: {"abstract", "abstractSource", "abstractRetrievedAt"}} for every work."""
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
            out[key] = {"abstract": (hit or {}).get("abstractText"), "abstractSource": source, "abstractRetrievedAt": _now()}
    for key, work in works.items():
        if key in out:
            continue
        if not work.get("pmid"):
            out[key] = {"abstract": None, "abstractSource": "no-identifier", "abstractRetrievedAt": _now()}
            continue
        response = get({"query": "EXT_ID:%s AND SRC:MED" % work["pmid"], "format": "json", "resultType": "core"})
        results = ((response or {}).get("resultList") or {}).get("result") or []
        hit = results[0] if results else None
        source = ("europe-pmc-failed" if response is None else "europe-pmc-no-hit" if hit is None
                  else "europe-pmc-core-pmid" if hit.get("abstractText") else "europe-pmc-no-abstract")
        out[key] = {"abstract": (hit or {}).get("abstractText"), "abstractSource": source, "abstractRetrievedAt": _now()}
    return out


def fill_record(record: Mapping[str, Any], get: Callable[[dict[str, Any]], dict[str, Any] | None], *,
                overwrite: bool = False) -> dict[str, Any]:
    """A review record whose works carry Europe PMC abstracts (existing abstracts kept unless ``overwrite``)."""
    works = {key: dict(work) for key, work in (record.get("works") or {}).items()}
    wanted = {key: work for key, work in works.items() if overwrite or not work.get("abstract")}
    for key, found in fetch_abstracts(wanted, get).items():
        works[key].update(found)
    return {**record, "works": works}
