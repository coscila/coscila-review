# SPDX-License-Identifier: Apache-2.0
"""The follow-up instrument runner: routing, receipts, validation, repair, resumption and model pins."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import followup_fixtures as F
from ocsl_review import instruments
from ocsl_review.claim_check import ConfidentialPolicy
from ocsl_review.errors import ConfidentialityError, ReviewError, ValidationError
from ocsl_review.followup_branch import FollowupBranch, read_artifact
from ocsl_review.followup_judge import (FollowupHttpJudge, accepted, block_items, judge_from_args, plan_for,
                                        run_items)

FUNCTION = json.loads((F.FIXTURES / "judge" / "citation-function-1.json").read_text(encoding="utf-8"))


def answer_with(response):
    def answer(messages, settings):
        wanted = {item["id"] for item in F.items_of(messages)}
        return json.dumps({"items": [item for item in response["items"] if item["id"] in wanted]})
    return answer


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        type(self).count += 1
        path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(path, self.census["parent"], protocol=F.protocol())
        self.branch = FollowupBranch(path)
        self.instrument = instruments.load("citation-function@1")

    def run_function(self, judge, items=None, **options):
        with self.branch.writing() as connection:
            return run_items(connection, self.instrument, items or FUNCTION["items"], judge, tier="locus",
                             model=F.MODEL, **options)

    def test_accepted_outputs_with_receipts_and_resumption(self):
        judge = F.ScriptedJudge(answer_with(FUNCTION["response"]))
        summary = self.run_function(judge)
        self.assertEqual((2, 0, 1), (summary["accepted"], summary["rejected"], summary["calls"]))
        with closing(self.branch.connect()) as connection:
            call = connection.execute("SELECT * FROM fu_calls").fetchone()
            self.assertEqual(("completed", "fp-synthetic", F.MODEL, "on-prem"),
                             (call["outcome"], call["fingerprint"], call["served_model"], call["judge_location"]))
            self.assertEqual(["entitled", "public"], json.loads(call["provenance_classes"]))
            request = json.loads(read_artifact(connection, call["request_sha256"]))
            self.assertEqual(self.instrument.text("systemPrompt"), request["messages"][0]["content"])
            self.assertNotIn("provenance", request["messages"][1]["content"])
            verdicts = accepted(connection, self.instrument.digest, "locus")
            self.assertEqual("notes-retraction", verdicts["locus-2"]["function"])
        messages, settings = judge.requests[0]
        self.assertEqual({"temperature": 0.0, "max_tokens": 1000, "stream": False, "reasoning_effort": "none"}, settings)
        again = self.run_function(judge)
        self.assertEqual((2, 0), (again["skipped"], again["calls"]))

    def test_entitled_items_never_reach_a_judge_that_is_not_on_premises(self):
        judge = F.ScriptedJudge(answer_with(FUNCTION["response"]), location="external")
        with self.assertRaisesRegex(ConfidentialityError, "on premises"):
            self.run_function(judge)
        self.assertEqual([], judge.requests)
        public = [item for item in FUNCTION["items"] if item["provenance"] == "public"]
        self.assertEqual(1, self.run_function(judge, public)["accepted"])

    def test_invalid_outputs_are_repaired_once(self):
        responses = iter([
            {"items": [FUNCTION["response"]["items"][0],
                       {**FUNCTION["response"]["items"][1], "quote": "a quote that is not in the sentence"}]},
            {"items": [FUNCTION["response"]["items"][1]]},
        ])
        judge = F.ScriptedJudge(lambda messages, settings: json.dumps(next(responses)))
        summary = self.run_function(judge)
        self.assertEqual((2, 1, 1, 1), (summary["accepted"], summary["rejected"], summary["calls"], summary["repairCalls"]))
        self.assertEqual(["locus-2"], [item["id"] for item in F.items_of(judge.requests[1][0])])
        with closing(self.branch.connect()) as connection:
            outcomes = connection.execute("SELECT item_key, outcome, error_code FROM fu_judgments ORDER BY judgment_id"
                                          ).fetchall()
            self.assertIn(("locus-2", "rejected", "invalid_quote"), [tuple(row) for row in outcomes])

    def test_gates_count_invalid_outputs_after_repair_and_receipts(self):
        from ocsl_review.followup_cli import gates
        bad = {"items": [FUNCTION["response"]["items"][0],
                         {**FUNCTION["response"]["items"][1], "quote": "a quote that is not in the sentence"}]}
        judge = F.ScriptedJudge(lambda messages, settings: json.dumps(
            {"items": [item for item in bad["items"] if item["id"] in {i["id"] for i in F.items_of(messages)}]}))
        self.run_function(judge)
        report = gates(self.branch.path)
        (share,) = report["invalidShares"].values()
        self.assertEqual(0.5, share)
        self.assertFalse(report["checks"]["invalidOutputsWithinBound"])
        self.assertTrue(report["checks"]["everyReceiptHasFingerprint"])
        self.assertTrue(report["checks"]["noEntitledEvidenceToExternalJudges"])
        self.assertTrue(report["checks"]["oneDigestPerInstrument"])
        self.assertFalse(report["checks"]["strataFrozenBeforeJudging"])

    def test_a_response_from_another_model_stops_the_run(self):
        judge = F.ScriptedJudge(answer_with(FUNCTION["response"]), served="another-model")
        summary = self.run_function(judge)
        self.assertEqual(("model-drift", 0), (summary["stopped"], summary["accepted"]))
        with closing(self.branch.connect()) as connection:
            self.assertEqual("model-drift", connection.execute("SELECT outcome FROM fu_calls").fetchone()[0])

    def test_a_failed_call_is_a_receipt_never_a_verdict(self):
        def fail(messages, settings):
            raise ReviewError("judge HTTP 503")
        summary = self.run_function(F.ScriptedJudge(fail))
        self.assertEqual((1, 0), (summary["failedCalls"], summary["accepted"]))

    def test_further_passes_send_items_left_open_by_failed_calls(self):
        attempts = []

        def flaky(messages, settings):
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise ReviewError("judge HTTP 502")
            return answer_with(FUNCTION["response"])(messages, settings)
        waits = []
        with self.branch.writing() as connection:
            summary = run_items(connection, self.instrument, FUNCTION["items"], F.ScriptedJudge(flaky), tier="locus",
                                model=F.MODEL, transport_retries=2, cooldown=3.0, sleep=waits.append)
        self.assertEqual((2, 1, 1, 0), (summary["accepted"], summary["failedCalls"], summary["passes"], summary["open"]))
        self.assertEqual([3.0], waits)
        def down(messages, settings):
            raise ReviewError("judge HTTP 503")
        with self.branch.writing() as connection:
            summary = run_items(connection, instruments.load("citation-function@1"),
                                [{**FUNCTION["items"][0], "id": "locus-9"}], F.ScriptedJudge(down), tier="locus",
                                model=F.MODEL, transport_retries=1, cooldown=1.0, sleep=waits.append)
        self.assertEqual((0, 2, 1, 1), (summary["accepted"], summary["failedCalls"], summary["passes"], summary["open"]))

    def test_items_need_ids_and_provenance(self):
        judge = F.ScriptedJudge(answer_with(FUNCTION["response"]))
        with self.assertRaisesRegex(ValidationError, "provenance"):
            self.run_function(judge, [{k: v for k, v in FUNCTION["items"][0].items() if k != "provenance"}])
        with self.assertRaisesRegex(ValidationError, "pinned served model"):
            with self.branch.writing() as connection:
                run_items(connection, self.instrument, FUNCTION["items"], judge, tier="locus", model="")

    def test_blocked_items_are_recorded_unsent(self):
        with self.branch.writing() as connection:
            self.assertEqual(1, block_items(connection, self.instrument, "locus",
                                            [("locus-9", "text-redacted", "no text parent")]))
            row = connection.execute("SELECT outcome, call_id, error_code FROM fu_judgments").fetchone()
            self.assertEqual(("blocked", None, "text-redacted"), tuple(row))

    def test_citation_support_runs_unchanged_one_item_per_call(self):
        support = instruments.load("citation-support@2")
        self.assertEqual(1, plan_for(support, "abstract").items_per_call)
        unit = "Marker zeta doubled the risk in adults."
        tasks = [{"id": f"support-{n}", "claim": "Marker zeta doubles the risk in adults.", "evidenceTier": "abstract",
                  "complete": True, "evidenceUnits": [{"id": "abstract", "text": unit}], "sourceSha256": "b" * 64,
                  "provenance": "public"} for n in (1, 2)]

        def answer(messages, settings):
            items = json.loads(messages[1]["content"])["items"]
            return json.dumps({"items": [{"id": item["id"], "supportVerdict": "direct-support",
                                          "rationale": "The abstract states it.", "escalate": False,
                                          "escalationReason": "",
                                          "evidence": [{"unitId": "abstract", "quote": "doubled the risk"}]}
                                         for item in items]})
        judge = F.ScriptedJudge(answer)
        with self.branch.writing() as connection:
            summary = run_items(connection, support, tasks, judge, tier="abstract", model=F.MODEL)
        self.assertEqual((2, 2), (summary["accepted"], summary["calls"]))
        self.assertEqual({"temperature": 0.0, "max_tokens": 3000, "stream": False}, judge.requests[0][1])


class HttpJudgeTests(unittest.TestCase):
    def test_records_the_request_bytes_and_the_fingerprint(self):
        sent = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            sent["url"], sent["body"], sent["headers"] = request.full_url, request.data, dict(request.header_items())
            return Response(json.dumps({"model": "m", "system_fingerprint": "fp-1", "usage": {"prompt_tokens": 3},
                                        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}).encode())
        judge = FollowupHttpJudge("http://judge.example:8000/v1", "m", opener=opener)
        messages = [{"role": "user", "content": "x"}]
        reply = judge.complete(messages, settings={"temperature": 0})
        self.assertEqual(("fp-1", "m", "stop"), (reply.fingerprint, reply.model, reply.finish_reason))
        self.assertEqual("http://judge.example:8000/v1/chat/completions", sent["url"])
        self.assertEqual(judge.body(messages, {"temperature": 0}), sent["body"])
        self.assertNotIn("Authorization", sent["headers"])

    def test_on_premises_needs_a_policy_that_lists_the_endpoint(self):
        args = SimpleNamespace(base_url="http://judge.example:8000/v1", broker_socket=None, model="m", policy=None,
                               on_premises=True, api_key_env=None)
        with self.assertRaisesRegex(ConfidentialityError, "--policy"):
            judge_from_args(args)
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            policy.write_text(json.dumps({"version": 2, "mode": "public-literature",
                                          "onPremEndpoints": ["http://judge.example:8000/v1/chat/completions"]}))
            judge, loaded = judge_from_args(SimpleNamespace(**{**vars(args), "policy": str(policy)}))
            self.assertEqual("on-prem", judge.location)
            self.assertTrue(loaded.endpoint_on_prem(judge.url))
            other = SimpleNamespace(**{**vars(args), "policy": str(policy), "base_url": "http://elsewhere.example/v1"})
            with self.assertRaisesRegex(ConfidentialityError, "not an on-prem endpoint"):
                judge_from_args(other)
        external, _ = judge_from_args(SimpleNamespace(**{**vars(args), "on_premises": False}))
        self.assertEqual("external", external.location)
        self.assertFalse(ConfidentialPolicy("public-literature").with_mode("confidential-manuscript")
                         .judge_allowed(external))

    def test_a_broker_judge_needs_its_module(self):
        args = SimpleNamespace(base_url=None, broker_socket="/tmp/none.sock", model="m", policy=None, on_premises=False)
        with mock.patch("importlib.import_module", side_effect=ModuleNotFoundError("x")):
            with self.assertRaisesRegex(ReviewError, "broker judge"):
                judge_from_args(args)
        with self.assertRaisesRegex(ValidationError, "--model"):
            judge_from_args(SimpleNamespace(**{**vars(args), "model": None}))


if __name__ == "__main__":
    unittest.main()
