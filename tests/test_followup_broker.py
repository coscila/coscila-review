# SPDX-License-Identifier: Apache-2.0
"""The broker judge of the follow-up instruments against a fake run-principal client (no socket, no daemon): verbatim
bundles by digest, idempotency keys from the request digest, served model and fingerprint from the item, drift,
fidelity stops, calls without a response, and on-premises routing only for a provider the policy lists."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_judge_broker as B
from ocsl_review import instruments
from ocsl_review.census_broker import BrokerTransport, idempotency_key, request_sha256
from ocsl_review.claim_check import ConfidentialPolicy
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.followup_branch import FollowupBranch
from ocsl_review.followup_judge import judge_from_args, run_items

FUNCTION = json.loads((F.FIXTURES / "judge" / "citation-function-1.json").read_text(encoding="utf-8"))
PROVIDER = "synthetic-onprem-provider"


class FakeBroker:
    """The routes of a run principal's socket a one-item verbatim job uses; every job settles at once."""

    def __init__(self, *, installed=(), served=F.MODEL, model=F.MODEL, digest_offset="", refuse=None, state="completed",
                 http_status=200, attempts=1):
        self.installed, self.served, self.model = list(installed), served, model
        self.digest_offset, self.refuse, self.state = digest_offset, refuse, state
        self.http_status, self.attempts = http_status, attempts
        self.requests: list[tuple[str, str, object]] = []
        self.jobs: dict[str, dict] = {}
        self.keys: dict[str, str] = {}

    def request(self, method, route, payload=None):
        self.requests.append((method, route, payload))
        if method == "GET" and route == "/configs":
            return {"authorization": {"principalId": "run-principal-followup", "kind": "run", "state": "active",
                                      "runId": "run-followup-1", "instruments": list(self.installed)},
                    "instrumentMode": 1, "instrumentVerbatim": 1,
                    "workers": [{"id": "bulk-worker", "bulkAvailable": True, "model": self.model,
                                 "providerId": PROVIDER}],
                    "instruments": [{"digest": digest, "transport": {"messages": "verbatim"}}
                                    for digest in self.installed]}
        if method == "POST" and route.startswith("/bulk/submit"):
            key = payload["idempotencyKey"]
            if key in self.keys:
                job = self.jobs[self.keys[key]]
                return {key_: job[key_] for key_ in ("jobId", "state", "instrumentDigest")}
            item = payload["items"][0]
            data = dict(item["input"])
            messages = data["messages"]
            body = {name: value for name, value in data.items() if name != "itemCount"}
            ids = [entry["id"] for entry in json.loads(messages[1]["content"])["items"]]
            output = json.dumps({"items": [entry for entry in FUNCTION["response"]["items"] if entry["id"] in ids]})
            result = {"id": item["id"], "state": self.state, "attempts": self.attempts, "httpStatus": self.http_status,
                      "usage": {"input": 100, "output": 20}, "finishReason": "stop"}
            if self.refuse:
                result.update(state="failed", errorCode=self.refuse, attempts=0)
            elif self.state == "completed":
                result.update(output=output, servedModel=self.served, systemFingerprint="fp-broker",
                              requestSha256=request_sha256(body) + self.digest_offset)
            else:
                result.update(errorCode="PROVIDER_UNAVAILABLE")
            job_id = f"job-{len(self.jobs) + 1}"
            self.jobs[job_id] = {"jobId": job_id, "state": "completed" if self.state == "completed" or self.refuse
                                 else "failed", "instrumentDigest": payload["instrumentDigest"], "result": result,
                                 "payload": payload}
            self.keys[key] = job_id
            job = self.jobs[job_id]
            return {key_: job[key_] for key_ in ("jobId", "state", "instrumentDigest")}
        if method == "GET" and "/results" in route:
            job = self.jobs[route.split("/")[2]]
            return {"results": [job["result"]], "hasMore": False, "nextCursor": None}
        raise AssertionError(f"unexpected broker route {method} {route}")


def function_digest() -> str:
    return instruments.broker_bundle("citation-function@1")["digest"]


