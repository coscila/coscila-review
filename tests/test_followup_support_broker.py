# SPDX-License-Identifier: Apache-2.0
"""Support judging through the run principal's broker: citation-support-verbatim@1, citation-support@2's prompt, output
schema, validator and class map with request settings declared per tier and one item per call.

A fake run-principal socket stands in for the execution daemon (no socket, no daemon, no network). It applies the
daemon's verbatim check to every item (puppenclaw's ``verbatimInstrumentCall`` in ``src/advanced/instruments.ts``): the
first message is a system message equal to the installed bundle's prompt and no other message is one; every message is
exactly role and content; the item declares 1 to itemsPerCall entries; the settings it repeats equal what the daemon
sends (the worker's model, max_tokens max(minimum, perItem x items), the bundle's temperature, seed and reasoning, no
stream); nothing else rides along. It refuses an item that fails the check before any provider call, sends the request
the daemon builds and reports that request's digest, and pauses on a served model other than the bundle's required one.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import corpus_assessment, followup_judge_broker as B, followup_net, followup_support as SP, instruments
from ocsl_review.census_broker import BrokerTransport, request_sha256
from ocsl_review.cli import main
from ocsl_review.errors import ValidationError
from ocsl_review.followup_branch import FollowupBranch, protocol_instruments, read_artifact
from ocsl_review.followup_estimands import agreement_label
from ocsl_review.followup_judge import module_for, plan_for, run_items, step_result
from ocsl_review.followup_model import BROKER_INSTRUMENTS, SUPPORT_INSTRUMENTS, VERBATIM_SUPPORT_INSTRUMENT

from test_followup_citedby import routes as citedby_routes
from test_followup_dependency import insert_frame, locus
from test_followup_numeric import TABLES
from test_followup_phase4_surface import SUPPORT_DIGEST
from test_followup_pipeline import PLAIN_FULL_TEXT, answer as pipeline_answer, pipeline_protocol
from test_followup_secondgen import ROUTES as SECONDGEN_ROUTES
from test_followup_status import RW_CSV, clean_environment, no_network, status_routes
from test_followup_strata import A1, A2, B1, lid
from test_followup_support import FULL_TEXT, provider_cache, quoting, routes

VERBATIM = VERBATIM_SUPPORT_INSTRUMENT
REAL_CLIENT = followup_net.client
TIERS = ("abstract", "full-text")
MODEL = instruments.load(VERBATIM).manifest["requiredModel"]["served"]
PROVIDER = "synthetic-onprem-provider"
TOOL_FIELDS = ("tools", "tool_choice", "functions", "function_call", "parallel_tool_calls")
SETTING_FIELDS = ("model", "max_tokens", "temperature", "seed", "reasoning_effort", "stream")


def verbatim_protocol(**instruments_changes) -> dict:
    """The fixture protocol with the verbatim support instrument pinned (no supportSettings) and its served model."""
    protocol = F.protocol(model={"served": MODEL, "location": "on-premises"})
    named = {key: value for key, value in protocol["instruments"].items() if key != "supportSettings"}
    protocol["instruments"] = {**named, "support": VERBATIM, **instruments_changes}
    return protocol


class Mismatch(Exception):
    """The daemon's INSTRUMENT_MESSAGE_MISMATCH: the item is refused before any provider call."""


def daemon_request(bundle: dict, item: object, model: str) -> dict:
    """The chat-completions request the execution daemon sends for one verbatim item, or Mismatch (see the module
    docstring for the checks)."""
    if not isinstance(item, dict):
        raise Mismatch("an item is an object {messages, itemCount}")
    if any(key in item for key in TOOL_FIELDS):
        raise Mismatch("verbatim calls carry no tools")
    extra = sorted(set(item) - {"messages", "itemCount", *SETTING_FIELDS})
    if extra:
        raise Mismatch(f"fields outside the instrument request: {extra}")
    messages = item.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Mismatch("messages must be a non-empty array")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise Mismatch("a message carries exactly role and content")
        if message["role"] not in ("system", "user", "assistant") or not isinstance(message["content"], str):
            raise Mismatch("a message has a role system, user or assistant and a string content")
    if messages[0]["role"] != "system" or messages[0]["content"] != bundle["systemPrompt"]:
        raise Mismatch("the first message must be the installed instrument prompt")
    if any(message["role"] == "system" for message in messages[1:]) or \
            not any(message["role"] == "user" for message in messages[1:]):
        raise Mismatch("one system message first and at least one user message")
    count = item.get("itemCount")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= bundle["itemsPerCall"]:
        raise Mismatch(f"itemCount must be 1 to {bundle['itemsPerCall']}")
    rule = bundle["maxOutputTokens"]
    max_tokens = max(rule["minimum"], rule["perItem"] * count)
    for name, sent in (("model", model), ("max_tokens", max_tokens), ("temperature", bundle["temperature"]),
                       ("reasoning_effort", bundle["reasoning"])):
        if name in item and item[name] != sent:
            raise Mismatch(f"{name} {item[name]!r} differs from {sent!r}")
    if "seed" in item and item["seed"] != bundle["seed"]:
        raise Mismatch("seed differs from the instrument's")
    if "stream" in item and item["stream"] is not False:
        raise Mismatch("verbatim calls are not streamed")
    request = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": bundle["temperature"]}
    if bundle["seed"] is not None:
        request["seed"] = bundle["seed"]
    request.update(reasoning_effort=bundle["reasoning"], stream=False)
    return request


