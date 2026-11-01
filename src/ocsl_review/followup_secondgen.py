# SPDX-License-Identifier: Apache-2.0
"""Second-generation propagation (``followup second-gen``): do later articles restate a review's claim that rests on a
compromised work, citing the review for it?

Stages (``--stage``, default ``all`` in this order):

- ``sample``: anchors are S1 loci whose dependency (``fu_dependency``) is sole or essential. For each anchor, the
  cited-by edges of its review (``fu_citedby_edges``, one per citing work) are sampled with the protocol seed (scope:
  second-generation and the anchor), at most the protocol's ``secondGeneration.edges``; inclusion probability = sampled
  / available (``fu_secondgen_sample``).
- ``acquire``: the public full text of every sampled citing work, Europe PMC ``fullTextXML`` for an open-access PMCID
  (looked up by DOI or PMID when the edge has none), through ``followup_net.client`` (identifiers only, no contact
  identity, the scholarly transport's User-Agent, retries and per-host spacing). Outcomes: obtained,
  no-open-full-text, fetch-failed, unavailable (not readable) and citation-not-located (``fu_secondgen_documents``);
  every response is a receipt (``fu_requests``). Entitled acquisition is not part of this build.
- ``candidates``: the citing article is extracted (``census_extraction.extract_review``); the bibliography entry whose
  DOI or PMID is the review's yields its citing sentences with the marker as rendered (``fu_secondgen_sentences``).
  One BM25 index (``retrieval.EvidenceIndex``) per anchor over the sentences of its sampled citing works; the anchor's
  citing sentence is the query, searched per citing work, and the top ``secondGeneration.k`` are the candidates, with
  the retrieval receipt kept (``fu_secondgen_candidates``). Sentences beyond k are unassessable ``not-judged``, never
  assumed different.
- ``judge``: claim-match@1 confirms each candidate (match, reliance, exact quote); rows in ``fu_secondgen_matches``
  carry the edge's inclusion probability.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from xml.etree import ElementTree

from . import census_extraction, claim_match
from .errors import ConfidentialityError, ProviderError, ReviewError, ValidationError
from .followup_branch import FollowupBranch, now, read_artifact, require_freeze, store_artifact
from .followup_judge import (accepted, close_judge, judge_from_args, judge_summary, prepare_judge, run_items,
                             step_result)
from .followup_model import canonical, digest
from .followup_net import record_request
from .followup_strata import protocol_of, replicate_groups, replicate_keys, seeded_sample
from .retrieval import EvidenceIndex, EvidenceUnit, normalise_identifier
from .scholarly import EUROPE_PMC

STAGES = ("sample", "acquire", "candidates", "judge")
TIER = claim_match.TIER
LOOKUP = "second-generation-lookup"
FULL_TEXT = "second-generation-full-text"
DONE = ("obtained", "no-open-full-text", "citation-not-located")


def settings(protocol: Mapping[str, Any]) -> dict[str, Any]:
    spec = protocol.get("secondGeneration")
    if not spec:
        raise ValidationError("the protocol declares no second-generation sample (secondGeneration.edges, k, sources)")
    return spec


def anchors(connection: sqlite3.Connection, *, pilot_only: bool = False) -> list[sqlite3.Row]:
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(
        "SELECT f.locus_id, f.journal, f.doi, f.label, f.seq, f.provenance_class, s.pilot, d.class AS dependency "
        "FROM fu_strata s JOIN fu_frame f ON f.locus_id=s.locus_id JOIN fu_dependency d ON d.locus_id=f.locus_id "
        "WHERE s.stratum='S1' AND d.class IN ('sole','essential')" + (" AND s.pilot=1" if pilot_only else "") +
        " ORDER BY f.locus_id").fetchall()


# ----------------------------------------------------------------------------- sample
def sample(connection: sqlite3.Connection, protocol: Mapping[str, Any]) -> dict[str, Any]:
    spec = settings(protocol)
    s1 = connection.execute("SELECT COUNT(*) FROM fu_strata WHERE stratum='S1'").fetchone()[0]
    judged = connection.execute("SELECT COUNT(*) FROM fu_dependency d JOIN fu_strata s ON s.locus_id=d.locus_id "
                                "AND s.stratum='S1'").fetchone()[0]
    if s1 and not judged:
        raise ValidationError("the S1 loci have no dependency rows; run followup dependency before second-gen")
    rows, available_total, without_edges = [], 0, 0
    selected_at = now()
    found = anchors(connection)
    for anchor in found:
        target = anchor["doi"].lower()
        edges: dict[str, str] = {}
        for edge_id, citing_key in connection.execute(
                "SELECT edge_id, citing_key FROM fu_citedby_edges WHERE target_doi=? ORDER BY citing_key, "
                "CASE source WHEN 'europe-pmc' THEN 0 ELSE 1 END, edge_id", (target,)):
            edges.setdefault(citing_key, edge_id)
        if not edges:
            without_edges += 1
            continue
        scope_parts = ("second-generation", anchor["locus_id"])
        chosen = seeded_sample(protocol["seed"], scope_parts, edges, int(spec["edges"]))
        available_total += len(edges)
        seed_text = canonical([protocol["seed"], *scope_parts])
        rows.extend((anchor["locus_id"], edges[citing], citing, target, len(edges), len(chosen),
                     len(chosen) / len(edges), seed_text, selected_at) for citing in chosen)
    connection.execute("DELETE FROM fu_secondgen_sample")
    connection.executemany("INSERT INTO fu_secondgen_sample(anchor_locus_id, edge_id, citing_key, target_doi, available, "
                           "sampled, inclusion_probability, seed, selected_at) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return {"anchors": len(found), "anchorsWithoutEdges": without_edges, "edgesAvailable": available_total,
            "edgesSampled": len(rows), "citingWorks": len({row[2] for row in rows}), "edgesPerAnchor": spec["edges"]}


def _in_scope(anchor_ids: Sequence[str] | None, column: str = "anchor_locus_id") -> tuple[str, list[str]]:
    """SQL condition restricting ``column`` to the anchors in scope (every anchor when ``anchor_ids`` is None)."""
    if anchor_ids is None:
        return "1=1", []
    return f"{column} IN ({','.join('?' for _ in anchor_ids)})", list(anchor_ids)


# ----------------------------------------------------------------------------- acquire
def _failure(error: BaseException) -> tuple[str, str, int | None]:
    """Receipt outcome, failure class and HTTP status of a provider failure (never the response text)."""
    match = re.search(r"HTTP (\d{3})", str(error))
    status = int(match.group(1)) if match else None
    if status == 404:
        return "not-found", "http-404", status
    if status in (429, 502, 503, 504):
        return "unavailable", f"http-{status}", status
    return "error", f"http-{status}" if status else type(error).__name__, status


def _lookup(connection: sqlite3.Connection, client: Any, identifiers: Mapping[str, str | None]) -> dict[str, Any]:
    """Europe PMC record of a citing work by DOI or PMID: its PMCID and whether it is open access."""
    doi, pmid = identifiers.get("doi"), identifiers.get("pmid")
    if doi:
        query, egress = f'DOI:"{doi}"', {"doi": doi}
    elif pmid:
        query, egress = f"EXT_ID:{pmid} AND SRC:MED", {"pmid": pmid}
    else:
        return {"outcome": "no-open-full-text", "detail": {"reason": "no DOI, PMID or PMCID"}}
    url = f"{EUROPE_PMC}/search"
    params = {"query": query, "pageSize": 5, "resultType": "core", "format": "json"}
    client._authorize("europe-pmc", LOOKUP, egress)
    try:
        response = client.transport.get(url, params=params)
        payload = response.json()
    except ProviderError as error:
        outcome, failure, status = _failure(error)
        request = record_request(connection, provider="europe-pmc", operation=LOOKUP, url=url, params=params,
                                 outcome=outcome, http_status=status, error=failure)
        return {"outcome": "fetch-failed", "request": request, "detail": {"stage": "lookup", "failure": failure}}
    request = record_request(connection, provider="europe-pmc", operation=LOOKUP, url=url, params=params, outcome="ok",
                             http_status=response.status, body=response.body)
    results = ((payload.get("resultList") or {}).get("result") or []) if isinstance(payload, dict) else []
    for record in results:
        if not isinstance(record, dict):
            continue
        same_doi = doi and normalise_identifier("doi", str(record.get("doi") or "")) == normalise_identifier("doi", doi)
        same_pmid = pmid and str(record.get("pmid") or "") == str(pmid)
        if same_doi or same_pmid:
            pmcid = normalise_identifier("pmcid", str(record.get("pmcid") or ""))
            return {"outcome": "found", "request": request, "pmcid": pmcid or None,
                    "open": record.get("isOpenAccess") in ("Y", True)}
    return {"outcome": "no-open-full-text", "request": request, "detail": {"reason": "no Europe PMC record"}}


def fetch_document(connection: sqlite3.Connection, client: Any, identifiers: Mapping[str, str | None]) -> dict[str, Any]:
    """Acquire one citing work's public full text; returns the fu_secondgen_documents fields."""
    pmcid = normalise_identifier("pmcid", identifiers.get("pmcid") or "") or None
    detail: dict[str, Any] = {"identifiers": sorted(name for name, value in identifiers.items() if value)}
    request = None
    if not pmcid:
        found = _lookup(connection, client, identifiers)
        request = found.get("request")
        if found["outcome"] != "found":
            return {"outcome": found["outcome"], "request_id": request, "detail": {**detail, **found.get("detail", {})}}
        if not found["pmcid"] or not found["open"]:
            return {"outcome": "no-open-full-text", "request_id": request,
                    "detail": {**detail, "reason": "no open-access full text in Europe PMC"}}
        pmcid = found["pmcid"]
    url = f"{EUROPE_PMC}/{urllib.parse.quote(pmcid, safe='')}/fullTextXML"
    try:
        document = client._source_document(url, {"pmcid": pmcid})
    except ConfidentialityError:
        raise
    except (ProviderError, ReviewError) as error:
        outcome, failure, status = _failure(error)
        request = record_request(connection, provider="europe-pmc", operation=FULL_TEXT, url=url, params=None,
                                 outcome=outcome, http_status=status, error=failure)
        return {"outcome": "no-open-full-text" if status == 404 else "fetch-failed", "request_id": request,
                "detail": {**detail, "stage": "full-text", "failure": failure, "pmcid": pmcid}}
    body = document["body"]
    request = record_request(connection, provider="europe-pmc", operation=FULL_TEXT, url=url, params=None,
                             outcome="ok", http_status=200, body=body)
    sha = hashlib.sha256(body).hexdigest()
    try:
        review = census_extraction.extract_review(body)
    except (ValidationError, ElementTree.ParseError, ValueError, KeyError, TypeError) as error:
        return {"outcome": "unavailable", "request_id": request, "source_sha256": sha,
                "detail": {**detail, "failure": f"extraction: {type(error).__name__}", "pmcid": pmcid}}
    named = normalise_identifier("doi", review.get("doi") or "")
    wanted = normalise_identifier("doi", identifiers.get("doi") or "")
    if named and wanted and named != wanted:
        return {"outcome": "fetch-failed", "request_id": request, "source_sha256": sha,
                "detail": {**detail, "failure": "the document names another DOI", "pmcid": pmcid}}
    return {"outcome": "obtained", "request_id": request, "source_sha256": sha, "provider": "europe-pmc",
            "provenance_class": "public", "detail": {**detail, "pmcid": pmcid}}


