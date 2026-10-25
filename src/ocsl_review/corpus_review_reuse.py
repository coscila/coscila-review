# SPDX-License-Identifier: Apache-2.0
"""Reuse retained structured reviews after hashing and checking article identity."""
from __future__ import annotations

import json

from .corpus_ingest import bind_manuscript
from .errors import ReviewError
from .ingest import ingest_manuscript
from .scholarly import normalize_doi


def retained_review_sources(repo, selected):
    wanted = {row["work_id"] for row in selected}
    sources = {}
    if not wanted:
        return sources
    # Scan the historical inventory once, never once per review in a large corpus.
    with repo.connect() as connection:
        for row in connection.execute("SELECT work_id,value_json FROM work_observations WHERE property='legacy.review_manifest'"):
            if row["work_id"] not in wanted:
                continue
            for source in json.loads(row["value_json"]).get("sources", []):
                if source.get("state") == "retained" and source.get("mediaType") in {"application/xml", "text/html"}:
                    sources.setdefault(row["work_id"], {})[source["sha256"]] = source
    # A label-free store keeps only a neutral retained-source index (store_client); study rows come first.
    from .store_client import retained_captures
    for row in selected:
        if row["work_id"] in sources:
            continue
        try:
            doi = normalize_doi(json.loads(row["metadata_json"]).get("doi") or "")
        except (ReviewError, ValueError):
            continue
        for capture in retained_captures(repo, doi):
            if capture["media_type"] in {"application/xml", "text/html"}:
                sources.setdefault(row["work_id"], {})[capture["capture_sha256"]] = {
                    "sha256": capture["capture_sha256"], "mediaType": capture["media_type"], "state": "retained",
                    "route": capture["route"], "capturedAt": capture["captured_at"]}
    return sources


def reuse_review(repo, row, candidates):
    metadata = json.loads(row["metadata_json"])
    if not metadata.get("doi"):
        return False
    expected = normalize_doi(metadata["doi"])
    for digest, source in sorted(candidates.items()):
        raw = repo.read_artifact(digest)
        suffix = ".html" if source["mediaType"] == "text/html" else ".xml"
        directory = repo.root / "ingest"
        directory.mkdir(exist_ok=True)
        path = directory / (digest + suffix)
        # Reconstruct from the checked immutable bytes, not a mutable old export.
        path.write_bytes(raw)
        try:
            manuscript = ingest_manuscript(path, confidential=False)
            publication = manuscript.get("structuredSource", {}).get("publication", {})
            observed = publication.get("doi") or publication.get("citation_doi")
            matches = bool(observed) and normalize_doi(observed) == expected
        except ReviewError as exc:
            repo.json_artifact({"sourceSha256": digest, "reviewId": row["review_id"],
                                "state": "unusable-retained-source", "reason": str(exc)}, "review-source-reuse")
            continue
        if not matches:
            repo.json_artifact({"sourceSha256": digest, "reviewId": row["review_id"],
                                "state": "identity-unconfirmed", "observedDoi": observed}, "review-source-reuse")
            continue
        bind_manuscript(repo, row["review_id"], manuscript, source_sha=digest,
                        source_url="https://doi.org/" + expected, provider="retained-structured-source")
        repo.json_artifact({"sourceSha256": digest, "reviewId": row["review_id"],
                            "state": "reused", "identityMethod": "structured-article-doi"}, "review-source-reuse")
        return True
    return False