class DaemonBroker:
    """A run principal's socket over installed verbatim bundles (``exported`` records of instruments.broker_bundle);
    every job has one item and settles at once. Requests are served one at a time, as the judging commands submit
    their calls from several threads."""

    def __init__(self, exported=(), *, worker_model=MODEL, served=None, answer=None):
        self.installed = {record["digest"]: record["bundle"] for record in exported}
        self.worker_model, self.served = worker_model, served or worker_model
        self.answer = answer or quoting()
        self.jobs: dict[str, dict] = {}
        self.keys: dict[str, str] = {}
        self.sent: list[tuple[str, dict]] = []          # (broker digest, request the daemon sent)
        self.refused: list[str] = []
        self.lock = threading.Lock()

    def request(self, method, route, payload=None):
        with self.lock:
            return self._request(method, route, payload)

    def _request(self, method, route, payload=None):
        if method == "GET" and route == "/configs":
            return {"authorization": {"principalId": "run-principal-support", "kind": "run", "state": "active",
                                      "runId": "run-support-1", "instruments": list(self.installed)},
                    "instrumentMode": 1, "instrumentVerbatim": 1,
                    "workers": [{"id": "bulk-worker", "bulkAvailable": True, "model": self.worker_model,
                                 "providerId": PROVIDER}],
                    "instruments": [{"digest": digest, "transport": {"messages": "verbatim"}}
                                    for digest in self.installed]}
        if method == "POST" and route.startswith("/bulk/submit"):
            key = payload["idempotencyKey"]
            if key not in self.keys:
                self.keys[key] = self._settle(payload)
            job = self.jobs[self.keys[key]]
            return {name: job[name] for name in ("jobId", "state", "instrumentDigest", "resumeAfter", "error")}
        if method == "GET" and "/results" in route:
            job = self.jobs[route.split("/")[2]]
            return {"results": [job["result"]], "hasMore": False, "nextCursor": None}
        raise AssertionError(f"unexpected broker route {method} {route}")

    def _settle(self, payload) -> str:
        (item,) = payload["items"]
        digest = payload["instrumentDigest"]
        bundle = self.installed[digest]
        job_id = f"job-{len(self.jobs) + 1}"
        job = {"jobId": job_id, "state": "completed", "instrumentDigest": digest, "resumeAfter": None, "error": None,
               "payload": payload}
        try:
            request = daemon_request(bundle, item["input"], self.worker_model)
        except Mismatch as refusal:
            self.refused.append(str(refusal))
            job["result"] = {"id": item["id"], "state": "failed", "attempts": 0, "error": str(refusal),
                             "errorCode": "INSTRUMENT_MESSAGE_MISMATCH"}
            self.jobs[job_id] = job
            return job_id
        self.sent.append((digest, request))
        result = {"id": item["id"], "attempts": 1, "httpStatus": 200, "usage": {"input": 120, "output": 40},
                  "finishReason": "stop", "servedModel": self.served, "systemFingerprint": "fp-daemon",
                  "requestSha256": request_sha256(request),
                  "output": self.answer(request["messages"], {})}
        required = bundle["requiredModelFingerprint"]
        if required and required != self.served:
            drift = f"Model drift: the instrument requires {required}; the provider served {self.served}."
            job.update(state="paused", error=drift)
            result.update(state="paused", errorCode="MODEL_DRIFT", error=drift)
        else:
            result["state"] = "completed"
        job["result"] = result
        self.jobs[job_id] = job
        return job_id


def exported_bundles() -> list[dict]:
    return [instruments.broker_bundle(VERBATIM, tier=tier) for tier in TIERS]


