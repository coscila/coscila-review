# SPDX-License-Identifier: Apache-2.0
"""Corpus-wide acquisition, local identity reconciliation and durable task planning."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .corpus_store import CorpusRepository, canonical, identity
from .corpus_ingest import ingest_file
from .corpus_library import now
from .errors import ConfidentialityError, ProviderError, ReviewError, ValidationError
from .evidence import complete_abstract
from .corpus_acquisition import failure_kind
from .scholarly import ScholarlyClient, normalize_doi, normalize_europe_pmc, parse_pubmed_record


def _identifiers(value):
    result = {k: str(v).strip() for k, v in value.items() if k in {"doi", "pmid", "pmcid"} and v}
    if "doi" in result:
        result["doi"] = normalize_doi(result["doi"])
    if "pmcid" in result:
        result["pmcid"] = result["pmcid"].upper()
    return result


def _text(value):
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def _matches(record, identifiers):
    """An identifier match cannot override a second explicitly conflicting ID."""
    try:
        observed = _identifiers(record)
    except ReviewError:
        return False
    shared = set(observed) & set(identifiers)
    return bool(shared) and all(observed[k].casefold() == identifiers[k].casefold() for k in shared)


def _title_in_reference(record, entry):
    candidate = json.loads(entry["candidate_identifiers_json"])
    title = _text(candidate.get("identity", {}).get("title"))
    observed = _text(record.get("title"))
    return bool(observed) and (title == observed if title else " " + observed + " " in " " + _text(entry["raw_reference"]) + " ")


def _cache_artifacts(repo, provenance):
    for item in provenance:
        path, expected = item.get("artifactPath"), item.get("sha256") or item.get("artifactSha256")
        if not path or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValidationError("provider cache provenance requires an artifact path and SHA-256")
        path = Path(path)
        if path.is_symlink():
            raise ValidationError("provider cache artifact must not be a symlink")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValidationError("provider cache provenance hash mismatch")
        repo.artifact(body, "raw-provider-response", item.get("mediaType") or "application/octet-stream")


def register_acquisition(repo: CorpusRepository, work_id: str, result: dict) -> str:
    """Raw provider responses remain portable even after the cache is removed."""
    _cache_artifacts(repo, result.get("cacheProvenance", []))
    digest = repo.json_artifact(result, "acquisition-receipt")
    with repo.connect(write=True) as connection:
        connection.execute("INSERT OR IGNORE INTO retrievals VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "retrieval-" + identity(work_id, digest), work_id, digest, "corpus-acquisition",
            "", result.get("retrievedAt") or now(), None,
            "acquired" if result.get("documents") or result.get("abstract") else "failed" if result.get("retryable") else "unavailable",
            None, canonical({"outcome": result.get("outcome"), "retryable": result.get("retryable", False)})))
    if result.get("abstract"):
        candidates = [c for c in result.get("abstractCandidates", []) if c.get("text") == result["abstract"]]
        selected = max(candidates, key=complete_abstract, default={})
        repo.document(work_id, {"text": result["abstract"], "complete": bool(result.get("abstractComplete") and complete_abstract(selected)),
                               "sourceUrl": selected.get("provenance", {}).get("sourceUrl", ""), "provenance": selected,
                               "acquisitionReceiptSha256": digest}, "abstract", result.get("abstractProvider") or "unknown")
    for document in result.get("documents", []):
        retained = dict(document)
        original = retained.pop("originalBody", None)
        if isinstance(original, bytes):
            retained["originalArtifactSha256"] = repo.artifact(original, "raw-publisher-source", document.get("mediaType", "application/octet-stream"))
        repo.document(work_id, {**retained, "acquisitionReceiptSha256": digest}, "full-text", document.get("provider", "unknown"))
    return digest


def _title_search(acquirer, entry):
    """Bounded candidate retrieval; printed title AND year remain the decision rule."""
    candidate = json.loads(entry["candidate_identifiers_json"])
    printed = candidate.get("identity", {})
    title = _text(printed.get("title"))
    years = {str(printed["year"])} if printed.get("year") else set(re.findall(r"\b(?:18|19|20)\d{2}\b", entry["raw_reference"]))
    if len(years) != 1:
        return {"state": "unavailable", "reason": "a unique printed publication year is required for title resolution", "records": []}
    year = next(iter(years))
    query = title or _text(entry["raw_reference"])
    if not query or len(query) > 2000:
        return {"state": "unavailable", "reason": "printed title query is absent or exceeds the bound", "records": []}
    with acquirer.cache.capture() as provenance:
        try:
            response = acquirer.client.resolve_reference({"title": query, "year": year})
        except ProviderError as exc:
            return {"state": "unresolved" if failure_kind(exc)["retryable"] else "unavailable",
                    "reason": str(exc), **failure_kind(exc), "records": [], "cacheProvenance": provenance}
        candidates = {}
        for record in response["records"]:
            if not _title_in_reference(record, entry) or str(record.get("year")) != year:
                continue
            identifiers = _identifiers(record)
            if not identifiers:
                continue
            key = next(f"{k}:{identifiers[k].casefold()}" for k in ("doi", "pmid", "pmcid") if k in identifiers)
            candidates[key] = identifiers
        return {"state": "verified" if len(candidates) == 1 else "conflict" if candidates else "unavailable",
                "reason": "exact normalized printed title and year; unique identifier required",
                "identifiers": next(iter(candidates.values())) if len(candidates) == 1 else {},
                "records": response["records"], "cacheProvenance": provenance,
                "candidateCount": len(candidates), "query": query, "year": year}


def _checkpoint(repo, analysis, entry, state, checkpoint):
    with repo.connect(write=True) as connection:
        connection.execute("INSERT INTO processing_state VALUES(?,?,?, ?,1,?,?) ON CONFLICT(analysis_id,task_key,phase) DO UPDATE SET status=excluded.status,attempt_count=processing_state.attempt_count+1,checkpoint_json=excluded.checkpoint_json,updated_at=excluded.updated_at",
                           (analysis, entry, "citation-acquisition", state, canonical(checkpoint), now()))


def _retained_abstract(repo, identifiers):
    """Reuse complete source fields, never upgrade legacy text or old verdicts.

    The original provider record establishes identity again; a cached work ID or
    alias is only a lookup hint. Printed-reference reconciliation still follows.
    """
    candidate_ids = {"work-" + identity(f"{scheme}:{value.lower()}") for scheme, value in identifiers.items()}
    with repo.connect() as connection:
        for scheme, value in identifiers.items():
            for row in connection.execute("SELECT * FROM work_identifiers WHERE scheme=? AND value=?", (scheme, value.lower())):
                # Proof registration and content integrity are checked even
                # though the structured provider record is re-read below.
                proof = row[3]
                if proof:
                    repo.verify_artifact(proof)
                    candidate_ids.add(row["work_id"])
        rows = []
        for work_id in candidate_ids:
            row = connection.execute("SELECT work_id,metadata_json FROM works WHERE work_id=?", (work_id,)).fetchone()
            if row:
                rows.append(dict(row))
    if len(rows) != 1:
        return None
    work = rows[0]
    metadata = json.loads(work["metadata_json"])
    known_ids = _identifiers(metadata)
    if not metadata.get("title") or any(known_ids[k].casefold() != value.casefold()
                                      for k, value in identifiers.items() if k in known_ids):
        return None
    evidence = repo.evidence(work["work_id"], "abstract")
    if not evidence or not evidence["complete"]:
        return None
    with repo.connect() as connection:
        row = connection.execute("SELECT metadata_json FROM corpus_documents WHERE document_id=?", (evidence["documentId"],)).fetchone()
    document_metadata = json.loads(row[0])
    source = json.loads(repo.read_artifact(evidence["sourceSha256"]))
    candidate = document_metadata.get("completeAbstractCandidate") or source.get("provenance")
    if not complete_abstract(candidate):
        return None
    if "\n\n".join(unit["text"] for unit in evidence["evidenceUnits"]) != candidate["text"]:
        return None
    original_receipt = source.get("acquisitionReceiptSha256")
    if document_metadata.get("completeAbstractCandidate"):
        # A legacy normalized column, even labelled Europe PMC, is insufficient.
        if candidate["rawMetadata"] not in source.get("resultList", {}).get("result", []):
            return None
    elif original_receipt:
        receipt = json.loads(repo.read_artifact(original_receipt))
        if receipt.get("abstract") != candidate["text"] or candidate not in receipt.get("abstractCandidates", []):
            return None
        for item in receipt.get("cacheProvenance", []):
            repo.verify_artifact(item.get("sha256") or item.get("artifactSha256"))
    else:
        return None
    if candidate["provider"] == "europe-pmc":
        record = normalize_europe_pmc(candidate["rawMetadata"])
    else:
        record = parse_pubmed_record(candidate["rawMetadata"]["xml"].encode(), candidate["provenance"]["identifiers"]["pmid"])
    if (not _matches(record, identifiers)
            or _identifiers(record) != _identifiers(candidate["provenance"]["identifiers"])
            or _text(record.get("title")) != _text(metadata["title"])):
        return None
    return {"operation": "reuse_retained_abstract", "outcome": "acquired", "retryable": False,
            "abstract": candidate["text"], "abstractComplete": True, "abstractCandidates": [candidate],
            "abstractProvider": candidate["provider"], "documents": [], "cacheProvenance": [],
            "retrievedAt": candidate["provenance"]["retrievedAt"],
            "identityResolution": {"records": [record], "method": "retained-structured-provider-identity"},
            "retainedEvidence": {"workId": work["work_id"], "documentId": evidence["documentId"],
                                 "sourceSha256": evidence["sourceSha256"], "acquisitionReceiptSha256": original_receipt},
            "publicationStatus": {"state": "unknown", "coverage": "retained-provider-record-only",
                                  "currentSurveillancePerformed": False},
            "limitations": ["Reused complete structured abstract and identity from retained evidence; publication status was not refreshed."]}


def acquire_corpus(repo: CorpusRepository, acquirer, *, workers: int = 12, retry: bool = False,
                   progress=None) -> dict:
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValidationError("acquisition workers must be a positive integer")
    with repo.connect() as connection:
        reviews = [dict(r) for r in connection.execute("SELECT r.review_id,w.work_id,w.metadata_json,c.phase,c.state FROM reviews r JOIN works w USING(work_id) JOIN corpus_reviews c USING(review_id) WHERE inclusion_state='included'")]
    selected = [r for r in reviews if r["phase"] == "pending" or r["state"] == "failed" or retry and r["state"] == "blocked"]
    analysis = repo.analysis()["analysis_id"]
    from .corpus_review_reuse import retained_review_sources, reuse_review
    retained = retained_review_sources(repo, selected)
    missing = []
    for row in selected:
        if not reuse_review(repo, row, retained.get(row["work_id"], {})):
            missing.append(row)
        if progress:
            progress()
    selected = missing

    def review_fetch(row):
        try:
            return row, acquirer.fetch_review(_identifiers(json.loads(row["metadata_json"]))), None
        except ReviewError as exc:
            if isinstance(exc, ConfidentialityError):
                raise
            return row, None, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(review_fetch, row) for row in selected]):
            row, result, error = future.result()
            if error:
                repo.review_state(row["review_id"], "acquisition", "failed", error)
                continue
            receipt = register_acquisition(repo, row["work_id"], result)
            doc = result.get("document")
            if not doc:
                repo.review_state(row["review_id"], "acquisition", "failed" if result.get("retryable") else "blocked",
                                  result.get("outcome"), {"receiptSha256": receipt})
                continue
            suffix = ".html" if doc["mediaType"] == "text/html" else ".xml"
            directory = repo.root / "ingest"
            directory.mkdir(exist_ok=True)
            path = directory / (hashlib.sha256(doc["body"]).hexdigest() + suffix)
            if not path.exists():
                path.write_bytes(doc["body"])
            ingest_file(repo, row["review_id"], path, public=True, source_url=doc["sourceUrl"], provider=doc["provider"])
            if progress:
                progress()

    with repo.connect() as connection:
        entries = [dict(r) for r in connection.execute(
            "SELECT * FROM bibliography_entries WHERE identity_state='unresolved' OR entry_id IN "
            "(SELECT task_key FROM processing_state WHERE analysis_id=? AND phase='citation-acquisition' AND status='failed')" +
            (" OR identity_state IN ('unavailable','conflict')" if retry else ""), (analysis,))]
    groups, title_groups = {}, {}
    for entry in entries:
        candidate = json.loads(entry["candidate_identifiers_json"])
        identifiers = _identifiers(candidate.get("identifiers", candidate))
        key = next((f"{k}:{identifiers[k].lower()}" for k in ("doi", "pmid", "pmcid") if identifiers.get(k)), None)
        if key:
            # Keep all printed identifiers per entry; a shared DOI does not erase
            # a conflicting PMID on another bibliography slot.
            groups.setdefault(key, {"identifiers": {key.split(":", 1)[0]: key.split(":", 1)[1]}, "entries": []})["entries"].append(entry)
        else:
            printed = candidate.get("identity", {})
            year_key = str(printed["year"]) if printed.get("year") else sorted(set(re.findall(r"\b(?:18|19|20)\d{2}\b", entry["raw_reference"])))
            key = identity(_text(printed.get("title")) or _text(entry["raw_reference"]), year_key)
            title_groups.setdefault(key, []).append(entry)
    values = iter(title_groups.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while chunk := [item for _, item in zip(range(workers * 4), values)]:
            futures = {pool.submit(_title_search, acquirer, group[0]): group for group in chunk}
            for future in as_completed(futures):
                group, lookup = futures[future], future.result()
                _cache_artifacts(repo, lookup.get("cacheProvenance", []))
                proof = repo.json_artifact(lookup, "title-resolution")
                if lookup["state"] == "verified":
                    ids = lookup["identifiers"]
                    key = next(f"{k}:{ids[k].lower()}" for k in ("doi", "pmid", "pmcid") if ids.get(k))
                    for entry in group:
                        entry["titleResolutionSha256"] = proof
                    groups.setdefault(key, {"identifiers": ids, "entries": []})["entries"].extend(group)
                else:
                    with repo.connect(write=True) as connection:
                        connection.executemany("UPDATE bibliography_entries SET identity_state=?,identity_evidence_sha256=? WHERE entry_id=?",
                                               [(lookup["state"], proof, entry["entry_id"]) for entry in group])
                    for entry in group:
                        _checkpoint(repo, analysis, entry["entry_id"], "failed" if lookup.get("retryable") else "blocked", {"titleResolutionSha256": proof})
    for group in groups.values():
        retained = _retained_abstract(repo, group["identifiers"])
        if retained:
            # Keep only a digest in the population-sized queue, not all abstracts.
            group["retainedReceiptSha256"] = repo.json_artifact(retained, "acquisition-receipt")
    prefetch = acquirer.prefetch_dois(g["identifiers"]["doi"] for g in groups.values()
                                    if g["identifiers"].get("doi") and not g.get("retainedReceiptSha256"))
    _cache_artifacts(repo, prefetch.get("cacheProvenance", []))
    repo.json_artifact(prefetch, "corpus-prefetch-receipt")

    def work_fetch(group):
        try:
            if group.get("retainedReceiptSha256"):
                return group, json.loads(repo.read_artifact(group["retainedReceiptSha256"])), None
            return group, acquirer.fetch_work(group["identifiers"]), None
        except ReviewError as exc:
            if isinstance(exc, ConfidentialityError):
                raise
            return group, None, str(exc)

    # Bounded futures avoid retaining a second 600k-item result corpus in memory.
    values = iter(groups.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while chunk := [item for _, item in zip(range(workers * 4), values)]:
            for future in as_completed([pool.submit(work_fetch, group) for group in chunk]):
                group, result, error = future.result()
                metadata = dict(group["identifiers"])
                if result:
                    records = result.get("identityResolution", {}).get("records", [])
                    metadata = {**next((r for r in records if r.get("title") and _matches(r, metadata)), {}), **metadata}
                else:
                    records = []
                    result = {"outcome": "retryable", "retryable": True, "error": error}
                work_id = repo.work(metadata)
                receipt = register_acquisition(repo, work_id, result)
                for entry in group["entries"]:
                    candidate = json.loads(entry["candidate_identifiers_json"])
                    printed_ids = _identifiers(candidate.get("identifiers", {})) or group["identifiers"]
                    printed = {**candidate.get("identity", {}), **printed_ids, "raw": entry["raw_reference"]}
                    reconciled = ScholarlyClient.reconcile_reference_metadata(records, printed)
                    # Identity is independent of minor bibliographic discrepancies.
                    # Exact identifier + exact normalized printed title is a checkable match.
                    matching = [r for r in records if _matches(r, printed_ids)]
                    titles = {_text(r.get("title")) for r in matching if r.get("title")}
                    title_match = len(titles) == 1 and any(_title_in_reference(r, entry) for r in matching)
                    # A conflicting provider identity remains visible even if a
                    # different provider agrees with the first printed identifier.
                    conflicts = any(any(r.get(k) and str(r[k]).casefold() != str(v).casefold()
                                        for k, v in printed_ids.items()) for r in records if _matches(r, group["identifiers"]))
                    state = "verified" if matching and title_match and not conflicts else "unresolved" if result.get("retryable") and not records else "unavailable" if not records else "conflict"
                    proof = repo.json_artifact({"acquisitionSha256": receipt, "printedReference": printed,
                                               "reconciliation": reconciled, "identityMethod": "exact-identifier-and-printed-title-v2",
                                               "identityState": state, "titleResolutionSha256": entry.get("titleResolutionSha256")}, "identity-reconciliation")
                    with repo.connect(write=True) as connection:
                        connection.execute("UPDATE bibliography_entries SET resolved_work_id=?,identity_state=?,identity_evidence_sha256=? WHERE entry_id=?",
                                           (work_id, state, proof, entry["entry_id"]))
                        if state == "verified":
                            for scheme, value in printed_ids.items():
                                connection.execute("INSERT OR IGNORE INTO work_identifiers VALUES (?,?,?,?)", (work_id, scheme, value.lower(), proof))
                    _checkpoint(repo, analysis, entry["entry_id"], "failed" if result.get("retryable") and not result.get("abstract") else "completed" if state == "verified" else "blocked", {"acquisitionSha256": receipt, "identityState": state})
                if progress:
                    progress()
    return repo.status()


def _escalated_loci(connection, analysis):
    assessed = {r[0] for r in connection.execute("SELECT locus_id FROM assessments WHERE analysis_id=? AND validation_state='accepted' AND evidence_level='abstract' AND json_extract(verdict_json,'$.escalate')=1", (analysis,))}
    # Missing/incomplete abstracts cannot generate a model escalation receipt.
    # They remain a separate, explicit acquisition reason; identity must be known.
    missing = {r[0] for r in connection.execute("SELECT l.locus_id FROM claim_loci l JOIN bibliography_entries b USING(entry_id) WHERE b.identity_state='verified' AND NOT EXISTS (SELECT 1 FROM corpus_documents d WHERE d.work_id=b.resolved_work_id AND d.evidence_tier='abstract' AND d.complete=1)")}
    return assessed | missing


def prepare_tasks(repo: CorpusRepository, *, tier: str = "abstract", context_chars: int = 100000) -> dict:
    from .corpus_assessment import TaskPlanner
    if tier not in {"abstract", "full-text"} or context_chars < 1000:
        raise ValidationError("invalid evidence tier or context limit")
    analysis = repo.analysis()["analysis_id"]
    with repo.connect() as connection:
        rows = [dict(r) for r in connection.execute("SELECT l.*,b.resolved_work_id,b.identity_state,b.candidate_identifiers_json,b.review_id,w.metadata_json,c.checkpoint_json FROM claim_loci l JOIN bibliography_entries b USING(entry_id) LEFT JOIN works w ON w.work_id=b.resolved_work_id JOIN corpus_reviews c USING(review_id)")]
        done = {(r[0], r[1]) for r in connection.execute("SELECT locus_id,tier FROM corpus_tasks WHERE analysis_id=? AND state IN ('completed','pending','running')", (analysis,))}
        done.update((r[0], r[1]) for r in connection.execute("SELECT locus_id,evidence_level FROM assessments WHERE analysis_id=? AND validation_state='accepted'", (analysis,)))
        escalated = _escalated_loci(connection, analysis)
    count = blocked = 0
    for row in rows:
        if (row["locus_id"], tier) in done:
            continue
        if tier == "full-text" and row["locus_id"] not in escalated:
            continue
        evidence = repo.evidence(row["resolved_work_id"], tier) if row["resolved_work_id"] else None
        reason = None if row["identity_state"] == "verified" else "cited work identity is " + row["identity_state"]
        if not evidence:
            reason = reason or tier + " evidence unavailable"
            evidence = {"evidenceTier": tier, "evidenceUnits": [], "complete": False}
        units = evidence["evidenceUnits"]
        if sum(len(u["text"]) for u in units) > context_chars:
            # Persist a deterministic cited-text window and label it incomplete.
            terms = set(re.findall(r"\w{4,}", row["claim_text"].lower()))
            ranked = sorted(enumerate(units), key=lambda p: (-len(terms & set(re.findall(r"\w{4,}", p[1]["text"].lower()))), p[0]))
            selected, used = [], 0
            for index, unit in ranked:
                remaining = context_chars - used
                if remaining <= 0:
                    break
                excerpt = unit if len(unit["text"]) <= remaining else {**unit, "text": unit["text"][:remaining], "excerpt": True}
                selected.append((index, excerpt)); used += len(excerpt["text"])
            evidence = {**evidence, "evidenceUnits": [u for _, u in sorted(selected)], "complete": False}
        try:
            locator = json.loads(row["locator"])
        except ValueError:
            locator = {"locator": row["locator"], "bindingStatus": "exact"}
        metadata = json.loads(row["metadata_json"] or "{}")
        cited_work = {key: metadata[key] for key in ("title", "year", "authors", "doi", "pmid", "pmcid", "journal")
                      if metadata.get(key) is not None}
        if not cited_work.get("journal") and metadata.get("container"):
            cited_work["journal"] = metadata["container"]
        spec = {**evidence, "claim": row["claim_text"], "context": json.loads(row["context_text"]),
                "citationLocus": {"id": row["locus_id"], **locator}, "claimType": locator.get("claimType", "background"),
                "citedWork": cited_work,
                "identifiers": json.loads(row["candidate_identifiers_json"]).get("identifiers", {}),
                "identityVerified": row["identity_state"] == "verified", "reviewId": row["review_id"],
                "reviewProof": json.loads(row["checkpoint_json"])}
        planned = TaskPlanner().plan({**spec, "id": row["locus_id"], "blockedReason": reason})
        reason = reason or planned["reason"]
        task_id = repo.add_task(analysis, row["locus_id"], tier, spec, reason)
        if not reason:
            with repo.connect(write=True) as connection:
                connection.execute("UPDATE corpus_tasks SET state='cancelled',reason='superseded by acquired evidence',updated_at=? WHERE analysis_id=? AND locus_id=? AND tier=? AND task_id!=? AND state='blocked'",
                                   (now(), analysis, row["locus_id"], tier, task_id))
        count += 1; blocked += bool(reason)
    return {"analysisId": analysis, "tier": tier, "tasks": count, "blocked": blocked}


def acquire_escalations(repo: CorpusRepository, acquirer, *, progress=None) -> dict:
    with repo.connect() as connection:
        loci = _escalated_loci(connection, repo.analysis()["analysis_id"])
        # Iterate the binding index rather than constructing a SQL IN list that
        # exceeds SQLite's variable bound on a large corpus.
        rows = {}
        for row in connection.execute("SELECT l.locus_id,w.work_id,w.metadata_json FROM claim_loci l JOIN bibliography_entries b USING(entry_id) JOIN works w ON w.work_id=b.resolved_work_id WHERE b.identity_state='verified'"):
            if row["locus_id"] in loci:
                rows[row["work_id"]] = dict(row)
    acquired = 0
    for row in rows.values():
        if repo.evidence(row["work_id"], "full-text"):
            continue
        result = acquirer.fetch_work(_identifiers(json.loads(row["metadata_json"])), include_full_text=True)
        register_acquisition(repo, row["work_id"], result)
        acquired += 1
        if progress:
            progress()
    return {"targetedWorks": len(rows), "acquisitionAttempts": acquired}
