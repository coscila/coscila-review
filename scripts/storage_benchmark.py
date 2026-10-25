#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Synthetic literature store and the storage acceptance measurement (opt-in, local only).

Builds a library shaped like a large review census: reviews, printed bibliography
entries and citation loci, cited works with abstracts, provider records and
identity proofs, archived legacy observations of every kind, and withheld legacy
artifacts. It then measures, on that store:

  1. snapshot: wall time, manifest size and file count;
  2. branch: wall time, and that the parent digest (manifest SHA-256, Merkle root,
     database SHA-256) is unchanged after writes in the branch;
  3. the label-free projection literature-evidence@2: the removed-row gate and the
     vocabulary scan, and that the source snapshot is unchanged.

Every number it prints is measured on the synthetic store it just built. It only
writes below --out and never reads anything else.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import resource
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocsl_review import corpus_library as library  # noqa: E402
from ocsl_review.corpus_packs import PackStore  # noqa: E402
from ocsl_review.corpus_store import CorpusRepository, canonical, identity  # noqa: E402

# Full-scale sizing (scale 1.0), roughly a large two-journal review census.
SIZES = {"reviews": 5600, "entries": 670000, "cited_works": 400000, "legacy_abstracts": 390000,
         "census_attempts": 100000, "full_texts": 20000, "audit_excerpts": 1000}
WORDS = ("tumour cell cohort patients survival expression receptor pathway inhibitor therapy response trial "
         "median increased decreased associated mutation signalling immune microenvironment metastasis ratio "
         "hazard dose model analysis tissue biomarker clinical outcome resistance proliferation apoptosis "
         "mechanism randomized phase prognostic sequencing genome protein regulation progression treatment").split()
CREATED = "2026-09-30T00:00:00+00:00"


def scaled(scale: float) -> dict:
    return {key: max(2, round(value * scale)) for key, value in SIZES.items()}


class Builder:
    def __init__(self, repo: CorpusRepository, *, seed: int, pack_bytes: int, level: int):
        self.repo = repo
        self.rng = random.Random(seed)
        self.packs = PackStore(repo.library.packs.root, target_bytes=pack_bytes, level=level)
        self.connection = sqlite3.connect(repo.db)
        for pragma in ("synchronous=OFF", "foreign_keys=OFF", "cache_size=-262144"):
            self.connection.execute(f"PRAGMA {pragma}")
        self.artifacts: list[tuple] = []
        self.pool = [" ".join(self.rng.choice(WORDS) for _ in range(170)) for _ in range(997)]
        self.counts: dict[str, int] = {}

    def text(self, number: int) -> str:
        return f"{self.pool[number % len(self.pool)]} (record {number})."

    def blob(self, body: bytes, kind: str, media: str = "application/json") -> str:
        digest = self.packs.put(body)
        self.artifacts.append((digest, "sources/" + digest, len(body), media, kind, CREATED))
        if len(self.artifacts) >= 50000:
            self.flush()
        return digest

    def json_blob(self, value, kind: str) -> str:
        return self.blob(canonical(value).encode(), kind)

    def flush(self):
        self.connection.executemany("INSERT OR IGNORE INTO source_artifacts VALUES (?,?,?,?,?,?)", self.artifacts)
        self.connection.commit()
        self.artifacts.clear()

    def rows(self, table: str, rows: list[tuple]):
        if rows:
            marks = ",".join("?" for _ in rows[0])
            self.connection.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({marks})", rows)
            self.counts[table] = self.counts.get(table, 0) + len(rows)


