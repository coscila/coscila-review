# SPDX-License-Identifier: Apache-2.0
"""Durable corpus batches through the scoped Lab broker, with per-item recovery."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import urllib.parse

from .confidentiality import authorize_egress, append_egress_event, load_scholarly_context, _audit_event
from .corpus_store import CorpusRepository, canonical, identity
from .corpus_library import now
from .corpus_assessment import AssessmentValidator, AssessmentError, build_messages, output_schema, PROMPT_PATH
from .errors import ConfidentialityError, ReviewError, ValidationError
from .public_literature import broker_catalog
from .util import is_within


class Broker:
    """Use the same authenticated Unix-socket routes as ocsl-workers.mjs."""

    def catalog(self):
        return broker_catalog()

    def request(self, method, route, payload=None):
        address = os.environ.get("PUPPENCLAW_ORCHESTRATION_SOCKET", "")
        if not os.path.isabs(address):
            raise ReviewError("advanced orchestration broker is unavailable")
        connection = http.client.HTTPConnection("localhost", timeout=60)
        try:
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(60)
            connection.sock.connect(address)
            body = canonical(payload).encode() if payload is not None else None
            if body and len(body) > 8 * 1024 * 1024:
                raise ValidationError("corpus broker payload exceeds the 8 MiB job bound")
            connection.request(method, route, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ReviewError("broker response exceeded the bounded result page")
            value = json.loads(raw)
            if response.status != 200:
                raise ReviewError("worker broker: " + str(value.get("error", response.status)))
            return value
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ReviewError("worker broker unavailable; retained batch can be resumed") from exc
        finally:
            connection.close()


@contextmanager
def controller_lock(repo):
    with (repo.root / "controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationError("another corpus controller owns this library") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def authorize(repo, paths, catalog, worker, tasks, inputs):
    """Verify evidence before either explicit egress or configured-public permission."""
    by_id = {task["task_id"]: task for task in tasks}
    sent = []
    for item in inputs:
        try:
            supplied = item["input"]["messages"]
            payload = json.loads(supplied[1]["content"])
            ids = [t["id"] for t in payload["items"]]
            expected = build_messages([by_id[i]["spec"] for i in ids], payload["evidenceTier"])
            if item["input"] != {"messages": expected}:
                raise ValueError("outgoing evidence changed")
            sent.extend(ids)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise ValidationError("outgoing batch does not match its verified task inputs") from exc
    if sorted(sent) != sorted(by_id) or len(sent) != len(set(sent)):
        raise ValidationError("outgoing batch differs from its pinned task membership")
    for task in tasks:
        spec = task["spec"]
        repo.read_artifact(task["spec_sha256"])
        repo.verify_artifact(spec["sourceSha256"])
        with repo.connect() as connection:
            locus = connection.execute("SELECT * FROM claim_loci WHERE locus_id=?", (task["locus_id"],)).fetchone()
            source = connection.execute("SELECT * FROM corpus_documents WHERE artifact_sha256=? AND document_id=?", (spec["sourceSha256"], spec.get("documentId"))).fetchone()
            units = {r["section_id"]: dict(r) for r in connection.execute("SELECT * FROM source_sections WHERE artifact_sha256=? AND work_id=?", (spec["sourceSha256"], source["work_id"] if source else ""))}
        if locus is None or locus["claim_text"] != spec["claim"]:
            raise ValidationError("task claim differs from its stored citation locus")
        if any(u["id"] not in units or (u["text"] != units[u["id"]]["extracted_text"] and not (
                spec.get("complete") is False and u.get("excerpt") is True and u["text"]
                and units[u["id"]]["extracted_text"].startswith(u["text"]))) for u in spec["evidenceUnits"]):
            raise ValidationError("task evidence differs from its registered immutable source")
        if source is None or source["provider"] != spec.get("provider") or source["source_url"] != spec.get("sourceUrl"):
            raise ValidationError("task source provenance differs from its registered document")
        if spec.get("reviewProof", {}).get("manuscriptSha256") != locus["review_artifact_sha256"]:
            raise ValidationError("public manuscript proof belongs to a different citation locus")
        if spec.get("complete") and (not source["complete"] or {u["id"] for u in spec["evidenceUnits"]} != set(units)):
            raise ValidationError("claimed complete evidence omits registered source sections")
    plan, selected = load_scholarly_context(paths, os.environ, cwd=repo.workspace.root)
    if selected != repo.workspace.root:
        raise ValidationError("corpus dispatch requires an exact scholarly workspace context")
    arguments = {"manuscriptEvidence": inputs, "workerAssignment": worker["assignment"]}
    try:
        event = authorize_egress(provider="platform-worker:" + worker["id"], operation="citation_census", arguments=arguments, plan=plan)
    except ConfidentialityError:
        if os.environ.get("OCSL_REVIEW_EGRESS_PLAN") or (repo.workspace.root / ".ocsl-review/egress-plan.json").exists():
            raise
        authority = catalog.get("authorization", {})
        parent = Path(authority.get("workspace", ""))
        if (authority.get("publicLiteratureAllowed") is not True or not authority.get("attemptId")
                or authority["attemptId"] != os.environ.get("PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID")
                or not parent.is_absolute() or any(p.is_symlink() for p in (parent, *parent.parents))
                or not is_within(repo.workspace.root, parent.resolve())
                or worker.get("evidencePolicy") != "public-literature"):
            raise ConfidentialityError("corpus dispatch needs the active approved public-literature worker")
        allowed = {"pubmed", "europe-pmc", "unpaywall", "publisher"}
        for task in tasks:
            spec, proof = task["spec"], task["spec"].get("reviewProof", {})
            manuscript = json.loads(repo.read_artifact(proof.get("manuscriptSha256", "")))
            if (proof.get("public") is not True or manuscript.get("confidential") is not False
                    or proof.get("provider") not in allowed or not str(proof.get("sourceUrl", "")).startswith("https://")
                    or not any(proof.get("publication", {}).get(k) for k in ("doi", "citation_doi", "pmid", "pmcid", "pmc"))
                    or spec.get("provider") not in allowed or not str(spec.get("sourceUrl", "")).startswith("https://")):
                raise ConfidentialityError("corpus public permission excludes unverified or local manuscript evidence")
        event = _audit_event("platform-worker:" + worker["id"], "citation_census", arguments, decision="allowed-configured-public-literature")
        event.update(attemptId=authority["attemptId"], workerConfigRevision=catalog["revision"])
    append_egress_event(repo.workspace.root, event)


class CorpusExecutor:
    def __init__(self, repo: CorpusRepository, paths=None, *, broker=None, authorization=authorize, analysis_id=None):
        self.repo, self.paths = repo, paths
        self.broker, self.authorization = broker or Broker(), authorization
        self.validator = AssessmentValidator()
        self.analysis = repo.analysis(analysis_id)["analysis_id"]

    def _catalog_worker(self, worker_id):
        catalog = self.broker.catalog()
        workers = [w for w in catalog.get("workers", []) if w.get("id") == worker_id]
        if len(workers) != 1 or not workers[0].get("bulkAvailable"):
            raise ValidationError("select a configured bulk worker")
        return catalog, workers[0]

    def _pin(self, catalog, worker, tier, batch_size):
        # Capacity may change; the scientific model, prompt and schema may not.
        scientific = {k: v for k, v in worker.items() if k != "bulkConcurrencyLimit"}
        pin = {"worker": scientific, "revision": catalog["revision"],
               "promptSha256": hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(),
               "schema": output_schema(), "batchSize": batch_size, "tier": tier}
        key = "executor:" + tier
        with self.repo.connect(write=True) as connection:
            row = connection.execute("SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND task_key=? AND phase='pin'", (self.analysis, key)).fetchone()
            if row and json.loads(row[0]) != pin:
                raise ValidationError("worker, prompt, schema or batching changed; start a new analysis namespace")
            connection.execute("INSERT OR IGNORE INTO processing_state VALUES (?,?,?,'completed',0,?,?)", (self.analysis, key, "pin", canonical(pin), now()))

    def submit(self, worker_id, *, tier="abstract", batch_size=1, concurrency=60, max_jobs=4, job_items=250, task_order=None):
        if not 1 <= batch_size <= 40 or not 1 <= concurrency <= 2**53-1 or not 1 <= max_jobs <= 100 or not 1 <= job_items <= 1000:
            raise ValidationError("invalid batch size, concurrency or job bound")
        catalog, worker = self._catalog_worker(worker_id)
        if batch_size > 1 and self.repo.analysis(self.analysis)["protocol"].get("kind") != "calibration":
            with self.repo.connect() as connection:
                decision = connection.execute("SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND task_key='calibration' AND phase='decision'", (self.analysis,)).fetchone()
            gate = json.loads(decision[0]) if decision else {}
            if not gate.get("complete") or not gate.get("qualified") or batch_size != gate.get("batchSize"):
                raise ValidationError("multi-item production needs a passing measured calibration for this analysis")
            instrument = gate.get("instrument") or {}
            if (instrument.get("worker") != {k:v for k,v in worker.items() if k != "bulkConcurrencyLimit"}
                    or instrument.get("revision") != catalog["revision"]
                    or instrument.get("promptSha256") != hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
                    or instrument.get("schema") != output_schema() or instrument.get("tier") != tier):
                raise ValidationError("calibration instrument differs from the selected worker or protocol")
        self._pin(catalog, worker, tier, batch_size)
        owner = catalog.get("authorization", {}).get("attemptId")
        if not owner:
            raise ValidationError("broker omitted active attempt ownership")
        submitted = []
        with controller_lock(self.repo):
            # A prepared request is persisted before enqueue, so retrying it is safe.
            with self.repo.connect() as connection:
                prepared = [dict(r) for r in connection.execute("SELECT * FROM corpus_batches WHERE analysis_id=? AND state='prepared' AND owner_attempt=? AND worker_id=?", (self.analysis, owner, worker_id))]
            for row in prepared:
                submitted.append(self._enqueue(row, catalog, worker))
            while len(submitted) < max_jobs:
                with self.repo.connect() as connection:
                    active = [r[0] for r in connection.execute("SELECT input_sha256 FROM corpus_batches WHERE state IN ('running','prepared','paused')")]
                reserved = sum(json.loads(self.repo.read_artifact(digest)).get("concurrency", 0) for digest in active)
                allocation = max(0, concurrency - reserved)
                if not allocation:
                    break
                tasks = self.repo.tasks(self.analysis, state="pending", tier=tier, limit=job_items * batch_size)
                if task_order is not None:
                    if len(set(task_order)) != len(task_order):
                        raise ValidationError("task ordering contains duplicate IDs")
                    pending = {t["task_id"]: t for t in self.repo.tasks(self.analysis, state="pending", tier=tier)}
                    tasks = [pending[t] for t in task_order if t in pending][:job_items * batch_size]
                if not tasks:
                    break
                groups, consumed = [], []
                cursor = 0
                while cursor < len(tasks) and len(groups) < job_items:
                    size = 1 if tasks[cursor]["attempts"] else batch_size
                    chunk = tasks[cursor:cursor+size]
                    if any(t["attempts"] for t in chunk):
                        chunk = chunk[:1]
                    messages = build_messages([t["spec"] for t in chunk], tier)
                    group = {"id": "item-" + identity([t["task_id"] for t in chunk], [t["attempts"] for t in chunk]),
                             "input": {"messages": messages}, "taskIds": [t["task_id"] for t in chunk]}
                    if len(canonical(groups + [group]).encode()) > 6 * 1024 * 1024:
                        if not groups:
                            raise ValidationError("single evidence batch exceeds the broker request bound")
                        break
                    groups.append(group); consumed.extend(chunk); cursor += len(chunk)
                payload = {"items": [{"id": g["id"], "input": g["input"]} for g in groups],
                           "schema": output_schema(), "concurrency": allocation, "configId": worker_id}
                batch_id = "batch-" + identity(self.analysis, owner, payload)
                payload["idempotencyKey"] = batch_id
                digest = self.repo.json_artifact(payload, "worker-request")
                with self.repo.connect(write=True) as connection:
                    connection.execute("INSERT OR IGNORE INTO corpus_batches VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?,?)", (
                        batch_id, self.analysis, worker_id, catalog["revision"], canonical(worker), digest,
                        canonical([{k:v for k,v in g.items() if k != "input"} for g in groups]), None, owner, "prepared", now(), now()))
                    for task in consumed:
                        connection.execute("UPDATE corpus_tasks SET state='running',attempts=attempts+1,updated_at=? WHERE task_id=?", (now(), task["task_id"]))
                with self.repo.connect() as connection:
                    row = dict(connection.execute("SELECT * FROM corpus_batches WHERE batch_id=?", (batch_id,)).fetchone())
                submitted.append(self._enqueue(row, catalog, worker))
        return {"submitted": submitted}

    def _enqueue(self, row, catalog, worker):
        if row["worker_id"] != worker["id"] or row["worker_revision"] != catalog["revision"]:
            raise ValidationError("prepared batch belongs to different pinned worker settings")
        without_capacity = lambda w: {k:v for k,v in w.items() if k != "bulkConcurrencyLimit"}
        if without_capacity(json.loads(row["worker_json"])) != without_capacity(worker):
            raise ValidationError("prepared worker settings changed without a revision")
        payload = json.loads(self.repo.read_artifact(row["input_sha256"]))
        ids = [t for g in json.loads(row["task_ids_json"]) for t in g["taskIds"]]
        tasks = self._tasks(ids)
        self.authorization(self.repo, self.paths, catalog, worker, tasks, payload["items"])
        response = self.broker.request("POST", "/bulk/submit?view=summary", payload)
        job = response.get("jobId")
        if not isinstance(job, str) or not re.fullmatch(r"[a-f0-9-]+", job):
            raise ValidationError("broker omitted a valid job ID")
        if response.get("configRevision", row["worker_revision"]) != row["worker_revision"]:
            raise ValidationError("broker submitted a different configuration revision")
        with self.repo.connect(write=True) as connection:
            connection.execute("UPDATE corpus_batches SET job_id=?,state='running',updated_at=? WHERE batch_id=?", (job, now(), row["batch_id"]))
        return {"batchId": row["batch_id"], "jobId": job, "tasks": len(ids)}

    def _tasks(self, ids):
        with self.repo.connect() as connection:
            rows = [dict(connection.execute("SELECT * FROM corpus_tasks WHERE task_id=?", (i,)).fetchone()) for i in ids]
        return [{**r, "spec": json.loads(self.repo.read_artifact(r["spec_sha256"]))} for r in rows]

    def import_record(self, batch, record):
        record = {k:v for k,v in record.items() if k != "revision"}
        groups = {g["id"]: g for g in json.loads(batch["task_ids_json"])}
        if record.get("id") not in groups:
            raise ValidationError("worker result escaped its pinned batch")
        if record.get("state") not in {"completed", "failed", "cancelled", "interrupted"}:
            return
        tasks = self._tasks(groups[record["id"]]["taskIds"])
        receipt_sha = self.repo.json_artifact(record, "worker-response")
        receipt_id = "receipt-" + identity(batch["batch_id"], receipt_sha)
        accepted, rejected = {}, {}
        try:
            if record["state"] != "completed":
                raise AssessmentError("transport_failure", record.get("error", "worker did not complete"), retryable=True)
            if not record.get("providerId") or record.get("model") in (None, "", "unknown") or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("promptSha256", ""))):
                raise AssessmentError("missing_receipt", "worker omitted actual model/provider/prompt provenance")
            pinned = json.loads(batch["worker_json"])
            if record["providerId"] != (pinned.get("providerId") or (pinned.get("modelProvider") or {}).get("id")):
                raise AssessmentError("wrong_provider", "receipt provider differs from the pinned worker")
            request = json.loads(self.repo.read_artifact(batch["input_sha256"]))
            submitted = next(i["input"] for i in request["items"] if i["id"] == record["id"])
            expected_input = hashlib.sha256(json.dumps(submitted, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if record.get("inputSha256") != expected_input:
                raise AssessmentError("wrong_input", "receipt input digest differs from the pinned request")
            validated = self.validator.validate_response([t["spec"] for t in tasks], record.get("output"))
            accepted, rejected = validated["accepted"], validated["rejected"]
        except AssessmentError as exc:
            rejected = {t["task_id"]: {"code": exc.code, "message": str(exc), "retryable": exc.retryable} for t in tasks}
        with self.repo.connect(write=True) as connection:
            if connection.execute("SELECT 1 FROM model_receipts WHERE receipt_id=?", (receipt_id,)).fetchone():
                return
            connection.execute("INSERT INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                receipt_id, self.analysis, record["id"], batch["input_sha256"], receipt_sha,
                record.get("providerId") or "unrecorded", record.get("model") or "unrecorded",
                canonical({"worker": json.loads(batch["worker_json"]), "promptSha256": record.get("promptSha256"), "batchId": batch["batch_id"], "jobId": batch["job_id"]}),
                canonical(record.get("usage") or {}), "completed" if accepted else "failed", batch["created_at"], now()))
            for task in tasks:
                task_id = task["task_id"]
                good = task_id in accepted
                observation = accepted[task_id] if good else rejected[task_id]
                assessment_id = "assessment-" + identity(receipt_id, task_id)
                connection.execute("INSERT OR IGNORE INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", (
                    assessment_id, self.analysis, task["locus_id"], receipt_id, "citation-support-v2", task["tier"],
                    canonical(observation), "accepted" if good else "rejected", now()))
                if good:
                    for quote in observation["evidence"]:
                        connection.execute("INSERT OR IGNORE INTO assessment_evidence VALUES (?,?,?,?)", (assessment_id, quote["unitId"], quote["quote"], quote["unitId"]))
                state = "completed" if good else "pending" if observation.get("retryable") and task["attempts"] < 2 else "blocked"
                # Accepted work cannot be lost if an earlier response is replayed later.
                connection.execute("UPDATE corpus_tasks SET state=?,reason=?,updated_at=? WHERE task_id=? AND state!='completed'", (
                    state, None if good else canonical(observation), now(), task_id))

    def resume(self):
        harvested = []
        with controller_lock(self.repo):
            with self.repo.connect() as connection:
                batches = [dict(r) for r in connection.execute("SELECT * FROM corpus_batches WHERE analysis_id=? AND state IN ('running','prepared','paused')", (self.analysis,))]
            owner = self.broker.catalog().get("authorization", {}).get("attemptId")
            for batch in batches:
                if not batch["job_id"]:
                    if batch["owner_attempt"] != owner:
                        found = self._recover_outbox(batch)
                        if found:
                            batch["job_id"] = found
                            with self.repo.connect(write=True) as connection:
                                connection.execute("UPDATE corpus_batches SET job_id=?,state='running',updated_at=? WHERE batch_id=?", (found, now(), batch["batch_id"]))
                        else:
                            self._release(batch, "interrupted")
                    if not batch["job_id"]:
                        continue
                while True:
                    page = self.broker.request("GET", f"/jobs/{batch['job_id']}/results?after={batch['cursor']}&limit=100")
                    cursor = page.get("nextCursor")
                    if not isinstance(cursor, int) or cursor < batch["cursor"]:
                        raise ValidationError("invalid broker result cursor")
                    for record in page.get("results", []):
                        self.import_record(batch, record)
                    with self.repo.connect(write=True) as connection:
                        connection.execute("UPDATE corpus_batches SET cursor=?,updated_at=? WHERE batch_id=?", (cursor, now(), batch["batch_id"]))
                    if page.get("hasMore") and cursor == batch["cursor"]:
                        raise ValidationError("broker result cursor failed to advance")
                    batch["cursor"] = cursor
                    if not page.get("hasMore"):
                        break
                summary = self.broker.request("GET", f"/jobs/{batch['job_id']}?view=summary")
                state = summary.get("state")
                if state in {"completed", "failed", "cancelled", "interrupted", "paused"}:
                    # Drain once more after observing terminal state to close completion races.
                    page = self.broker.request("GET", f"/jobs/{batch['job_id']}/results?after={batch['cursor']}&limit=100")
                    for record in page.get("results", []):
                        self.import_record(batch, record)
                    if page.get("hasMore"):
                        continue
                    if state == "paused" and batch["owner_attempt"] == owner:
                        with self.repo.connect(write=True) as connection:
                            connection.execute("UPDATE corpus_batches SET state='paused',updated_at=? WHERE batch_id=?", (now(), batch["batch_id"]))
                    else:
                        state = state if state != "paused" else "interrupted"
                        self._release(batch, state)
                harvested.append({"jobId": batch["job_id"], "state": state, "cursor": batch["cursor"]})
        return {"jobs": harvested, **self.repo.status()}

    def _recover_outbox(self, batch):
        """Reconcile an accepted POST whose response was lost, even across attempts."""
        payload = json.loads(self.repo.read_artifact(batch["input_sha256"]))
        expected = {item["id"]: hashlib.sha256(canonical(item["input"]).encode()).hexdigest() for item in payload["items"]}
        after, matches = 0, []
        while True:
            page = self.broker.request("GET", f"/jobs?view=summary&after={after}&limit=100")
            for job in page.get("jobs", []):
                if job.get("attemptId") != batch["owner_attempt"] or job.get("configId") != batch["worker_id"] or job.get("configRevision") != batch["worker_revision"]:
                    continue
                seen, cursor = {}, 0
                while True:
                    results = self.broker.request("GET", f"/jobs/{job['jobId']}/results?after={cursor}&limit=100")
                    for item in results.get("results", []):
                        seen[item["id"]] = item.get("inputSha256")
                    if not results.get("hasMore"):
                        break
                    if results.get("nextCursor", cursor) <= cursor:
                        raise ValidationError("nonadvancing outbox result cursor")
                    cursor = results["nextCursor"]
                if seen == expected:
                    matches.append(job["jobId"])
            if not page.get("hasMore"):
                break
            if page.get("nextCursor", after) <= after:
                raise ValidationError("nonadvancing outbox job cursor")
            after = page["nextCursor"]
        if len(matches) > 1:
            raise ValidationError("multiple broker jobs match the same prepared batch; reconcile explicitly")
        return matches[0] if matches else None

    def _release(self, batch, state):
        with self.repo.connect(write=True) as connection:
            connection.execute("UPDATE corpus_batches SET state=?,updated_at=? WHERE batch_id=?", (state, now(), batch["batch_id"]))
            for group in json.loads(batch["task_ids_json"]):
                for task_id in group["taskIds"]:
                    connection.execute("UPDATE corpus_tasks SET state=CASE WHEN attempts<2 THEN 'pending' ELSE 'blocked' END,reason=?,updated_at=? WHERE task_id=? AND state='running'", (
                        "worker job ended without an accepted item: " + state, now(), task_id))