class VerbatimSupportInstrumentTests(unittest.TestCase):
    def test_it_carries_citation_support_2s_prompt_schema_validator_and_class_map_unchanged(self):
        verbatim, support = instruments.load(VERBATIM), instruments.load("citation-support@2")
        self.assertTrue(instruments.verify(verbatim)["ok"])
        self.assertEqual(SUPPORT_DIGEST, support.digest, "citation-support@2 stays registered unchanged")
        self.assertIn("citation-support@2", instruments.available())
        for role in ("systemPrompt", "outputSchema"):
            self.assertEqual(support.file_path(role), verbatim.file_path(role))
            self.assertEqual(support.manifest["files"][role]["sha256"], verbatim.manifest["files"][role]["sha256"])
        self.assertEqual(corpus_assessment.PROMPT_PATH.resolve(), verbatim.file_path("systemPrompt"))
        self.assertEqual(support.module_digests(), verbatim.module_digests())
        self.assertEqual(support.manifest["validator"]["module"], verbatim.manifest["validator"]["module"])
        self.assertEqual(support.manifest["validator"]["id"], verbatim.manifest["validator"]["id"])
        self.assertEqual(verbatim.module_digests()["validator"], verbatim.manifest["validator"]["sha256"])
        self.assertEqual(support.manifest["classMap"], verbatim.manifest["classMap"])
        self.assertEqual(support.manifest["unit"], verbatim.manifest["unit"])
        self.assertEqual(sorted(support.manifest["tiers"]), sorted(verbatim.manifest["tiers"]))
        self.assertNotEqual(support.digest, verbatim.digest)
        self.assertEqual(("citation-support-verbatim", 1), instruments.parse_id(VERBATIM))
        self.assertEqual(("citation-support@2", VERBATIM), SUPPORT_INSTRUMENTS)
        self.assertIn(VERBATIM, BROKER_INSTRUMENTS)

    def test_each_tier_declares_its_request_settings_and_one_item_per_call(self):
        verbatim = instruments.load(VERBATIM)
        census = instruments.load("citation-census@1")
        self.assertEqual({"served": census.manifest["requiredModel"]["served"], "fingerprint": "record-when-offered",
                          "onMismatch": "pause"}, verbatim.manifest["requiredModel"])
        self.assertEqual("verbatim", verbatim.manifest["transport"]["messages"])
        for tier, reasoning in (("abstract", "none"), ("full-text", "low")):
            with self.subTest(tier=tier):
                spec = verbatim.tier(tier)
                request = spec["request"]
                self.assertEqual((1, 0.0, None, reasoning, False, 3000, 700, 1),
                                 (spec["geometry"]["itemsPerCall"], request["temperature"], request["seed"],
                                  request["reasoningEffort"], request["stream"], request["maxOutputTokens"]["floor"],
                                  request["maxOutputTokens"]["perItem"], spec["repair"]["passes"]))
                self.assertEqual("on-premises judges only", spec["evidencePolicy"]["entitledRouting"])
                plan = plan_for(verbatim, tier)
                self.assertEqual({"temperature": 0.0, "max_tokens": 3000, "stream": False,
                                  "reasoning_effort": reasoning}, plan.settings(1))
                self.assertEqual((1, 1), (plan.items_per_call, plan.repair_passes))
        with self.assertRaisesRegex(ValidationError, "declares its own request settings"):
            plan_for(verbatim, "abstract", {"reasoningEffort": "high"})

    def test_each_tier_exports_its_own_broker_bundle(self):
        verbatim = instruments.load(VERBATIM)
        sent = corpus_assessment.build_messages([{"id": "locus-1|abstract", "claim": "A claim.", "complete": True,
                                                  "evidenceUnits": [{"id": "u1", "text": "A complete abstract."}],
                                                  "sourceSha256": "a" * 64}], "abstract")[0]["content"]
        digests = set()
        for tier, reasoning in (("abstract", "none"), ("full-text", "low")):
            with self.subTest(tier=tier):
                exported = instruments.broker_bundle(VERBATIM, tier=tier)
                bundle = exported["bundle"]
                self.assertEqual((VERBATIM, verbatim.digest, tier, {}),
                                 (exported["instrumentId"], exported["registryDigest"], exported["tier"],
                                  exported["parameters"]))
                self.assertEqual(sent, bundle["systemPrompt"], "the system message the planner sends, byte for byte")
                self.assertEqual({"instrumentId": VERBATIM, "version": 1, "responseSchema": None,
                                  "maxOutputTokens": {"perItem": 700, "minimum": 3000}, "temperature": 0.0,
                                  "seed": None, "reasoning": reasoning, "itemsPerCall": 1,
                                  "requiredModelFingerprint": MODEL, "transport": {"messages": "verbatim"}},
                                 {key: value for key, value in bundle.items() if key != "systemPrompt"})
                digests.add(exported["digest"])
        self.assertEqual(2, len(digests), "one broker bundle per tier")
        with self.assertRaisesRegex(ValidationError, "choose one with --tier"):
            instruments.broker_bundle(VERBATIM)

    def test_instruments_export_prints_each_tiers_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            for tier in TIERS:
                with self.subTest(tier=tier):
                    target = Path(directory) / f"export-{tier}.json"
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        code = main(["--result-file", str(target), "instruments", "export", VERBATIM, "--tier", tier])
                    record = json.loads(target.read_text(encoding="utf-8"))
                    self.assertEqual(0, code, record)
                    self.assertEqual(instruments.broker_bundle(VERBATIM, tier=tier), record["result"])
                    self.assertEqual(f"PUT /v2/instruments/{record['result']['digest']}", record["result"]["install"])

    def test_replicates_are_compared_on_the_shared_class_map(self):
        for verdict, label in (("direct-support", "supported"), ("contradicted", "adverse"),
                               ("partial-indirect", "uncertain")):
            self.assertEqual(label, agreement_label(VERBATIM, {"supportVerdict": verdict}))
            self.assertEqual(label, agreement_label("citation-support@2", {"supportVerdict": verdict}))

    def test_it_sends_what_citation_support_2_sends_under_a_protocol_predeclaring_none_and_low(self):
        """The same locus gets the same messages and request settings from both instruments when the protocol
        predeclares reasoning off at the abstract tier and low at the full-text tier for citation-support@2, and one
        answer is read the same way by both."""
        verbatim, support = instruments.load(VERBATIM), instruments.load("citation-support@2")
        predeclared = F.PROTOCOL["instruments"]["supportSettings"]
        self.assertEqual({"abstract": {"reasoningEffort": "none"}, "full-text": {"reasoningEffort": "low"}},
                         predeclared)
        for tier in TIERS:
            with self.subTest(tier=tier):
                items = [{key: value for key, value in item.items() if key != "provenance"}
                         for item in support_items(tier, 1)]
                ours, theirs = module_for(verbatim), module_for(support)
                messages = ours.build_messages(items, tier)
                self.assertEqual(theirs.build_messages(items, tier), messages)
                self.assertEqual(plan_for(support, tier, predeclared[tier]).settings(1),
                                 plan_for(verbatim, tier).settings(1))
                self.assertEqual((1, 1), (plan_for(support, tier, predeclared[tier]).items_per_call,
                                          plan_for(verbatim, tier).items_per_call))
                for verdict in ("direct-support", "contradicted", "abstain"):
                    text = quoting(verdict)(messages, {})
                    self.assertEqual(theirs.validate_response(items, text), ours.validate_response(items, text))


def support_items(tier: str, count: int = 2) -> list[dict]:
    return [{"id": f"locus-{number}|{tier}", "claim": f"Marker zeta number {number} doubles the risk.",
             "context": [], "citationLocus": {"bindingStatus": "exact", "marker": "", "section": "Results"},
             "citedWork": {"title": "A cited work", "year": 2020}, "evidenceTier": tier, "complete": True,
             "evidenceUnits": [{"id": "u0001", "text": f"Marker zeta number {number} doubled the risk in adults."}],
             "sourceSha256": "c" * 64, "identityVerified": True, "provenance": "public"} for number in range(count)]


