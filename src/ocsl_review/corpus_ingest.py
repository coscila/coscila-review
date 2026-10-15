# SPDX-License-Identifier: Apache-2.0
"""Deterministic corpus manifests and citation bindings from maintained parsers."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from .claim_inventory import build_claim_inventory
from .corpus_store import CorpusRepository, canonical, identity
from .errors import ValidationError
from .ingest import ingest_manuscript


def import_manifest(repo: CorpusRepository, path: Path) -> dict:
    raw = path.read_bytes()
    digest = repo.artifact(raw, "corpus-manifest", "text/csv" if path.suffix == ".csv" else "application/json")
    if path.suffix == ".csv":
        records = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    else:
        records = json.loads(raw)
        if isinstance(records, dict):
            records = records.get("reviews")
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValidationError("manifest must contain review records")
    ids = []
    for record in records:
        record = dict(record)
        for scheme in ("doi", "pmid", "pmcid"):
            value = record.get(scheme)
            if value:
                record[scheme] = str(value).strip().removeprefix("https://doi.org/")
        inclusion = record.get("inclusion_state", "included")
        if inclusion not in {"included", "excluded", "unresolved"}:
            raise ValidationError("unknown manifest inclusion state")
        ids.append(repo.review(record, inclusion=inclusion, reason="manifest:" + digest))
    return {"records": len(records), "distinctReviews": len(set(ids)), "manifestSha256": digest}


def bind_manuscript(repo: CorpusRepository, review_id: str, manuscript: dict,
                    *, source_sha: str | None = None, source_url: str = "", provider: str = "local") -> dict:
    """Store every printed reference and every exact occurrence, without judging."""
    graph = build_claim_inventory(manuscript)
    # Acquisition time belongs in retrieval receipts, not deterministic bindings.
    graph.pop("createdAt", None)
    manuscript_sha = repo.json_artifact(manuscript, "review-manuscript")
    graph_sha = repo.json_artifact(graph, "review-citation-graph")
    with repo.connect() as connection:
        review = connection.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    if review is None:
        raise ValidationError("unknown review")
    source_sha = source_sha or manuscript_sha
    repo.read_artifact(source_sha)
    references = {r["id"]: r for r in graph["references"]}
    entries = {}
    with repo.connect(write=True) as connection:
        for reference in manuscript.get("bibliographyEntries", []):
            # Internal parser IDs are stable even when printed labels are absent.
            label = reference["id"]
            entry_id = "entry-" + identity(review_id, label)
            seed = references[label]
            candidate = {"identifiers": seed.get("identifiers", {}), "identity": seed.get("identity", {}),
                         "printedLabel": reference.get("label"), "sourceIdentity": reference.get("sourceIdentity")}
            old = connection.execute("SELECT raw_reference,candidate_identifiers_json FROM bibliography_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if old and (old[0] != reference["raw"] or old[1] != canonical(candidate)):
                raise ValidationError("review bibliography changed; create a new corpus version")
            connection.execute("INSERT OR IGNORE INTO bibliography_entries VALUES (?,?,?,?,?,NULL,'unresolved',NULL)",
                               (entry_id, review_id, label, reference["raw"], canonical(candidate)))
            entries[label] = entry_id
        clusters = {c["id"]: c for c in graph["citationClusters"]}
        loci = 0
        unbound = set()
        for claim in graph["claims"]:
            for cluster_id in claim["citationClusterIds"]:
                cluster = clusters[cluster_id]
                if cluster["bindingStatus"] != "exact":
                    unbound.add(cluster_id)
                    continue
                for ref_id in cluster["referenceIds"]:
                    locator = canonical({"claim": claim["id"], "cluster": cluster_id, "locator": claim["locator"],
                                         "claimType": claim["claimType"], "bindingStatus": "exact"})
                    locus_id = "locus-" + identity(entries[ref_id], manuscript_sha, locator)
                    connection.execute("INSERT OR IGNORE INTO claim_loci VALUES (?,?,?,?,?,?,?)",
                                       (locus_id, entries[ref_id], manuscript_sha, locator, claim["text"],
                                        canonical([]), "structured-inventory-v2"))
                    loci += 1
    checkpoint = {"manuscriptSha256": manuscript_sha, "graphSha256": graph_sha, "rawSourceSha256": source_sha,
                  "sourceUrl": source_url, "provider": provider, "public": manuscript.get("confidential") is False,
                  "publication": manuscript.get("structuredSource", {}).get("publication", {}),
                  "bibliographyEntries": len(entries), "citationLoci": loci, "unboundClusters": sorted(unbound)}
    repo.review_state(review_id, "binding", "blocked" if unbound else "completed",
                      "unresolved citation bindings" if unbound else None, checkpoint, manuscript_sha)
    return {"reviewId": review_id, **checkpoint}


def ingest_file(repo: CorpusRepository, review_id: str, path: Path, *, public: bool = False,
                source_url: str = "", provider: str = "local") -> dict:
    raw = path.read_bytes()
    digest = repo.artifact(raw, "review-raw-source", "application/octet-stream")
    manuscript = ingest_manuscript(path, confidential=not public)
    if manuscript["source"]["sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValidationError("review file changed during ingestion")
    return bind_manuscript(repo, review_id, manuscript, source_sha=digest, source_url=source_url, provider=provider)