def acquire(connection: sqlite3.Connection, client: Any, anchor_ids: Sequence[str] | None) -> dict[str, Any]:
    if client is None:
        raise ValidationError("the acquire stage needs a provider client")
    condition, params = _in_scope(anchor_ids)
    needed = sorted({row[0] for row in connection.execute(
        f"SELECT DISTINCT citing_key FROM fu_secondgen_sample WHERE {condition}", params)})
    known = dict(connection.execute("SELECT citing_key, outcome FROM fu_secondgen_documents").fetchall())
    outcomes: dict[str, int] = {}
    fetched = 0
    for citing_key in needed:
        if known.get(citing_key) in DONE:
            continue
        identifiers: dict[str, str | None] = {"doi": None, "pmid": None, "pmcid": None}
        for doi, pmid, pmcid in connection.execute("SELECT citing_doi, citing_pmid, citing_pmcid FROM fu_citedby_edges "
                                                   "WHERE citing_key=? ORDER BY edge_id", (citing_key,)):
            identifiers["doi"] = identifiers["doi"] or doi
            identifiers["pmid"] = identifiers["pmid"] or pmid
            identifiers["pmcid"] = identifiers["pmcid"] or pmcid
        if citing_key.startswith("doi:") and not identifiers["doi"]:
            identifiers["doi"] = citing_key[4:]
        if citing_key.startswith("pmid:") and not identifiers["pmid"]:
            identifiers["pmid"] = citing_key[5:]
        result = fetch_document(connection, client, identifiers)
        fetched += 1
        connection.execute(
            "INSERT OR REPLACE INTO fu_secondgen_documents(citing_key, provider, provenance_class, source_sha256, "
            "outcome, request_id, retrieved_at, detail_json) VALUES(?,?,?,?,?,?,?,?)",
            (citing_key, result.get("provider"), result.get("provenance_class"), result.get("source_sha256"),
             result["outcome"], result.get("request_id"), now(), canonical(result.get("detail") or {})))
        outcomes[result["outcome"]] = outcomes.get(result["outcome"], 0) + 1
        connection.commit()
    return {"citingWorks": len(needed), "fetched": fetched, "outcomes": dict(sorted(outcomes.items()))}


