# SPDX-License-Identifier: Apache-2.0
"""Exercise durable dispatch with real evidence storage and an in-memory broker.

Only the scoped HTTP transport is replaced: prompts, assessment validation,
foreign keys, immutable artifacts, task transitions and receipt imports are real.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ocsl_review.confidentiality import EgressPlan
from ocsl_review.corpus_assessment import build_messages
from ocsl_review.corpus_dispatch import CorpusExecutor, authorize
from ocsl_review.corpus_library import add_analysis
from ocsl_review.corpus_store import CorpusRepository, canonical
from ocsl_review.errors import ConfidentialityError, ReviewError, ValidationError


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class FakeBroker:
    """Attempt-scoped idempotency, summaries and revisioned results; no network."""

    def __init__(self, workspace):
        self.settings = {
            "revision": 1,
            "authorization": {"attemptId": "owner-1", "workspace": str(workspace),
                              "publicLiteratureAllowed": True},
            "workers": [{"id": "w1", "assignment": "Judge supplied public evidence",
                         "agent": "codex", "effort": "none", "bulkAvailable": True,
                         "bulkConcurrencyLimit": 60, "evidencePolicy": "public-literature",
                         "providerId": "remote_qwen38_none", "model": "chat",
                         "modelProvider": {"id": "remote_qwen38_none", "model": "chat"}}],
        }
        self.jobs = {}
        self.calls = []
        self.idempotency = {}
        self.lose_next_submit_response = False

    def catalog(self):
        return copy.deepcopy(self.settings)

    def request(self, method, route, payload=None):
        self.calls.append((method, route, copy.deepcopy(payload)))
        url = urlsplit(route)
        query = parse_qs(url.query)
        if method == "POST" and url.path == "/bulk/submit":
            key = (self.settings["authorization"]["attemptId"], payload["idempotencyKey"])
            if key not in self.idempotency:
                job_id = f"00000000-0000-0000-0000-{len(self.jobs) + 1:012d}"
                self.idempotency[key] = job_id
                # Match real JSON serialization rather than preserving Python key order.
                request = json.loads(canonical(payload))
                self.jobs[job_id] = {
                    "jobId": job_id, "attemptId": key[0], "configId": payload["configId"],
                    "configRevision": self.settings["revision"], "state": "running",
                    "request": request,
                    "records": [{"id": item["id"], "state": "queued", "revision": i + 1,
                                 "inputSha256": digest(item["input"])}
                                for i, item in enumerate(request["items"])],
                }
            job = self.jobs[self.idempotency[key]]
            if self.lose_next_submit_response:
                self.lose_next_submit_response = False
                raise ReviewError("synthetic lost POST response after durable acceptance")
            return self.summary(job)
        if method == "GET" and url.path == "/jobs":
            after = int(query.get("after", [0])[0])
            limit = int(query.get("limit", [100])[0])
            jobs = list(self.jobs.values())[after:after + limit]
            cursor = after + len(jobs)
            return {"jobs": [self.summary(job) for job in jobs], "nextCursor": cursor,
                    "hasMore": cursor < len(self.jobs)}
        if method == "GET" and url.path.startswith("/jobs/"):
            job = self.jobs[url.path.split("/")[2]]
            if url.path.endswith("/results"):
                after = int(query.get("after", [0])[0])
                limit = int(query.get("limit", [100])[0])
                latest = {r["id"]: r for r in job["records"]}
                records = sorted((r for r in latest.values() if r["revision"] > after),
                                 key=lambda r: r["revision"])
                page = records[:limit]
                return {"results": copy.deepcopy(page),
                        "nextCursor": page[-1]["revision"] if page else after,
                        "hasMore": len(records) > limit}
            return self.summary(job)
        raise AssertionError(f"unexpected broker request: {method} {route}")

    @staticmethod
    def summary(job):
        return {key: job[key] for key in
                ("jobId", "attemptId", "configId", "configRevision", "state")}

    def complete(self, job_id, malformed=(), receipt_overrides=None):
        job = self.jobs[job_id]
        for item in job["request"]["items"]:
            tasks = json.loads(item["input"]["messages"][1]["content"])["items"]
            observations = []
            for task in tasks:
                observation = {
                    "id": task["id"], "supportVerdict": "direct-support",
                    "rationale": "The supplied abstract explicitly states the sample size.",
                    "evidence": [{"unitId": task["evidenceUnits"][0]["id"],
                                  "quote": "We included 60 patients."}],
                    "escalate": False, "escalationReason": "",
                }
                if task["id"] in malformed:
                    observation.pop("rationale")
                observations.append(observation)
            record = {"id": item["id"], "state": "completed",
                      "providerId": "remote_qwen38_none", "model": "Qwen-fixture",
                      "promptSha256": "a" * 64, "inputSha256": digest(item["input"]),
                      "output": {"items": observations},
                      "usage": {"inputTokens": 100, "outputTokens": 25}}
            record.update(receipt_overrides or {})
            record["revision"] = max((r["revision"] for r in job["records"]), default=0) + 1
            job["records"].append(record)
        job["state"] = "completed"


class CorpusDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = CorpusRepository.create(self.root, "work/corpus", "Fixture review",
                                            "main", {"version": 2, "kind": "calibration"})
        self.broker = FakeBroker(self.root)
        self.executor = CorpusExecutor(self.repo, broker=self.broker,
                                       authorization=lambda *args: None)
        self.review_id = self.repo.review({"doi": "10.1234/review", "title": "Synthetic review"})
        self.work_id = self.repo.work({"doi": "10.1234/source", "title": "Synthetic trial"})
        self.manuscript_sha = self.repo.json_artifact({"confidential": False}, "review-manuscript")
        self.repo.document(self.work_id, {"text": "We included 60 patients. Treatment used qPCR.",
                                         "complete": True,
                                         "sourceUrl": "https://europepmc.org/article/MED/123"},
                           "abstract", "europe-pmc")
        self.evidence = self.repo.evidence(self.work_id, "abstract")
        with self.repo.connect(write=True) as connection:
            connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", (
                "entry-1", self.review_id, "1", "Synthetic trial", "{}", self.work_id,
                "verified", self.evidence["sourceSha256"]))
        self.loci = 0

    def add_tasks(self, count, analysis="main", proof_provider="europe-pmc"):
        ids = []
        for _ in range(count):
            self.loci += 1
            locus = f"locus-{self.loci}"
            claim = "The study included 60 patients."
            with self.repo.connect(write=True) as connection:
                connection.execute("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", (
                    locus, "entry-1", self.manuscript_sha, f"paragraph-{self.loci}",
                    claim, "This trial is discussed here.", "fixture"))
            spec = {**self.evidence, "claim": claim, "claimType": "numeric",
                    "citationLocus": {"id": locus, "bindingStatus": "exact"},
                    "context": ["This trial is discussed here."],
                    "citedWork": {"title": "Synthetic trial"},
                    "identifiers": {"doi": "10.1234/source"}, "identityVerified": True,
                    "reviewProof": {"manuscriptSha256": self.manuscript_sha, "public": True,
                                    "provider": proof_provider,
                                    "sourceUrl": "https://doi.org/10.1234/review" if proof_provider == "retained-structured-source"
                                    else "https://europepmc.org/article/MED/456",
                                    "publication": {"doi": "10.1234/review"}}}
            ids.append(self.repo.add_task(analysis, locus, "abstract", spec))
        return ids

    def add_analysis(self, name, protocol=None):
        with self.repo.connect(write=True) as connection:
            add_analysis(connection, self.repo.analysis()["corpus_id"], "Another question", name,
                         protocol=protocol or {"version": 2, "kind": "calibration"})
        return CorpusExecutor(self.repo, broker=self.broker, authorization=lambda *args: None,
                              analysis_id=name)

    def rows(self, table):
        with self.repo.connect() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def states(self):
        return {row["task_id"]: (row["state"], row["attempts"]) for row in self.rows("corpus_tasks")}

    def test_valid_peers_persist_and_only_malformed_task_gets_one_singleton_repair(self):
        tasks = self.add_tasks(4)
        first = self.executor.submit("w1", batch_size=4)["submitted"][0]
        self.broker.complete(first["jobId"], malformed=[tasks[1]])
        self.executor.resume()
        self.assertEqual(self.states()[tasks[1]], ("pending", 1))
        for task in set(tasks) - {tasks[1]}:
            self.assertEqual(self.states()[task], ("completed", 1))
        self.assertEqual(len([r for r in self.rows("assessments") if r["validation_state"] == "accepted"]), 3)
        self.assertEqual(len(self.rows("assessment_evidence")), 3)
        repair = self.executor.submit("w1", batch_size=4)["submitted"][0]
        items = self.broker.jobs[repair["jobId"]]["request"]["items"]
        self.assertEqual(len(items), 1)
        repaired_tasks = json.loads(items[0]["input"]["messages"][1]["content"])["items"]
        self.assertEqual([task["id"] for task in repaired_tasks], [tasks[1]])
        self.broker.complete(repair["jobId"], malformed=[tasks[1]])
        self.executor.resume()
        self.assertEqual(self.states()[tasks[1]], ("blocked", 2))
        self.assertEqual(self.executor.submit("w1", batch_size=4)["submitted"], [])
        self.assertEqual(len(self.broker.jobs), 2)
        self.repo.library.verify()

    def test_replayed_receipt_deduplicates_and_old_failure_cannot_undo_acceptance(self):
        task = self.add_tasks(1)[0]
        job = self.executor.submit("w1")["submitted"][0]["jobId"]
        self.broker.complete(job)
        self.executor.resume()
        batch = self.rows("corpus_batches")[0]
        record = self.broker.jobs[job]["records"][-1]
        self.executor.import_record(batch, {**record, "revision": record["revision"] + 100})
        self.assertEqual(len(self.rows("model_receipts")), 1)
        self.assertEqual(len(self.rows("assessments")), 1)
        self.assertEqual(len(self.rows("assessment_evidence")), 1)
        self.executor.import_record(batch, {**record, "state": "failed", "error": "older transport failure"})
        self.assertEqual(self.states()[task], ("completed", 1))
        self.assertEqual(len(self.rows("model_receipts")), 2)
        self.assertEqual(len([r for r in self.rows("assessments") if r["validation_state"] == "accepted"]), 1)

    def test_up_to_three_jobs_in_flight_sized_to_group_limit_and_scoped_to_analysis(self):
        self.broker.settings["workers"][0]["bulkConcurrencyLimit"] = 30
        tasks = self.add_tasks(7)
        other = self.add_analysis("second")
        self.add_tasks(1, "second")
        jobs = [job["jobId"] for job in self.executor.submit("w1", concurrency=60, job_items=2)["submitted"]]
        self.assertEqual(len(jobs), 3)
        self.assertEqual([len(self.broker.jobs[j]["request"]["items"]) for j in jobs], [2, 2, 2])
        self.assertEqual({self.broker.jobs[j]["request"]["concurrency"] for j in jobs}, {30})
        # Another analysis is neither blocked by nor charged for these jobs.
        second = other.submit("w1", concurrency=20)["submitted"]
        self.assertEqual(self.broker.jobs[second[0]["jobId"]]["request"]["concurrency"], 20)
        self.assertEqual(self.executor.submit("w1", job_items=2)["submitted"], [])
        self.assertEqual(self.executor.in_flight(), 3)
        self.assertEqual(sum(state == ("pending", 0) for state in self.states().values()), 1)
        self.broker.complete(jobs[0])
        self.executor.resume()
        fourth = self.executor.submit("w1", job_items=2, max_jobs=10)["submitted"]
        self.assertEqual(len(fourth), 1)
        self.assertEqual(self.executor.in_flight(), 3)
        for job in jobs[1:] + [fourth[0]["jobId"]]:
            self.broker.complete(job)
        self.executor.resume()
        self.assertEqual({self.states()[task] for task in tasks}, {("completed", 1)})
        self.assertEqual(self.executor.in_flight(), 0)

    def write_statements(self):
        """Statements per write connection, to count page transactions."""
        transactions, original = [], CorpusRepository.connect

        @contextmanager
        def connect(repo, *, write=False):
            with original(repo, write=write) as connection:
                if write:
                    statements = []
                    transactions.append(statements)
                    connection.set_trace_callback(statements.append)
                yield connection
        return transactions, connect

    def test_each_result_page_is_one_transaction_with_its_cursor_and_replays_idempotently(self):
        tasks = self.add_tasks(5)
        job = self.executor.submit("w1")["submitted"][0]["jobId"]
        self.broker.complete(job)
        batch = self.rows("corpus_batches")[0]
        with self.repo.connect(write=True) as connection:
            connection.execute("CREATE TRIGGER injected BEFORE UPDATE OF cursor ON corpus_batches "
                               "BEGIN SELECT RAISE(ABORT,'injected cursor failure'); END")
        with patch("ocsl_review.corpus_dispatch.RESULT_PAGE_RECORDS", 2):
            with self.assertRaisesRegex(Exception, "injected cursor failure"):
                self.executor.resume()
        self.assertEqual(self.rows("model_receipts"), [])
        self.assertEqual({self.states()[t] for t in tasks}, {("running", 1)})
        with self.repo.connect(write=True) as connection:
            connection.execute("DROP TRIGGER injected")
        transactions, connect = self.write_statements()
        reads, original_read = [], CorpusRepository.read_artifact

        def counting_read(repo, digest):
            reads.append(digest)
            return original_read(repo, digest)

        fresh = CorpusExecutor(self.repo, broker=self.broker, authorization=lambda *args: None)
        with patch("ocsl_review.corpus_dispatch.RESULT_PAGE_RECORDS", 2), \
                patch.object(CorpusRepository, "connect", connect), \
                patch.object(CorpusRepository, "read_artifact", counting_read):
            fresh.resume()
        pages = [t for t in transactions if any("INSERT INTO model_receipts" in s for s in t)]
        self.assertEqual(len(pages), 3)
        self.assertTrue(all(any("UPDATE corpus_batches SET cursor" in s for s in t) for t in pages))
        self.assertEqual(reads.count(batch["input_sha256"]), 1, "the batch request is parsed once")
        self.assertEqual({self.states()[t] for t in tasks}, {("completed", 1)})
        records = [r for r in self.broker.jobs[job]["records"] if r["state"] == "completed"]
        before = {table: len(self.rows(table)) for table in ("model_receipts", "assessments", "assessment_evidence")}
        self.executor.import_page(self.rows("corpus_batches")[0], records + records[:2])
        self.assertEqual({table: len(self.rows(table)) for table in before}, before)
        self.assertEqual(before["model_receipts"], 5)
        self.repo.library.verify()

    def test_paused_job_is_not_resubmitted_until_new_approved_owner(self):
        task = self.add_tasks(1)[0]
        job = self.executor.submit("w1")["submitted"][0]["jobId"]
        self.broker.jobs[job]["state"] = "paused"
        self.executor.resume()
        self.executor.resume()
        self.assertEqual(self.rows("corpus_batches")[0]["state"], "paused")
        self.assertEqual(self.states()[task], ("running", 1))
        self.assertEqual(self.executor.submit("w1")["submitted"], [])
        self.assertEqual(len(self.broker.jobs), 1)
        self.broker.settings["authorization"]["attemptId"] = "owner-2"
        resumed = self.executor.resume()
        self.assertFalse(any(job["state"] == "paused" for job in resumed["jobs"]),
                         "the execute loop must not stop again after approved owner recovery")
        self.assertEqual(self.states()[task], ("pending", 1))
        self.executor.submit("w1")
        self.assertEqual(self.states()[task], ("running", 2))
        self.assertEqual(len(self.broker.jobs), 2)

    def test_prepared_outbox_replays_idempotently_without_incrementing_attempt(self):
        task = self.add_tasks(1)[0]
        self.broker.lose_next_submit_response = True
        with self.assertRaisesRegex(ReviewError, "lost POST response"):
            self.executor.submit("w1")
        batch = self.rows("corpus_batches")[0]
        self.assertEqual((batch["state"], batch["job_id"]), ("prepared", None))
        self.executor.submit("w1")
        self.assertEqual(len(self.broker.jobs), 1)
        posts = [call[2] for call in self.broker.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(self.states()[task], ("running", 1))
        self.broker.complete(next(iter(self.broker.jobs)))
        self.executor.resume()
        self.assertEqual(self.states()[task], ("completed", 1))

    def test_cross_attempt_outbox_recovers_accepted_job_before_resubmission(self):
        task = self.add_tasks(1)[0]
        self.broker.lose_next_submit_response = True
        with self.assertRaises(ReviewError):
            self.executor.submit("w1")
        job = next(iter(self.broker.jobs))
        self.broker.complete(job)
        self.broker.settings["authorization"]["attemptId"] = "owner-2"
        self.executor.resume()
        self.assertEqual(self.states()[task], ("completed", 1))
        self.assertEqual(self.rows("corpus_batches")[0]["job_id"], job)
        self.assertEqual(self.executor.submit("w1")["submitted"], [])
        self.assertEqual(len(self.broker.jobs), 1)

    def test_cross_attempt_outbox_attaches_still_running_job_without_duplicate_work(self):
        task = self.add_tasks(1)[0]
        self.broker.lose_next_submit_response = True
        with self.assertRaises(ReviewError):
            self.executor.submit("w1")
        job = next(iter(self.broker.jobs))
        self.broker.settings["authorization"]["attemptId"] = "owner-2"
        self.executor.resume()
        self.assertEqual(self.rows("corpus_batches")[0]["job_id"], job)
        self.assertEqual(self.states()[task], ("running", 1))
        self.assertEqual(self.executor.submit("w1")["submitted"], [])
        self.assertEqual(len(self.broker.jobs), 1)

    def test_outbox_matching_uses_complete_input_hashes_not_just_owner_and_config(self):
        self.add_tasks(2)
        self.broker.lose_next_submit_response = True
        with self.assertRaises(ReviewError):
            self.executor.submit("w1")
        job = next(iter(self.broker.jobs))
        unrelated = copy.deepcopy(self.broker.jobs[job])
        unrelated["jobId"] = "00000000-0000-0000-0000-000000000002"
        unrelated["records"][0]["inputSha256"] = "0" * 64
        self.broker.jobs[unrelated["jobId"]] = unrelated
        self.broker.settings["authorization"]["attemptId"] = "owner-2"
        self.executor.resume()
        self.assertEqual(self.rows("corpus_batches")[0]["job_id"], job)
        self.assertEqual(len([c for c in self.broker.calls if c[0] == "POST"]), 1)

    def test_prepared_snapshot_allows_capacity_change_but_rejects_scientific_change(self):
        self.add_tasks(1)
        self.broker.lose_next_submit_response = True
        with self.assertRaises(ReviewError):
            self.executor.submit("w1")
        original = self.rows("corpus_batches")[0]["worker_json"]
        self.broker.settings["workers"][0]["assignment"] += " Changed."
        with self.assertRaisesRegex(ValidationError, "changed"):
            self.executor.submit("w1")
        self.assertEqual(len([c for c in self.broker.calls if c[0] == "POST"]), 1)
        self.broker.settings["workers"][0] = json.loads(original)
        self.broker.settings["workers"][0]["bulkConcurrencyLimit"] = 30
        self.executor.submit("w1")
        self.assertEqual(self.rows("corpus_batches")[0]["worker_json"], original)
        self.assertEqual(len(self.broker.jobs), 1)

    def test_receipt_provider_and_input_are_required_and_bound_to_request(self):
        cases = (({"providerId": "different-provider"}, "wrong_provider"),
                 ({"inputSha256": "f" * 64}, "wrong_input"),
                 ({"inputSha256": None}, "wrong_input"),
                 ({"model": "unknown"}, "missing_receipt"))
        for index, (override, code) in enumerate(cases):
            with self.subTest(code=code, override=override):
                name = f"receipt-{index}"
                executor = self.add_analysis(name)
                task = self.add_tasks(1, name)[0]
                job = executor.submit("w1")["submitted"][0]["jobId"]
                self.broker.complete(job, receipt_overrides=override)
                executor.resume()
                self.assertEqual(self.states()[task], ("blocked", 1))
                row = next(r for r in self.rows("corpus_tasks") if r["task_id"] == task)
                self.assertEqual(json.loads(row["reason"])["code"], code)
        self.assertFalse(any(r["validation_state"] == "accepted" for r in self.rows("assessments")))

    def test_broker_configuration_revision_mismatch_leaves_prepared_receipt(self):
        self.add_tasks(1)
        request = self.broker.request

        def wrong_revision(method, route, payload=None):
            response = request(method, route, payload)
            return {**response, "configRevision": 99} if method == "POST" else response

        with patch.object(self.broker, "request", side_effect=wrong_revision):
            with self.assertRaisesRegex(ValidationError, "different configuration revision"):
                self.executor.submit("w1")
        self.assertEqual(self.rows("corpus_batches")[0]["state"], "prepared")
        self.assertEqual(self.rows("model_receipts"), [])

    def test_production_batching_requires_matching_passing_calibration(self):
        executor = self.add_analysis("production", {"version": 2})
        self.add_tasks(4, "production")
        with self.assertRaisesRegex(ValidationError, "passing measured calibration"):
            executor.submit("w1", batch_size=4)
        self.assertEqual(self.broker.jobs, {})

    def test_passing_calibration_does_not_authorize_a_different_scientific_worker(self):
        self.add_tasks(4)
        job = self.executor.submit("w1", batch_size=4)["submitted"][0]["jobId"]
        self.broker.complete(job)
        self.executor.resume()
        pin = next(row for row in self.rows("processing_state") if row["phase"] == "pin")
        executor = self.add_analysis("production", {"version": 2})
        self.add_tasks(4, "production")
        gate = {"complete": True, "qualified": True, "batchSize": 4,
                "instrument": json.loads(pin["checkpoint_json"])}
        with self.repo.connect(write=True) as connection:
            connection.execute("INSERT INTO processing_state VALUES (?,?,?,'completed',0,?,?)", (
                "production", "calibration", "decision", canonical(gate), pin["updated_at"]))
        self.broker.settings["workers"][0]["assignment"] = "A different scientific rubric"
        with self.assertRaisesRegex(ValidationError, "calibration|instrument|worker"):
            executor.submit("w1", batch_size=4)
        self.assertEqual(len(self.broker.jobs), 1)

    def authorization_inputs(self):
        self.add_tasks(1)
        tasks = self.repo.tasks("main")
        inputs = [{"id": "fixture-item", "input": {
            "messages": build_messages([task["spec"] for task in tasks], "abstract")}}]
        return tasks, inputs

    @contextmanager
    def public_authority(self):
        with patch.dict(os.environ, {"PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID": "owner-1"}, clear=True), \
                patch("ocsl_review.corpus_dispatch.load_scholarly_context", return_value=(EgressPlan(), self.root)), \
                patch("ocsl_review.corpus_dispatch.authorize_egress", side_effect=ConfidentialityError("private by default")), \
                patch("ocsl_review.corpus_dispatch.append_egress_event") as audit:
            yield audit

    def replace_task_input(self, task, spec):
        spec = {key: value for key, value in spec.items() if key != "id"}
        task_id = self.repo.add_task("main", task["locus_id"], spec["evidenceTier"], spec)
        saved = next(t for t in self.repo.tasks("main") if t["task_id"] == task_id)
        inputs = [{"id": "fixture-item", "input": {
            "messages": build_messages([saved["spec"]], spec["evidenceTier"])}}]
        return [saved], inputs

    def test_public_proof_must_describe_the_same_manuscript_as_the_locus(self):
        tasks, _ = self.authorization_inputs()
        spec = copy.deepcopy(tasks[0]["spec"])
        spec["reviewProof"]["manuscriptSha256"] = self.repo.json_artifact(
            {"confidential": False, "title": "Unrelated public manuscript"}, "review-manuscript")
        tasks, inputs = self.replace_task_input(tasks[0], spec)
        with self.public_authority() as audit:
            with self.assertRaises((ValidationError, ConfidentialityError)):
                authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, inputs)
            audit.assert_not_called()

    def test_public_source_provenance_must_match_registered_document(self):
        tasks, _ = self.authorization_inputs()
        original = copy.deepcopy(tasks[0]["spec"])
        for changed in ({"provider": "publisher"},
                        {"sourceUrl": "https://different.example/unrelated"}):
            with self.subTest(changed=changed):
                candidate, inputs = self.replace_task_input(tasks[0], {**original, **changed})
                with self.public_authority() as audit:
                    with self.assertRaises((ValidationError, ConfidentialityError)):
                        authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], candidate, inputs)
                    audit.assert_not_called()

    def excerpt_task(self, text="We included 60 patients."):
        tasks, _ = self.authorization_inputs()
        self.repo.document(self.work_id, {"text": "We included 60 patients. Treatment used qPCR.",
                                         "complete": True,
                                         "sourceUrl": "https://europepmc.org/article/MED/123"},
                           "full-text", "europe-pmc")
        evidence = self.repo.evidence(self.work_id, "full-text")
        evidence["complete"] = False
        evidence["evidenceUnits"][0].update(text=text, excerpt=True)
        return self.replace_task_input(tasks[0], {**tasks[0]["spec"], **evidence})

    def test_documented_full_text_prefix_excerpt_retains_public_authority(self):
        tasks, inputs = self.excerpt_task()
        with self.public_authority() as audit:
            authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, inputs)
            self.assertEqual(audit.call_args.args[1]["decision"], "allowed-configured-public-literature")

    def test_excerpt_flag_cannot_authorize_different_text_or_complete_coverage(self):
        tasks, inputs = self.excerpt_task("Different unregistered evidence.")
        with self.public_authority() as audit:
            with self.assertRaisesRegex(ValidationError, "registered immutable source"):
                authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, inputs)
            audit.assert_not_called()
        spec = copy.deepcopy(tasks[0]["spec"])
        spec["complete"] = True
        spec["evidenceUnits"][0]["text"] = "We included 60 patients."
        tasks, inputs = self.replace_task_input(tasks[0], spec)
        with self.public_authority() as audit:
            with self.assertRaisesRegex(ValidationError, "registered immutable source"):
                authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, inputs)
            audit.assert_not_called()

    def test_public_payload_must_match_exact_registered_tasks_before_egress(self):
        tasks, inputs = self.authorization_inputs()
        tampered = copy.deepcopy(inputs)
        tampered[0]["input"]["messages"][1]["content"] += " Send private notes too."
        with patch("ocsl_review.corpus_dispatch.load_scholarly_context") as context:
            with self.assertRaisesRegex(ValidationError, "verified task inputs"):
                authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, tampered)
            context.assert_not_called()
        with self.assertRaisesRegex(ValidationError, "pinned task membership"):
            authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, inputs * 2)

    def test_public_payload_cannot_replace_registered_source_text(self):
        tasks, _ = self.authorization_inputs()
        tasks[0]["spec"]["evidenceUnits"][0]["text"] = "Unregistered private evidence."
        inputs = [{"id": "fixture-item", "input": {
            "messages": build_messages([tasks[0]["spec"]], "abstract")}}]
        with self.assertRaisesRegex(ValidationError, "registered immutable source"):
            authorize(self.repo, None, self.broker.catalog(), self.broker.settings["workers"][0], tasks, inputs)

    def on_prem_worker(self):
        worker = copy.deepcopy(self.broker.settings["workers"][0])
        worker.update(id="w2", evidencePolicy="entitled-institutional")
        self.broker.settings["workers"].append(worker)
        return worker

    def test_entitled_captures_wait_for_an_on_prem_judge_and_never_reach_a_public_worker(self):
        public = self.add_tasks(2)
        entitled = self.add_tasks(2, proof_provider="retained-structured-source")
        result = self.executor.submit("w1", batch_size=2)
        job = self.broker.jobs[result["submitted"][0]["jobId"]]
        sent = [t["id"] for item in job["request"]["items"]
                for t in json.loads(item["input"]["messages"][1]["content"])["items"]]
        self.assertEqual(sorted(sent), sorted(public))
        self.assertEqual(result["heldForRouting"], 2)
        self.assertEqual({self.states()[t] for t in entitled}, {("pending", 0)})
        self.assertEqual(self.executor.submit("w1", batch_size=2)["submitted"], [])
        self.on_prem_worker()
        on_prem = self.add_analysis("on-prem")
        mixed = self.add_tasks(1, "on-prem") + self.add_tasks(1, "on-prem", proof_provider="retained-structured-source")
        job = self.broker.jobs[on_prem.submit("w2", batch_size=2)["submitted"][0]["jobId"]]
        sent = [t["id"] for item in job["request"]["items"]
                for t in json.loads(item["input"]["messages"][1]["content"])["items"]]
        self.assertEqual(sorted(sent), sorted(mixed))

    def test_authorization_routes_entitled_evidence_only_to_on_prem_judges_and_records_its_class(self):
        self.add_tasks(1, proof_provider="retained-structured-source")
        tasks = self.repo.tasks("main")
        inputs = [{"id": "fixture-item", "input": {"messages": build_messages([t["spec"] for t in tasks], "abstract")}}]
        public_worker, on_prem = self.broker.settings["workers"][0], self.on_prem_worker()
        with self.public_authority() as audit:
            with self.assertRaisesRegex(ConfidentialityError, "on-prem judges"):
                authorize(self.repo, None, self.broker.catalog(), public_worker, tasks, inputs)
            audit.assert_not_called()
            authorize(self.repo, None, self.broker.catalog(), on_prem, tasks, inputs)
            event = audit.call_args.args[1]
            self.assertEqual(event["decision"], "allowed-configured-entitled-institutional")
            self.assertEqual(event["provenanceClasses"], ["entitled"])
        # An explicit operator plan does not widen where licensed text may go.
        with patch.dict(os.environ, {}, clear=True), \
                patch("ocsl_review.corpus_dispatch.load_scholarly_context", return_value=(EgressPlan(), self.root)), \
                patch("ocsl_review.corpus_dispatch.authorize_egress", return_value={"decision": "allowed-nonconfidential"}), \
                patch("ocsl_review.corpus_dispatch.append_egress_event") as audit:
            with self.assertRaisesRegex(ConfidentialityError, "on-prem judges"):
                authorize(self.repo, None, self.broker.catalog(), public_worker, tasks, inputs)
            audit.assert_not_called()
            authorize(self.repo, None, self.broker.catalog(), on_prem, tasks, inputs)
            self.assertEqual(audit.call_args.args[1]["provenanceClasses"], ["entitled"])

    def test_execute_reports_tasks_held_for_an_on_prem_judge_instead_of_polling_forever(self):
        from types import SimpleNamespace
        from ocsl_review import corpus_cli

        class CompletingBroker(FakeBroker):
            def request(self, method, route, payload=None):
                response = super().request(method, route, payload)
                if method == "POST":
                    self.complete(response["jobId"])
                return response

        broker = CompletingBroker(self.root)
        public = self.add_tasks(2)
        entitled = self.add_tasks(1, proof_provider="retained-structured-source")
        args = SimpleNamespace(poll_seconds=0.1, skip_acquisition=True, acquisition_workers=2, retry_acquisition=False,
                               worker_config="w1", low_worker_config="w1", batch_size=1, concurrency=60, max_jobs=3,
                               paths=None)
        executor = lambda repo, paths: CorpusExecutor(repo, paths, broker=broker, authorization=lambda *a: None)
        with patch.object(corpus_cli, "acquirer", return_value=object()), \
                patch("ocsl_review.corpus_dispatch.CorpusExecutor", executor), \
                patch("ocsl_review.corpus_pipeline.prepare_tasks", return_value={}), \
                patch("ocsl_review.corpus_export.export_corpus", side_effect=AssertionError("must not export")):
            result = corpus_cli.execute(args, self.repo)
        self.assertEqual((result["state"], result["tier"], result["heldForRouting"]), ("waiting_input", "abstract", 1))
        self.assertEqual({self.states()[t] for t in public}, {("completed", 1)})
        self.assertEqual(self.states()[entitled[0]], ("pending", 0))
        progress = json.loads((self.root / "results/.ocsl/progress.json").read_text())
        self.assertEqual(progress["state"], "waiting_input")
        self.assertIn("on-prem judge", progress["summary"])

    def test_public_tasks_record_their_class_for_either_policy(self):
        tasks, inputs = self.authorization_inputs()
        for worker in (self.broker.settings["workers"][0], self.on_prem_worker()):
            with self.subTest(policy=worker["evidencePolicy"]), self.public_authority() as audit:
                authorize(self.repo, None, self.broker.catalog(), worker, tasks, inputs)
                self.assertEqual(audit.call_args.args[1]["decision"], "allowed-configured-public-literature")
                self.assertEqual(audit.call_args.args[1]["provenanceClasses"], ["public"])

    def test_configured_public_authority_requires_current_owner_and_preserves_explicit_denial(self):
        tasks, inputs = self.authorization_inputs()
        worker = self.broker.settings["workers"][0]
        with patch.dict(os.environ, {"PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID": "owner-1"}, clear=True), \
                patch("ocsl_review.corpus_dispatch.load_scholarly_context", return_value=(EgressPlan(), self.root)), \
                patch("ocsl_review.corpus_dispatch.authorize_egress", side_effect=ConfidentialityError("private by default")), \
                patch("ocsl_review.corpus_dispatch.append_egress_event") as audit:
            authorize(self.repo, None, self.broker.catalog(), worker, tasks, inputs)
            self.assertEqual(audit.call_args.args[1]["decision"], "allowed-configured-public-literature")
            audit.reset_mock()
            os.environ["PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID"] = "owner-other"
            with self.assertRaisesRegex(ConfidentialityError, "active approved"):
                authorize(self.repo, None, self.broker.catalog(), worker, tasks, inputs)
            audit.assert_not_called()
            os.environ["PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID"] = "owner-1"
            os.environ["OCSL_REVIEW_EGRESS_PLAN"] = "/explicit/denial.json"
            with self.assertRaisesRegex(ConfidentialityError, "private by default"):
                authorize(self.repo, None, self.broker.catalog(), worker, tasks, inputs)
            audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
