# SPDX-License-Identifier: Apache-2.0
"""Works citing the affected reviews: Europe PMC citations, and OpenAlex where it answers.

Targets are the reviews with at least one S1 locus whose dependency is sole or essential (``--widen``: every review
with an S1 locus). Each source is a ``followup_model.CitedBySource``:

- ``EuropePmcCitedBy``: the review's Europe PMC record (search ``DOI:"..."``), then
  ``/{source}/{id}/citations?page=N&pageSize=1000&format=json`` until ``hitCount`` citations are read; ``complete``,
  ``truncated`` at the page cap, ``no-identifier`` when Europe PMC has no record of the review, ``error`` when a
  request fails.
- ``OpenAlexCitedBy``: ``/works/doi:{doi}`` for the work id, then ``/works?filter=cites:{id}`` with cursor paging. Any
  failure (connection, 401, 403, 429, a server error, a timeout, a malformed body) is the state ``unavailable`` with its
  reason; the traversal never raises for it and Europe PMC stays primary. An operator key
  (``OCSL_REVIEW_OPENALEX_API_KEY``) is sent as OpenAlex expects and never recorded; ``mailto`` only with the operator's
  ``OCSL_REVIEW_CONTACT_EMAIL``.

Edges of one target are united by identifier across sources: a citing work's key is its DOI, else its PMID, else its
PMCID, else its OpenAlex id (``doi:``, ``pmid:``, ``pmcid:``, ``openalex:``; ``europe-pmc:<source>/<id>`` for a record
with none of them). ``fu_citedby_edges`` keeps one row per target, source and citing key; ``fu_citedby_coverage``
one per target and source; every run registers the graph as a dataset version (kind ``cited-by-graph``).
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import followup_net
from .errors import ProviderError, ValidationError
from .followup_branch import FollowupBranch, now, register_dataset, require_freeze
from .followup_model import CitedByEdge, CitedByResult, TargetRef, canonical
from .followup_status import ProviderSession, Recorder, normal_doi, normal_pmid

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX = "https://api.openalex.org"
OPERATION = "followup_citedby"
PAGE_SIZE = 1000
OPENALEX_PER_PAGE = 200
OPENALEX_KEY_ENV = "OCSL_REVIEW_OPENALEX_API_KEY"
SOURCES = ("europe-pmc", "openalex")
KEY_ORDER = ("doi", "pmid", "pmcid", "openalex")


def make_client(workspace: Path | None = None) -> Any:
    from . import followup_net
    return followup_net.client(workspace=workspace)


def normal_pmcid(value: Any) -> str | None:
    match = re.fullmatch(r"(?:PMC)?(\d{1,12})", str(value or "").strip(), re.I)
    return "PMC" + match.group(1) if match else None


def _year(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if re.fullmatch(r"\d{4}", text) else None


def edge_key(edge: CitedByEdge) -> str:
    for scheme, value in (("doi", edge.citing_doi), ("pmid", edge.citing_pmid), ("pmcid", edge.citing_pmcid),
                          ("openalex", edge.citing_openalex)):
        if value:
            return f"{scheme}:{value}"
    return edge.citing_key


class EuropePmcCitedBy:
    """Europe PMC citations of a review, page by page until its hit count."""

    name = "europe-pmc"

    def __init__(self, session: ProviderSession, *, max_pages: int = 100, page_size: int = PAGE_SIZE):
        self.session, self.max_pages, self.page_size = session, max(1, int(max_pages)), int(page_size)

    def citing(self, target: TargetRef, *, limit: int | None = None) -> CitedByResult:
        doi = normal_doi(target.doi)
        if doi is None:
            return CitedByResult(target, self.name, "no-identifier", reason="no DOI")
        answer = self.session.get("europe-pmc", f"{EUROPE_PMC}/search",
                                  params={"query": f'DOI:"{doi}"', "pageSize": 5, "resultType": "core",
                                          "format": "json"}, egress={"doi": doi})
        if not answer.ok:
            return CitedByResult(target, self.name, "error", reason=f"record lookup: {answer.error}")
        try:
            results = (answer.json().get("resultList") or {}).get("result") or []
        except (ProviderError, AttributeError):
            return CitedByResult(target, self.name, "error", reason="record lookup: malformed-response")
        record = next((item for item in results if isinstance(item, Mapping) and normal_doi(item.get("doi")) == doi),
                      None)
        if record is None or not record.get("id") or not record.get("source"):
            return CitedByResult(target, self.name, "no-identifier", reason="no Europe PMC record of the review")
        source, identifier = str(record["source"]), str(record["id"])
        if source == "MED" and normal_pmid(identifier):
            egress: dict[str, Any] = {"pmid": identifier}
        elif source == "PMC" and normal_pmcid(identifier):
            egress = {"pmcid": normal_pmcid(identifier)}
        else:
            egress = {"doi": doi}
        edges: list[CitedByEdge] = []
        hit_count = None
        url = f"{EUROPE_PMC}/{urllib.parse.quote(source, safe='')}/{urllib.parse.quote(identifier, safe='')}/citations"
        page = 1
        while True:
            if page > self.max_pages:
                return CitedByResult(target, self.name, "truncated", edges, hit_count,
                                     f"page cap of {self.max_pages} reached")
            answer = self.session.get("europe-pmc", url, params={"page": page, "pageSize": self.page_size,
                                                                 "format": "json"}, egress={**egress, "page": page})
            if not answer.ok:
                return CitedByResult(target, self.name, "error", edges, hit_count, f"page {page}: {answer.error}")
            try:
                data = answer.json()
                hit_count = int(data.get("hitCount") or 0)
                citations = (data.get("citationList") or {}).get("citation") or []
                if not isinstance(citations, list):
                    raise ProviderError("malformed citation list")
            except (ProviderError, AttributeError, TypeError, ValueError):
                return CitedByResult(target, self.name, "error", edges, hit_count, f"page {page}: malformed-response")
            retrieved = now()
            for citation in citations:
                if isinstance(citation, Mapping) and citation.get("id"):
                    edges.append(self._edge(doi, citation, answer.request_id, retrieved))
            if limit is not None and len(edges) >= limit:
                return CitedByResult(target, self.name, "truncated", edges[:limit], hit_count,
                                     f"edge limit of {limit} reached")
            if not citations or len(edges) >= hit_count:
                return CitedByResult(target, self.name, "complete", edges, hit_count)
            page += 1

    @staticmethod
    def _edge(target_doi: str, citation: Mapping[str, Any], request_id: str, retrieved: str) -> CitedByEdge:
        source, identifier = str(citation.get("source") or ""), str(citation.get("id"))
        pmid = identifier if source == "MED" and normal_pmid(identifier) else normal_pmid(citation.get("pmid"))
        pmcid = normal_pmcid(identifier) if source == "PMC" else normal_pmcid(citation.get("pmcid"))
        edge = CitedByEdge(target_doi, f"europe-pmc:{source}/{identifier}", "europe-pmc", request_id, retrieved,
                           citing_doi=normal_doi(citation.get("doi")), citing_pmid=pmid, citing_pmcid=pmcid,
                           citing_title=str(citation.get("title") or "") or None,
                           citing_year=_year(citation.get("pubYear")))
        return dataclasses.replace(edge, citing_key=edge_key(edge))


class OpenAlexCitedBy:
    """OpenAlex works citing a review (``cites:`` filter, cursor paging); unavailability is a state, never an error."""

    name = "openalex"

    def __init__(self, session: ProviderSession, *, max_pages: int = 100, per_page: int = OPENALEX_PER_PAGE,
                 api_key: str | None = None):
        self.session, self.max_pages, self.per_page = session, max(1, int(max_pages)), int(per_page)
        self.api_key = api_key if api_key is not None else (os.environ.get(OPENALEX_KEY_ENV) or None)

    def _params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.session.contact:
            params["mailto"] = self.session.contact
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def citing(self, target: TargetRef, *, limit: int | None = None) -> CitedByResult:
        doi = normal_doi(target.doi)
        if doi is None:
            return CitedByResult(target, self.name, "no-identifier", reason="no DOI")
        answer = self.session.get("openalex", f"{OPENALEX}/works/doi:{urllib.parse.quote(doi, safe='/')}",
                                  params=self._params() or None, egress={"doi": doi})
        if not answer.ok:
            return CitedByResult(target, self.name, "unavailable", reason=f"work lookup: {answer.error}")
        try:
            work = answer.json()
            work_id = str(work.get("id") or "").rsplit("/", 1)[-1]
            if not re.fullmatch(r"W\d+", work_id):
                raise ProviderError("no OpenAlex work id")
        except (ProviderError, AttributeError):
            return CitedByResult(target, self.name, "unavailable", reason="work lookup: malformed-response")
        edges: list[CitedByEdge] = []
        count, cursor, pages = None, "*", 0
        while cursor:
            if pages >= self.max_pages:
                return CitedByResult(target, self.name, "truncated", edges, count,
                                     f"page cap of {self.max_pages} reached")
            answer = self.session.get("openalex", f"{OPENALEX}/works",
                                      params={"filter": f"cites:{work_id}", "per-page": self.per_page,
                                              "cursor": cursor, **self._params()},
                                      egress={"doi": doi, "cursor": cursor})
            pages += 1
            if not answer.ok:
                return CitedByResult(target, self.name, "unavailable", edges, count, f"page {pages}: {answer.error}")
            try:
                data = answer.json()
                meta = data.get("meta") or {}
                count = int(meta.get("count")) if meta.get("count") is not None else count
                results = data.get("results")
                if not isinstance(results, list):
                    raise ProviderError("malformed result list")
                cursor = meta.get("next_cursor") or None
            except (ProviderError, AttributeError, TypeError, ValueError):
                return CitedByResult(target, self.name, "unavailable", edges, count, f"page {pages}: malformed-response")
            retrieved = now()
            for result in results:
                if isinstance(result, Mapping):
                    edge = self._edge(doi, result, answer.request_id, retrieved)
                    if edge is not None:
                        edges.append(edge)
            if limit is not None and len(edges) >= limit:
                return CitedByResult(target, self.name, "truncated", edges[:limit], count,
                                     f"edge limit of {limit} reached")
            if not results:
                break
        return CitedByResult(target, self.name, "complete", edges, count)

    @staticmethod
    def _edge(target_doi: str, result: Mapping[str, Any], request_id: str, retrieved: str) -> CitedByEdge | None:
        work_id = str(result.get("id") or "").rsplit("/", 1)[-1]
        ids = result.get("ids") if isinstance(result.get("ids"), Mapping) else {}
        pmid = normal_pmid(str(ids.get("pmid") or "").rstrip("/").rsplit("/", 1)[-1])
        pmcid = normal_pmcid(str(ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1])
        if not re.fullmatch(r"W\d+", work_id):
            return None
        date = str(result.get("publication_date") or "") or None
        edge = CitedByEdge(target_doi, f"openalex:{work_id}", "openalex", request_id, retrieved,
                           citing_doi=normal_doi(result.get("doi")), citing_pmid=pmid, citing_pmcid=pmcid,
                           citing_openalex=work_id, citing_title=str(result.get("title") or "") or None,
                           citing_year=_year(result.get("publication_year")),
                           citing_first_online=date if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None)
        return dataclasses.replace(edge, citing_key=edge_key(edge))


def unite(edges: Sequence[CitedByEdge]) -> list[CitedByEdge]:
    """Edges of one target re-keyed so that records sharing any identifier carry one citing key."""
    parent = list(range(len(edges)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    seen: dict[str, int] = {}
    for index, edge in enumerate(edges):
        for scheme, value in (("doi", edge.citing_doi), ("pmid", edge.citing_pmid), ("pmcid", edge.citing_pmcid),
                              ("openalex", edge.citing_openalex)):
            if not value:
                continue
            identifier = f"{scheme}:{value}"
            if identifier in seen:
                parent[find(index)] = find(seen[identifier])
            else:
                seen[identifier] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(edges)):
        groups.setdefault(find(index), []).append(index)
    keyed = [None] * len(edges)
    for members in groups.values():
        candidates = [edges[i] for i in members]
        key = None
        for scheme in KEY_ORDER:
            values = sorted({getattr(edge, f"citing_{scheme}") for edge in candidates
                             if getattr(edge, f"citing_{scheme}")})
            if values:
                key = f"{scheme}:{values[0]}"
                break
        for i in members:
            keyed[i] = dataclasses.replace(edges[i], citing_key=key or edges[i].citing_key)
    return [edge for edge in keyed if edge is not None]


def targets(connection: Any, *, widen: bool = False) -> list[TargetRef]:
    """Reviews with an S1 locus whose dependency is sole or essential (``widen``: any S1 locus), with identifiers."""
    if widen:
        sql = ("SELECT DISTINCT f.journal, lower(f.doi) FROM fu_frame f JOIN fu_strata s ON s.locus_id=f.locus_id "
               "AND s.stratum='S1' ORDER BY f.journal, lower(f.doi)")
    else:
        sql = ("SELECT DISTINCT f.journal, lower(f.doi) FROM fu_frame f JOIN fu_strata s ON s.locus_id=f.locus_id "
               "AND s.stratum='S1' JOIN fu_dependency d ON d.locus_id=f.locus_id WHERE d.class IN ('sole','essential') "
               "ORDER BY f.journal, lower(f.doi)")
    rows = connection.execute(sql).fetchall()
    enumeration = connection.execute("SELECT 1 FROM census.sqlite_master WHERE type='table' AND name='enumeration'"
                                     ).fetchone() is not None
    found = []
    for journal, doi in rows:
        pmid = pmcid = None
        if enumeration:
            row = connection.execute("SELECT pmid, pmcid FROM census.enumeration WHERE journal=? AND lower(doi)=?",
                                     (journal, doi)).fetchone()
            if row:
                pmid, pmcid = normal_pmid(row[0]), normal_pmcid(row[1])
        found.append(TargetRef(doi, journal, pmid, pmcid))
    return found


def _stored_edges(connection: Any, target_doi: str, sources: Sequence[str]) -> list[CitedByEdge]:
    if not sources:
        return []
    rows = connection.execute(
        "SELECT * FROM fu_citedby_edges WHERE target_doi=? AND source IN "
        f"({','.join('?' for _ in sources)}) ORDER BY edge_id", (target_doi, *sources)).fetchall()
    return [CitedByEdge(row["target_doi"], row["citing_key"], row["source"], row["request_id"], row["retrieved_at"],
                        row["citing_doi"], row["citing_pmid"], row["citing_pmcid"], row["citing_openalex"],
                        row["citing_title"], row["citing_year"], row["citing_first_online"]) for row in rows]


def write_target(connection: Any, target: TargetRef, results: Sequence[CitedByResult]) -> int:
    """Replace a target's edges of the traversed sources, re-key all its edges across sources, write coverage."""
    traversed = {result.source for result in results}
    kept = _stored_edges(connection, target.doi, [source for source in SOURCES if source not in traversed])
    edges = unite(kept + [edge for result in results for edge in result.edges])
    connection.execute("DELETE FROM fu_citedby_edges WHERE target_doi=?", (target.doi,))
    connection.executemany("INSERT OR IGNORE INTO fu_citedby_edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (edge.edge_id, edge.target_doi, target.journal, edge.citing_key, edge.citing_doi, edge.citing_pmid,
         edge.citing_pmcid, edge.citing_openalex, edge.citing_title, edge.citing_year, edge.citing_first_online,
         edge.source, edge.request_id, edge.retrieved_at) for edge in edges])
    checked = now()
    for result in results:
        connection.execute("INSERT OR REPLACE INTO fu_citedby_coverage VALUES(?,?,?,?,?,?,?)",
                           (target.doi, result.source, result.state, result.hit_count, len(result.edges),
                            result.reason, checked))
    return len(edges)