# ----------------------------------------------------------------------------- candidates
def _review_pmids(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        return {row[0].lower(): str(row[1]) for row in connection.execute(
            "SELECT doi, pmid FROM census.enumeration WHERE pmid IS NOT NULL")}
    except sqlite3.DatabaseError:
        return {}


def sentence_id(citing_key: str, target_doi: str, text: str) -> str:
    return "sentence-" + digest(citing_key, target_doi, hashlib.sha256(text.encode("utf-8")).hexdigest())[:32]


def extract_sentences(body: bytes, citing_key: str, targets: Iterable[str],
                      pmids: Mapping[str, str]) -> dict[str, list[tuple] | None]:
    """Per target review: the citing sentences of the later article (None when its entry is not located)."""
    review = census_extraction.extract_review(body)
    located: dict[str, list[tuple] | None] = {}
    for target in targets:
        entry = None
        for candidate in review["entries"]:
            doi = normalise_identifier("doi", candidate.identifiers.get("doi") or "")
            pmid = str(candidate.identifiers.get("pmid") or "")
            if (doi and doi == target) or (pmid and pmids.get(target) == pmid):
                entry = candidate
                break
        if entry is None:
            located[target] = None
            continue
        rows, seen = [], set()
        for locus in sorted(entry.loci, key=lambda item: (item.unit, item.offset)):
            if locus.sentence in seen:
                continue
            seen.add(locus.sentence)
            marker = census_extraction.MARK.rstrip() % census_extraction.marker_name(entry.target, html=False)
            rows.append((sentence_id(citing_key, target, locus.sentence), citing_key, target, len(rows) + 1,
                         locus.section, marker, locus.sentence,
                         hashlib.sha256(locus.sentence.encode("utf-8")).hexdigest()))
        located[target] = rows
    return located


def locate(connection: sqlite3.Connection, citing_keys: Iterable[str]) -> tuple[int, set[tuple[str, str]]]:
    """Citing sentences of every obtained document for every review it was sampled for; returns the sentences written
    and the (citing work, review) pairs whose bibliography entry was not located."""
    documents = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT citing_key, source_sha256, outcome FROM fu_secondgen_documents")}
    pmids = _review_pmids(connection)
    written, not_located = 0, set()
    for citing_key in sorted(set(citing_keys)):
        sha, outcome = documents.get(citing_key, (None, None))
        if outcome not in ("obtained", "citation-not-located") or not sha:
            continue
        targets = sorted(row[0] for row in connection.execute(
            "SELECT DISTINCT target_doi FROM fu_secondgen_sample WHERE citing_key=?", (citing_key,)))
        located = extract_sentences(read_artifact(connection, sha), citing_key, targets, pmids)
        for target, rows in located.items():
            connection.execute("DELETE FROM fu_secondgen_sentences WHERE citing_key=? AND target_doi=?",
                               (citing_key, target))
            if rows is None:
                not_located.add((citing_key, target))
                continue
            connection.executemany("INSERT OR REPLACE INTO fu_secondgen_sentences(sentence_id, citing_key, target_doi, "
                                   "ordinal, section, marker, text, text_sha256) VALUES(?,?,?,?,?,?,?,?)", rows)
            written += len(rows)
        connection.execute("UPDATE fu_secondgen_documents SET outcome=? WHERE citing_key=?",
                           ("obtained" if any(rows is not None for rows in located.values()) else
                            "citation-not-located", citing_key))
    return written, not_located


def candidates(connection: sqlite3.Connection, branch: FollowupBranch, protocol: Mapping[str, Any],
               anchor_ids: Sequence[str] | None, instrument: Any) -> dict[str, Any]:
    k = int(settings(protocol)["k"])
    condition, params = _in_scope(anchor_ids)
    sampled = connection.execute(f"SELECT anchor_locus_id, citing_key, target_doi, inclusion_probability FROM "
                                 f"fu_secondgen_sample WHERE {condition} ORDER BY anchor_locus_id, citing_key",
                                 params).fetchall()
    written, not_located = locate(connection, [row[1] for row in sampled])
    outcomes = dict(connection.execute("SELECT citing_key, outcome FROM fu_secondgen_documents").fetchall())
    by_anchor: dict[str, list[tuple[str, str, float]]] = {}
    for anchor, citing_key, target, probability in sampled:
        by_anchor.setdefault(anchor, []).append((citing_key, target, probability))
    ranked_total = beyond_total = 0
    created = now()
    with tempfile.TemporaryDirectory(prefix="ocsl-secondgen-") as scratch:
        for number, (anchor, edges) in enumerate(sorted(by_anchor.items())):
            locus = connection.execute("SELECT journal, doi, label, seq FROM fu_frame WHERE locus_id=?",
                                       (anchor,)).fetchone()
            text, reason = branch.sentence(connection, locus[0], locus[1], locus[2], locus[3])
            connection.execute("DELETE FROM fu_secondgen_matches WHERE anchor_locus_id=?", (anchor,))
            connection.execute("DELETE FROM fu_secondgen_candidates WHERE anchor_locus_id=?", (anchor,))
            pool = {citing_key: (target, probability, connection.execute(
                "SELECT sentence_id, text FROM fu_secondgen_sentences WHERE citing_key=? AND target_doi=? ORDER BY "
                "ordinal", (citing_key, target)).fetchall()) for citing_key, target, probability in edges}
            units = [EvidenceUnit(sid, citing_key, "citing-sentence", sentence)
                     for citing_key, (_, _, rows) in sorted(pool.items()) for sid, sentence in rows]
            index = EvidenceIndex.build(Path(scratch) / f"anchor-{number}.sqlite3", units) if units and text else None
            # Markers are rendering, not content: the query is the claim's words only.
            query = census_extraction.MARK_PATTERN.sub(" ", text or "")
            try:
                for citing_key, (target, probability, rows) in sorted(pool.items()):
                    if not rows:
                        why = ("citation-not-located" if (citing_key, target) in not_located
                               else outcomes.get(citing_key) or "not-acquired")
                        connection.execute("INSERT INTO fu_secondgen_candidates(anchor_locus_id, citing_key, state, "
                                           "reason, k, sentences, candidates, created_at) VALUES(?,?,?,?,?,?,?,?)",
                                           (anchor, citing_key, "no-sentences", why, k, 0, 0, created))
                        continue
                    if index is None:
                        connection.execute("INSERT INTO fu_secondgen_candidates(anchor_locus_id, citing_key, state, "
                                           "reason, k, sentences, candidates, created_at) VALUES(?,?,?,?,?,?,?,?)",
                                           (anchor, citing_key, "anchor-text-unavailable", reason, k, len(rows), 0,
                                            created))
                        connection.executemany(
                            "INSERT INTO fu_secondgen_matches(anchor_locus_id, sentence_id, citing_key, assessable, "
                            "reason, inclusion_probability) VALUES(?,?,?,0,?,?)",
                            [(anchor, sid, citing_key, reason or "no-citing-text", probability) for sid, _ in rows])
                        beyond_total += len(rows)
                        continue
                    receipt = index.search(query, work_ids=[citing_key], k=k)
                    receipt_sha = store_artifact(connection, canonical(receipt.record()).encode("utf-8"),
                                                 "retrieval-receipt", "application/json")
                    hits = {hit.unit_id: hit for hit in receipt.hits}
                    connection.execute("INSERT INTO fu_secondgen_candidates(anchor_locus_id, citing_key, state, reason, "
                                       "receipt_id, receipt_sha256, index_digest, k, sentences, candidates, created_at) "
                                       "VALUES(?,?,?,NULL,?,?,?,?,?,?,?)",
                                       (anchor, citing_key, "ranked", receipt.receipt_id, receipt_sha,
                                        receipt.index_digest, k, len(rows), len(hits), created))
                    connection.executemany(
                        "INSERT INTO fu_secondgen_matches(anchor_locus_id, sentence_id, citing_key, rank, score, "
                        "assessable, reason, inclusion_probability) VALUES(?,?,?,?,?,0,'not-judged',?)",
                        [(anchor, sid, citing_key, hits[sid].rank if sid in hits else None,
                          hits[sid].score if sid in hits else None, probability) for sid, _ in rows])
                    ranked_total += len(hits)
                    beyond_total += len(rows) - len(hits)
            finally:
                if index is not None:
                    index.close()
    return {"k": k, "sentences": written, "anchors": len(by_anchor), "candidates": ranked_total,
            "notJudgedBeyondK": beyond_total, "citationsNotLocated": len(not_located),
            "verdictsApplied": refresh_matches(connection, instrument)}


# ----------------------------------------------------------------------------- judge
def refresh_matches(connection: sqlite3.Connection, instrument: Any) -> int:
    """Write the primary claim-match@1 judgments into the ranked candidate rows (rejected -> invalid-output)."""
    verdicts = accepted(connection, instrument.digest, TIER, "primary")
    judgment_ids = dict(connection.execute(
        "SELECT item_key, judgment_id FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant='primary' "
        "AND outcome='accepted'", (instrument.digest, TIER)).fetchall())
    rejected = {row[0] for row in connection.execute(
        "SELECT DISTINCT item_key FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant='primary' "
        "AND outcome='rejected'", (instrument.digest, TIER))}
    applied = 0
    for anchor, sentence in connection.execute("SELECT anchor_locus_id, sentence_id FROM fu_secondgen_matches "
                                               "WHERE rank IS NOT NULL").fetchall():
        key = f"{anchor}|{sentence}"
        if key in verdicts:
            fields = claim_match.row(verdicts[key])
            judgment = judgment_ids.get(key)
            applied += 1
        else:
            fields = {"match": None, "reliance": None, "assessable": 0, "quote": None,
                      "reason": "invalid-output" if key in rejected else "not-judged"}
            judgment = None
        connection.execute("UPDATE fu_secondgen_matches SET match=?, reliance=?, assessable=?, reason=?, quote=?, "
                           "judgment_id=? WHERE anchor_locus_id=? AND sentence_id=?",
                           (fields["match"], fields["reliance"], fields["assessable"], fields["reason"], fields["quote"],
                            judgment, anchor, sentence))
    return applied


def judge_items(connection: sqlite3.Connection, branch: FollowupBranch,
                anchor_ids: Sequence[str] | None) -> list[dict[str, Any]]:
    condition, params = _in_scope(anchor_ids, "m.anchor_locus_id")
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    items, texts = [], {}
    for row in cursor.execute(
            "SELECT m.anchor_locus_id, m.sentence_id, s.text, s.marker, s.section, f.journal, f.doi, f.label, f.seq, "
            "f.provenance_class, r.title AS review_title, d.provenance_class AS document_provenance "
            "FROM fu_secondgen_matches m JOIN fu_secondgen_sentences s ON s.sentence_id=m.sentence_id "
            "JOIN fu_frame f ON f.locus_id=m.anchor_locus_id "
            "JOIN census.reviews r ON r.journal=f.journal AND r.doi=f.doi "
            "LEFT JOIN fu_secondgen_documents d ON d.citing_key=m.citing_key "
            f"WHERE m.rank IS NOT NULL AND {condition} ORDER BY m.anchor_locus_id, m.citing_key, m.rank", params):
        anchor = row["anchor_locus_id"]
        if anchor not in texts:
            texts[anchor] = branch.sentence(connection, row["journal"], row["doi"], row["label"], row["seq"])[0]
        if not texts[anchor]:
            continue
        entitled = row["provenance_class"] != "public" or row["document_provenance"] == "entitled"
        items.append({"id": f"{anchor}|{row['sentence_id']}", "originalClaim": texts[anchor],
                      "originalReview": row["review_title"] or "", "candidateSentence": row["text"],
                      "candidateMarker": row["marker"], "candidateSection": row["section"] or "",
                      "provenance": "entitled" if entitled else "public"})
    return items


def confirm(connection: sqlite3.Connection, branch: FollowupBranch, protocol: Mapping[str, Any], instrument: Any,
            judge_: Any, policy: Any, model: str, *, anchor_ids: Sequence[str] | None, variant: str, public_only: bool,
            limit: int | None, concurrency: int, transport_retries: int, cooldown: float,
            sleep: Callable[[float], None], code_revision: str | None) -> dict[str, Any]:
    items = judge_items(connection, branch, anchor_ids)
    if variant == "replicate":
        anchors_of = {item["id"]: item["id"].split("|")[0] for item in items}
        groups = replicate_groups(connection, set(anchors_of.values()))
        keep = set(replicate_keys(protocol, [item["id"] for item in items], scope_parts=(instrument.id,),
                                  groups={key: groups[anchor] for key, anchor in anchors_of.items()}))
        items = [item for item in items if item["id"] in keep]
    if public_only:
        items = [item for item in items if item["provenance"] == "public"]
    if limit is not None:
        items = items[:max(0, int(limit))]
    summary = run_items(connection, instrument, items, judge_, tier=TIER, model=model, policy=policy, variant=variant,
                        concurrency=concurrency, code_revision=code_revision, transport_retries=transport_retries,
                        cooldown=cooldown, sleep=sleep)
    applied = refresh_matches(connection, instrument) if variant == "primary" else None
    return {"sent": len(items), "verdictsApplied": applied, "run": summary}


# ----------------------------------------------------------------------------- command
def run_second_gen(branch: FollowupBranch, *, stages: Sequence[str] = STAGES, client: Any = None, judge_: Any = None,
                   policy: Any = None, model: str | None = None, scope: str = "all", variant: str = "primary",
                   public_only: bool = False, limit: int | None = None, concurrency: int = 1,
                   transport_retries: int = 0, cooldown: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                   code_revision: str | None = None) -> dict[str, Any]:
    unknown = sorted(set(stages) - set(STAGES))
    if unknown:
        raise ValidationError("unknown second-gen stages: " + ", ".join(unknown))
    result: dict[str, Any] = {"database": str(branch.path), "stages": [stage for stage in STAGES if stage in stages]}
    with branch.writing() as connection:
        require_freeze(connection)
        protocol = protocol_of(branch)
        settings(protocol)
        if "judge" in stages and model != protocol["model"]["served"]:
            raise ValidationError("--model must be the served model the protocol pins (model.served)")
        instrument = claim_match.instrument()
        if "judge" in stages:
            prepare_judge(judge_, instrument, TIER)
        if "sample" in stages:
            result["sample"] = sample(connection, protocol)
        anchor_ids = [row["locus_id"] for row in anchors(connection, pilot_only=True)] if scope == "pilot" else None
        if "acquire" in stages:
            result["acquire"] = acquire(connection, client, anchor_ids)
        if "candidates" in stages:
            result["candidates"] = candidates(connection, branch, protocol, anchor_ids, instrument)
        summary = None
        if "judge" in stages:
            confirmed = confirm(connection, branch, protocol, instrument, judge_, policy, model,
                                anchor_ids=anchor_ids, variant=variant, public_only=public_only, limit=limit,
                                concurrency=concurrency, transport_retries=transport_retries, cooldown=cooldown,
                                sleep=sleep, code_revision=code_revision)
            summary = confirmed.pop("run")
            result.update(confirm=confirmed, run=summary, judge=judge_summary(judge_))
    return {**result, **step_result(summary, judge_ if summary is not None else None)}


def cmd_second_gen(args: Any) -> dict[str, Any]:
    from . import followup_net
    from .census_runner import code_revision
    stages = STAGES if args.stage == "all" else (args.stage,)
    client = judge_ = policy = None
    if "acquire" in stages:
        client = followup_net.client(workspace=Path(args.workspace) if getattr(args, "workspace", None) else None)
    if "judge" in stages:
        judge_, policy = judge_from_args(args)
    try:
        return run_second_gen(FollowupBranch(Path(args.database)), stages=stages, client=client, judge_=judge_,
                              policy=policy, model=getattr(args, "model", None), scope=args.scope,
                              variant=args.variant, public_only=bool(args.public_only), limit=args.limit,
                              concurrency=args.concurrency, transport_retries=args.transport_retries,
                              cooldown=args.transport_cooldown, code_revision=code_revision())
    finally:
        if judge_ is not None:
            close_judge(judge_)
