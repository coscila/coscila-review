# SPDX-License-Identifier: Apache-2.0
"""Resumable, per-locus citation census using the platform worker broker.

Acquisition remains owned by literature.py. This module records model
measurements independently of the manuscript graph: abstract, full-text and
panel passes are immutable observations, never replacement scientific truth.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import jsonschema

from . import literature as L
from .confidentiality import authorize_egress, append_egress_event, load_scholarly_context
from .errors import ConfidentialityError, ReviewError, ValidationError
from .evidence import complete_abstract
from .scholarly import utc_now
from .util import atomic_json, atomic_write, sha256_bytes, sha256_file
from .worker_configuration import is_positive_safe_integer, matches_pinned_worker_configuration

ROOT = ".ocsl-review/census"
SPECIFIC = {"numeric", "causal", "quotation", "method", "software", "dataset", "novelty"}
SUPPORT = {"direct-support", "scope-caveat"}
ADVERSE = {"contradicted", "not-addressed"}


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _database(workspace: Path) -> sqlite3.Connection:
    path = L._path(workspace, ROOT + "/census.sqlite3")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists() and path.stat().st_nlink != 1:
        raise ValidationError("census database shares mutable inodes")
    connection = sqlite3.connect(path, factory=_Connection)
    os.chmod(path, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, spec TEXT NOT NULL, job TEXT, attempt INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS tasks(id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id), spec TEXT NOT NULL,
        PRIMARY KEY(id,run_id));
      CREATE TABLE IF NOT EXISTS assessments(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
        accepted INTEGER NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL,
        FOREIGN KEY(task_id,run_id) REFERENCES tasks(id,run_id));
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        kind TEXT NOT NULL, task_ids TEXT NOT NULL, attempt INTEGER NOT NULL, parent_job TEXT);
      CREATE TRIGGER IF NOT EXISTS assessment_no_update BEFORE UPDATE ON assessments
        BEGIN SELECT RAISE(ABORT, 'assessment history is append-only'); END;
      CREATE TRIGGER IF NOT EXISTS assessment_no_delete BEFORE DELETE ON assessments
        BEGIN SELECT RAISE(ABORT, 'assessment history is append-only'); END;
      CREATE TRIGGER IF NOT EXISTS task_no_update BEFORE UPDATE ON tasks
        BEGIN SELECT RAISE(ABORT, 'task inputs are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS task_no_delete BEFORE DELETE ON tasks
        BEGIN SELECT RAISE(ABORT, 'task inputs are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS job_no_update BEFORE UPDATE ON jobs
        BEGIN SELECT RAISE(ABORT, 'job provenance is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS job_no_delete BEFORE DELETE ON jobs
        BEGIN SELECT RAISE(ABORT, 'job provenance is immutable'); END;
    """)
    return connection


def _run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValidationError("unknown census run")
    result = {**dict(row), "spec": json.loads(row["spec"])}
    if "census-" + L.digest(result["spec"])[:24] != run_id:
        raise ValidationError("pinned census run configuration changed")
    return result


