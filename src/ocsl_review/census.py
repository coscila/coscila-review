"""Resumable, per-locus citation census using the platform worker broker.

Acquisition remains owned by literature.py. This module records model
measurements independently of the manuscript graph: abstract, full-text and
panel passes are immutable observations, never replacement scientific truth.
"""
from __future__ import annotations

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
from .errors import ReviewError, ValidationError
from .scholarly import utc_now
from .util import atomic_json, atomic_write, sha256_bytes, sha256_file

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
      CREATE TRIGGER IF NOT EXISTS assessment_no_update BEFORE UPDATE ON assessments
        BEGIN SELECT RAISE(ABORT, 'assessment history is append-only'); END;
      CREATE TRIGGER IF NOT EXISTS assessment_no_delete BEFORE DELETE ON assessments
        BEGIN SELECT RAISE(ABORT, 'assessment history is append-only'); END;
      CREATE TRIGGER IF NOT EXISTS task_no_update BEFORE UPDATE ON tasks
        BEGIN SELECT RAISE(ABORT, 'task inputs are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS task_no_delete BEFORE DELETE ON tasks
        BEGIN SELECT RAISE(ABORT, 'task inputs are immutable'); END;
    """)
    return connection


def _run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValidationError("unknown census run")
    return {**dict(row), "spec": json.loads(row["spec"])}


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


def prepare(args: Any, workspace: Path, ledger: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    L._require_confirmation(ledger)
    manuscript = L._read_json(L._path(workspace, ledger["manuscriptPath"], exists=True))
    if not manuscript["ingestion"]["complete"]:
        raise ValidationError("census requires complete, reconciled manuscript extraction")
    prompt_path = args.paths.home / "prompts/citation-support-v1.txt"
    schema_path = args.paths.home / "schemas/census-assessment.schema.json"
    prompt = prompt_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text())
    entries = {entry["referenceId"]: entry for entry in ledger["entries"]}
    references = {reference["id"]: reference for reference in graph["references"]}
    clusters = {cluster["id"]: cluster for cluster in graph["citationClusters"]}
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
                    candidate = next((candidate for candidate in entry.get("abstractCandidates", [])
                                      if candidate.get("complete") and candidate.get("text")), None)
                    if source and source["accessLevel"] == "abstract-only":
                        document = L._verified_source(workspace, source)
                    elif candidate:
                        text = candidate["text"]
                        document = {"body": text.encode("utf-8"), "text": text, "complete": True,
                                    "blocks": [{"id": "abstract", "kind": "abstract", "locator": "Complete abstract", "text": text}]}
                        source_id = L._store_source(workspace, ledger, document, access_level="abstract-only", provider=candidate["provider"])
                        source = ledger["sources"][source_id]
                        document = L._verified_source(workspace, source)
                    else:
                        document = None
                        reason = reason or "complete abstract unavailable"
                elif source and source["accessLevel"] == "full-text":
                    document = L._verified_source(workspace, source)
                else:
                    document = None
                    reason = reason or "full text unavailable"
                if source and not source["complete"]:
                    reason = reason or "source extraction incomplete"
                if not document:
                    source = None
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
    if args.limit:
        if args.limit < 1:
            raise ValidationError("pilot limit must be positive")
        tasks = tasks[:args.limit]
    if not tasks:
        raise ValidationError("census has no citation loci in its selected scope")
    spec = {"schemaVersion": 1, "mode": args.mode, "workerConfigId": args.worker_config,
            "scopeSha256": ledger["scopeSha256"], "bindingSha256": ledger["bindingSha256"],
            "promptSha256": sha256_file(prompt_path), "outputSchemaSha256": L.digest(schema),
            "taskSetSha256": L.digest(sorted(tasks, key=lambda task: task["id"])), "samplingDesign": "pilot" if args.limit or args.reference_id else "all-bound-loci",
            "sourceManuscriptSha256": manuscript["source"]["sha256"]}
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
        if not args.paths.lab_repo:
            raise ValidationError("platform worker helper unavailable; set OCSL_WORKERS_CLI")
        helper = str(args.paths.lab_repo / "tools/ocsl-workers.mjs")
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
    schema_path = L._path(workspace, f"{ROOT}/{args.run}/output-schema.json", exists=True)
    if L.digest(json.loads(schema_path.read_text())) != run["spec"]["outputSchemaSha256"]:
        raise ValidationError("pinned worker output schema changed")
    schema = json.loads(schema_path.read_text())
    counts = {"accepted": 0, "rejected": 0, "duplicates": 0}
    for record in records:
        if record.get("state") not in {"completed", "failed", "cancelled", "interrupted"}:
            continue
        payload = {"result": record, "validationError": None}
        accepted = False
        try:
            if record["state"] != "completed":
                raise ValidationError("worker task did not complete")
            if not isinstance(record.get("model"), str) or not record["model"] or not isinstance(record.get("providerId"), str) or not record["providerId"]:
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


def cmd_run(args: Any) -> dict[str, Any]:
    workspace = L._workspace(args)
    with L._locked(workspace), _database(workspace) as connection:
        _, _, run = _load(args, workspace, connection)
        if run["job"] and not args.retry:
            return {"runId": args.run, "jobId": run["job"], "state": "already-submitted"}
        tasks = _task_rows(connection, args.run)
        _validate_sources(workspace, tasks)
        plan, selected = load_scholarly_context(args.paths, os.environ, cwd=workspace)
        if selected != workspace:
            raise ValidationError("census dispatch requires an exact scholarly workspace context")
        accepted_ids = {r[0] for r in connection.execute("SELECT task_id FROM assessments WHERE run_id=? AND accepted=1", (args.run,))}
        selected_tasks = [task for task in tasks if not task["blockedReason"] and task["id"] not in accepted_ids]
        if not selected_tasks:
            return {"runId": args.run, "state": "nothing-to-dispatch"}
        event = authorize_egress(provider="platform-worker:" + run["spec"]["workerConfigId"],
                                 operation="citation_census", arguments={"manuscriptEvidence": [t["input"] for t in selected_tasks]}, plan=plan)
        append_egress_event(workspace, event)
        attempt = run["attempt"] + int(bool(args.retry))
        items = L._path(workspace, f"{ROOT}/{args.run}/dispatch-{attempt}.jsonl")
        atomic_write(items, "".join(json.dumps({"id": task["id"], "input": task["input"]}, ensure_ascii=False) + "\n" for task in selected_tasks), mode=0o600)
        schema_path = L._path(workspace, f"{ROOT}/{args.run}/output-schema.json", exists=True)
        if L.digest(json.loads(schema_path.read_text())) != run["spec"]["outputSchemaSha256"]:
            raise ValidationError("pinned schema changed before dispatch")
        response = _worker(args, workspace, "bulk", "--config", run["spec"]["workerConfigId"],
                           "--key", f"{args.run}:{attempt}", "--items", str(items), "--schema", str(schema_path))
        if not isinstance(response.get("jobId"), str) or not response["jobId"]:
            raise ValidationError("worker submission omitted jobId")
        connection.execute("UPDATE runs SET job=?,attempt=? WHERE id=?", (response["jobId"], attempt, args.run))
        return {"runId": args.run, **response}


def _poll(args: Any, *, resume: bool = False) -> dict[str, Any]:
    workspace = L._workspace(args)
    with L._locked(workspace), _database(workspace) as connection:
        _, _, run = _load(args, workspace, connection)
        if not run["job"]:
            return {"runId": args.run, "state": "prepared", "tasks": len(_task_rows(connection, args.run))}
        response = _worker(args, workspace, "resume" if resume else "status", "--job", run["job"])
        results = response.get("results", [])
        if results:
            response["import"] = import_results(args, workspace, connection, results)
        return {"runId": args.run, **response}


def cmd_status(args: Any) -> dict[str, Any]:
    return _poll(args)


def cmd_resume(args: Any) -> dict[str, Any]:
    return _poll(args, resume=True)


def cmd_import(args: Any) -> dict[str, Any]:
    workspace = L._workspace(args)
    path = L._path(workspace, args.input, exists=True)
    if path.stat().st_size > L.MAX_STATE_BYTES:
        raise ValidationError("worker results exceed import size bound")
    try:
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("worker result JSONL is invalid") from exc
    if not all(isinstance(record, dict) for record in records):
        raise ValidationError("worker results must be JSON objects")
    with L._locked(workspace), _database(workspace) as connection:
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
            target.execute("CREATE TABLE worker_calls(assessment_id TEXT PRIMARY KEY, provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, prompt_sha256 TEXT)")
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
                def token_count(name):
                    value = usage.get(name)
                    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                target.execute("INSERT INTO worker_calls VALUES(?,?,?,?,?,?,?)", (
                    snapshot["id"], result.get("providerId"), result.get("model"),
                    token_count("inputTokens"), token_count("outputTokens"), token_count("totalTokens"),
                    result.get("promptSha256")))
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