class DaemonCheckTests(unittest.TestCase):
    """The broker judge's support calls against the daemon's verbatim check."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        census = F.make_parent(self.root)
        path = self.root / "branch.sqlite3"
        FollowupBranch.create(path, census["parent"], protocol=verbatim_protocol(), study_bundle="b" * 64)
        self.branch = FollowupBranch(path)
        self.instrument = instruments.load(VERBATIM)

    def judge(self, broker):
        transport = BrokerTransport(str(self.root / "unused.sock"), client=broker)
        transport.open()
        judge = B.BrokerJudge(transport, model=MODEL, location="on-prem", wait_seconds=5)
        self.addCleanup(judge.close)
        return judge

    def run_tier(self, judge, tier, items=None):
        with self.branch.writing() as connection:
            return run_items(connection, self.instrument, items or support_items(tier), judge, tier=tier, model=MODEL)

    def test_support_calls_pass_the_daemon_check_one_item_per_call_under_their_tiers_bundle(self):
        broker = DaemonBroker(exported_bundles())
        for tier, reasoning in (("abstract", "none"), ("full-text", "low")):
            with self.subTest(tier=tier):
                judge = self.judge(broker)
                judge.prepare(self.instrument, tier)
                before = len(broker.jobs)
                summary = self.run_tier(judge, tier)
                self.assertEqual((2, 0, 2, None), (summary["accepted"], summary["rejected"], summary["calls"],
                                                   summary["stopped"]))
                digest = instruments.broker_bundle(VERBATIM, tier=tier)["digest"]
                jobs = list(broker.jobs.values())[before:]
                self.assertEqual({digest}, {job["instrumentDigest"] for job in jobs})
                for job in jobs:
                    (item,) = job["payload"]["items"]
                    self.assertEqual({"messages", "itemCount", "model", "max_tokens", "temperature", "stream",
                                      "reasoning_effort"}, set(item["input"]))
                    self.assertEqual((1, reasoning, 3000, MODEL),
                                     (item["input"]["itemCount"], item["input"]["reasoning_effort"],
                                      item["input"]["max_tokens"], item["input"]["model"]))
                    self.assertEqual("completed", job["result"]["state"])
                self.assertEqual({f"{VERBATIM} {tier}": digest}, {label: value for label, value in judge.bundles.items()
                                                                  if label.endswith(tier)})
        self.assertEqual([], broker.refused)
        self.assertEqual({instruments.broker_bundle(VERBATIM, tier=tier)["digest"] for tier in TIERS},
                         {digest for digest, _ in broker.sent})
        with closing(self.branch.connect()) as connection:
            calls = connection.execute("SELECT tier, served_model, fingerprint, judge_location, study_bundle_sha256 "
                                       "FROM fu_calls").fetchall()
        self.assertEqual(4, len(calls))
        self.assertEqual({(MODEL, "fp-daemon", "on-prem", "b" * 64)}, {tuple(call)[1:] for call in calls})

    def test_the_tiers_sharing_one_prompt_are_told_apart_by_their_settings(self):
        judge = self.judge(DaemonBroker(exported_bundles()))
        messages = corpus_assessment.build_messages([{key: value for key, value in item.items() if key != "provenance"}
                                                     for item in support_items("full-text", 1)], "full-text")
        for tier in TIERS:
            with self.subTest(tier=tier):
                settings = plan_for(self.instrument, tier).settings(1)
                exported = judge.bundle_for(messages, settings)
                self.assertEqual((VERBATIM, tier), (exported["instrumentId"], exported["tier"]))
        other = plan_for(self.instrument, "abstract").settings(1) | {"max_tokens": 4000}
        with self.assertRaisesRegex(ValidationError, "fit several broker bundles"):
            judge.bundle_for(messages, other)
        judge.prepare(self.instrument, "full-text")
        self.assertEqual("full-text", judge.bundle_for(messages, other)["tier"],
                         "settings that fit no tier go out under the prepared tier, which the broker refuses")

    def test_a_call_with_another_setting_is_refused_before_any_provider_call(self):
        broker = DaemonBroker(exported_bundles())
        judge = self.judge(broker)
        judge.prepare(self.instrument, "abstract")

        class Wider:
            """citation-support-verbatim@1's abstract tier, but with a larger output-token floor."""
            def __getattr__(self, name):
                return getattr(self.plan, name)

            def __init__(self, plan):
                self.plan = plan

            def settings(self, items):
                return {**self.plan.settings(items), "max_tokens": 4000}
        with mock.patch("ocsl_review.followup_judge.plan_for",
                        lambda instrument, tier, settings=None: Wider(plan_for(instrument, tier, settings))):
            summary = self.run_tier(judge, "abstract")
        self.assertEqual((0, 1, "fidelity"), (summary["accepted"], summary["failedCalls"], summary["stopped"]))
        self.assertEqual([], broker.sent, "no request reached a provider")
        self.assertTrue(broker.refused and broker.refused[0].startswith("max_tokens 4000"), broker.refused)
        result = step_result(summary, judge)
        self.assertTrue(result["reason"].startswith("fidelity: "), result["reason"])
        self.assertEqual("FIDELITY", result["transport"]["halted"]["code"])

    def test_another_served_model_pauses_at_the_broker_as_model_drift(self):
        broker = DaemonBroker(exported_bundles(), served="another-model")
        judge = self.judge(broker)
        judge.prepare(self.instrument, "abstract")
        summary = self.run_tier(judge, "abstract")
        self.assertEqual(("model-drift", 0), (summary["stopped"], summary["accepted"]))
        self.assertEqual("MODEL_DRIFT", judge.transport.halted["code"])
        result = step_result(summary, judge)
        self.assertEqual("paused", result["state"])
        self.assertTrue(result["reason"].startswith("model_drift: served 'another-model'"), result["reason"])

    @staticmethod
    def answers_after(invalid: int):
        """A judge whose first ``invalid`` answers cannot be read and whose later answers are valid."""
        valid = quoting()
        given: list[int] = []

        def answer(messages, settings):
            given.append(1)
            return "not json" if len(given) <= invalid else valid(messages, settings)
        return answer, given

    def test_a_repair_call_starts_a_job_of_its_own_and_reaches_the_model(self):
        answer, given = self.answers_after(1)
        broker = DaemonBroker(exported_bundles(), answer=answer)
        judge = self.judge(broker)
        judge.prepare(self.instrument, "abstract")
        summary = self.run_tier(judge, "abstract", support_items("abstract", 1))
        self.assertEqual((1, 1, 1, 0), (summary["calls"], summary["repairCalls"], summary["accepted"],
                                        summary["invalid"]))
        self.assertEqual((2, 2, 2), (len(broker.jobs), len(broker.sent), len(given)),
                         "the repair call is a new broker job and a new request to the model")
        with closing(self.branch.connect()) as connection:
            responses = connection.execute("SELECT kind, response_sha256 FROM fu_calls ORDER BY kind").fetchall()
        self.assertEqual(["first", "repair"], [row[0] for row in responses])
        self.assertNotEqual(responses[0][1], responses[1][1])

    def test_a_later_invocation_judges_an_item_left_invalid_again(self):
        answer, given = self.answers_after(2)
        broker = DaemonBroker(exported_bundles(), answer=answer)
        items = support_items("abstract", 1)
        outcomes = []
        for _ in range(2):
            judge = self.judge(broker)
            judge.prepare(self.instrument, "abstract")
            summary = self.run_tier(judge, "abstract", items)
            outcomes.append((summary["accepted"], summary["invalid"]))
        self.assertEqual([(0, 1), (1, 0)], outcomes)
        self.assertEqual((3, 3), (len(broker.sent), len(given)),
                         "the second invocation's first call reaches the model instead of replaying the first answer")

    def test_a_replicate_starts_jobs_of_its_own_and_is_judged_afresh(self):
        valid, abstaining = quoting(), quoting("abstain")
        seen: dict[str, int] = {}

        def answer(messages, settings):
            # The model answers a request it has answered before with an abstention: replicate verdicts can differ.
            key = request_sha256({"messages": messages})
            seen[key] = seen.get(key, 0) + 1
            return (valid if seen[key] == 1 else abstaining)(messages, settings)
        broker = DaemonBroker(exported_bundles(), answer=answer)
        items = support_items("abstract", 2)
        primary_judge = self.judge(broker)
        primary_judge.prepare(self.instrument, "abstract")
        self.run_tier(primary_judge, "abstract", items)
        jobs, sent = len(broker.jobs), len(broker.sent)
        replicate_judge = self.judge(broker)
        replicate_judge.prepare(self.instrument, "abstract")
        with self.branch.writing() as connection:
            run_items(connection, self.instrument, items, replicate_judge, tier="abstract", model=MODEL,
                      variant="replicate")
        self.assertEqual((jobs + 2, sent + 2), (len(broker.jobs), len(broker.sent)),
                         "every replicate call is a new broker job and a new request to the model")
        with closing(self.branch.connect()) as connection:
            verdicts = {(row[0], row[1]): json.loads(row[2])["supportVerdict"] for row in connection.execute(
                "SELECT variant, item_key, verdict_json FROM fu_judgments WHERE outcome='accepted'")}
            shared = connection.execute(
                "SELECT COUNT(*) FROM fu_calls r JOIN fu_calls p ON p.response_sha256=r.response_sha256 "
                "WHERE r.variant='replicate' AND p.variant='primary'").fetchone()[0]
        self.assertEqual(0, shared, "no replicate receipt carries a primary answer")
        for item in items:
            self.assertEqual("abstain", verdicts[("replicate", item["id"])])
            self.assertNotEqual("abstain", verdicts[("primary", item["id"])])

    def test_a_call_that_never_settled_meets_its_job_on_the_next_invocation(self):
        class PausedFirst(DaemonBroker):
            """The first job pauses on a provider limit that lifts only later; the next submission of its key finds it
            completed."""
            def _settle(self, payload):
                job_id = super()._settle(payload)
                if len(self.jobs) == 1:
                    job = self.jobs[job_id]
                    self.held = (job_id, job["state"], dict(job["result"]))
                    job.update(state="paused", resumeAfter="2999-01-01T00:00:00Z", error="PROVIDER_LIMIT")
                    job["result"] = {**job["result"], "state": "paused", "errorCode": "PROVIDER_LIMIT"}
                return job_id

            def lift(self):
                job_id, state, result = self.held
                self.jobs[job_id].update(state=state, resumeAfter=None, error=None, result=result)

        broker = PausedFirst(exported_bundles())
        items = support_items("abstract", 1)
        judge = self.judge(broker)
        judge.prepare(self.instrument, "abstract")
        first = self.run_tier(judge, "abstract", items)
        self.assertEqual((0, "transport"), (first["accepted"], first["stopped"]))
        with closing(self.branch.connect()) as connection:
            (error,) = connection.execute("SELECT error FROM fu_calls").fetchone()
        self.assertTrue(error.startswith("CallOpen: broker paused"), error)
        broker.lift()
        judge = self.judge(broker)
        judge.prepare(self.instrument, "abstract")
        second = self.run_tier(judge, "abstract", items)
        self.assertEqual(1, second["accepted"])
        self.assertEqual(1, len(broker.jobs), "the second invocation met the job the first call left at the broker")