def _task_rows(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [json.loads(row["spec"]) for row in connection.execute(
        "SELECT spec FROM tasks WHERE run_id=? ORDER BY id", (run_id,))]


def _load(args: Any, workspace: Path, connection: sqlite3.Connection):
    ledger, graph = L._load(args, workspace)
    L._require_confirmation(ledger)
    run = _run(connection, args.run)
    if run["spec"]["scopeSha256"] != ledger["scopeSha256"] or run["spec"]["bindingSha256"] != ledger["bindingSha256"]:
        raise ValidationError("census run belongs to obsolete manuscript or citation bindings")
    if L.digest(_task_rows(connection, args.run)) != run["spec"]["taskSetSha256"]:
        raise ValidationError("immutable census task inputs changed")
    return ledger, graph, run


def _validate_sources(workspace: Path, tasks: list[dict[str, Any]]) -> None:
    seen = set()
    for task in tasks:
        source = task.get("source")
        if not source or source["id"] in seen:
            continue
        L._verified_source(workspace, source)
        seen.add(source["id"])


def _configuration(args: Any, workspace: Path) -> tuple[int, dict[str, Any]]:
    catalog = _worker(args, workspace, "configs")
    revision = catalog.get("revision")
    workers = catalog.get("workers")
    if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(workers, list):
        raise ValidationError("worker catalog omitted its configuration revision")
    matches = [worker for worker in workers if isinstance(worker, dict) and worker.get("id") == args.worker_config]
    if len(matches) != 1:
        raise ValidationError("select one configured platform worker ID")
    snapshot = matches[0]
    fields = {"id", "assignment", "agent", "effort", "modelProvider", "providerId", "model", "bulkAvailable", "evidencePolicy", "bulkConcurrencyLimit"}
    provider_fields = {"id", "model", "kind", "label", "reasoningProfile"}
    if set(snapshot) - fields or not isinstance(snapshot.get("modelProvider"), dict) or set(snapshot["modelProvider"]) - provider_fields:
        raise ValidationError("worker catalog exposed fields outside its public configuration contract")
    if not isinstance(snapshot.get("bulkAvailable"), bool):
        raise ValidationError("worker catalog omitted transport capability")
    if "evidencePolicy" in snapshot and snapshot["evidencePolicy"] != "public-literature":
        raise ValidationError("worker catalog has an unknown evidence policy")
    limit = snapshot.get("bulkConcurrencyLimit")
    if "bulkConcurrencyLimit" in snapshot and not is_positive_safe_integer(limit):
        raise ValidationError("worker catalog has an invalid bulk concurrency limit")
    return revision, snapshot


def _check_configuration(args: Any, workspace: Path, run: dict[str, Any]) -> None:
    selected = copy.copy(args)
    selected.worker_config = run["spec"]["workerConfigId"]
    revision, snapshot = _configuration(selected, workspace)
    if (revision != run["spec"]["workerConfigRevision"]
            or not matches_pinned_worker_configuration(run["spec"], snapshot)):
        raise ValidationError("worker settings changed; prepare an independently pinned census run")


def _schema(workspace: Path, run: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = L._path(workspace, f"{ROOT}/{run['id']}/output-schema.json", exists=True)
    value = L._read_json(path)
    if L.digest(value) != run["spec"]["outputSchemaSha256"]:
        raise ValidationError("pinned worker output schema changed")
    return path, value


def _authorize(args: Any, workspace: Path, run: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    _validate_sources(workspace, tasks)
    plan, selected = load_scholarly_context(args.paths, os.environ, cwd=workspace)
    if selected != workspace:
        raise ValidationError("census dispatch requires an exact scholarly workspace context")
    try:
        event = authorize_egress(provider="platform-worker:" + run["spec"]["workerConfigId"],
                                 operation="citation_census", arguments={"manuscriptEvidence": [t["input"] for t in tasks],
                                                                           "workerAssignment": run["spec"]["workerConfig"]["assignment"]}, plan=plan)
    except ConfidentialityError:
        # Explicit operator restrictions always prevail. Workspace flags alone
        # never authorize sending evidence to a worker.
        if (os.environ.get("OCSL_REVIEW_EGRESS_PLAN")
                or (workspace / ".ocsl-review/egress-plan.json").exists()
                or run["spec"]["workerConfig"].get("evidencePolicy") != "public-literature"):
            raise
        from .public_literature import authorize_public_census
        event = authorize_public_census(workspace, run, tasks)
    append_egress_event(workspace, event)


def prepare(args: Any, workspace: Path, ledger: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    L._require_confirmation(ledger)
    manuscript = L._read_json(L._path(workspace, ledger["manuscriptPath"], exists=True))
    if not manuscript["ingestion"]["complete"]:
        raise ValidationError("census requires complete, reconciled manuscript extraction")
    prompt_path = args.paths.home / "prompts/citation-support-v1.txt"
    schema_path = args.paths.home / "schemas/census-assessment.schema.json"
    prompt = prompt_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    revision, configuration = _configuration(args, workspace)
    entries = {entry["referenceId"]: entry for entry in ledger["entries"]}
    if set(args.reference_id) - set(entries):
        raise ValidationError("pilot filter contains an unknown manuscript reference ID")
    references = {reference["id"]: reference for reference in graph["references"]}
    clusters = {cluster["id"]: cluster for cluster in graph["citationClusters"]}
    abstracts = {reference_id: next((candidate for candidate in entry.get("abstractCandidates", [])
                                    if complete_abstract(candidate)), None)
                 for reference_id, entry in entries.items()} if args.mode == "abstract" else {}
    tasks = []
    for claim in graph["claims"]:
        for cluster_id in claim["citationClusterIds"]:
            cluster = clusters[cluster_id]
            for reference_id in cluster["referenceIds"]:
                if args.reference_id and reference_id not in args.reference_id:
                    continue
                entry, reference = entries[reference_id], references[reference_id]
                source = ledger["sources"].get(entry["sourceId"])
                reason = None
                if cluster["bindingStatus"] != "exact" or reference["resolutionStatus"] != "resolved" or reference.get("identityEvidence", {}).get("agreementStrength") != "strong":
                    reason = "identity or citation binding is unverified"
                if args.mode == "abstract":
                    candidate = abstracts[reference_id]
                    if candidate and source and source["accessLevel"] == "abstract-only" and source["textSha256"] == candidate["provenance"]["textSha256"]:
                        document = L._verified_source(workspace, source)
                    elif candidate:
                        text = candidate["text"]
                        document = {"body": text.encode("utf-8"), "text": text, "complete": True,
                                    "sourceUrl": candidate["provenance"]["sourceUrl"],
                                    "identifiers": candidate["provenance"]["identifiers"],
                                    "abstractProvenance": candidate["provenance"],
                                    "blocks": [{"id": "abstract", "kind": "abstract", "locator": "Complete abstract", "text": text}]}
                        source_id = L._store_source(workspace, ledger, document, access_level="abstract-only", provider=candidate["provider"])
                        source = ledger["sources"][source_id]
                        document = L._verified_source(workspace, source)
                    else:
                        document = None
                        reason = reason or "complete abstract provenance unavailable; retry acquisition for legacy candidates"
                elif source and source["accessLevel"] == "full-text":
                    document = L._verified_source(workspace, source)
                else:
                    document = None
                    reason = reason or "full text unavailable"
                if source and not source["complete"]:
                    reason = reason or "source extraction incomplete"
                if not document:
                    source = None
                elif source:
                    source = copy.deepcopy(source)
                    source["readUnitIds"] = []  # Operator reading progress does not change pinned evidence.
                task_id = "task-" + L.digest([claim["id"], cluster_id, reference_id])[:24]
                context = []
                anchor = next((c for c in manuscript["citationClusters"] if c["id"] == cluster_id), None)
                if anchor and anchor.get("sentenceId"):
                    sentences = manuscript["sentences"]
                    index = next(i for i, sentence in enumerate(sentences) if sentence["id"] == anchor["sentenceId"])
                    context = [sentence["text"] for sentence in sentences[max(0, index - 1):index + 2]
                               if sentence["paragraphId"] == sentences[index]["paragraphId"]]
                payload = {"claim": claim["text"], "claimType": claim["claimType"],
                           "citationLocus": cluster, "context": context,
                           "sourceAnchors": anchor.get("sourceAnchors", []) if anchor else [],
                           "citedWork": reference["identity"], "identifiers": reference["identifiers"],
                           "evidenceTier": source["accessLevel"] if source else "inaccessible",
                           "evidenceUnits": document["blocks"] if document else [],
                           "sourceSha256": source["artifactSha256"] if source else None,
                           "complete": bool(source and source["complete"])}
                messages = [{"role": "system", "content": prompt},
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
                tasks.append({"id": task_id, "claimId": claim["id"], "claimType": claim["claimType"],
                              "referenceId": reference_id, "citationClusterId": cluster_id,
                              "source": source, "blockedReason": reason,
                              "input": {"messages": messages}, "inputSha256": L.digest(messages)})
    if args.limit is not None:
        if args.limit < 1:
            raise ValidationError("pilot limit must be positive")
        tasks = tasks[:args.limit]
    if not tasks:
        raise ValidationError("census has no citation loci in its selected scope")
    spec = {"schemaVersion": 1, "mode": args.mode, "workerConfigId": args.worker_config,
            "workerConfigRevision": revision, "workerConfig": configuration,
            "workerConfigSha256": L.digest(configuration),
            "transport": "bulk" if configuration["bulkAvailable"] else "agent",
            "scopeSha256": ledger["scopeSha256"], "bindingSha256": ledger["bindingSha256"],
            "promptSha256": sha256_file(prompt_path), "outputSchemaSha256": L.digest(schema),
            "taskSetSha256": L.digest(sorted(tasks, key=lambda task: task["id"])), "samplingDesign": "pilot" if args.limit or args.reference_id else "all-bound-loci",
            "sourceManuscriptSha256": manuscript["source"]["sha256"],
            "sourceManuscriptArtifactSha256": L.digest(manuscript)}
    run_id = "census-" + L.digest(spec)[:24]
    with _database(workspace) as connection:
        connection.execute("INSERT OR IGNORE INTO runs(id,spec) VALUES(?,?)", (run_id, json.dumps(spec)))
        connection.executemany("INSERT OR IGNORE INTO tasks VALUES(?,?,?)",
                              [(task["id"], run_id, json.dumps(task, ensure_ascii=False)) for task in tasks])
    root = f"{ROOT}/{run_id}"
    atomic_json(L._path(workspace, root + "/output-schema.json"), schema, mode=0o600)
    atomic_json(L._path(workspace, root + "/manifest.json"), spec, mode=0o600)
    atomic_write(L._path(workspace, root + "/tasks.jsonl"), "".join(json.dumps({"id": task["id"], "input": task["input"]}, ensure_ascii=False) + "\n" for task in tasks if not task["blockedReason"]), mode=0o600)
    L._save(workspace, ledger, graph, event="census-inputs-prepared")
    return {"runId": run_id, "mode": args.mode, "tasks": len(tasks),
            "dispatchable": sum(not t["blockedReason"] for t in tasks),
            "blocked": sum(bool(t["blockedReason"]) for t in tasks),
            "manifest": root + "/manifest.json", "state": "prepared"}


def _worker(args: Any, workspace: Path, *command: str) -> dict[str, Any]:
    helper = os.environ.get("OCSL_WORKERS_CLI")
    if not helper:
        helper = str(L._path(workspace, "tools/ocsl-workers.mjs", exists=True))
    elif not Path(helper).expanduser().is_absolute():
        raise ValidationError("explicit OCSL_WORKERS_CLI must be an absolute helper path")
    else:
        helper = str(Path(helper).expanduser())
    if not Path(helper).is_file():
        raise ValidationError("platform worker helper is not installed")
    try:
        result = subprocess.run([os.environ.get("OCSL_WORKERS_NODE", "node"), helper, *command],
                                cwd=workspace, capture_output=True, text=True, timeout=65, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError("platform worker command unavailable or timed out; resume by idempotency key") from exc
    if result.returncode:
        raise ReviewError("platform worker command failed: " + result.stderr[-1500:])
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise ValidationError("platform worker returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("platform worker response is not an object")
    return value


def validate_assessment(task: dict[str, Any], output: Any, schema: dict[str, Any], workspace: Path) -> dict[str, Any]:
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError as exc:
            raise ValidationError("worker scientific output is not JSON") from exc
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(output))
    if errors:
        raise ValidationError("worker scientific output violates pinned schema: " + errors[0].message)
    source = task["source"]
    if not source or task["blockedReason"]:
        raise ValidationError("worker cannot judge a blocked or missing source")
    document = L._verified_source(workspace, source)
    units = {block["id"]: block["text"] for block in document["blocks"]}
    if set(output["readUnitIds"]) != set(units):
        raise ValidationError("worker did not report complete source reading")
    for evidence in output["evidence"]:
        if evidence["unitId"] not in units or evidence["quote"] not in units[evidence["unitId"]]:
            raise ValidationError("worker quote is absent from its named source unit")
    verdict = output["supportVerdict"]
    if verdict != "abstain":
        if not output["evidence"] or not source["complete"]:
            raise ValidationError("substantive verdict needs complete evidence and a verbatim quote")
        if source["accessLevel"] == "abstract-only":
            if task["claimType"] in SPECIFIC or not output["abstractSufficientReason"].strip():
                raise ValidationError("specific claims require full text; general abstract sufficiency needs a reason")
            if verdict == "not-addressed":
                raise ValidationError("absence from an abstract cannot establish not-addressed")
    return output


def import_results(args: Any, workspace: Path, connection: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, Any]:
    _, _, run = _load(args, workspace, connection)
    tasks = {task["id"]: task for task in _task_rows(connection, args.run)}
    if not isinstance(records, list) or not all(isinstance(record, dict) and isinstance(record.get("id"), str) for record in records):
        raise ValidationError("worker results need object records with string task IDs")
    if len({record.get("id") for record in records}) != len(records) or any(record.get("id") not in tasks for record in records):
        raise ValidationError("worker results contain duplicate or unknown task IDs")
    _, schema = _schema(workspace, run)
    counts = {"accepted": 0, "rejected": 0, "duplicates": 0}
    for record in records:
        if record.get("state") not in {"completed", "failed", "cancelled", "interrupted"}:
            continue
        payload = {"result": record, "validationError": None}
        accepted = False
        try:
            if record["state"] != "completed":
                raise ValidationError("worker task did not complete")
            if not isinstance(record.get("model"), str) or not record["model"] or record["model"] == "unknown" or not isinstance(record.get("providerId"), str) or not record["providerId"]:
                raise ValidationError("worker did not record actual provider and model")
            if not re.fullmatch(r"[a-f0-9]{64}", str(record.get("promptSha256", ""))):
                raise ValidationError("worker did not record its actual prompt digest")
            if record.get("usage") is not None and not isinstance(record["usage"], dict):
                raise ValidationError("worker usage must be an object or explicitly unknown")
            payload["assessment"] = validate_assessment(tasks[record["id"]], record.get("output"), schema, workspace)
            accepted = True
        except ValidationError as exc:
            payload["validationError"] = str(exc)
        history_id = "assessment-" + L.digest([args.run, record])
        old = connection.execute("SELECT payload FROM assessments WHERE id=?", (history_id,)).fetchone()
        if old:
            if old["payload"] != json.dumps(payload, ensure_ascii=False):
                raise ValidationError("same worker result was replayed against changed evidence")
            counts["duplicates"] += 1
            continue
        connection.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?)", (
            history_id, args.run, record["id"], int(accepted), json.dumps(payload, ensure_ascii=False), utc_now()))
        counts["accepted" if accepted else "rejected"] += 1
    return {"runId": args.run, **counts}


def cmd_prepare(args: Any) -> dict[str, Any]:
    workspace = L._workspace(args)
    with L._locked(workspace):
        ledger, graph = L._load(args, workspace)
        return prepare(args, workspace, ledger, graph)


def _jobs(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY rowid", (run_id,))]
    for row in rows:
        row["task_ids"] = json.loads(row["task_ids"])
    return rows


def _remember_job(connection: sqlite3.Connection, run: dict[str, Any], response: dict[str, Any],
                  kind: str, task_ids: list[str], attempt: int, parent_job: str | None = None) -> None:
    job_id = response.get("jobId")
    if not isinstance(job_id, str) or not job_id:
        raise ValidationError("worker submission omitted jobId")
    if response.get("configRevision") is not None and response["configRevision"] != run["spec"]["workerConfigRevision"]:
        raise ValidationError("worker job revision disagrees with the pinned run")
    expected = (job_id, run["id"], kind, json.dumps(sorted(task_ids)), attempt, parent_job)
    old = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if old and tuple(old) != expected:
        raise ValidationError("worker job replay changed its immutable provenance")
    connection.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)", expected)
    connection.execute("UPDATE runs SET job=?,attempt=? WHERE id=?", (job_id, attempt, run["id"]))
    connection.commit()  # Each accepted submission survives interruption before the next locus.


def _job_results(args: Any, workspace: Path, connection: sqlite3.Connection, job: dict[str, Any],
                 records: list[dict[str, Any]]) -> dict[str, Any]:
    prepared = []
    for record in records:
        if not isinstance(record, dict):
            raise ValidationError("worker result records must be objects")
        item = dict(record)
        # Result-page revision is a delivery cursor, not a new scientific
        # assessment. Full status replies omit it; both routes must deduplicate
        # the same immutable result and preserve real output/receipt changes.
        item.pop("revision", None)
        if job["kind"] == "agent":
            if item.get("id") != job["id"] or len(job["task_ids"]) != 1:
                raise ValidationError("native result does not belong to its recorded citation job")
            item["workerItemId"] = item["id"]
            item["id"] = job["task_ids"][0]
        elif item.get("id") not in job["task_ids"]:
            raise ValidationError("bulk result escaped its submitted citation task set")
        item["workerJobId"] = job["id"]
        prepared.append(item)
    return import_results(args, workspace, connection, prepared)


def cmd_run(args: Any) -> dict[str, Any]:
    concurrency = getattr(args, "concurrency", None)
    if concurrency is not None and not is_positive_safe_integer(concurrency):
        raise ValidationError("concurrency must be a positive safe integer (at most 9007199254740991)")
    workspace = L._workspace(args)
    with L._locked(workspace), _database(workspace) as connection:
        _, _, run = _load(args, workspace, connection)
        if concurrency is not None and run["spec"]["transport"] != "bulk":
            raise ValidationError("concurrency is supported only for bulk census jobs")
        tasks = _task_rows(connection, args.run)
        accepted_ids = {r[0] for r in connection.execute("SELECT task_id FROM assessments WHERE run_id=? AND accepted=1", (args.run,))}
        selected_tasks = [task for task in tasks if not task["blockedReason"] and task["id"] not in accepted_ids]
        if not selected_tasks:
            return {"runId": args.run, "state": "nothing-to-dispatch"}
        attempt = run["attempt"] + int(bool(args.retry))
        submitted = {task_id for job in _jobs(connection, args.run) if job["attempt"] == attempt for task_id in job["task_ids"]}
        selected_tasks = [task for task in selected_tasks if task["id"] not in submitted]
        if not selected_tasks:
            return {"runId": args.run, "jobId": run["job"], "state": "already-submitted"}
        _authorize(args, workspace, run, selected_tasks)
        _check_configuration(args, workspace, run)
        schema_path, schema = _schema(workspace, run)
        items = L._path(workspace, f"{ROOT}/{args.run}/dispatch-{attempt}.jsonl")
        atomic_write(items, "".join(json.dumps({"id": task["id"], "input": task["input"]}, ensure_ascii=False) + "\n" for task in selected_tasks), mode=0o600)
        if run["spec"]["transport"] == "bulk":
            options = ["--concurrency", str(concurrency)] if concurrency is not None else []
            response = _worker(args, workspace, "bulk", "--config", run["spec"]["workerConfigId"],
                               "--key", f"{args.run}:{attempt}", "--items", str(items), "--schema", str(schema_path), *options)
            _remember_job(connection, run, response, "bulk", [task["id"] for task in selected_tasks], attempt)
            return {"runId": args.run, **response}
        jobs = []
        for task in selected_tasks:
            task_path = L._path(workspace, f"{ROOT}/{args.run}/agent-{attempt}-{task['id']}.txt")
            text = "Evaluate the following messages as one citation-support task. Return only the schema-conforming JSON result.\n"
            text += json.dumps({"messages": task["input"]["messages"], "outputSchema": schema}, ensure_ascii=False)
            atomic_write(task_path, text, mode=0o600)
            response = _worker(args, workspace, "agent", "--config", run["spec"]["workerConfigId"],
                               "--key", f"{args.run}:{attempt}:{task['id']}", "--task-file", str(task_path))
            _remember_job(connection, run, response, "agent", [task["id"]], attempt)
            jobs.append({"jobId": response["jobId"], "taskId": task["id"], "state": response.get("state")})
        return {"runId": args.run, "state": "submitted", "transport": "agent", "jobs": jobs}


def _poll(args: Any, *, command: str = "status") -> dict[str, Any]:
    workspace = L._workspace(args)
    with L._locked(workspace), _database(workspace) as connection:
        _, _, run = _load(args, workspace, connection)
        jobs = _jobs(connection, args.run)
        if getattr(args, "job", None):
            jobs = [job for job in jobs if job["id"] == args.job]
            if not jobs:
                raise ValidationError("worker job does not belong to this census run")
        else:
            # Resumed jobs are new immutable jobs; poll the newest leaf in each chain.
            parents = {job["parent_job"] for job in jobs if job["parent_job"]}
            jobs = [job for job in jobs if job["id"] not in parents]
        if not jobs:
            return {"runId": args.run, "state": "prepared", "tasks": len(_task_rows(connection, args.run))}
        if command == "wait" and len(jobs) != 1:
            raise ValidationError("wait is bounded to one job; select --job from census status")
        if command == "resume":
            selected_ids = {task_id for job in jobs for task_id in job["task_ids"]}
            _authorize(args, workspace, run, [task for task in _task_rows(connection, args.run) if task["id"] in selected_ids])
            _check_configuration(args, workspace, run)
        responses = []
        for job in jobs:
            if command == "resume":
                previous = _worker(args, workspace, "status", "--job", job["id"])
                old_records = previous.pop("results", [])
                if old_records:
                    previous["import"] = _job_results(args, workspace, connection, job, old_records)
                if previous.get("state") not in {"paused", "failed", "interrupted", "cancelled"}:
                    responses.append(previous)
                    continue
            argv = [command, "--job", job["id"]]
            results_path = None
            if command == "wait":
                timeout = args.timeout_ms
                if not 1 <= timeout <= 60000:
                    raise ValidationError("wait timeout must be from 1 to 60000 milliseconds")
                results_path = L._path(workspace, f"{ROOT}/{args.run}/job-{job['id']}-results.jsonl")
                argv.extend(["--timeout-ms", str(timeout), "--out", str(results_path)])
            response = _worker(args, workspace, *argv)
            observed = job
            if command == "resume" and response.get("jobId") != job["id"]:
                _remember_job(connection, run, response, job["kind"], job["task_ids"], job["attempt"], job["id"])
                observed = {**job, "id": response["jobId"]}
            if results_path is not None:
                if Path(response.get("resultsFile", "")) != results_path:
                    raise ValidationError("worker wait did not return the requested results artifact")
                records = _read_results(L._path(workspace, results_path, exists=True))
            else:
                records = response.pop("results", [])
            if records:
                response["import"] = _job_results(args, workspace, connection, observed, records)
            responses.append(response)
        states = {response.get("state") for response in responses}
        state = next((s for s in ("paused", "running", "queued", "failed", "interrupted", "cancelled", "completed") if s in states), "unknown")
        return {"runId": args.run, "state": state, "jobs": responses}


def cmd_status(args: Any) -> dict[str, Any]:
    return _poll(args)


def cmd_resume(args: Any) -> dict[str, Any]:
    return _poll(args, command="resume")


def cmd_wait(args: Any) -> dict[str, Any]:
    return _poll(args, command="wait")


def cmd_cancel(args: Any) -> dict[str, Any]:
    return _poll(args, command="cancel")


def cmd_list(args: Any) -> dict[str, Any]:
    workspace = L._workspace(args)
    with L._locked(workspace), _database(workspace) as connection:
        runs = []
        for row in connection.execute("SELECT id FROM runs ORDER BY rowid"):
            run = _run(connection, row["id"])
            runs.append({"runId": run["id"], "mode": run["spec"]["mode"],
                         "workerConfigId": run["spec"]["workerConfigId"],
                         "workerConfigRevision": run["spec"]["workerConfigRevision"],
                         "transport": run["spec"]["transport"], "attempt": run["attempt"],
                         "jobIds": [job["id"] for job in _jobs(connection, run["id"])]})
        return {"runs": runs}


def _read_results(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_nlink != 1:
        raise ValidationError("worker results share mutable inodes")
    if path.stat().st_size > L.MAX_STATE_BYTES:
        raise ValidationError("worker results exceed import size bound")
    try:
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("worker result JSONL is invalid") from exc
    if not all(isinstance(record, dict) for record in records):
        raise ValidationError("worker results must be JSON objects")
    return records


def cmd_import(args: Any) -> dict[str, Any]:
    workspace = L._workspace(args)
    path = L._path(workspace, args.input, exists=True)
    records = _read_results(path)
    with L._locked(workspace), _database(workspace) as connection:
        if getattr(args, "job", None):
            job = next((job for job in _jobs(connection, args.run) if job["id"] == args.job), None)
            if not job:
                raise ValidationError("worker import job does not belong to this census run")
            return _job_results(args, workspace, connection, job, records)
        return import_results(args, workspace, connection, records)


def cmd_export(args: Any) -> dict[str, Any]:
    workspace = L._workspace(args)
    with L._locked(workspace), _database(workspace) as connection:
        ledger, _, run = _load(args, workspace, connection)
        tasks = _task_rows(connection, args.run)
        _validate_sources(workspace, tasks)
        output = L._path(workspace, args.output + "/facts.json")
        output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        db_path = L._path(workspace, args.output + "/facts.sqlite3")
        if db_path.exists() and db_path.stat().st_nlink != 1:
            raise ValidationError("facts database shares inodes")
        # Replace a snapshot atomically; never mutate the authoritative history.
        temp_path = L._path(workspace, args.output + "/facts.sqlite3.tmp")
        if temp_path.exists():
            temp_path.unlink()
        snapshots = []
        with sqlite3.connect(temp_path, factory=_Connection) as target:
            target.execute("CREATE TABLE measurements(task_id TEXT PRIMARY KEY, assessment_id TEXT, category TEXT, model TEXT, evidence_tier TEXT, source_sha256 TEXT)")
            target.execute("CREATE TABLE worker_calls(assessment_id TEXT PRIMARY KEY, provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, prompt_sha256 TEXT, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, usage_quality TEXT, usage_json TEXT)")
            for task in tasks:
                row = connection.execute("SELECT * FROM assessments WHERE run_id=? AND task_id=? AND accepted=1 ORDER BY rowid DESC LIMIT 1", (args.run, task["id"])).fetchone()
                payload = json.loads(row["payload"]) if row else None
                verdict = payload["assessment"]["supportVerdict"] if payload else "abstain"
                category = "supported" if verdict in SUPPORT else "unsupported" if verdict in ADVERSE else "uncertain"
                target.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?)", (
                    task["id"], row["id"] if row else None, category,
                    payload["result"]["model"] if payload else None,
                    task["source"]["accessLevel"] if task["source"] else "inaccessible",
                    task["source"]["artifactSha256"] if task["source"] else None))
            snapshots = [dict(row) for row in connection.execute("SELECT * FROM assessments WHERE run_id=? ORDER BY rowid", (args.run,))]
            for snapshot in snapshots:
                result = json.loads(snapshot["payload"])["result"]
                usage = result.get("usage") or {}
                def token_count(name, alias=None):
                    value = usage.get(name, usage.get(alias))
                    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                target.execute("INSERT INTO worker_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    snapshot["id"], result.get("providerId"), result.get("model"),
                    token_count("input", "inputTokens"), token_count("output", "outputTokens"), token_count("total", "totalTokens"),
                    result.get("promptSha256"), token_count("cacheRead"), token_count("cacheWrite"),
                    token_count("reasoning"), usage.get("quality"), json.dumps(usage, ensure_ascii=False)))
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, db_path)
        db_hash = sha256_file(db_path)
        queries = {
            "citation_loci": "SELECT COUNT(*) FROM measurements",
            "model_assessed": "SELECT COUNT(*) FROM measurements WHERE assessment_id IS NOT NULL",
            "supported": "SELECT COUNT(*) FROM measurements WHERE category='supported'",
            "unsupported": "SELECT COUNT(*) FROM measurements WHERE category='unsupported'",
            "uncertain": "SELECT COUNT(*) FROM measurements WHERE category='uncertain'",
            "classifiable": "SELECT COUNT(*) FROM measurements WHERE category IN ('supported','unsupported')",
            "unsupported_share": "SELECT 1.0*SUM(category='unsupported')/NULLIF(SUM(category IN ('supported','unsupported')),0) FROM measurements",
            "input_tokens_recorded": "SELECT SUM(input_tokens) FROM worker_calls",
            "output_tokens_recorded": "SELECT SUM(output_tokens) FROM worker_calls",
            "total_tokens_recorded": "SELECT SUM(total_tokens) FROM worker_calls",
            "worker_calls": "SELECT COUNT(*) FROM worker_calls",
            "calls_missing_total_tokens": "SELECT COUNT(*) FROM worker_calls WHERE total_tokens IS NULL",
            "cache_read_tokens_recorded": "SELECT SUM(cache_read_tokens) FROM worker_calls",
            "cache_write_tokens_recorded": "SELECT SUM(cache_write_tokens) FROM worker_calls",
            "reasoning_tokens_recorded": "SELECT SUM(reasoning_tokens) FROM worker_calls",
        }
        facts = {}
        with sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True, factory=_Connection) as frozen:
            for key, sql in queries.items():
                facts[key] = {"value": frozen.execute(sql).fetchone()[0], "sql": sql,
                              "method": "Per-locus model measurement; pending, inaccessible and partial assessments are uncertain",
                              "source": {"path": "facts.sqlite3", "sha256": db_hash}}
        source_hashes = sorted({task["source"]["artifactSha256"] for task in tasks if task["source"]})
        history = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in snapshots)
        atomic_write(L._path(workspace, args.output + "/assessments.jsonl"), history, mode=0o600)
        bundle = {"schemaVersion": 1, "facts": facts, "provenance": {
            "runId": args.run, **run["spec"], "sourceSha256": source_hashes,
            "assessmentHistorySha256": sha256_bytes(history.encode("utf-8")),
            "scopeConfirmation": ledger["confirmation"],
            "tokenAccounting": "Only provider-recorded token counts; missing values remain NULL. worker_calls retains provider/model breakdown and retries.",
            "interpretation": "Model measurements, not human gold; historical oncology scores are comparators only"}}
        atomic_json(output, bundle, mode=0o600)
        return {"runId": args.run, "facts": str(output), "measurements": len(tasks), "assessments": len(snapshots)}