def generate(workspace: Path, relative: str, *, scale: float = 1.0, seed: int = 7, pack_bytes: int = 4 << 20,
             level: int = 1, progress=print) -> dict:
    """Build the synthetic library at workspace/relative; returns generation statistics."""
    started = time.monotonic()
    sizes = scaled(scale)
    repo = CorpusRepository.create(workspace, relative, "Synthetic storage benchmark", "synthetic-study", {"v": 1})
    corpus = repo.analysis()["corpus_id"]
    b = Builder(repo, seed=seed, pack_bytes=pack_bytes, level=level)
    db = b.connection
    method = b.json_blob({"importer": "synthetic-legacy-import"}, "legacy-import-manifest")
    protocol = canonical({"legacyOnly": True, "sourceKind": "synthetic"})
    legacy_run = "run-legacy"
    db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
               (legacy_run, corpus, protocol, hashlib.sha256(protocol.encode()).hexdigest(), None, CREATED))
    db.execute("INSERT INTO analyses VALUES (?,?,?,?,?)", ("legacy-synthetic", legacy_run, "Preserved legacy observations", None, CREATED))
    withheld = {
        "database": b.blob(b"SQLite format 3\x00 citations abs_category abs_support_verdict abs_class sure_not_supported " * 2000,
                           "legacy-original-database", "application/vnd.sqlite3"),
        "manifest": b.blob(b"".join(canonical({"item_id": str(i), "orig": {"category": "false", "support_verdict": "contradicted"},
                                               "cited_work": {"doi": f"10.9/cited{i}"}}).encode() + b"\n" for i in range(50)),
                           "legacy-fulltext-selection-manifest", "application/x-ndjson"),
    }
    with b.packs.batch():
        # Reviews: works, retained captures and their legacy manifests.
        reviews = []
        for r in range(sizes["reviews"]):
            doi = f"10.5555/review.{r}"
            work = "work-" + identity(f"doi:{doi}")
            capture = b.blob((f"<article><front><article-id pub-id-type='doi'>{doi}</article-id></front><body><p>"
                              + b.text(r) * 12 + "</p></body></article>").encode(), "legacy-original-source", "application/xml")
            reviews.append((r, doi, work, capture))
            b.rows("works", [(work, f"Synthetic review {r}", 2020 + r % 6,
                              canonical({"doi": doi, "title": f"Synthetic review {r}", "journal": f"Journal {r % 2}",
                                         "stratum": "Review", "label": "Review", "year": 2020 + r % 6}))])
            review_id = "review-" + identity(corpus, work)
            b.rows("reviews", [(review_id, corpus, work, "included", canonical({"stratum": "Review"}), "synthetic manifest")])
            b.rows("corpus_reviews", [(review_id, "binding", "completed", None, capture, "{}", CREATED)])
            b.rows("work_observations", [
                ("legacy-observation-" + identity("manifest", r), "legacy-synthetic", work, "legacy.review_manifest",
                 canonical({"row": {"doi": doi, "route": "jats", "n_never_cited": r % 7},
                            "sources": [{"originalPath": f"work/raw/{r}.xml", "sha256": capture, "mediaType": "application/xml",
                                         "state": "retained"}], "activeStudyInclusion": "not-inferred"}),
                 "legacy-import-v1", method, None, "unresolved", CREATED),
                ("legacy-observation-" + identity("summary", r), "legacy-synthetic", work, "legacy.review_summary",
                 canonical({"row": {"doi": doi, "abs_sure_supported": 30 + r % 9, "abs_unsure": 5, "abs_sure_not_supported": r % 11},
                            "sourceTable": "reviews"}), "legacy-import-v1", method, None, "unresolved", CREATED)])
        db.commit()
        progress({"phase": "reviews", "count": len(reviews)})
        # Cited works with provider records, identity proofs and abstracts.
        cited = []
        for w in range(sizes["cited_works"]):
            doi = f"10.7777/cited.{w}"
            work = "work-" + identity(f"doi:{doi}")
            abstract = b.text(w)
            raw = b.json_blob({"request": {"resultType": "core", "queryString": f"DOI:{doi}"},
                               "resultList": {"result": [{"doi": doi, "title": f"Cited work {w}", "abstractText": abstract,
                                                          "pubYear": str(2000 + w % 25)}]}}, "raw-provider-response")
            receipt = b.json_blob({"outcome": "acquired", "abstract": abstract, "cacheProvenance": [{"sha256": raw}],
                                   "retrievedAt": CREATED}, "acquisition-receipt")
            proof = b.json_blob({"acquisitionSha256": receipt, "identityMethod": "exact-identifier-and-printed-title-v2",
                                 "identityState": "verified", "printedReference": {"doi": doi}}, "identity-reconciliation")
            blocks = [{"id": "abstract", "text": abstract, "kind": "abstract"}]
            document = b.json_blob({"text": abstract, "complete": True, "blocks": blocks, "acquisitionReceiptSha256": receipt,
                                    "rawArtifactSha256": raw}, "source-document")
            if w % 2 == 0:
                b.blob((f"<article><body><p>{abstract}</p>" * 4 + "</body></article>").encode(), "raw-publisher-source",
                       "application/xml")
            b.rows("works", [(work, f"Cited work {w}", 2000 + w % 25,
                              canonical({"doi": doi, "title": f"Cited work {w}", "year": 2000 + w % 25,
                                         "provider": "europe-pmc", "container": f"Journal of Synthetic Results {w % 50}"}))])
            b.rows("work_identifiers", [(work, "doi", doi, proof)])
            b.rows("retrievals", [("retrieval-" + identity(work, receipt), work, receipt, "corpus-acquisition", "", CREATED,
                                   None, "acquired", None, canonical({"outcome": "acquired", "retryable": False}))])
            b.rows("corpus_documents", [("document-" + identity(work, document, "abstract"), work, document, "abstract",
                                         f"https://www.ebi.ac.uk/europepmc/{w}", "europe-pmc", 1,
                                         canonical({"rawArtifactSha256": raw}), CREATED)])
            b.rows("source_sections", [("section-" + identity(document, "abstract"), work, document, "abstract", "abstract",
                                        abstract, hashlib.sha256(abstract.encode()).hexdigest(), "corpus-v2", 1)])
            cited.append((work, doi))
            if w % 50000 == 49999:
                db.commit()
                progress({"phase": "cited-works", "count": w + 1})
        db.commit()
        # Legacy abstracts backed by shards (byte cache), with a pointer to a withheld database.
        shard_rows = []
        for n in range(sizes["legacy_abstracts"]):
            work, doi = cited[n % len(cited)]
            shard_rows.append({"work_key": n, "doi": doi, "abstract": b.text(n + 7), "abstract_source": "epmc"})
            if len(shard_rows) == 1000 or n == sizes["legacy_abstracts"] - 1:
                shard = b.json_blob({"sourceDatabaseSha256": withheld["database"], "table": "works", "records": shard_rows},
                                    "legacy-evidence-shard")
                docs, secs = [], []
                for row in shard_rows:
                    target = "work-" + identity(f"doi:{row['doi']}")
                    docs.append(("document-" + identity(target, shard, "abstract"), target, shard, "abstract", "",
                                 "legacy:epmc", 0, canonical({"sourceDatabaseSha256": withheld["database"],
                                                              "legacyProviderClaim": "epmc",
                                                              "completenessBasis": "legacy-field-only-unverified"}), CREATED))
                    locator = canonical({"table": "works", "rowid": row["work_key"], "column": "abstract"})
                    secs.append(("section-" + identity(target, shard, locator), target, shard, locator, "abstract",
                                 row["abstract"], hashlib.sha256(row["abstract"].encode()).hexdigest(), "legacy-evidence-v1", 0))
                b.rows("corpus_documents", docs)
                b.rows("source_sections", secs)
                shard_rows = []
        db.commit()
        progress({"phase": "legacy-abstracts", "count": sizes["legacy_abstracts"]})
        # Retained full texts, and judged-sample excerpts whose provenance must be withheld.
        for n in range(sizes["full_texts"]):
            work, doi = cited[(n * 7) % len(cited)]
            text = (b.text(n) + " ") * 6
            original = b.blob(f"<article><body><p>{text}</p></body></article>".encode(), "legacy-original-source", "application/xml")
            b.rows("corpus_documents", [("document-" + identity(work, original, "full-text"), work, original, "full-text", "",
                                         "legacy-original-source", 1, canonical({"identityMatched": True}), CREATED)])
            b.rows("source_sections", [("section-" + identity(work, original, "full"), work, original,
                                        "independent full document extraction", "full-text", text,
                                        hashlib.sha256(text.encode()).hexdigest(), "legacy-evidence-v1", 1)])
        for n in range(sizes["audit_excerpts"]):
            work, doi = cited[(n * 13) % len(cited)]
            text = b.text(n + 11)
            cache = b.blob(text.encode(), "legacy-original-source", "text/plain")
            b.rows("corpus_documents", [("document-" + identity(work, cache, "full-text"), work, cache, "full-text", "",
                                         "legacy-audit-excerpt", 0, canonical({"selectionManifestSha256": withheld["manifest"],
                                                                                "selectionLine": n + 1}), CREATED)])
            b.rows("source_sections", [("section-" + identity(work, cache, "excerpt"), work, cache, "legacy audited text export",
                                        "full-text", text, hashlib.sha256(text.encode()).hexdigest(), "legacy-evidence-v1", 0)])
        db.commit()
        # Printed entries, loci, legacy scores/loci/bibliography, and v2-style judgments (all dropped later).
        receipts, entry_number = {}, 0
        per_review = max(1, sizes["entries"] // len(reviews))
        for r, doi, work, capture in reviews:
            review_id = "review-" + identity(corpus, work)
            request = b.json_blob({"review": doi, "items": per_review}, "worker-request")
            response = b.json_blob({"review": doi, "verdicts": [{"c": "T", "v": "direct"}] * 3}, "worker-response")
            receipt = "receipt-" + identity(review_id)
            b.rows("model_receipts", [(receipt, "synthetic-study", review_id, request, response, "synthetic", "synthetic-model",
                                       "{}", "{}", "completed", CREATED, CREATED)])
            entries, loci, assessments, observations, tasks = [], [], [], [], []
            for label in range(per_review):
                cited_work, cited_doi = cited[entry_number % len(cited)]
                entry = "entry-" + identity(review_id, label)
                proof = b.json_blob({"printedReference": {"doi": cited_doi, "label": label}, "identityState": "verified"},
                                    "title-resolution")
                entries.append((entry, review_id, str(label), f"[{label}] Printed reference to {cited_doi}",
                                canonical({"identifiers": {"doi": cited_doi}}), cited_work, "verified", proof))
                locus = "locus-" + identity(entry, "c1")
                loci.append((locus, entry, capture, canonical({"claim": label}), b.text(entry_number)[:240],
                             "[]", "structured-inventory-v2"))
                spec = b.json_blob({"locus": locus, "tier": "abstract"}, "task-input")
                tasks.append(("task-" + identity(locus), "synthetic-study", locus, "abstract", spec, "completed", None, 1, CREATED))
                assessments.append(("assessment-" + identity(locus), "synthetic-study", locus, receipt, "citation-support-v2",
                                    "abstract", canonical({"supportVerdict": "supported", "evidenceCoverage": "complete"}),
                                    "accepted", CREATED))
                row = {"journal": f"J{r % 2}", "doi": doi, "label": str(label), "abs_category": "false" if label % 9 == 0 else "true",
                       "abs_support_verdict": "contradicted" if label % 9 == 0 else "direct-support",
                       "abs_class": "sure_not_supported" if label % 9 == 0 else "sure_supported"}
                for prop, extra in (("legacy.citation_score", {"row": row, "sourceTable": "citations"}),
                                    ("legacy.bibliography", {"row": {"doi": doi, "label": label, "printed": cited_doi},
                                                             "sourceTable": "bib_refs"}),
                                    ("legacy.corrected_bibliography", {"row": {"doi": doi, "label": label, "printed": cited_doi},
                                                                       "sourceTable": "refs_corrected"}),
                                    ("legacy.citation_locus", {"row": {"doi": doi, "label": label, "sentence": b.text(label)[:200]},
                                                               "sourceTable": "citations"}),
                                    ("legacy.corrected_locus", {"row": {"doi": doi, "label": label, "sentence_s0": b.text(label)[:200]},
                                                                "sourceTable": "loci_corrected"})):
                    observations.append(("legacy-observation-" + identity(prop, entry_number), "legacy-synthetic", work, prop,
                                         canonical(extra), "legacy-import-v1", method, None, "unresolved", CREATED))
                entry_number += 1
            b.rows("bibliography_entries", entries)
            b.rows("claim_loci", loci)
            b.rows("corpus_tasks", tasks)
            b.rows("assessments", assessments)
            b.rows("work_observations", observations)
            if r % 500 == 499:
                db.commit()
                progress({"phase": "entries", "count": entry_number})
        db.commit()
        for n in range(sizes["census_attempts"]):
            work = reviews[n % len(reviews)][2]
            payload = b.json_blob({"assessment": {"supportVerdict": "supported"}, "attempt": n}, "legacy-assessment-record")
            b.json_blob({"task": n}, "legacy-task-record")
            b.rows("work_observations", [("legacy-observation-" + identity("attempt", n), "legacy-synthetic", work,
                                          "legacy.census_attempt", canonical({"legacyAccepted": True, "rawPayloadSha256": payload,
                                                                              "assessment": {"supportVerdict": "supported"}}),
                                          "legacy-import-v1", method, None, "unresolved", CREATED)])
        b.flush()
        db.commit()
    db.close()
    stats = repo.library.packs.stats()
    return {"sizes": sizes, "rows": b.counts, "packs": stats, "database_bytes": repo.db.stat().st_size,
            "seconds": round(time.monotonic() - started, 1)}


def digest_state(snapshot: library.Library) -> dict:
    manifest = json.loads(snapshot.manifest_path.read_text())
    return {"manifest_sha256": library.digest_file(snapshot.manifest_path), "merkle_root": manifest["merkle"]["root"],
            "database_sha256": library.digest_file(snapshot.db)}


def measure(out: Path, *, scale: float, pack_bytes: int, workers: int, projection: bool = True, progress=print) -> dict:
    from ocsl_review.store_projection import build_projection, verify_projection
    out.mkdir(parents=True, exist_ok=True)
    workspace = library.Workspace(out)
    report = {"scale": scale, "pack_bytes": pack_bytes,
              "generation": generate(out, "store", scale=scale, pack_bytes=pack_bytes, progress=progress)}
    progress({"phase": "generated", **report["generation"]["packs"]})
    store = CorpusRepository(out, "store")
    started = time.monotonic()
    snap = store.library.snapshot("snapshot")
    report["snapshot"] = {"seconds": round(time.monotonic() - started, 3), "phases": snap["phases"],
                          "manifest_bytes": snap["manifest_bytes"],
                          "files": sum(1 for p in (out / "snapshot").rglob("*") if p.is_file()), "packs": snap["packs"],
                          "linked_packs": snap["linked_packs"], "database_bytes": (out / "snapshot/corpus.sqlite3").stat().st_size}
    parent = library.Library(workspace, "snapshot")
    before = digest_state(parent)
    started = time.monotonic()
    created = parent.branch("branch", "A different question on the same evidence", "followup", {"v": 2})
    report["branch"] = {"seconds": round(time.monotonic() - started, 3), "phases": created["phases"],
                        "delta_bytes": (out / "branch/corpus.sqlite3").stat().st_size}
    started = time.monotonic()
    branch = CorpusRepository(out, "branch")
    work = branch.work({"doi": "10.7777/cited.1", "title": "Corrected in the branch"})
    branch.document(work, {"text": "Branch-only evidence.", "complete": True}, "abstract", "fixture")
    evidence = branch.evidence("work-" + identity("doi:10.7777/cited.2"), "abstract")
    report["branch"]["write_and_read_seconds"] = round(time.monotonic() - started, 3)
    report["branch"]["parent_evidence_visible"] = bool(evidence and evidence["evidenceUnits"])
    report["branch"]["union_works"] = branch.status()["counts"]["works"]
    after = digest_state(parent)
    report["branch"]["parent_digest_unchanged"] = before == after
    report["branch"]["parent_digest"] = after
    started = time.monotonic()
    parent.verify(level="standard")
    report["snapshot"]["standard_verify_seconds"] = round(time.monotonic() - started, 3)
    if projection:
        started = time.monotonic()
        receipt = build_projection(out / "snapshot", out / "projection", workers=workers)
        report["projection"] = {"seconds": round(time.monotonic() - started, 3), "gate": receipt["gate"],
                                "kept": receipt["kept"], "dropped": receipt["dropped"],
                                "withheldArtifactKinds": receipt["withheldArtifactKinds"],
                                "retainedIndex": receipt["retainedIndex"], "files": receipt["files"]}
        report["projection"]["verify"] = verify_projection(out / "projection", level="standard")["gate"]
        report["projection"]["source_unchanged"] = digest_state(parent) == before
    report["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="new or empty directory for the synthetic store")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--pack-bytes", type=int, default=4 << 20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-projection", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out).absolute()
    if out.exists() and any(out.iterdir()):
        parser.error("--out must be new or empty")
    report = measure(out, scale=args.scale, pack_bytes=args.pack_bytes, workers=args.workers,
                     projection=not args.no_projection,
                     progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
