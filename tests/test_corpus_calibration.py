# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import uuid

from ocsl_review.corpus_calibration import calibrate, validate
from ocsl_review.corpus_dispatch import CorpusExecutor
from ocsl_review.corpus_store import CorpusRepository, canonical
from ocsl_review.errors import ValidationError


class FixtureBroker:
    """In-memory broker fixture; real executor/validator/storage run unchanged."""

    def __init__(self):
        self.jobs = {}
        self.ready = False
        self.paused = False
        self.missing_once = False
        self.drift_on_job = None
        self.authorization_calls = 0

    def catalog(self):
        return {"revision": 1, "authorization": {"attemptId": "fixture-attempt"}, "workers": [
            {"id": "w1", "assignment": "Fixture primary", "bulkAvailable": True, "effort": "none",
             "providerId": "fixture", "modelProvider": {"id": "fixture"}},
            {"id": "w2", "assignment": "Fixture low", "bulkAvailable": True, "effort": "low",
             "providerId": "fixture", "modelProvider": {"id": "fixture"}},
            {"id": "w3", "assignment": "Fixture blind", "bulkAvailable": True, "effort": "none",
             "providerId": "fixture", "modelProvider": {"id": "fixture"}},
        ]}

    def authorize(self, *args):
        self.authorization_calls += 1

    def request(self, method, route, payload=None):
        if method == "POST":
            job_id = str(uuid.UUID(int=len(self.jobs) + 1))
            records = []
            for group in payload["items"]:
                tasks = json.loads(group["input"]["messages"][1]["content"])["items"]
                items = [{"id": task["id"], "supportVerdict": "direct-support",
                          "rationale": "The exact count is explicit.",
                          "evidence": [{"unitId": task["evidenceUnits"][0]["id"],
                                        "quote": "The study included 60 patients."}],
                          "escalate": False, "escalationReason": ""} for task in tasks]
                if self.missing_once and len(items) == 4:
                    items.pop(0)
                    self.missing_once = False
                if self.drift_on_job == len(self.jobs) + 1 and not records:
                    items[0]["supportVerdict"] = "scope-caveat"
                records.append({"id": group["id"], "state": "completed", "providerId": "fixture",
                                "model": "fixture-model", "promptSha256": "d" * 64,
                                "inputSha256": hashlib.sha256(json.dumps(group["input"], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                                "output": {"items": items}, "usage": {"input": 15, "output": 10}})
            self.jobs[job_id] = {"payload": payload, "records": records}
            return {"jobId": job_id, "configRevision": 1}
        parsed = urlparse(route)
        job = self.jobs[parsed.path.split("/")[2]]
        if parsed.path.endswith("/results"):
            after = int(parse_qs(parsed.query)["after"][0])
            records = job["records"] if self.ready else []
            return {"results": records[after:], "nextCursor": len(records), "hasMore": False}
        return {"state": "paused" if self.paused else "completed" if self.ready else "running"}


class CorpusCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = CorpusRepository.create(self.root, "work/library", "Fixture question", "production-v2", {"version": 2})
        source_work = self.repo.work({"doi": "10.1234/source", "title": "Fixture source"})
        self.repo.document(source_work, {"text": "The study included 60 patients.", "complete": True}, "abstract", "fixture")
        evidence = self.repo.evidence(source_work, "abstract")
        artifact = self.repo.artifact(b"fixture citing article", "review-raw")
        for i in range(8):
            review = self.repo.review({"doi": f"10.1234/review-{i}", "journal": "Journal A" if i % 2 else "Journal B"})
            entry, locus = f"entry-{i}", f"locus-{i}"
            claim_type = "numeric" if i % 3 else "fact"
            with self.repo.connect(write=True) as connection:
                connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)",
                                   (entry, review, "1", "Fixture source", "{}", source_work, "verified", artifact))
                connection.execute("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)",
                                   (locus, entry, artifact, canonical({"claimType": claim_type, "position": i}),
                                    "The study included 60 patients.", "[]", "fixture-v1"))
            spec = {**evidence, "claim": "The study included 60 patients.", "claimType": claim_type,
                    "citationLocus": {"id": locus, "bindingStatus": "exact"},
                    "citedWork": {"title": "Fixture source"}, "context": [], "identifiers": {}}
            self.repo.add_task("production-v2", locus, "abstract", spec)

    def call(self, **kwargs):
        return calibrate(self.repo, None, sample_size=8, **kwargs)

    def executor_factory(self, broker):
        def create(repo, paths, *, analysis_id):
            return CorpusExecutor(repo, paths, broker=broker, authorization=broker.authorize, analysis_id=analysis_id)
        return create

    def test_preparation_is_bounded_stratified_idempotent_and_never_claims_live_acceptance(self):
        with patch("ocsl_review.corpus_calibration.CorpusExecutor") as executor:
            first = self.call()
            second = self.call()
        executor.assert_not_called()
        self.assertEqual(first["manifest"], second["manifest"])
        self.assertEqual(first["manifest"]["plannedItemJudgments"], 32)
        self.assertEqual(first["manifest"]["additionalValidation"]["uniqueLociCap"], 1000)
        self.assertFalse(first["report"]["complete"])
        self.assertFalse(first["report"]["qualified"])
        self.assertIsNone(first["report"]["pairedAgreement"]["confidenceInterval95"])
        self.assertEqual(first["report"]["batchSize"], 1)
        strata = {tuple(item["stratum"]) for item in first["manifest"]["sample"]}
        self.assertEqual(len(strata), 4)
        with self.repo.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM corpus_tasks").fetchone()[0], 40)
            self.assertEqual(connection.execute("SELECT count(*) FROM model_receipts").fetchone()[0], 0)
        self.assertEqual(self.repo.analysis()["analysis_id"], "production-v2")

    def test_real_executor_fixture_resumes_without_duplicate_dispatch_and_retains_positions(self):
        broker = FixtureBroker()
        with patch("ocsl_review.corpus_calibration.CorpusExecutor", self.executor_factory(broker)):
            initial = self.call(execute=True)
            self.assertFalse(initial["report"]["complete"])
            self.assertGreater(len(broker.jobs), 0)
            broker.ready = True
            for _ in range(8):
                final = self.call(execute=True)
                if final["report"]["complete"]:
                    break
            again = self.call(execute=True)
        self.assertTrue(final["report"]["qualified"])
        self.assertTrue(again["report"]["qualified"])
        self.assertEqual(final["report"]["batchSize"], 4)
        self.assertEqual(len(broker.jobs), 4)
        self.assertEqual(broker.authorization_calls, 4)
        payloads = [job["payload"] for job in broker.jobs.values()]
        normal = json.loads(payloads[1]["items"][0]["input"]["messages"][1]["content"])["items"]
        reordered = json.loads(payloads[2]["items"][0]["input"]["messages"][1]["content"])["items"]
        self.assertEqual([t["citationLocus"]["id"] for t in normal],
                         list(reversed([t["citationLocus"]["id"] for t in reordered])))
        self.assertEqual(final["report"]["pairedAgreement"]["fractionOfSample"], 1)
        interval = final["report"]["pairedAgreement"]["confidenceInterval95"]
        self.assertGreater(interval[0], 0.67)
        self.assertLess(interval[0], 0.68)
        self.assertAlmostEqual(interval[1], 1)
        self.assertEqual(final["report"]["instrument"]["worker"]["id"], "w1")
        self.assertEqual(final["report"]["variants"]["none4"]["requestReceipts"], 2)
        self.assertTrue(all(t["state"] == "pending" for t in self.repo.tasks("production-v2")))

    def test_repair_does_not_hide_first_attempt_failure_or_discard_accepted_peers(self):
        broker = FixtureBroker()
        broker.missing_once = True
        with patch("ocsl_review.corpus_calibration.CorpusExecutor", self.executor_factory(broker)):
            self.call(execute=True)
            broker.ready = True
            repairing = self.call(execute=True)
            for _ in range(8):
                final = self.call(execute=True)
                if final["report"]["complete"]:
                    break
        variant = final["report"]["variants"]["none4"]
        self.assertFalse(repairing["report"]["complete"])
        self.assertTrue(final["report"]["complete"])
        self.assertFalse(final["report"]["qualified"])
        self.assertEqual(variant["firstValidItems"], 7)
        self.assertEqual(variant["taskStates"]["completed"], 8)
        self.assertEqual(variant["repairItemAttempts"], 1)
        self.assertEqual(len(broker.jobs), 5)

    def test_changed_budget_or_worker_and_oversized_calibration_fail_before_dispatch(self):
        self.call()
        for options in ({"validation_cap": 999}, {"worker_id": "other"}):
            with self.subTest(options=options), self.assertRaisesRegex(ValidationError, "settings changed"):
                self.call(**options)
        for sample, cap in ((301, 1000), (300, 1001), (True, 1000)):
            with self.subTest(sample=sample, cap=cap), self.assertRaises(ValidationError):
                calibrate(self.repo, None, sample_size=sample, validation_cap=cap)

    def test_valid_but_position_sensitive_verdicts_fail_the_batch_gate(self):
        broker = FixtureBroker()
        broker.drift_on_job = 3
        with patch("ocsl_review.corpus_calibration.CorpusExecutor", self.executor_factory(broker)):
            self.call(execute=True)
            broker.ready = True
            for _ in range(8):
                final = self.call(execute=True)
                if final["report"]["complete"]:
                    break
        self.assertTrue(final["report"]["complete"])
        self.assertTrue(all(r["validItemFraction"] == 1 for r in final["report"]["variants"].values()))
        self.assertEqual(final["report"]["pairedAgreement"]["fractionOfSample"], 1)
        self.assertEqual(final["report"]["reorderedAgreement"]["fractionOfSample"], 7/8)
        self.assertFalse(final["report"]["qualified"])
        self.assertEqual(final["report"]["batchSize"], 1)

    def test_missing_profile_prevents_any_live_dispatch(self):
        broker = FixtureBroker()
        original = broker.catalog
        def changed():
            value = original()
            value["workers"][1]["effort"] = "xhigh"
            return value
        broker.catalog = changed
        with patch("ocsl_review.corpus_calibration.CorpusExecutor", self.executor_factory(broker)), \
                self.assertRaisesRegex(ValidationError, "reasoning differs"):
            self.call(execute=True)
        self.assertFalse(broker.jobs)

    def test_concurrency_is_forwarded_and_provider_pause_stops_new_submissions(self):
        broker = FixtureBroker()
        with patch("ocsl_review.corpus_calibration.CorpusExecutor", self.executor_factory(broker)):
            self.call(execute=True, concurrency=7)
            jobs_before = len(broker.jobs)
            self.assertTrue(all(job["payload"]["concurrency"] == 7 for job in broker.jobs.values()))
            broker.paused = True
            paused = self.call(execute=True, concurrency=7)
        self.assertTrue(paused["paused"])
        self.assertFalse(paused["report"]["complete"])
        self.assertEqual(len(broker.jobs), jobs_before)

    def test_additional_validation_is_blind_stratified_capped_and_resumable(self):
        self.call(validation_cap=4)
        broker = FixtureBroker()
        broker.ready = True
        primary = self.executor_factory(broker)(self.repo, None, analysis_id="production-v2")
        primary.submit("w1")
        primary.resume()
        with patch("ocsl_review.corpus_calibration.CorpusExecutor", self.executor_factory(broker)):
            with self.assertRaisesRegex(ValidationError, "pinned unique-locus budget"):
                validate(self.repo, None, sample_size=5)
            prepared = validate(self.repo, None, sample_size=4)
            first = validate(self.repo, None, sample_size=4, execute=True)
            final = validate(self.repo, None, sample_size=4, execute=True)
            repeated = validate(self.repo, None, sample_size=4, execute=True)
            with self.assertRaisesRegex(ValidationError, "settings changed"):
                validate(self.repo, None, sample_size=3)
        self.assertEqual(len(prepared["manifest"]["sample"]), 4)
        self.assertEqual({s["selection"] for s in prepared["manifest"]["sample"]}, {"stratified", "targeted"})
        self.assertFalse(first["report"]["complete"])
        self.assertTrue(final["report"]["complete"])
        self.assertTrue(repeated["report"]["complete"])
        self.assertEqual(final["report"]["agreementBySelection"]["all"]["fractionOfSample"], 1)
        self.assertEqual(len(broker.jobs), 2)
        for group in list(broker.jobs.values())[-1]["payload"]["items"]:
            self.assertNotIn("supportVerdict", group["input"]["messages"][1]["content"])
        self.assertEqual(self.repo.analysis()["analysis_id"], "production-v2")

    def test_validation_cannot_preempt_primary_results_or_invent_an_unpinned_budget(self):
        with self.assertRaisesRegex(ValidationError, "pin the additional validation budget"):
            validate(self.repo, None, sample_size=4)
        self.call()
        with self.assertRaisesRegex(ValidationError, "accepted primary abstract judgments"):
            validate(self.repo, None, sample_size=4)


if __name__ == "__main__":
    unittest.main()