class BrokerJudgeTests(unittest.TestCase):
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

    def judge(self, broker, *, location="on-prem"):
        transport = BrokerTransport(str(self.root / "unused.sock"), client=broker)
        transport.open()
        judge = B.BrokerJudge(transport, model=F.MODEL, location=location, wait_seconds=5)
        self.addCleanup(judge.close)
        return judge

    def run_function(self, judge, items=None):
        with self.branch.writing() as connection:
            return run_items(connection, self.instrument, items or FUNCTION["items"], judge, tier="locus",
                             model=F.MODEL)

    def test_a_verbatim_job_per_call_with_the_items_served_model_and_fingerprint(self):
        broker = FakeBroker(installed=[function_digest()])
        judge = self.judge(broker)
        judge.prepare(self.instrument, "locus")
        summary = self.run_function(judge)
        self.assertEqual((2, 0, 1), (summary["accepted"], summary["rejected"], summary["calls"]))
        ((job,),) = [list(broker.jobs.values())]
        submitted = job["payload"]
        self.assertEqual((function_digest(), 1, "bulk-worker"), (submitted["instrumentDigest"], len(submitted["items"]),
                                                                 submitted["configId"]))
        sent = submitted["items"][0]["input"]
        self.assertEqual(self.instrument.text("systemPrompt"), sent["messages"][0]["content"])
        self.assertEqual({"messages", "itemCount", "model", "max_tokens", "temperature", "stream", "reasoning_effort"},
                         set(sent))
        self.assertEqual(2, sent["itemCount"])
        expected = request_sha256({key: value for key, value in sent.items() if key != "itemCount"})
        self.assertEqual(idempotency_key(submitted["items"][0]["id"], expected), submitted["idempotencyKey"])
        with closing(self.branch.connect()) as connection:
            call = connection.execute("SELECT * FROM fu_calls").fetchone()
        self.assertEqual((F.MODEL, "fp-broker", "on-prem", None, 100, 20),
                         (call["served_model"], call["fingerprint"], call["judge_location"], call["endpoint"],
                          call["prompt_tokens"], call["completion_tokens"]))
        self.assertTrue(call["judge"].startswith("broker-judge:"))
        messages, options = self._messages()
        reply = judge.complete(messages, **options)
        self.assertEqual(F.MODEL, reply.model)
        self.assertEqual(1, len(broker.jobs), "the same request meets the job the broker already holds")

    def _messages(self):
        from ocsl_review import citation_function
        from ocsl_review.followup_judge import plan_for
        messages = citation_function.build_messages([{k: v for k, v in item.items() if k != "provenance"}
                                                     for item in FUNCTION["items"]])
        return messages, {"settings": plan_for(self.instrument, "locus").settings(2)}

    def test_only_verbatim_bundles_the_broker_installed_can_run(self):
        judge = self.judge(FakeBroker(installed=[]))
        with self.assertRaisesRegex(ValidationError, "lacks instrument bundles"):
            judge.prepare(self.instrument, "locus")
        with self.assertRaisesRegex(ValidationError, "no verbatim message transport"):
            judge.prepare(instruments.load("citation-support@2"), "abstract")
        with self.assertRaisesRegex(ValidationError, "only the verbatim follow-up instruments"):
            judge.complete([{"role": "system", "content": "another prompt"}, {"role": "user", "content": "{}"}],
                           settings={})

    def test_another_served_model_stops_the_run_as_drift(self):
        judge = self.judge(FakeBroker(installed=[function_digest()], served="another-model"))
        summary = self.run_function(judge)
        self.assertEqual(("model-drift", 0), (summary["stopped"], summary["accepted"]))

    def test_fidelity_failures_halt_the_transport(self):
        for broker, code in ((FakeBroker(installed=[function_digest()], digest_offset="0"), "request_mismatch"),
                             (FakeBroker(installed=[function_digest()], refuse="INSTRUMENT_MESSAGE_MISMATCH"),
                              "fidelity")):
            with self.subTest(code=code):
                judge = self.judge(broker)
                summary = self.run_function(judge)
                self.assertEqual((0, 1, 2), (summary["accepted"], summary["failedCalls"], summary["open"]))
                self.assertEqual(code, judge.stopped)
                self.assertIsNotNone(judge.transport.halted)

    def test_a_failed_item_is_a_call_without_a_response(self):
        judge = self.judge(FakeBroker(installed=[function_digest()], state="failed", http_status=503))
        summary = self.run_function(judge)
        self.assertEqual((0, 1, 2), (summary["accepted"], summary["failedCalls"], summary["open"]))
        self.assertIsNone(judge.stopped)
        with closing(self.branch.connect()) as connection:
            error = connection.execute("SELECT outcome, error FROM fu_calls").fetchone()
        self.assertEqual("failed", error["outcome"])
        self.assertIn("PROVIDER_UNAVAILABLE", error["error"])

    def test_entitled_items_need_an_on_premises_broker_provider(self):
        args = SimpleNamespace(base_url=None, broker_socket=str(self.root / "unused.sock"), model=F.MODEL,
                               policy=None, on_premises=False, api_key_env=None, broker_worker=None)
        broker = FakeBroker(installed=[function_digest()])

        def transport(socket_path, worker=None):
            return BrokerTransport(socket_path, worker=worker, client=broker)
        policy = self.root / f"policy-{self.count}.json"
        policy.write_text(json.dumps({"version": 2, "mode": "public-literature",
                                      "onPremBrokerProviders": [{"provider": PROVIDER, "models": [F.MODEL]}]}))
        other = self.root / f"other-{self.count}.json"
        other.write_text(json.dumps({"version": 2, "mode": "public-literature",
                                     "onPremBrokerProviders": [{"provider": "elsewhere", "models": []}]}))
        with mock.patch.object(B, "BrokerTransport", transport):
            external, _ = judge_from_args(args)
            self.assertEqual("external", external.location)
            with self.assertRaisesRegex(ConfidentialityError, "on premises"):
                self.run_function(external)
            with self.assertRaisesRegex(ConfidentialityError, "--policy"):
                judge_from_args(SimpleNamespace(**{**vars(args), "on_premises": True}))
            with self.assertRaisesRegex(ConfidentialityError, "not an on-prem broker provider"):
                judge_from_args(SimpleNamespace(**{**vars(args), "on_premises": True, "policy": str(other)}))
            onprem, loaded = judge_from_args(SimpleNamespace(**{**vars(args), "on_premises": True,
                                                                "policy": str(policy)}))
            self.assertEqual("on-prem", onprem.location)
            self.assertTrue(ConfidentialPolicy.load(policy).broker_provider_on_prem(PROVIDER, F.MODEL))
            self.assertEqual(2, self.run_function(onprem)["accepted"])
            with self.assertRaisesRegex(ValidationError, "requests model"):
                judge_from_args(SimpleNamespace(**{**vars(args), "model": "another-model"}))
            with self.assertRaisesRegex(ValidationError, "drop --api-key-env"):
                judge_from_args(SimpleNamespace(**{**vars(args), "api_key_env": "SOME_KEY"}))


if __name__ == "__main__":
    unittest.main()
