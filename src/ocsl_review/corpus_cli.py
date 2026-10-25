# SPDX-License-Identifier: Apache-2.0
"""Maintained corpus controller; controllers use these commands instead of scripts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import threading

from .corpus_store import CorpusRepository
from .errors import ValidationError


def add_commands(families, register):
    family = families.add_parser("corpus", help="build, assess, resume and fork a complete literature corpus")
    commands = family.add_subparsers(dest="corpus_command", required=True)
    for name in ("init", "import-manifest", "import-legacy", "import-snapshot", "enumerate", "acquire", "prepare", "run", "resume", "execute", "status", "export", "snapshot", "branch", "verify", "pack", "calibrate", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", default=".")
        command.add_argument("--library", default="work/literature")
        if name in {"init", "branch"}:
            command.add_argument("--question", required=True)
            command.add_argument("--analysis", required=True)
            command.add_argument("--protocol", required=True)
        if name in {"import-manifest", "import-legacy", "import-snapshot"}:
            command.add_argument("input")
        if name == "import-legacy":
            command.add_argument("--kind", choices=("historical", "census", "evidence"), required=True)
            command.add_argument("--source-root")
        if name == "enumerate":
            command.add_argument("--query", required=True)
            command.add_argument("--label", required=True)
        if name in {"acquire", "execute"}:
            command.add_argument("--acquisition-workers", type=int, default=12)
            command.add_argument("--retry-acquisition", action="store_true")
        if name in {"prepare", "run", "acquire"}:
            command.add_argument("--tier", choices=("abstract", "full-text"), default="abstract")
        if name in {"run", "execute", "calibrate", "validate"}:
            command.add_argument("--worker-config", required=True)
            command.add_argument("--concurrency", type=int, default=60)
        if name in {"run", "execute"}:
            command.add_argument("--batch-size", type=int, default=1)
            command.add_argument("--max-jobs", type=int, default=4)
        if name in {"execute", "calibrate"}:
            command.add_argument("--low-worker-config", required=True)
        if name in {"execute", "calibrate", "validate"}:
            command.add_argument("--poll-seconds", type=float, default=5)
        if name == "execute":
            command.add_argument("--skip-acquisition", action="store_true")
        if name == "calibrate":
            command.add_argument("--sample-size", type=int, default=300)
            command.add_argument("--validation-cap", type=int, default=1000)
            command.add_argument("--execute", action="store_true")
            command.add_argument("--until-complete", action="store_true")
        if name == "validate":
            command.add_argument("--sample-size", type=int, default=1000)
            command.add_argument("--execute", action="store_true")
            command.add_argument("--until-complete", action="store_true")
        if name in {"export", "snapshot", "branch"}:
            command.add_argument("--output", required=True)
        if name == "verify":
            command.add_argument("--level", choices=("light", "standard", "deep"), default="standard",
                                 help="light: manifest, database and Merkle root; deep: every blob and row hash")
        register(command, "corpus_cli", "command", "read-only" if name in {"status", "verify"} else "workspace", state_required=False)


def repository(args):
    return CorpusRepository(Path(args.workspace).absolute(), args.library)


def acquirer(args, repo):
    from .confidentiality import load_scholarly_context
    from .scholarly import ScholarlyClient
    from .corpus_acquisition import CorpusAcquirer
    plan, selected = load_scholarly_context(args.paths, os.environ, cwd=repo.workspace.root)
    if selected != repo.workspace.root:
        raise ValidationError("corpus acquisition requires an exact scholarly workspace context")
    client = ScholarlyClient(plan=plan, workspace=selected)
    return CorpusAcquirer(repo.root / "cache/provider.sqlite3", client)


class Progress:
    def __init__(self, repo):
        self.repo, self.last = repo, 0.0
        self.phase, self.summary = "corpus", "Corpus acquisition and assessment are active"
        self.state = "running"
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def __enter__(self):
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stop.wait(30):
            self(force=True)

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        self.thread.join(timeout=35)
        if kind:
            self(force=True, state="failed", summary="Corpus controller stopped; durable results are saved. Inspect the command error and resume.")

    def __call__(self, *, force=False, phase=None, state="running", summary=None):
        from .util import atomic_write
        from .corpus_library import now
        with self.lock:
            # A queued heartbeat must never replace the terminal checkpoint.
            if self.state != "running":
                return
            if state == "running" and not force and time.monotonic() - self.last < 15:
                return
            self.last = time.monotonic()
            status = self.repo.status()
            tasks = status["taskStates"]
            total = sum(t["count"] for t in tasks)
            completed = sum(t["count"] for t in tasks if t["state"] == "completed")
            self.phase = phase if phase is not None else self.phase
            self.summary = summary if summary is not None else self.summary
            value = {"version": 1, "phase": self.phase, "state": state, "summary": self.summary,
                     "unit": "citation assessments",
                     "warnings": [], "intermediateRelativePath": self.repo.relative + "/corpus.sqlite3",
                     "updatedAt": now(), "expectedUpdateIntervalSeconds": 60}
            if total:
                value.update(numerator=completed, denominator=total)
            directory = self.repo.workspace.path("results/.ocsl")
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write(directory / "progress.json", json.dumps(value) + "\n")
            atomic_write(directory / "corpus-status.json", json.dumps(status) + "\n")
            self.state = state
            if state != "running":
                self.stop.set()


def execute(args, repo):
    with Progress(repo) as progress:
        return _execute(args, repo, progress)


def _execute(args, repo, progress):
    from .corpus_pipeline import acquire_corpus, acquire_escalations, prepare_tasks
    from .corpus_dispatch import CorpusExecutor
    from .corpus_export import export_corpus
    if not 0.1 <= args.poll_seconds <= 60:
        raise ValidationError("poll interval must be from 0.1 to 60 seconds")
    progress(force=True)
    acquisition = acquirer(args, repo)
    if not args.skip_acquisition:
        acquire_corpus(repo, acquisition, workers=args.acquisition_workers, retry=args.retry_acquisition, progress=progress)
    executor = CorpusExecutor(repo, args.paths)
    for tier, worker, size in (("abstract", args.worker_config, args.batch_size), ("full-text", args.low_worker_config, 1)):
        if tier == "full-text":
            acquire_escalations(repo, acquisition, progress=progress)
        prepare_tasks(repo, tier=tier)
        while True:
            result = executor.resume()
            if any(job["state"] == "paused" for job in result["jobs"]):
                progress(force=True, phase=tier, state="waiting_input", summary="Worker provider paused; accepted results are saved")
                return {"state": "paused", **result}
            executor.submit(worker, tier=tier, batch_size=size, concurrency=args.concurrency, max_jobs=args.max_jobs)
            progress(phase=tier)
            with repo.connect() as connection:
                remaining = connection.execute("SELECT count(*) FROM corpus_tasks WHERE analysis_id=? AND tier=? AND state IN ('pending','running')", (executor.analysis, tier)).fetchone()[0]
            if not remaining:
                break
            time.sleep(args.poll_seconds)
    result = export_corpus(repo)
    progress(force=True, state="complete", summary="Corpus processing finished; evidence limits and blocked items are recorded")
    return result


def command(args):
    name = args.corpus_command
    if name == "import-snapshot":
        from .corpus_export import import_snapshot
        return import_snapshot(Path(args.workspace).absolute(), args.library, args.input)
    if name == "init":
        from .corpus_export import implementation_manifest, record_implementation
        protocol = json.loads(Path(args.protocol).read_text())
        manifest = implementation_manifest()
        repo = CorpusRepository.create(Path(args.workspace).absolute(), args.library, args.question, args.analysis, protocol,
                                       "source-tree-sha256:" + manifest["sha256"])
        record_implementation(repo, manifest)
        return repo.status()
    repo = repository(args)
    if name == "status":
        return repo.status()
    if name == "verify":
        return repo.library.verify(level=args.level)
    if name == "pack":
        return repo.library.pack_loose()
    if name == "snapshot":
        return repo.library.snapshot(args.output)
    if name == "branch":
        from .corpus_export import implementation_manifest, record_implementation
        manifest = implementation_manifest()
        result = repo.library.branch(args.output, args.question, args.analysis, json.loads(Path(args.protocol).read_text()),
                                     "source-tree-sha256:" + manifest["sha256"])
        record_implementation(CorpusRepository(repo.workspace.root, args.output), manifest)
        return result
    if name == "import-manifest":
        from .corpus_ingest import import_manifest
        return import_manifest(repo, Path(args.input))
    if name == "import-legacy":
        if args.kind == "evidence":
            from .corpus_seed import import_legacy_evidence
            return import_legacy_evidence(repo, Path(args.input), source_root=Path(args.source_root) if args.source_root else None)
        from .corpus_import import import_legacy_scores, import_legacy_census
        return (import_legacy_scores if args.kind == "historical" else import_legacy_census)(repo, Path(args.input))
    if name == "enumerate":
        from .corpus_enumeration import enumerate_reviews
        return enumerate_reviews(repo, acquirer(args, repo), query=args.query, label=args.label)
    if name == "acquire":
        from .corpus_pipeline import acquire_corpus, acquire_escalations
        acquisition = acquirer(args, repo)
        if args.tier == "full-text":
            return acquire_escalations(repo, acquisition, progress=Progress(repo))
        return acquire_corpus(repo, acquisition, workers=args.acquisition_workers, retry=args.retry_acquisition, progress=Progress(repo))
    if name == "prepare":
        from .corpus_pipeline import prepare_tasks
        return prepare_tasks(repo, tier=args.tier)
    if name in {"run", "resume"}:
        from .corpus_dispatch import CorpusExecutor
        executor = CorpusExecutor(repo, args.paths)
        if name == "resume":
            return executor.resume()
        return executor.submit(args.worker_config, tier=args.tier, batch_size=args.batch_size, concurrency=args.concurrency, max_jobs=args.max_jobs)
    if name == "execute":
        return execute(args, repo)
    if name == "export":
        from .corpus_export import export_corpus
        return export_corpus(repo, args.output)
    if name in {"calibrate", "validate"}:
        from .corpus_calibration import calibrate, validate
        if args.until_complete and not args.execute:
            raise ValidationError("--until-complete requires --execute")
        if not 0.1 <= args.poll_seconds <= 60:
            raise ValidationError("poll interval must be from 0.1 to 60 seconds")
        options = {"worker_id": args.worker_config, "sample_size": args.sample_size,
                   "execute": args.execute, "concurrency": args.concurrency}
        if name == "calibrate":
            options.update(low_worker_id=args.low_worker_config, validation_cap=args.validation_cap)
        with Progress(repo) as progress:
            while True:
                result = (calibrate if name == "calibrate" else validate)(repo, args.paths, **options)
                progress(force=True, phase=name)
                if not args.until_complete or result.get("paused") or result["report"]["complete"]:
                    progress.stop.set()
                    if result.get("paused"):
                        progress(force=True, phase=name, state="waiting_input", summary="Worker provider paused; results are saved")
                    elif result["report"]["complete"]:
                        progress(force=True, phase=name, state="complete", summary="Bounded validation finished; report and model receipts are saved")
                    return result
                time.sleep(args.poll_seconds)
    raise ValidationError("unknown corpus command")