class SupportCommandTests(unittest.TestCase):
    """``followup support`` with the verbatim instrument: through ``--broker-socket`` like function, numeric and
    second-gen, or on a judge called directly, and the refusals before any call."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        cls.cache = provider_cache(cls.root)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        for patcher in (mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network),
                        mock.patch.dict(os.environ, clean_environment(), clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def branch(self, protocol=None) -> FollowupBranch:
        type(self).count += 1
        path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(path, self.census["parent"], protocol=protocol or verbatim_protocol(),
                              study_bundle="b" * 64)
        branch = FollowupBranch(path)
        with branch.writing() as connection:
            insert_frame(connection)
        return branch

    def invoke(self, branch, broker, *argv) -> dict:
        policy = self.root / f"policy-{self.count}.json"
        policy.write_text(json.dumps({"version": 2, "mode": "public-literature",
                                      "onPremBrokerProviders": [{"provider": PROVIDER, "models": [MODEL]}]}))
        transport = F.FixtureTransport(routes())
        target = self.root / f"result-{self.count}-{len(broker.jobs)}.json"

        def broker_transport(socket_path, worker=None):
            return BrokerTransport(socket_path, worker=worker, client=broker)
        with mock.patch.object(B, "BrokerTransport", broker_transport), \
                mock.patch.object(SP, "make_client", lambda workspace=None: followup_net.client(
                    transport=transport, sleep=lambda seconds: None)), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "support", "--database", str(branch.path),
                         "--model", MODEL, "--broker-socket", str(self.root / "run-principal.sock"), "--on-premises",
                         "--policy", str(policy), "--concurrency", "2", "--transport-retries", "0", *argv])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual(["followup", "support"], record["command"])
        return record["result"]

    def rows(self, branch, tier):
        with closing(branch.connect()) as connection:
            return {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM fu_support WHERE tier=?",
                                                                               (tier,))}

    def test_support_runs_through_the_broker_socket_like_the_other_judging_commands(self):
        branch = self.branch()
        broker = DaemonBroker(exported_bundles())
        expected = {tier: instruments.broker_bundle(VERBATIM, tier=tier)["digest"] for tier in TIERS}
        result = self.invoke(branch, broker, "--tier", "abstract", "--provider-cache", str(self.cache))
        self.assertEqual(("completed", 5, 5, 5, 0), (result["state"], result["sent"], result["accepted"],
                                                      result["calls"], result["invalid"]))
        self.assertEqual((VERBATIM, instruments.load(VERBATIM).digest), (result["instrument"], result["digest"]))
        self.assertTrue(result["judge"]["name"].startswith("broker-judge:"))
        self.assertEqual(("on-prem", {f"{VERBATIM} abstract": expected["abstract"]}),
                         (result["judge"]["location"], result["judge"]["bundles"]))
        self.assertEqual({"tier": "abstract", "reasoningEffort": "none", "temperature": 0.0, "maxTokens": 3000,
                          "itemsPerCall": 1, "source": f"instrument {VERBATIM}"}, result["requestSettings"])
        self.assertEqual("run-principal-support", result["transport"]["principalId"])
        self.assertEqual({expected["abstract"]}, {job["instrumentDigest"] for job in broker.jobs.values()})
        self.assertEqual({1}, {len(job["payload"]["items"]) for job in broker.jobs.values()})
        self.assertEqual([], broker.refused)
        rows = self.rows(branch, "abstract")
        self.assertEqual(("direct-support", 1), (rows[locus("a1/4")]["verdict"], rows[locus("a1/4")]["assessable"]))
        with closing(branch.connect()) as connection:
            calls = connection.execute("SELECT instrument_id, served_model, study_bundle_sha256, request_sha256 "
                                       "FROM fu_calls").fetchall()
            bodies = [json.loads(read_artifact(connection, call["request_sha256"])) for call in calls]
        self.assertEqual({(VERBATIM, MODEL, "b" * 64)}, {tuple(call)[:3] for call in calls})
        sent = [request for _, request in broker.sent]
        for body in bodies:
            self.assertIn(body, sent, "every receipt's request is the one the daemon sent upstream")
        full = self.invoke(branch, broker, "--tier", "full-text", "--europe-pmc")
        self.assertEqual(("completed", 2, {f"{VERBATIM} full-text": expected["full-text"]}, "low"),
                         (full["state"], full["accepted"], full["judge"]["bundles"],
                          full["requestSettings"]["reasoningEffort"]))
        self.assertEqual({"none", "low"}, {request["reasoning_effort"] for _, request in broker.sent})
        self.assertEqual("direct-support", self.rows(branch, "full-text")[locus("a1/1")]["verdict"])

    def test_citation_support_2_still_refuses_the_broker(self):
        branch = self.branch(F.protocol())
        broker = DaemonBroker(exported_bundles(), worker_model=F.MODEL)
        transport = BrokerTransport(str(self.root / "unused.sock"), client=broker)
        transport.open()
        judge = B.BrokerJudge(transport, model=F.MODEL, location="on-prem", wait_seconds=5)
        self.addCleanup(judge.close)
        scholarly = followup_net.client(transport=F.FixtureTransport(routes()), sleep=lambda seconds: None)
        with branch.writing() as connection, self.assertRaisesRegex(ValidationError, "no verbatim message transport"):
            SP.run_support(connection, branch, judge, None, tier="abstract", model=F.MODEL, scholarly=scholarly)
        self.assertEqual({}, broker.jobs)

    def test_the_protocol_must_pin_the_instruments_served_model(self):
        protocol = verbatim_protocol()
        protocol["model"] = {"served": F.MODEL, "location": "on-premises"}
        branch = self.branch(protocol)
        judge = F.ScriptedJudge(quoting())
        scholarly = followup_net.client(transport=F.FixtureTransport(routes()), sleep=lambda seconds: None)
        with branch.writing() as connection, self.assertRaisesRegex(ValidationError, "requires the served model"):
            SP.run_support(connection, branch, judge, None, tier="abstract", model=F.MODEL, scholarly=scholarly)
        self.assertEqual([], judge.requests)

    def test_a_protocol_that_predeclares_settings_for_the_verbatim_instrument_is_refused(self):
        protocol = verbatim_protocol(supportSettings=copy.deepcopy(F.PROTOCOL["instruments"]["supportSettings"]))
        with self.assertRaisesRegex(ValidationError, "supportSettings"):
            FollowupBranch.create(self.root / f"refused-{self.count}.sqlite3", self.census["parent"],
                                  protocol=protocol)
        with self.assertRaisesRegex(ValidationError, "must be left out"):
            SP.support_settings(protocol, "abstract", instruments.load(VERBATIM))
        self.assertIsNone(SP.support_settings(verbatim_protocol(), "full-text", instruments.load(VERBATIM)))
        self.assertEqual({"reasoningEffort": "low"},
                         SP.support_settings(F.protocol(), "full-text", instruments.load("citation-support@2")))
        with self.assertRaisesRegex(ValidationError, "support runs"):
            SP.support_instrument({"instruments": {"support": "citation-support@3"}})

    def test_a_judge_called_directly_gets_the_settings_each_tier_declares(self):
        branch = self.branch()
        scholarly = followup_net.client(transport=F.FixtureTransport(routes()), sleep=lambda seconds: None)
        for tier, reasoning, options in (("abstract", "none", {}), ("full-text", "low", {"europe_pmc": True})):
            with self.subTest(tier=tier):
                judge = F.ScriptedJudge(quoting(), model=MODEL)
                with branch.writing() as connection:
                    result = SP.run_support(connection, branch, judge, None, tier=tier, model=MODEL,
                                            scholarly=scholarly, **options)
                self.assertTrue(judge.requests)
                self.assertEqual({(reasoning, 3000, 0.0, False)},
                                 {(settings["reasoning_effort"], settings["max_tokens"], settings["temperature"],
                                   settings["stream"]) for _, settings in judge.requests})
                self.assertEqual({1}, {len(json.loads(messages[1]["content"])["items"])
                                       for messages, _ in judge.requests})
                self.assertEqual((VERBATIM, f"instrument {VERBATIM}"),
                                 (result["run"]["instrument"], result["requestSettings"]["source"]))


class BrokerPipelineTests(unittest.TestCase):
    """The audit end to end with every judging command on one run principal's broker socket: the protocol pins
    citation-support-verbatim@1 and every registry digest, the study bundle pins the four instruments, and the fake
    socket applies the daemon's verbatim check to every call before it answers with the pipeline's scripted judge."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.census = F.make_parent(self.root)
        self.database = self.root / "followup.sqlite3"
        protocol = pipeline_protocol()
        protocol["model"] = {"served": MODEL, "location": "on-premises"}
        named = {key: value for key, value in protocol["instruments"].items() if key != "supportSettings"}
        protocol["instruments"] = {**named, "support": VERBATIM}
        self.pinned = {instrument_id: instruments.load(instrument_id).digest
                       for instrument_id in protocol_instruments(protocol)}
        protocol["instruments"]["digests"] = dict(self.pinned)
        self.protocol = self.root / "protocol.json"
        self.protocol.write_text(json.dumps(protocol), encoding="utf-8")
        bundle = instruments.study_bundle_digest(protocol=protocol, study_prompt="synthetic study prompt",
                                                 instruments=self.pinned, dataset_versions={}, package_tags={})
        self.bundle = self.root / "study-bundle.json"
        self.bundle.write_text(json.dumps(bundle), encoding="utf-8")
        self.bundle_sha = bundle["sha256"]
        self.exported = {(record["instrumentId"], record["tier"]): record
                         for instrument_id in BROKER_INSTRUMENTS
                         for record in (instruments.broker_bundle(instrument_id, tier=tier)
                                        for tier in sorted(instruments.load(instrument_id).manifest["tiers"]))}
        self.broker = DaemonBroker(list(self.exported.values()), answer=pipeline_answer)
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"version": 2, "mode": "public-literature",
                                           "onPremBrokerProviders": [{"provider": PROVIDER, "models": [MODEL]}]}),
                               encoding="utf-8")
        for patcher in (mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network),
                        mock.patch.dict(os.environ, clean_environment(), clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = 0

    def invoke(self, verb: str, *argv: str, routes=None) -> dict:
        self.calls += 1
        target = self.root / f"result-{self.calls}-{verb}.json"

        def broker_transport(socket_path, worker=None):
            return BrokerTransport(socket_path, worker=worker, client=self.broker)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(B, "BrokerTransport", broker_transport))
            if routes is not None:
                transport = F.FixtureTransport(routes)
                stack.enter_context(mock.patch("ocsl_review.followup_net.client", lambda **kwargs: REAL_CLIENT(
                    transport=transport, sleep=lambda seconds: None)))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(redirect_stderr(StringIO()))
            code = main(["--result-file", str(target), "followup", verb, *argv])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, f"followup {verb}: {record.get('error')}")
        return record["result"]

    def rows(self, sql: str, *params) -> list[dict]:
        with closing(FollowupBranch(self.database).connect()) as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def test_every_judging_command_runs_through_the_broker_and_every_gate_holds(self):
        database = ("--database", str(self.database))
        judge = ("--model", MODEL, "--broker-socket", str(self.root / "run-principal.sock"), "--on-premises",
                 "--policy", str(self.policy))
        self.invoke("init", *database, "--parent", str(self.census["parent"]), "--parent-text",
                    str(self.census["text"]), "--protocol", str(self.protocol), "--study-bundle", str(self.bundle))
        self.invoke("status-import", *database, "--csv", str(RW_CSV), "--declared-version", "synthetic-snapshot")
        self.invoke("status-fetch", *database, routes=status_routes())
        self.invoke("status-resolve", *database)
        self.invoke("hotspots", *database)
        self.invoke("strata", *database, "--freeze")
        full_text = {**routes(), FULL_TEXT.format("PMC900004"): (200, PLAIN_FULL_TEXT)}
        judged = {
            (VERBATIM, "abstract"): self.invoke("support", *database, "--tier", "abstract", *judge, routes=routes()),
            (VERBATIM, "full-text"): self.invoke("support", *database, "--tier", "full-text", "--europe-pmc", *judge,
                                                 routes=full_text)}
        jobs_before, sent_before = len(self.broker.jobs), len(self.broker.sent)
        replicate = self.invoke("support", *database, "--tier", "full-text", "--variant", "replicate", *judge,
                                routes=full_text)
        # the replicate's agreement compares fresh answers: every replicate call is a broker job and a model request
        self.assertEqual((replicate["calls"], replicate["calls"]),
                         (len(self.broker.jobs) - jobs_before, len(self.broker.sent) - sent_before))
        judged[("citation-function@1", "locus")] = self.invoke("function", *database, *judge)
        self.invoke("dependency", *database)
        judged[("numeric-check@1", None)] = self.invoke("numeric", *database, "--tables", str(TABLES),
                                                        "--allow-synthetic-tables", *judge)
        self.invoke("citedby", *database, routes=citedby_routes())
        judged[("claim-match@1", None)] = self.invoke("second-gen", *database, *judge, routes=SECONDGEN_ROUTES)
        for (instrument_id, tier), result in judged.items():
            with self.subTest(instrument=instrument_id, tier=tier):
                self.assertEqual("completed", result["state"], result.get("reason"))
                self.assertGreater(result["calls"], 0)
                self.assertTrue(result["judge"]["name"].startswith("broker-judge:"))
                self.assertEqual("on-prem", result["judge"]["location"])
                expected = {f"{key[0]} {key[1]}": record["digest"] for key, record in self.exported.items()
                            if key[0] == instrument_id and (tier is None or key[1] == tier)}
                self.assertEqual(expected, result["judge"]["bundles"])
        self.assertEqual(("completed", "full-text"), (replicate["state"], replicate["tier"]))
        self.assertGreater(replicate["accepted"], 0)
        # every call passed the daemon's verbatim check, and every installed bundle carried calls
        self.assertEqual([], self.broker.refused)
        self.assertEqual({record["digest"] for record in self.exported.values()},
                         {job["instrumentDigest"] for job in self.broker.jobs.values()})
        calls = self.rows("SELECT instrument_id, instrument_digest, served_model, study_bundle_sha256, request_sha256 "
                          "FROM fu_calls")
        self.assertEqual({(instrument_id, digest) for instrument_id, digest in self.pinned.items()},
                         {(call["instrument_id"], call["instrument_digest"]) for call in calls})
        self.assertEqual({(MODEL, self.bundle_sha)}, {(call["served_model"], call["study_bundle_sha256"])
                                                      for call in calls})
        sent = [request for _, request in self.broker.sent]
        with closing(FollowupBranch(self.database).connect()) as connection:
            for call in calls:
                self.assertIn(json.loads(read_artifact(connection, call["request_sha256"])), sent)
        # the verdicts are the direct-judge pipeline's: the same dependency classes
        dependency = {row["locus_id"]: row["class"] for row in self.rows("SELECT locus_id, class FROM fu_dependency")}
        self.assertEqual({lid(F.JOURNAL_A, A1, "1"): "essential", lid(F.JOURNAL_A, A2, "1"): "sole",
                          lid(F.JOURNAL_A, A2, "2"): "sole", lid(F.JOURNAL_B, B1, "1"): "sole",
                          lid(F.JOURNAL_B, B1, "2"): "redundant"}, dependency)
        self.invoke("lag", *database)
        self.invoke("estimands", *database)
        gates = self.invoke("gates", *database)
        self.assertTrue(gates["ok"], gates)
        for check in ("instrumentDigestsAsPinned", "everyReceiptCarriesTheStudyBundle", "oneDigestPerInstrument",
                      "replicateAgreementReported", "noEntitledEvidenceToExternalJudges"):
            self.assertTrue(gates["checks"][check], check)
        agreement = {row["key"]: row["value"] for row in self.rows(
            "SELECT key, value FROM fu_estimates WHERE estimand='replicate-agreement'")}
        self.assertEqual(1.0, agreement["followup.replicate-agreement.citation-support-verbatim-1.full-text.agreement"
                                       ".all.all.value"])
        facts = self.invoke("facts", *database, "--output", str(self.root / "facts"))
        self.assertEqual(facts["count"], self.invoke("verify-facts", "--facts", str(self.root / "facts"))["facts"])
        self.invoke("export", *database, "--output", str(self.root / "exports"))
        self.invoke("verify", *database)


if __name__ == "__main__":
    unittest.main()