def graph_digest(connection: Any) -> tuple[str, int]:
    value = hashlib.sha256()
    count = 0
    for row in connection.execute("SELECT target_doi, source, citing_key, citing_doi, citing_pmid, citing_pmcid, "
                                  "citing_openalex, citing_year, citing_first_online FROM fu_citedby_edges "
                                  "ORDER BY target_doi, source, citing_key"):
        value.update(canonical(list(row)).encode("utf-8") + b"\n")
        count += 1
    return value.hexdigest(), count


def traverse(connection: Any, scholarly: Any, *, sources: Sequence[str], widen: bool = False,
             limit: int | None = None, max_pages: int = 100) -> dict[str, Any]:
    unknown = sorted(set(sources) - set(SOURCES))
    if unknown or not sources:
        raise ValidationError("cited-by sources are europe-pmc and openalex")
    if limit is not None and limit < 1:
        raise ValidationError("--limit must be positive")
    recorder = Recorder(connection)
    session = ProviderSession(scholarly, recorder, operation=OPERATION)
    adapters = [EuropePmcCitedBy(session, max_pages=max_pages) if source == "europe-pmc"
                else OpenAlexCitedBy(session, max_pages=max_pages) for source in sources]
    chosen = targets(connection, widen=widen)
    if limit:
        chosen = chosen[:limit]
    states: dict[str, Counter] = {source: Counter() for source in sources}
    found: dict[str, int] = {source: 0 for source in sources}
    throttled: dict[str, int] = {}
    for position, target in enumerate(chosen):
        hot = [source for source in sources if followup_net.throttled(scholarly, source)]
        if hot:
            # A provider throttled every recent request: stop the traversal (a later run traverses again).
            throttled = {source: len(chosen) - position for source in hot}
            break
        results = [adapter.citing(target) for adapter in adapters]
        write_target(connection, target, results)
        for result in results:
            states[result.source][result.state] += 1
            found[result.source] += len(result.edges)
        connection.commit()
    sha, rows = graph_digest(connection)
    dataset = register_dataset(connection, kind="cited-by-graph", name="cited-by-graph", declared_version=now(),
                               sha256=sha, rows=rows, detail={"sources": list(sources), "widen": widen,
                                                              "targets": len(chosen)})
    united = connection.execute("SELECT COUNT(*) FROM (SELECT DISTINCT target_doi, citing_key FROM fu_citedby_edges)"
                                ).fetchone()[0]
    dependency_rows = connection.execute("SELECT COUNT(*) FROM fu_dependency").fetchone()[0]
    notes = [] if widen or dependency_rows else ["no dependency rows: run followup dependency first, or --widen"]
    result = {"targets": len(chosen), "widened": widen, "sources": list(sources), "dependencyRows": dependency_rows,
              "notes": notes,
              "states": {source: dict(sorted(count.items())) for source, count in states.items()}, "edges": found,
              "graphEdges": rows, "unitedCitingWorks": united, "dataset": dataset, "requests": recorder.summary(),
              "state": "completed"}
    if throttled:
        left = ", ".join(f"{source}: {count} targets" for source, count in sorted(throttled.items()))
        result.update(state="paused", throttled=throttled,
                      reason=f"transport: provider throttled ({left} not traversed); run citedby again to resume")
    return result


def cmd_citedby(args: Any) -> dict[str, Any]:
    branch = FollowupBranch(Path(args.database))
    protocol = branch.protocol() or {}
    sources = list(dict.fromkeys(args.source)) if getattr(args, "source", None) else list(
        (protocol.get("secondGeneration") or {}).get("sources") or SOURCES)
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else None
    scholarly = make_client(workspace)
    with branch.writing() as connection:
        freeze = require_freeze(connection)
        result = traverse(connection, scholarly, sources=sources, widen=getattr(args, "widen", False),
                          limit=getattr(args, "limit", None), max_pages=getattr(args, "max_pages", 100) or 100)
    return {**result, "freeze": freeze["freeze_id"]}
