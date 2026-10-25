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
from .corpus_store import CorpusRepository, canonical, identity, json_safe
from .corpus_library import now
from .corpus_assessment import AssessmentValidator, AssessmentError, build_messages, output_schema, PROMPT_PATH
from .errors import ConfidentialityError, ReviewError, ValidationError
from .public_literature import broker_catalog
from .util import is_within

# Jobs in flight per analysis; each is sized to the worker's capacity-group limit and
# the broker's limiter queues items globally, so the next job starts while one drains.
MAX_JOBS_IN_FLIGHT = 3
RESULT_PAGE_RECORDS = 100
TERMINAL_RECORD_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled", "interrupted", "paused"})
ACTIVE_BATCH_STATES = ("running", "prepared", "paused")


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
        self._batches = {}

    def _catalog_worker(self, worker_id):
        catalog = self.broker.catalog()
        workers = [w for w in catalog.get("workers", []) if w.get("id") == worker_id]
        if len(workers) != 1 or not workers[0].get("bulkAvailable"):
            raise ValidationError("select a configured bulk worker")
        return catalog, workers[0]

    def _pin(self, catalog, worker, tier, batch_size):
        # Capacity may change; the scientific model, prompt and schema may not.
        revision = self.repo.analysis(self.analysis)["code_revision"]
        if revision and revision.startswith("source-tree-sha256:"):
            from .corpus_export import implementation_manifest
            if revision != "source-tree-sha256:" + implementation_manifest()["sha256"]:
                raise ValidationError("implementation changed; branch a new analysis with the current code")
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

    @staticmethod
    def _job_concurrency(worker, requested):
        """A job asks for the worker's capacity-group limit, never more than requested."""
        limit = worker.get("bulkConcurrencyLimit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1:
            return min(requested, limit)
        return requested

    def in_flight(self):
        """Batches of this analysis that hold tasks: prepared, running or paused."""
        with self.repo.connect() as connection:
            return connection.execute("SELECT count(*) FROM corpus_batches WHERE analysis_id=? AND state IN (?,?,?)",
                                      (self.analysis, *ACTIVE_BATCH_STATES)).fetchone()[0]

    def _select(self, tier, wanted, task_order, eligible):
        """Pending tasks in queue order that the worker may receive, and how many were held back."""
        if task_order is not None:
            if len(set(task_order)) != len(task_order):
                raise ValidationError("task ordering contains duplicate IDs")
            pending = {t["task_id"]: t for t in self.repo.tasks(self.analysis, state="pending", tier=tier)}
            ordered = [pending[t] for t in task_order if t in pending]
            allowed = [task for task in ordered if eligible(task)]
            return allowed[:wanted], len(ordered) - len(allowed)
        selected, held, after = [], 0, None
        while len(selected) < wanted:
            rows = self.repo.tasks(self.analysis, state="pending", tier=tier, limit=wanted, after=after)
            if not rows:
                break
            after = rows[-1]["task_id"]
            for task in rows:
                if not eligible(task):
                    held += 1
                elif len(selected) < wanted:
                    selected.append(task)
        return selected, held

    def submit(self, worker_id, *, tier="abstract", batch_size=1, concurrency=60, max_jobs=MAX_JOBS_IN_FLIGHT, job_items=250, task_order=None):
        """Keep up to min(max_jobs, 3) jobs of this analysis in flight, each sized to the group limit."""
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
        limit, job_concurrency = min(max_jobs, MAX_JOBS_IN_FLIGHT), self._job_concurrency(worker, concurrency)
        submitted, held = [], 0
        with controller_lock(self.repo):
            # A prepared request is persisted before enqueue, so retrying it is safe.
            with self.repo.connect() as connection:
                prepared = [dict(r) for r in connection.execute("SELECT * FROM corpus_batches WHERE analysis_id=? AND state='prepared' AND owner_attempt=? AND worker_id=?", (self.analysis, owner, worker_id))]
            for row in prepared:
                submitted.append(self._enqueue(row, catalog, worker))
            while self.in_flight() < limit:
                tasks, held = self._select(tier, job_items * batch_size, task_order, lambda task: True)
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
                           "schema": output_schema(), "concurrency": job_concurrency, "configId": worker_id}
                batch_id = "batch-" + identity(self.analysis, owner, payload)
                payload["idempotencyKey"] = batch_id
                digest = self.repo.json_artifact(payload, "worker-request")
                with self.repo.connect(write=True) as connection:
                    connection.execute("INSERT OR IGNORE INTO corpus_batches VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?,?)", (
                        batch_id, self.analysis, worker_id, catalog["revision"], canonical(worker), digest,
                        canonical([{k:v for k,v in g.items() if k != "input"} for g in groups]), None, owner, "prepared", now(), now()))
                    connection.executemany("UPDATE corpus_tasks SET state='running',attempts=attempts+1,updated_at=? WHERE task_id=?",
                                           [(now(), task["task_id"]) for task in consumed])
                with self.repo.connect() as connection:
                    row = dict(connection.execute("SELECT * FROM corpus_batches WHERE batch_id=?", (batch_id,)).fetchone())
                submitted.append(self._enqueue(row, catalog, worker))
        return {"submitted": submitted, "heldForRouting": held}

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
        """Task rows with verified specs, loaded through one connection."""
        if not ids:
            return []
        with self.repo.connect() as connection:
            rows = {}
            for start in range(0, len(ids), 5000):
                rows.update((r["task_id"], dict(r)) for r in connection.execute(
                    "SELECT * FROM corpus_tasks WHERE task_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(ids[start:start + 5000]),)))
            if any(task_id not in rows for task_id in ids):
                raise ValidationError("batch refers to a task that is not stored")
            bodies = self.repo.read_artifacts([rows[i]["spec_sha256"] for i in ids], connection=connection)
        return [{**rows[i], "spec": json.loads(bodies[rows[i]["spec_sha256"]])} for i in ids]

    def _batch(self, batch):
        """Parse a pinned batch request once: item groups, expected input digests and worker."""
        key = (batch["batch_id"], batch["input_sha256"])
        context = self._batches.get(key)
        if context is None:
            request = json.loads(self.repo.read_artifact(batch["input_sha256"]))
            pinned = json.loads(batch["worker_json"])
            context = {
                "groups": {g["id"]: g for g in json.loads(batch["task_ids_json"])},
                "inputs": {item["id"]: hashlib.sha256(json.dumps(item["input"], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                           for item in request["items"]},
                "worker": pinned,
                "provider": pinned.get("providerId") or (pinned.get("modelProvider") or {}).get("id"),
            }
            if len(self._batches) >= 8:
                self._batches.clear()
            self._batches[key] = context
        return context

    def _judge(self, context, record, tasks):
        accepted, rejected = {}, {}
        try:
            if record["state"] != "completed":
                raise AssessmentError("transport_failure", record.get("error", "worker did not complete"), retryable=True)
            if not record.get("providerId") or record.get("model") in (None, "", "unknown") or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("promptSha256", ""))):
                raise AssessmentError("missing_receipt", "worker omitted actual model/provider/prompt provenance")
            if record["providerId"] != context["provider"]:
                raise AssessmentError("wrong_provider", "receipt provider differs from the pinned worker")
            if record.get("inputSha256") != context["inputs"].get(record["id"]):
                raise AssessmentError("wrong_input", "receipt input digest differs from the pinned request")
            validated = self.validator.validate_response([t["spec"] for t in tasks], record.get("output"))
            accepted, rejected = validated["accepted"], validated["rejected"]
        except AssessmentError as exc:
            rejected = {t["task_id"]: {"code": exc.code, "message": str(exc), "retryable": exc.retryable} for t in tasks}
        return accepted, rejected

    def import_record(self, batch, record):
        self.import_page(batch, [record])

    def import_page(self, batch, records, *, cursor=None):
        """Import one result page in one transaction, together with the advanced cursor.

        Receipts are content addressed, so replaying a page imports nothing twice and
        an accepted task never returns to another state.
        """
        context = self._batch(batch)
        terminal = []
        for record in records:
            record = {k: v for k, v in record.items() if k != "revision"}
            if record.get("id") not in context["groups"]:
                raise ValidationError("worker result escaped its pinned batch")
            if record.get("state") in TERMINAL_RECORD_STATES:
                terminal.append(record)
        loaded = {task["task_id"]: task for task in self._tasks(
            list(dict.fromkeys(t for r in terminal for t in context["groups"][r["id"]]["taskIds"])))}
        receipts = self.repo.stage_artifacts((canonical(json_safe(record)).encode(), "worker-response", "application/json")
                                             for record in terminal)
        judged = []
        for record, receipt in zip(terminal, receipts):
            tasks = [loaded[t] for t in context["groups"][record["id"]]["taskIds"]]
            judged.append((record, receipt[0], tasks, *self._judge(context, record, tasks)))
        with self.repo.connect(write=True) as connection:
            self.repo.register_artifacts(connection, receipts)
            ids = ["receipt-" + identity(batch["batch_id"], receipt_sha) for _, receipt_sha, *_ in judged]
            known = {row[0] for row in connection.execute(
                "SELECT receipt_id FROM model_receipts WHERE receipt_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
            receipt_rows, assessment_rows, evidence_rows, state_rows = [], [], [], []
            for receipt_id, (record, receipt_sha, tasks, accepted, rejected) in zip(ids, judged):
                if receipt_id in known:
                    continue
                known.add(receipt_id)
                receipt_rows.append((
                    receipt_id, self.analysis, record["id"], batch["input_sha256"], receipt_sha,
                    record.get("providerId") or "unrecorded", record.get("model") or "unrecorded",
                    canonical({"worker": context["worker"], "promptSha256": record.get("promptSha256"), "batchId": batch["batch_id"], "jobId": batch["job_id"]}),
                    canonical(record.get("usage") or {}), "completed" if accepted else "failed", batch["created_at"], now()))
                for task in tasks:
                    task_id = task["task_id"]
                    good = task_id in accepted
                    observation = accepted[task_id] if good else rejected[task_id]
                    assessment_id = "assessment-" + identity(receipt_id, task_id)
                    assessment_rows.append((assessment_id, self.analysis, task["locus_id"], receipt_id, "citation-support-v2",
                                            task["tier"], canonical(observation), "accepted" if good else "rejected", now()))
                    if good:
                        evidence_rows += [(assessment_id, quote["unitId"], quote["quote"], quote["unitId"]) for quote in observation["evidence"]]
                    state = "completed" if good else "pending" if observation.get("retryable") and task["attempts"] < 2 else "blocked"
                    state_rows.append((state, None if good else canonical(observation), now(), task_id))
            connection.executemany("INSERT INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", receipt_rows)
            connection.executemany("INSERT OR IGNORE INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", assessment_rows)
            connection.executemany("INSERT OR IGNORE INTO assessment_evidence VALUES (?,?,?,?)", evidence_rows)
            # Accepted work cannot be lost if an earlier response is replayed later.
            connection.executemany("UPDATE corpus_tasks SET state=?,reason=?,updated_at=? WHERE task_id=? AND state!='completed'", state_rows)
            if cursor is not None:
                connection.execute("UPDATE corpus_batches SET cursor=?,updated_at=? WHERE batch_id=?", (cursor, now(), batch["batch_id"]))

    def _results(self, batch):
        page = self.broker.request("GET", f"/jobs/{batch['job_id']}/results?after={batch['cursor']}&limit={RESULT_PAGE_RECORDS}")
        cursor = page.get("nextCursor")
        valid = isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= batch["cursor"]
        return page, cursor if valid else None

    def resume(self):
        harvested = []
        with controller_lock(self.repo):
            with self.repo.connect() as connection:
                batches = [dict(r) for r in connection.execute("SELECT * FROM corpus_batches WHERE analysis_id=? AND state IN (?,?,?)", (self.analysis, *ACTIVE_BATCH_STATES))]
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
                    page, cursor = self._results(batch)
                    if cursor is None:
                        raise ValidationError("invalid broker result cursor")
                    self.import_page(batch, page.get("results", []), cursor=cursor)
                    if page.get("hasMore") and cursor == batch["cursor"]:
                        raise ValidationError("broker result cursor failed to advance")
                    batch["cursor"] = cursor
                    if not page.get("hasMore"):
                        break
                summary = self.broker.request("GET", f"/jobs/{batch['job_id']}?view=summary")
                state = summary.get("state")
                if state in TERMINAL_JOB_STATES:
                    # Drain once more after observing terminal state to close completion races.
                    page, cursor = self._results(batch)
                    self.import_page(batch, page.get("results", []), cursor=cursor)
                    if cursor is not None:
                        batch["cursor"] = cursor
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
            connection.executemany("UPDATE corpus_tasks SET state=CASE WHEN attempts<2 THEN 'pending' ELSE 'blocked' END,reason=?,updated_at=? WHERE task_id=? AND state='running'", [
                ("worker job ended without an accepted item: " + state, now(), task_id)
                for group in json.loads(batch["task_ids_json"]) for task_id in group["taskIds"]])
