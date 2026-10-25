#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Opt-in synthetic benchmark for the corpus executor (execution acceptance).

Generates a synthetic citation corpus (default 200,000 loci) in a new temporary
workspace and measures, with the maintained code paths:

- task preparation (keyset-streamed planner, batched task storage);
- result import from an in-memory broker (one transaction per result page);
- status latency (progress counters);
- egress-ledger appends (chained JSONL segments).

No network, model endpoint, deployment path or existing library is touched; the
broker is an in-process stand-in and the workspace is created fresh and removed
afterwards unless --keep is given. Run from a source checkout:

    PYTHONPATH=src python3 tests/benchmark_corpus_executor.py --loci 200000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

from ocsl_review.confidentiality import _audit_event, append_egress_event, verify_egress_ledger
from ocsl_review.corpus_dispatch import CorpusExecutor
from ocsl_review.corpus_library import now
from ocsl_review.corpus_pipeline import prepare_tasks
from ocsl_review.corpus_store import CorpusRepository, canonical, identity, sync_batch

ACCEPTANCE = {"prepareTasksPerSecond": 2000, "importResultsPerSecond": 1000, "statusMilliseconds": 50,
              "egressSeconds": 10.0}
WORDS = ("cohort", "patients", "randomised", "treatment", "outcome", "survival", "biomarker", "expression",
         "tumour", "inhibitor", "response", "trial", "mortality", "signalling", "receptor", "therapy")


def _sentence(seed: int, length: int) -> str:
    words = [WORDS[(seed * 7 + i * 3) % len(WORDS)] for i in range(length)]
    return (" ".join(words)).capitalize() + "."


class SyntheticBroker:
    """In-process stand-in for the scoped broker: jobs, revisioned result pages, summaries."""

    def __init__(self, workspace: Path):
        self.worker = {"id": "w1", "assignment": "Judge supplied public evidence", "agent": "codex",
                       "effort": "none", "bulkAvailable": True, "bulkConcurrencyLimit": 60,
                       "evidencePolicy": "public-literature", "providerId": "synthetic-judge",
                       "model": "synthetic", "modelProvider": {"id": "synthetic-judge", "model": "synthetic"}}
        self.settings = {"revision": 1, "workers": [self.worker],
                         "authorization": {"attemptId": "benchmark-attempt", "workspace": str(workspace),
                                           "publicLiteratureAllowed": True}}
        self.jobs: dict[str, dict] = {}

    def catalog(self):
        return json.loads(json.dumps(self.settings))

    def request(self, method, route, payload=None):
        path, _, query = route.partition("?")
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if method == "POST" and path == "/bulk/submit":
            job_id = f"00000000-0000-0000-0000-{len(self.jobs) + 1:012d}"
            self.jobs[job_id] = {"jobId": job_id, "state": "running", "items": payload["items"], "records": []}
            return {"jobId": job_id, "configRevision": 1, "state": "running"}
        job = self.jobs[path.split("/")[2]]
        if path.endswith("/results"):
            after, limit = int(params.get("after", 0)), int(params.get("limit", 100))
            page = job["records"][after:after + limit]
            return {"results": page, "nextCursor": after + len(page), "hasMore": after + limit < len(job["records"])}
        return {"jobId": job["jobId"], "state": job["state"]}

    def complete(self, job_id):
        job = self.jobs[job_id]
        for revision, item in enumerate(job["items"], start=1):
            tasks = json.loads(item["input"]["messages"][1]["content"])["items"]
            output = {"items": [{"id": task["id"], "supportVerdict": "direct-support",
                                 "rationale": "The supplied abstract states the finding.",
                                 "evidence": [{"unitId": task["evidenceUnits"][0]["id"],
                                               "quote": task["evidenceUnits"][0]["text"][:40]}],
                                 "escalate": False, "escalationReason": ""} for task in tasks]}
            job["records"].append({"id": item["id"], "state": "completed", "providerId": "synthetic-judge",
                                   "model": "synthetic", "promptSha256": "0" * 64,
                                   "inputSha256": hashlib.sha256(canonical(item["input"]).encode()).hexdigest(),
                                   "output": output, "usage": {"inputTokens": 500, "outputTokens": 60},
                                   "revision": revision})
        job["state"], job["items"] = "completed", []  # the stand-in keeps only what it still serves


def generate(repo: CorpusRepository, loci: int, *, loci_per_review: int = 50, entries_per_work: int = 2) -> dict:
    """Write a synthetic corpus with bulk SQL: reviews, cited works with abstracts, entries and loci.

    Artifacts go where the library keeps them: into its packs (one batch), or loose
    files behind one barrier for a library without packs.
    """
    started = time.perf_counter()
    analysis = repo.analysis()
    sources, packs = repo.root / "sources", repo.library.packs
    created = now()
    artifacts, files, blobs = [], [], {}

    def artifact(value) -> str:
        body = canonical(value).encode()
        digest = hashlib.sha256(body).hexdigest()
        if packs is not None:
            blobs.setdefault(digest, body)
        else:
            path = sources / digest
            if not path.exists():
                path.write_bytes(body)
                path.chmod(0o444)
                files.append(path)
        artifacts.append((digest, "sources/" + digest, len(body), "application/json", "synthetic", created))
        return digest

    reviews = max(1, loci // loci_per_review)
    works = max(1, loci // entries_per_work)
    review_rows, review_works, corpus_reviews, manuscripts = [], [], [], []
    for index in range(reviews):
        doi = f"10.5555/synthetic-review-{index}"
        work_id = "work-" + identity(f"doi:{doi}")
        review_id = "review-" + identity(analysis["corpus_id"], work_id)
        manuscript = artifact({"confidential": False, "synthetic": True, "review": index})
        proof = {"manuscriptSha256": manuscript, "provider": "europe-pmc", "public": True,
                 "sourceUrl": f"https://europepmc.org/article/MED/{index}", "publication": {"doi": doi}}
        review_works.append((work_id, f"Synthetic review {index}", 2020 + index % 6, canonical({"doi": doi})))
        review_rows.append((review_id, analysis["corpus_id"], work_id, "included",
                            canonical({"journal": "Synthetic Journal", "stratum": index % 3}), "synthetic manifest"))
        corpus_reviews.append((review_id, "binding", "completed", None, None, canonical(proof), created))
        manuscripts.append((review_id, manuscript))
    cited, documents, sections = [], [], []
    for index in range(works):
        work_id = "work-" + identity(f"doi:10.5555/cited-{index}")
        text = " ".join(_sentence(index + k, 12 + (index + k) % 9) for k in range(8))
        document = artifact({"text": text, "complete": True, "synthetic": True, "work": index,
                             "blocks": [{"id": "abstract", "text": text, "kind": "abstract"}]})
        cited.append((work_id, f"Cited work {index}", 2000 + index % 25,
                      canonical({"doi": f"10.5555/cited-{index}", "title": f"Cited work {index}",
                                 "year": 2000 + index % 25, "journal": "Synthetic Reports"})))
        documents.append(("document-" + identity(work_id, document, "abstract"), work_id, document, "abstract",
                          f"https://europepmc.org/abstract/MED/{index}", "europe-pmc", 1,
                          canonical({"rawArtifactSha256": document}), created))
        sections.append(("section-" + identity(document, "abstract"), work_id, document, "abstract", "abstract", text,
                         hashlib.sha256(text.encode()).hexdigest(), "corpus-v2", 1))
    entries, claim_loci = [], []
    for index in range(loci):
        review_id, manuscript = manuscripts[index % reviews]
        work_id = cited[index % works][0]
        entry_id = "entry-" + identity(review_id, f"ref-{index}")
        entries.append((entry_id, review_id, f"ref-{index}", f"Printed reference {index}.",
                        canonical({"identifiers": {"doi": f"10.5555/cited-{index % works}"}}), work_id, "verified",
                        manuscript))
        locator = canonical({"claim": f"claim-{index}", "cluster": f"cluster-{index}", "bindingStatus": "exact",
                             "claimType": "numeric" if index % 3 == 0 else "background",
                             "locator": {"anchor": f"sentence-{index}:c1"}})
        claim_loci.append(("locus-" + identity(entry_id, manuscript, locator), entry_id, manuscript, locator,
                           _sentence(index, 14), canonical([_sentence(index + 1, 10), _sentence(index + 2, 12)]),
                           "synthetic-v1"))
    if packs is not None:
        with packs.batch():
            for digest, body in blobs.items():
                packs.put(body, digest)
    sync_batch(sources, files)
    with repo.connect(write=True) as connection:
        connection.executemany("INSERT OR IGNORE INTO source_artifacts VALUES (?,?,?,?,?,?)", artifacts)
        connection.executemany("INSERT INTO works VALUES (?,?,?,?)", review_works + cited)
        connection.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?)", review_rows)
        connection.executemany("INSERT INTO corpus_reviews VALUES (?,?,?,?,?,?,?)", corpus_reviews)
        connection.executemany("INSERT INTO corpus_documents VALUES (?,?,?,?,?,?,?,?,?)", documents)
        connection.executemany("INSERT INTO source_sections VALUES (?,?,?,?,?,?,?,?,?)", sections)
        connection.executemany("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", entries)
        connection.executemany("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", claim_loci)
    return {"reviews": reviews, "citedWorks": works, "loci": loci, "artifacts": len(files) + len(blobs),
            "storage": "packs" if packs is not None else "loose", "seconds": round(time.perf_counter() - started, 3)}


def measure_import(repo: CorpusRepository, broker: SyntheticBroker, *, limit: int | None, job_items: int) -> dict:
    executor = CorpusExecutor(repo, broker=broker, authorization=lambda *args: None)
    submit_seconds = import_seconds = 0.0
    imported = 0
    while limit is None or imported < limit:
        started = time.perf_counter()
        submitted = executor.submit("w1", job_items=job_items)["submitted"]
        submit_seconds += time.perf_counter() - started
        if not submitted:
            break
        for job in submitted:
            broker.complete(job["jobId"])
        before = repo.task_counts(executor.analysis).get("completed", 0)
        started = time.perf_counter()
        executor.resume()
        import_seconds += time.perf_counter() - started
        imported += repo.task_counts(executor.analysis).get("completed", 0) - before
    return {"results": imported, "seconds": round(import_seconds, 3),
            "resultsPerSecond": round(imported / import_seconds, 1) if import_seconds else None,
            "submitSeconds": round(submit_seconds, 3),
            "submittedTasksPerSecond": round(imported / submit_seconds, 1) if submit_seconds else None}


def measure_status(repo: CorpusRepository, calls: int = 200) -> dict:
    samples = []
    for _ in range(calls):
        started = time.perf_counter()
        repo.status()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {"calls": calls, "medianMilliseconds": round(statistics.median(samples), 3),
            "p95Milliseconds": round(samples[int(len(samples) * 0.95) - 1], 3),
            "maxMilliseconds": round(samples[-1], 3)}


def measure_egress(workspace: Path, count: int) -> dict:
    events = [_audit_event("platform-worker:w1", "citation_census", {"doi": f"10.5555/cited-{i}", "rows": 1},
                           decision="allowed-minimized") for i in range(count)]
    started = time.perf_counter()
    for event in events:
        append_egress_event(workspace, event)
    seconds = time.perf_counter() - started
    started = time.perf_counter()
    verified = verify_egress_ledger(workspace)
    return {"events": count, "seconds": round(seconds, 3), "eventsPerSecond": round(count / seconds, 1),
            "verifiedEvents": verified["chainedEvents"], "segments": verified["segments"],
            "verifySeconds": round(time.perf_counter() - started, 3)}


def run_benchmark(*, loci: int = 200_000, results: int | None = None, egress_events: int = 100_000,
                  job_items: int = 1000, parent: Path | None = None, keep: bool = False) -> dict:
    if os.environ.get("LAB_WORKSPACE_ROOT") and parent is not None:
        root = Path(os.environ["LAB_WORKSPACE_ROOT"]).resolve()
        if parent.resolve() == root or root in parent.resolve().parents:
            raise SystemExit("refusing to benchmark inside a deployment workspace root")
    workspace = Path(tempfile.mkdtemp(prefix="ocsl-corpus-benchmark-", dir=parent))
    try:
        repo = CorpusRepository.create(workspace, "work/literature", "Synthetic benchmark question", "benchmark",
                                       {"version": 2, "kind": "synthetic-benchmark"})
        corpus = generate(repo, loci)
        started = time.perf_counter()
        planned = prepare_tasks(repo, tier="abstract")
        prepare_seconds = time.perf_counter() - started
        prepare = {"tasks": planned["tasks"], "blocked": planned["blocked"], "seconds": round(prepare_seconds, 3),
                   "tasksPerSecond": round(planned["tasks"] / prepare_seconds, 1)}
        status_before = measure_status(repo)
        imported = measure_import(repo, SyntheticBroker(workspace), limit=results, job_items=job_items)
        status_after = measure_status(repo)
        egress = measure_egress(workspace, egress_events)
        status = status_after if status_after["p95Milliseconds"] >= status_before["p95Milliseconds"] else status_before
        report = {"corpus": corpus, "prepare": prepare, "import": imported,
                  "status": {"beforeImport": status_before, "afterImport": status_after}, "egress": egress,
                  "counts": repo.status()["counts"], "python": sys.version.split()[0],
                  "workspace": str(workspace) if keep else None}
        report["acceptance"] = {
            "prepare": prepare["tasksPerSecond"] >= ACCEPTANCE["prepareTasksPerSecond"],
            "import": (imported["resultsPerSecond"] or 0) >= ACCEPTANCE["importResultsPerSecond"],
            "status": status["p95Milliseconds"] <= ACCEPTANCE["statusMilliseconds"],
            "egress": egress["seconds"] < ACCEPTANCE["egressSeconds"],
            "thresholds": ACCEPTANCE,
        }
        return report
    finally:
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--loci", type=int, default=200_000)
    parser.add_argument("--results", type=int, default=None, help="stop importing after this many results")
    parser.add_argument("--egress-events", type=int, default=100_000)
    parser.add_argument("--job-items", type=int, default=1000)
    parser.add_argument("--parent", type=Path, default=None, help="directory for the temporary workspace")
    parser.add_argument("--keep", action="store_true", help="keep the generated workspace")
    parser.add_argument("--check", action="store_true", help="exit 1 when an acceptance threshold is missed")
    args = parser.parse_args(argv)
    report = run_benchmark(loci=args.loci, results=args.results, egress_events=args.egress_events,
                           job_items=args.job_items, parent=args.parent, keep=args.keep)
    print(json.dumps(report, indent=2, sort_keys=True))
    passed = all(value for key, value in report["acceptance"].items() if key != "thresholds")
    return 0 if passed or not args.check else 1


if __name__ == "__main__":
    sys.exit(main())
