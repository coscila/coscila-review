# SPDX-License-Identifier: Apache-2.0
"""Capacity probe against a local mock OpenAI-compatible judge (threads; no real model endpoint is contacted)."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import random
import re
import secrets
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ocsl_review import capacity_probe as P
from ocsl_review import citation_census as C
from ocsl_review.cli import main
from ocsl_review.errors import ValidationError

from test_census_runner import RunnerTestCase

MODEL = C.instrument().manifest["requiredModel"]["served"]
FAST = dict(cell_seconds=0.6, warmup_seconds=0.15, ramp_seconds=0.05, drain_seconds=3.0, pause_seconds=0.02,
            request_timeout=5.0, sample_seconds=0.1, baseline_samples=2, abort_window_seconds=0.5, abort_min_calls=8,
            saturation_seconds=0.3, abandon_grace_seconds=1.0, heartbeat_seconds=0.0)


class MockJudge:
    """A chat-completions server with configurable latency, failures, saturation, fingerprint and a metrics endpoint."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latency = 0.01
        self.model, self.fingerprint, self.cached = MODEL, "fp-mock-1", 64
        self.fail_every, self.fail_status, self.reset_every, self.garbage_every = 0, 500, 0, 0
        self.drop_every, self.empty_every, self.length_every = 0, 0, 0
        self.foreign, self.waiting, self.kv, self.extra_generation = 0, 0, 0.25, 0
        self.visible, self.preempt_per_fetch, self.preemptions = 1.0, 0, 0
        self.fail_message, self.generation_on_failure = "mock failure", 0
        self.close_connections, self.echo_authorization, self.models_status = False, False, 200
        self.metrics_fail_after, self.metrics_body = None, None
        self.tenant = None          # {"after": fetches, "running": n, "generation": tokens, "success": requests} per fetch
        self.requests = self.connections = self.metrics_requests = self.inflight = 0
        self.generation_total = self.prompt_total = self.success_total = 0
        self.prefix_queries = self.prefix_hits = 0
        self.dropped = self.garbage = self.empty = self.failed = self.truncated = self.resets = 0
        self.chat_connections, self.chat_authorization, self.other_authorization = set(), set(), []
        self.bodies: list[str] = []
        self.payloads: list[dict] = []

    def answer(self, k: int, payload: dict) -> tuple[str, str]:
        labels = re.findall(r"^### id=(.+)$", payload["messages"][1]["content"], flags=re.M)
        if self.empty_every and k % self.empty_every == 0:
            self.empty += 1
            return "", "stop"
        if self.drop_every and k % self.drop_every == 0 and len(labels) > 1:
            labels = labels[:-1]
            self.dropped += 1
        finish = "stop"
        if self.length_every and k % self.length_every == 0:
            self.truncated += 1
            finish = "length"
        lines = [json.dumps({"l": label, "c": "T", "v": "direct", "q": "", "e": 0, "r": "mock rationale"})
                 for label in labels]
        return "\n".join(lines), finish

    def metrics(self) -> tuple[int, bytes]:
        with self.lock:
            self.metrics_requests += 1
            if self.metrics_fail_after is not None and self.metrics_requests > self.metrics_fail_after:
                return 500, b"metrics unavailable"
            if self.metrics_body is not None:
                return 200, self.metrics_body.encode()
            self.generation_total += self.extra_generation
            self.preemptions += self.preempt_per_fetch
            tenant_running = 0
            if self.tenant and self.metrics_requests > self.tenant["after"]:
                tenant_running = self.tenant.get("running", 0)
                self.generation_total += self.tenant.get("generation", 0)
                self.success_total += self.tenant.get("success", 0)
            # ``visible`` < 1: metrics of one replica behind a balancer, which sees only part of our calls and counters
            values = {"vllm:num_requests_running": round(self.inflight * self.visible) + self.foreign + tenant_running,
                      "vllm:num_requests_waiting": self.waiting, "vllm:kv_cache_usage_perc": self.kv,
                      "vllm:num_preemptions_total": self.preemptions,
                      "vllm:prompt_tokens_total": round(self.prompt_total * self.visible),
                      "vllm:generation_tokens_total": round(self.generation_total * self.visible),
                      "vllm:request_success_total": round(self.success_total * self.visible),
                      "vllm:prefix_cache_queries_total": self.prefix_queries,
                      "vllm:prefix_cache_hits_total": self.prefix_hits}
        lines = ["# HELP vllm:num_requests_running Number of requests in model execution batches.",
                 "# TYPE vllm:num_requests_running gauge"]
        lines += ['%s{engine="0",model_name="%s"} %s' % (name, MODEL, float(value)) for name, value in values.items()]
        lines.append('vllm:generation_tokens_created{engine="0",model_name="%s"} 1.7e+09' % MODEL)
        return 200, ("\n".join(lines) + "\n").encode()


def start(judge: MockJudge):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # headers and body without a Nagle stall
            with judge.lock:
                judge.connections += 1
                self.connection_id = judge.connections

        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if judge.close_connections:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            with judge.lock:
                judge.other_authorization.append((self.path, self.headers.get("Authorization")))
            if self.path == "/metrics":
                status, body = judge.metrics()
                self.send(status, body, "text/plain; version=0.0.4")
            elif self.path == "/v1/models":
                body = json.dumps({"object": "list", "data": [{"id": judge.model, "object": "model"}]}).encode()
                self.send(judge.models_status, body if judge.models_status == 200 else b'{"error": "no"}')
            else:
                self.send(404, b"{}")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body)
            authorization = self.headers.get("Authorization")
            with judge.lock:
                judge.requests += 1
                k = judge.requests
                judge.inflight += 1
                judge.chat_connections.add(self.connection_id)
                judge.chat_authorization.add(authorization)
                judge.bodies.append(hashlib.sha256(body).hexdigest())
                if len(judge.payloads) < 50:
                    judge.payloads.append(payload)
            time.sleep(judge.latency)
            with judge.lock:
                judge.inflight -= 1          # like vLLM: a finished request leaves the running count before its response
                if judge.reset_every and k % judge.reset_every == 0:
                    judge.resets += 1
                    reset = True
                else:
                    reset = False
                failing = bool(judge.fail_every and k % judge.fail_every == 0)
                garbage = bool(judge.garbage_every and k % judge.garbage_every == 0)
                judge.failed += failing
                judge.garbage += garbage and not failing
            if reset:
                self.close_connection = True
                return
            if failing:
                with judge.lock:
                    judge.generation_total += judge.generation_on_failure
                error = {"error": {"message": judge.fail_message}}
                if judge.echo_authorization:
                    error["error"]["request_headers"] = {"Authorization": authorization}
                self.send(judge.fail_status, json.dumps(error).encode())
                return
            if garbage:
                self.send(200, b"<html>proxy error</html>", "text/html")
                return
            with judge.lock:
                content, finish = judge.answer(k, payload)
                prompt, completion = len(body) // 4, len(content) // 4 + 1
                judge.prompt_total += prompt
                judge.generation_total += completion
                judge.success_total += 1
                judge.prefix_queries += prompt
                judge.prefix_hits += min(prompt, 64)
            usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
            if judge.cached is not None:
                usage["prompt_tokens_details"] = {"cached_tokens": judge.cached}
            response = {"id": "cmpl-%d" % k, "object": "chat.completion", "model": judge.model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                     "finish_reason": finish}], "usage": usage}
            if judge.fingerprint is not None:
                response["system_fingerprint"] = judge.fingerprint
            self.send(200, json.dumps(response).encode())

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass                              # abandoned calls break pipes on purpose

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.judge = MockJudge()
        self.server = start(self.judge)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.events: list[str] = []
        self.runs = 0

    def settings(self, cells: str, *, metrics: bool = True, **overrides) -> P.ProbeSettings:
        values = {**FAST, **overrides}
        return P.ProbeSettings(base_url=self.url + "/v1", metrics_url=(self.url + "/metrics") if metrics else None,
                               cells=P.parse_cells([cells]), order=values.pop("order", "as-given"), **values)

    def probe(self, cells: str, *, metrics: bool = True, environ=None, **overrides) -> dict:
        self.runs += 1
        probe = P.CapacityProbe(self.settings(cells, metrics=metrics, **overrides), progress=self.events.append,
                                environ=environ or {})
        self.output = self.root / ("run-%d" % self.runs)
        return probe.run(self.output)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in (self.output / P.CALLS_NAME).read_text().splitlines()]


def decision_cell(name, rate, *, state="completed", invalid=0.001, model=MODEL, measured=240.0, p95=60.0,
                  completed=None, output=30.0, prompt=500.0, tail=True, reason=None, rule=None):
    """A cell summary as the probe writes it, for the decision rules."""
    streams, items = map(int, name.split("x"))
    return {"cell": name, "streams": streams, "itemsPerCall": items, "state": state, "reason": reason,
            "role": P.cell_role(streams, items, P.ProbeSettings()),
            "abort": {"rule": rule or {"saturated": "saturation", "metrics-lost": "metrics-unavailable",
                                       "aborted": "transport-failures"}.get(state)} if state != "completed" else None,
            "entries": {"perSecond": rate, "perSecondCompleted": rate if completed is None else completed},
            "invalidOutputShare": invalid, "transportFailureShare": 0.0, "latencySeconds": {"p95": p95},
            "timing": {"cellSeconds": 300.0, "warmupSeconds": 30.0, "measuredSeconds": measured, "tailComplete": tail},
            "tokens": {"promptPerEntry": prompt, "completionPerEntry": output},
            "served": {"models": {model: 10}, "requiredModel": MODEL, "matchesInstrument": model == MODEL}}


def simulate_processor_sharing(*, streams=20, capacity=8.0, ramp=5.0, window=(10.0, 150.0), tail=True, seed=1,
                               spread=0.5, items=10):
    """Calls of ``streams`` closed-loop streams on a processor-sharing server of ``capacity`` entries per second.

    Streams start staggered over ``ramp`` and keep sending until the window ends, and with ``tail`` until every
    call that overlaps the window's end has finished.
    """
    rng = random.Random(seed)
    pending = sorted((i * ramp / streams, i) for i in range(streams))
    active: dict[int, list] = {}
    records, t, edge = [], 0.0, None

    def call(i, now):
        size = max(1, round(items * rng.uniform(1 - spread, 1 + spread)))
        active[i] = [now, float(size), size]
    while pending or active:
        n = len(active)
        done = min(((w[1] * n / capacity) + t, i) for i, w in active.items()) if active else (float("inf"), None)
        begin = pending[0] if pending else (float("inf"), None)
        step = min(done[0], begin[0]) - t
        for w in active.values():
            w[1] -= step * capacity / n
        t += step
        if begin[0] <= done[0]:
            pending.pop(0)
            call(begin[1], t)
            continue
        w = active.pop(done[1])
        records.append({"start": w[0], "end": t, "valid": w[2]})
        if edge is None and t >= window[1]:
            edge = {j for j, c in active.items() if c[0] < window[1]}
        if edge is not None:
            edge.discard(done[1])
        if t < window[1] or (tail and edge):
            call(done[1], t)
    return records


class RequestTests(ProbeCase):
    def test_requests_are_the_instruments_first_calls_over_synthetic_entries(self):
        report = self.probe("2x1,2x5")
        self.assertEqual("completed", report["state"])
        journal = P.DEFAULT_JOURNAL
        for payload in self.judge.payloads:
            items = len(re.findall(r"^### id=", payload["messages"][1]["content"], flags=re.M))
            self.assertEqual(MODEL, payload["model"])
            self.assertEqual(0.0, payload["temperature"])
            self.assertEqual("none", payload["reasoning_effort"])
            self.assertIs(False, payload["stream"])
            self.assertEqual(max(3000, 700 * items), payload["max_tokens"])
            self.assertEqual(C.system_prompt(journal), payload["messages"][0]["content"])
            self.assertNotIn("doi:10.5555/capacity-probe", payload["messages"][1]["content"])  # printed entry never sent
        # Every recorded body is rebuilt byte for byte from the cell's synthetic batch with the instrument's builders.
        settings = self.settings("2x1,2x5")
        sent = set(self.judge.bodies)
        for record in self.calls()[:6] + self.calls()[-6:]:
            streams, items = map(int, record["cell"].split("x"))
            batch = P.cell_corpus(settings, streams, items).batch(items, record["batch"])
            payload, body, labels = P.build_request(batch, journal=journal, model=MODEL)
            self.assertEqual(C.build_abstract_messages(batch.items, batch.title, journal), payload["messages"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), record["bodySha256"])
            self.assertIn(record["bodySha256"], sent)
            self.assertEqual(len(labels), record["items"])
        self.assertEqual(report["instrument"]["digest"], C.instrument().digest)
        self.assertEqual({"1": 3000, "5": 3500}, report["request"]["maxTokens"])
        self.assertEqual(P.FORMAT, report["format"])

    def test_synthetic_entries_are_deterministic_bounded_and_grouped_by_review(self):
        first, again, other = P.SyntheticCorpus(7, "60x40"), P.SyntheticCorpus(7, "60x40"), P.SyntheticCorpus(7, "60x10")
        a = [P.build_request(b, journal="J", model="m")[1] for b, _ in zip(first.batches(40), range(6))]
        b = [P.build_request(b, journal="J", model="m")[1] for b, _ in zip(again.batches(40), range(6))]
        c = [P.build_request(b, journal="J", model="m")[1] for b, _ in zip(other.batches(40), range(6))]
        self.assertEqual(a, b)
        self.assertFalse(set(a) & set(c))
        cut, sentences, missing, cut_count, total = C.abstract_cut(), set(), 0, 0, 0
        restated, passages = 0, 0
        for batch, _ in zip(first.batches(40), range(25)):
            numbers = [int(item["label"]) for item in batch.items]
            self.assertEqual(list(range(numbers[0], numbers[0] + len(numbers))), numbers)   # bibliography order
            self.assertLessEqual(len(batch.items), 40)
            for item in batch.items:
                total += 1
                sentences.add(len(item["citing_passages"]))
                self.assertTrue(item["printed"].endswith("." + item["label"]))
                self.assertIn("⟨B%s⟩" % item["label"], item["citing_passages"][0]["sentence"])
                evidence = C.abstract_evidence(item)
                self.assertLessEqual(evidence["sentCharacters"], cut)
                missing += not item["abstract"]
                cut_count += evidence["cut"]
                if item["abstract"]:
                    words = set(item["abstract"].lower().replace(".", " ").replace(":", " ").split())
                    for passage in item["citing_passages"]:
                        passages += 1
                        text = passage["sentence"].rsplit(" ⟨", 1)[0].lower().replace(".", " ").replace(":", " ")
                        restated += set(text.split()) <= words
        self.assertEqual({1, 2, 3}, sentences)
        self.assertTrue(0 < missing < total * 0.15)
        self.assertTrue(0.05 < cut_count / total < 0.4)
        # Most citing sentences restate a span of the entry's own abstract (the profile's citingFromAbstract share).
        self.assertAlmostEqual(P.PROFILE["citingFromAbstract"]["share"], restated / passages, delta=0.06)

    def test_grid_order_cells_and_limits(self):
        settings = P.ProbeSettings(max_streams=60)
        grid = P.ordered_grid(settings)
        self.assertEqual(16, len(grid))
        # The instrument's geometry within the allowed concurrency first (the deciding cell before any exploratory
        # geometry), then the other cells within it, then the cells above it, each group by ascending load.
        self.assertEqual(["30x40", "60x40", "30x1", "60x1", "30x5", "30x10", "60x5", "60x10", "90x1", "120x1"],
                         [g["cell"] for g in grid[:10]])
        roles = {g["cell"]: (g["role"], g["aboveAllowed"]) for g in grid}
        self.assertEqual(("decisive", False), roles["60x40"])
        self.assertEqual(("instrument", False), roles["30x40"])
        self.assertEqual(("exploratory", False), roles["60x10"])
        self.assertEqual(("instrument", True), roles["90x40"])
        self.assertEqual("120x40", grid[-1]["cell"])
        self.assertEqual("above --max-streams 60", grid[-1]["skip"])
        self.assertEqual(8, sum(1 for g in grid if g["skip"]))
        self.assertEqual(["30x40", "60x40", "90x40", "120x40", "30x1"],
                         [g["cell"] for g in P.ordered_grid(P.ProbeSettings(allowed_streams=120))[:5]])
        by_items = P.ordered_grid(P.ProbeSettings(order="items"))
        self.assertEqual(["30x1", "60x1", "90x1", "120x1", "30x5"], [g["cell"] for g in by_items[:5]])
        by_streams = P.ordered_grid(P.ProbeSettings(order="streams"))
        self.assertEqual(["30x1", "30x5", "30x10", "30x40", "60x1"], [g["cell"] for g in by_streams[:5]])
        given = P.ordered_grid(P.ProbeSettings(order="as-given", cells=P.parse_cells(["60x40,30x1", "60x40"])))
        self.assertEqual(["60x40", "30x1"], [g["cell"] for g in given])
        shuffled = [g["cell"] for g in P.ordered_grid(P.ProbeSettings(order="shuffle", seed=3))]
        self.assertEqual(shuffled, [g["cell"] for g in P.ordered_grid(P.ProbeSettings(order="shuffle", seed=3))])
        self.assertEqual(16, len(set(shuffled)))
        for bad in (dict(cells=((60, 41),)), dict(cells=((0, 5),)), dict(order="as-given"), dict(warmup_seconds=400),
                    dict(ramp_seconds=40), dict(abort_failure_share=0), dict(journal="two\nlines"),
                    dict(api_key_env="NOT A NAME"), dict(max_streams=20), dict(abort_min_failures=0),
                    dict(drain_seconds=float("nan")), dict(cell_seconds=float("inf")), dict(shared_tolerance=-1.0),
                    dict(expect_output_tokens=0.0), dict(expect_tolerance=0.0), dict(profile={"reviewEntries": {"median": 0}})):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                P.ProbeSettings(**bad).validate(live=False)
        with self.assertRaises(ValidationError):
            P.parse_cells(["60by40"])
        with self.assertRaises(ValidationError):
            P.ProbeSettings().validate(live=True)            # no --base-url
        self.assertEqual(0.0, P.ProbeSettings().shared_tolerance)

    def test_profiles_are_validated_and_replace_the_built_in_sizes(self):
        merged = P.validate_profile({"reviewEntries": {"median": 60, "sigma": 0.5, "min": 4, "max": 300},
                                     "citingSentences": {"weights": [0.7, 0.2, 0.1]}})
        self.assertEqual(60, merged["reviewEntries"]["median"])
        self.assertEqual(P.PROFILE["sentenceCharacters"], merged["sentenceCharacters"])
        for bad in ({"unknown": 1}, {"reviewEntries": {"median": 10, "sigma": 0.2, "min": 9, "max": 3}},
                    {"citingSentences": {"weights": [0, 0]}}, {"citingFromAbstract": {"share": 2}},
                    {"abstractCharacters": {"missingShare": 1.5}}, {"titleWords": {"min": 0, "max": 3}}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                P.validate_profile(bad)
        small = P.SyntheticCorpus(1, "4x40", merged)
        sizes = [small.review_size(r) for r in range(200)]
        self.assertLess(sum(sizes) / len(sizes), 90)
        self.assertTrue(all(4 <= size <= 300 for size in sizes))


class PoolTests(ProbeCase):
    def test_calls_share_one_keep_alive_connection_per_stream(self):
        self.judge.latency = 0.005
        report = self.probe("4x1", metrics=False)
        cell = report["cells"][0]
        self.assertGreater(self.judge.requests, 40)
        self.assertLessEqual(len(self.judge.chat_connections), 4)
        self.assertLessEqual(cell["client"]["connectionsOpened"], 4)
        self.assertEqual(4, cell["client"]["poolSize"])
        self.assertGreaterEqual(cell["client"]["connectionReuses"], cell["calls"]["sent"] - 4)
        self.assertEqual(0, cell["client"]["serverClosedConnections"])
        self.assertEqual(len(self.judge.chat_connections), sum(1 for record in self.calls() if not record["reused"]))

    def test_a_call_that_connects_after_the_cell_ended_sends_nothing(self):
        pool = P.ConnectionPool(P.Endpoint.parse(self.url + "/v1", "base URL"), 2, 5.0)
        connection, reused = pool.acquire()
        self.assertFalse(reused)
        pool.close()
        with self.assertRaises(P.PoolClosed):
            connection.request("POST", "/v1/chat/completions", body=b"{}", headers={"Content-Type": "application/json"})
        connection.close()
        with self.assertRaises(P.PoolClosed):
            pool.acquire()
        time.sleep(0.05)
        self.assertEqual(1, pool.stats()["connectionsOpened"])
        self.assertEqual(0, self.judge.requests)

    def test_a_server_that_closes_connections_is_reconnected_without_failures(self):
        self.judge.close_connections = True
        report = self.probe("3x1", metrics=False)
        cell = report["cells"][0]
        self.assertEqual({}, cell["failures"])
        self.assertEqual(cell["calls"]["answered"], cell["client"]["serverClosedConnections"])
        self.assertEqual(cell["calls"]["sent"], cell["client"]["connectionsOpened"])
        self.assertEqual(self.judge.requests, len(self.judge.chat_connections))


class RateTests(ProbeCase):
    def test_entries_per_second_follow_streams_items_and_latency(self):
        self.judge.latency = 0.1
        report = self.probe("4x5", metrics=False, cell_seconds=1.6, warmup_seconds=0.4)
        cell = report["cells"][0]
        expected = 4 * cell["calls"]["meanItems"] / 0.1      # streams x items per call / latency
        rate = cell["entries"]["perSecond"]
        self.assertGreater(rate, 0.75 * expected)
        self.assertLess(rate, 1.02 * expected)
        self.assertGreater(cell["entries"]["meanConcurrency"], 3.5)
        self.assertFalse(cell["entries"]["clientLimited"])
        self.assertAlmostEqual(cell["entries"]["perSecond"], cell["entries"]["perSecondCompleted"], delta=0.35 * rate)
        self.assertGreaterEqual(cell["latencySeconds"]["p50"], 0.1)
        self.assertLessEqual(cell["latencySeconds"]["p50"], cell["latencySeconds"]["p95"])
        self.assertLessEqual(cell["latencySeconds"]["p95"], cell["latencySeconds"]["p99"])
        self.assertEqual("calls started inside the measured window", cell["latencySeconds"]["scope"])
        self.assertEqual(cell["tokens"]["prompt"], sum(r["promptTokens"] for r in self.calls()))
        self.assertAlmostEqual(cell["tokens"]["cachedShare"],
                               64 * cell["calls"]["answered"] / cell["tokens"]["prompt"], places=3)
        self.assertEqual("usage", cell["tokens"]["cachedShareSource"])
        # The tail: the streams kept sending past the window until its last calls had finished, so the window was
        # not truncated, and calls were sent after it.
        timing = cell["timing"]
        self.assertTrue(timing["tailComplete"])
        self.assertEqual(4, timing["edgeCalls"])
        self.assertEqual(timing["windowLimitSeconds"], timing["windowEndSeconds"])
        self.assertEqual(0.0, timing["truncatedSeconds"])
        self.assertTrue(any(r["start"] >= timing["windowEndSeconds"] for r in self.calls()))
        self.assertGreaterEqual(timing["windowStartSeconds"], 0.4)

    def test_time_weighted_rate_does_not_depend_on_where_the_window_cuts_calls(self):
        # Two synchronised streams, 3 s per call of 10 entries: 20/3 entries per second in steady state.
        records = [{"start": float(s), "end": float(s + 3), "valid": 10} for s in range(0, 30, 3) for _ in range(2)]
        for start, end in ((1.0, 8.0), (2.5, 9.5), (0.0, 30.0), (4.2, 5.1)):
            with self.subTest(window=(start, end)):
                self.assertAlmostEqual(20 / 3, P.time_weighted_rate(records, start, end, lambda r: r["valid"]))
        # Counting completions inside a short window swings with the phase.
        self.assertAlmostEqual(40 / 7, P.completed_rate(records, 1.0, 8.0, lambda r: r["valid"]))
        self.assertAlmostEqual(0.0, P.completed_rate(records, 4.2, 5.1, lambda r: r["valid"]))
        self.assertAlmostEqual(2.0, P.mean_concurrency(records, 1.0, 8.0))
        self.assertIsNone(P.time_weighted_rate(records, 5.0, 5.0, lambda r: r["valid"]))
        self.assertEqual(2.5, P.percentile([1, 2, 3, 4], 0.5))
        self.assertEqual(4, P.percentile([4], 0.99))
        self.assertAlmostEqual(3.97, P.percentile([1, 2, 3, 4], 0.99))
        self.assertIsNone(P.percentile([], 0.5))

    def test_the_steady_state_window_removes_the_edge_bias_on_a_processor_sharing_server(self):
        valid = lambda r: r["valid"]
        naive, steady, truncated = [], [], []
        for seed in range(6):
            # Sending stops at the window's end, which starts after the warm-up only: the edges read high.
            records = simulate_processor_sharing(tail=False, seed=seed)
            naive.append(P.time_weighted_rate(records, 10.0, 150.0, valid))
            # The probe's window: after the ramp's calls, with the tail.
            records = simulate_processor_sharing(tail=True, seed=seed)
            start = max(10.0, max(r["end"] for r in records if r["start"] < 5.0))
            steady.append(P.time_weighted_rate(records, start, 150.0, valid))
            # A stop rule: sending stops at once; the window ends at the earliest start of the calls still open.
            records = simulate_processor_sharing(tail=False, window=(10.0, 200.0), seed=seed)
            start = max(10.0, max(r["end"] for r in records if r["start"] < 5.0))
            end = min([200.0] + [r["start"] for r in records if r["start"] < 200.0 < r["end"]])
            truncated.append(P.time_weighted_rate(records, start, end, valid))
        mean = lambda values: sum(values) / len(values)
        self.assertGreater(mean(naive), 8.0 * 1.01)
        self.assertAlmostEqual(8.0, mean(steady), delta=0.02)
        self.assertAlmostEqual(8.0, mean(truncated), delta=0.02)


class AbortTests(ProbeCase):
    def test_transport_failures_above_the_share_stop_the_cell_and_the_grid(self):
        self.judge.fail_every = 4
        report = self.probe("2x1,4x1")
        first, second = report["cells"]
        self.assertEqual("aborted", report["state"])
        self.assertEqual("aborted", first["state"])
        self.assertEqual("transport-failures", first["abort"]["rule"])
        self.assertGreater(first["abort"]["share"], 0.05)
        self.assertGreaterEqual(first["abort"]["calls"], FAST["abort_min_calls"])
        self.assertLess(first["abort"]["at"], FAST["cell_seconds"])
        self.assertEqual("skipped", second["state"])
        self.assertEqual("grid stopped at 2x1 (transport-failures)", second["reason"])
        self.assertEqual("2x1", report["abort"]["cell"])
        self.assertIn("Remaining cells: `--cells 4x1`", (self.output / P.MARKDOWN_NAME).read_text())
        self.assertIsNone(report["exitCriterion"]["passes"])
        self.assertTrue(any('"event": "abort"' in event for event in self.events))

    def test_failures_below_the_share_are_classified_without_aborting(self):
        self.judge.fail_every, self.judge.reset_every = 61, 67
        report = self.probe("3x1", metrics=False, abort_window_seconds=2.0)
        cell = report["cells"][0]
        self.assertEqual("completed", cell["state"])
        self.assertEqual(self.judge.failed, cell["failures"].get("http-500", 0))
        self.assertEqual(self.judge.resets, cell["failures"].get("reset", 0))
        self.assertGreater(self.judge.failed + self.judge.resets, 0)
        self.assertAlmostEqual(cell["transportFailureShare"], (self.judge.failed + self.judge.resets) / self.judge.requests,
                               places=4)

    def test_server_saturation_stops_the_grid_and_keeps_a_truncated_steady_window(self):
        self.judge.waiting = 3
        report = self.probe("2x40,2x5", cell_seconds=2.0, allowed_streams=2)
        first = report["cells"][0]
        self.assertEqual("saturated", first["state"])
        self.assertEqual("saturation", first["abort"]["rule"])
        self.assertIn("waiting", first["abort"]["signals"])
        self.assertGreaterEqual(first["abort"]["seconds"], FAST["saturation_seconds"])   # recorded to 3 digits
        self.assertGreaterEqual(first["abort"]["since"], FAST["warmup_seconds"])     # the ramp never counts
        self.assertLess(first["abort"]["at"], 2.0)
        self.assertEqual(0, first["calls"]["abandoned"])                  # calls in flight finished, none cut off
        self.assertGreater(first["entries"]["perSecond"], 0)
        self.assertGreater(first["server"]["saturatedSamples"], 2)
        timing = first["timing"]
        self.assertFalse(timing["tailComplete"])
        self.assertLessEqual(timing["windowEndSeconds"], timing["windowLimitSeconds"])   # ends at the cut calls' start
        self.assertEqual("skipped", report["cells"][1]["state"])
        self.assertEqual("grid stopped at 2x40 (saturation)", report["cells"][1]["reason"])
        self.assertEqual("aborted", report["state"])
        verdict = report["exitCriterion"]
        self.assertEqual(("2x40", "saturated", True), (verdict["cell"], verdict["assessment"]["state"],
                                                       verdict["assessment"]["eligible"]))
        self.assertIn("stopped by the saturation rule", verdict["line"])

    def test_http_429_responses_count_as_saturation_without_metrics(self):
        self.judge.fail_every, self.judge.fail_status, self.judge.fail_message = 1, 429, "rate limited"
        report = self.probe("2x1", metrics=False, abort_failure_share=1.0, cell_seconds=2.0)
        cell = report["cells"][0]
        self.assertEqual("saturated", cell["state"])
        self.assertEqual(["http-429-503"], cell["abort"]["signals"])
        self.assertEqual(cell["calls"]["failed"], cell["failures"]["http-429"])
        self.assertEqual("rate limited", cell["failureExamples"]["http-429"][0]["detail"])

    def test_preemptions_count_as_saturation(self):
        self.judge.preempt_per_fetch = 1
        report = self.probe("2x1", cell_seconds=2.0)
        cell = report["cells"][0]
        self.assertEqual("saturated", cell["state"])
        self.assertEqual(["preemptions"], cell["abort"]["signals"])
        self.assertGreater(cell["server"]["counters"]["preemptions"], 0)
        self.assertFalse(cell["server"]["counters"]["tokensComparable"])   # recomputed prefills may count twice

    def test_failures_at_a_slow_call_rate_stop_once_enough_failures_accumulate(self):
        self.judge.latency, self.judge.fail_every = 0.05, 2
        report = self.probe("1x1", metrics=False, abort_min_calls=1000, abort_window_seconds=5.0, cell_seconds=2.0)
        cell = report["cells"][0]
        self.assertEqual("aborted", cell["state"])
        self.assertEqual((6, 3), (cell["abort"]["calls"], cell["abort"]["failures"]))
        self.assertIsNone(report["exitCriterion"]["assessment"])

    def test_abort_monitor_rules(self):
        settings = P.ProbeSettings(abort_window_seconds=60.0, abort_min_calls=20, abort_min_failures=3,
                                   saturation_seconds=60.0)
        fired = []
        monitor = P.AbortMonitor(settings, fired.append)
        for t in range(10):
            monitor.call_finished(float(t), failed=t in (2, 6))      # 2 of 10: neither 20 calls nor 3 failures
        self.assertEqual([], fired)
        monitor.call_finished(10.0, failed=True)                     # the third failure decides
        self.assertEqual(("transport-failures", 11, 3), (fired[0]["rule"], fired[0]["calls"], fired[0]["failures"]))
        fired.clear()
        monitor = P.AbortMonitor(settings, fired.append)
        for t in range(20):
            monitor.call_finished(float(t), failed=t == 0)           # 1 of 20 is 5 %: not above the share
        self.assertEqual([], fired)
        for t in range(81, 101):
            monitor.call_finished(float(t), failed=t == 81)          # the first failure left the 60 s window
        self.assertEqual([], fired)
        monitor.call_finished(101.0, failed=True)                    # 2 of 21 calls in the window
        self.assertEqual((21, 2), (fired[0]["calls"], fired[0]["failures"]))
        fired.clear()
        monitor = P.AbortMonitor(settings, fired.append)
        for t, saturated in ((30, True), (60, True), (85, None), (89, True), (95, False), (155, True), (200, True)):
            monitor.sample(float(t), saturated, ["waiting"])
        self.assertEqual([], fired)                                  # only an unsaturated sample breaks a streak
        monitor.sample(216.0, True, ["waiting"])
        self.assertEqual(("saturation", 155.0, 61.0), (fired[0]["rule"], fired[0]["since"], fired[0]["seconds"]))
        monitor.sample(300.0, True, ["waiting"])
        self.assertEqual(1, len(fired))                              # a rule fires once
        fired.clear()
        monitor = P.AbortMonitor(settings, fired.append)
        for t, saturated in ((30, True), (85, None), (91, True)):     # a sample without metrics is neutral
            monitor.sample(float(t), saturated, ["waiting"])
        self.assertEqual(("saturation", 30.0), (fired[0]["rule"], fired[0]["since"]))
        fired.clear()
        monitor = P.AbortMonitor(settings, fired.append)
        for t in (30, 60, 90):
            monitor.sample(float(t), None, [], in_flight=0)          # no calls in flight: nothing to watch
        for t in (100, 130, 160):
            monitor.sample(float(t), None, [], in_flight=5)
        self.assertEqual([], fired)
        monitor.sample(161.0, None, [], in_flight=5)                 # metrics missing for more than 60 s
        self.assertEqual(("metrics-unavailable", 100.0), (fired[0]["rule"], fired[0]["since"]))

    def test_calls_cut_off_at_the_drain_are_abandoned_not_failed(self):
        self.judge.latency = 1.5
        report = self.probe("2x1", metrics=False, cell_seconds=0.5, warmup_seconds=0.1, ramp_seconds=0.0,
                            drain_seconds=0.2)
        cell = report["cells"][0]
        self.assertEqual("completed", cell["state"])
        self.assertEqual(2, cell["calls"]["abandoned"])
        self.assertEqual(2, cell["calls"]["abandonedInFlight"])
        self.assertEqual({}, cell["failures"])
        self.assertIsNone(cell["transportFailureShare"])
        self.assertFalse(cell["timing"]["tailComplete"])            # the window's calls outlasted the tail
        self.assertLess(cell["timing"]["wallSeconds"], 1.4)

    def test_metrics_that_fail_during_a_cell_stop_it_and_keep_its_window(self):
        # The readability check, two baseline samples and the cell's idle sample succeed; the run samples fail.
        self.judge.metrics_fail_after = 4
        report = self.probe("2x1", cell_seconds=2.0)
        cell = report["cells"][0]
        self.assertEqual("metrics-lost", cell["state"])
        self.assertEqual("metrics-unavailable", cell["abort"]["rule"])
        self.assertEqual(0, cell["calls"]["abandoned"])
        self.assertLess(cell["abort"]["at"], 2.0)
        self.assertEqual("aborted", report["state"])

    def test_unreadable_or_unrecognised_metrics_are_refused_before_any_cell(self):
        self.judge.metrics_fail_after = 0
        with self.assertRaisesRegex(ValidationError, "cannot be read"):
            self.probe("2x1")
        self.assertEqual(0, self.judge.requests)
        self.assertFalse(self.output.exists())
        self.judge.metrics_fail_after, self.judge.metrics_body = None, "sglang:num_running_reqs 4\n"
        with self.assertRaisesRegex(ValidationError, "no vLLM request gauge"):
            self.probe("2x1")
        self.assertEqual(0, self.judge.requests)


class OutputTests(ProbeCase):
    def test_invalid_outputs_are_counted_with_the_instruments_validator(self):
        self.judge.drop_every, self.judge.empty_every, self.judge.garbage_every = 3, 7, 11
        self.judge.length_every = 5
        report = self.probe("2x5", metrics=False, abort_min_calls=10_000, abort_min_failures=10_000)
        cell = report["cells"][0]
        answered = [r for r in self.calls() if r["outcome"] == "answered"]
        empty_entries = sum(r["items"] for r in answered if r["empty"])
        self.assertEqual(self.judge.garbage, cell["failures"].get("invalid-json", 0))
        self.assertEqual(self.judge.empty, cell["emptyCompletions"])
        self.assertEqual(self.judge.dropped + empty_entries, cell["entries"]["invalid"])
        self.assertEqual(cell["entries"]["answered"] - cell["entries"]["invalid"], cell["entries"]["valid"])
        self.assertAlmostEqual(cell["entries"]["invalid"] / cell["entries"]["answered"], cell["invalidOutputShare"],
                               places=4)
        self.assertEqual(sum(1 for r in answered if r["missing"]), cell["invalidCalls"])
        self.assertEqual(self.judge.truncated, cell["truncatedCalls"])
        self.assertTrue(cell["invalidExamples"])

    def test_failure_examples_reach_the_report_and_the_markdown(self):
        self.judge.fail_every, self.judge.fail_status = 4, 400
        self.judge.fail_message = "This model's maximum context length is 32768 tokens | requested 48000"
        report = self.probe("2x1", metrics=False, abort_failure_share=1.0)
        cell = report["cells"][0]
        examples = cell["failureExamples"]["http-400"]
        self.assertTrue(1 <= len(examples) <= 3)
        self.assertEqual((400, self.judge.fail_message), (examples[0]["status"], examples[0]["detail"]))
        self.assertEqual(cell["calls"]["failed"], cell["failures"]["http-400"])
        markdown = (self.output / P.MARKDOWN_NAME).read_text()
        self.assertIn("2x1 http-400: This model's maximum context length is 32768 tokens \\| requested 48000", markdown)
        self.assertTrue(all("detail" not in record for record in self.calls()))

    def test_response_classification(self):
        labels = ["1", "2", "3"]
        line = lambda label, category="T": json.dumps({"l": label, "c": category, "v": "direct", "q": "", "e": 0, "r": "x"})
        completion = lambda content, **extra: json.dumps({"model": "m", "choices": [{"message": {"content": content},
                                                          "finish_reason": "stop"}], **extra}).encode()
        full = P.classify_response(200, completion("\n".join(line(l) for l in labels),
                                                   usage={"prompt_tokens": 10, "completion_tokens": 5}), labels)
        self.assertEqual(("answered", 3, 0), (full["outcome"], full["valid"], full["missing"]))
        self.assertIsNone(full["cachedTokens"])
        partial = P.classify_response(200, completion(line("1") + "\nnot a line\n" + line("2", "Z")), labels)
        self.assertEqual((1, 2), (partial["valid"], partial["missing"]))
        self.assertEqual(["2", "3"], partial["examples"]["missing"])
        self.assertEqual(3, P.classify_response(200, completion(""), labels)["missing"])
        self.assertTrue(P.classify_response(200, completion(None), labels)["empty"])
        context = b'{"object": "error", "message": "maximum context length is 32768 tokens", "code": 400}'
        for status, body, failure, detail in (
                (500, b"{}", "http-500", "{}"), (429, b"", "http-429", ""),
                (400, context, "http-400", "maximum context length is 32768 tokens"),
                (503, b'{"error": {"message": "  server\\n busy "}}', "http-503", "server busy"),
                (200, b"<html>proxy " + b"x" * 400, "invalid-json", "<html>proxy " + "x" * 188),
                (200, b"[1, 2]", "invalid-json", "[1, 2]"), (200, b'{"choices": []}', "invalid-response", "no choices"),
                (200, b'{"choices": [{"message": "x"}]}', "invalid-response", "no message text"),
                (200, b'{"choices": [{"message": {"content": 5}}]}', "invalid-response", "no message text")):
            with self.subTest(body=body):
                self.assertEqual({"outcome": "failed", "failure": failure, "detail": detail},
                                 P.classify_response(status, body, labels))
        scrubbed = P.classify_response(200, completion("", model="echo-K3Y"), labels, scrub=lambda s: s.replace("K3Y", "#"))
        self.assertEqual("echo-#", scrubbed["model"])
        import http.client
        self.assertEqual("timeout", P.failure_class(socket.timeout()))
        self.assertEqual("reset", P.failure_class(http.client.RemoteDisconnected("x")))
        self.assertEqual("reset", P.failure_class(http.client.IncompleteRead(b"")))
        self.assertEqual("refused", P.failure_class(ConnectionRefusedError()))
        self.assertEqual("protocol", P.failure_class(http.client.BadStatusLine("x")))
        self.assertFalse(P.may_have_run({"outcome": "failed", "failure": "http-413"}))
        self.assertFalse(P.may_have_run({"outcome": "failed", "failure": "refused"}))
        self.assertTrue(P.may_have_run({"outcome": "failed", "failure": "http-502"}))
        self.assertTrue(P.may_have_run({"outcome": "abandoned", "failure": "reset"}))

    def test_report_shape_markdown_and_exit_line(self):
        self.judge.latency = 0.1         # server time dominates, so the metrics cover our calls (an unshared verdict)
        report = self.probe("2x40,2x1,3x40", allowed_streams=2, target_rate=10.0, cell_seconds=0.8)
        for key in ("format", "state", "startedAt", "finishedAt", "code", "client", "instrument", "request", "endpoint",
                    "synthetic", "expectation", "grid", "abortRules", "decision", "preflight", "baseline", "cells",
                    "server", "exitCriterion", "allowedConcurrency", "files"):
            self.assertIn(key, report)
        self.assertEqual(P.FORMAT, report["format"])
        self.assertEqual([MODEL], report["preflight"]["models"])
        self.assertEqual(["2x40", "2x1", "3x40"], [c["cell"] for c in report["cells"]])
        self.assertEqual(["decisive", "exploratory", "instrument"], [c["role"] for c in report["cells"]])
        cell = report["cells"][0]
        for key in ("calls", "entries", "tokens", "latencySeconds", "invalidOutputShare", "failures", "server", "client",
                    "served", "timing", "transportFailureShare"):
            self.assertIn(key, cell)
        self.assertEqual({"fp-mock-1": cell["calls"]["answered"]}, cell["served"]["fingerprints"])
        self.assertTrue(cell["served"]["matchesInstrument"])
        self.assertIsNotNone(cell["client"]["rssMaxMB"])
        self.assertIsNotNone(cell["tokens"]["promptPerEntry"])
        self.assertGreater(len(cell["server"]["samples"]), 3)
        verdict = report["exitCriterion"]
        self.assertEqual("2x40", verdict["cell"])
        self.assertTrue(verdict["passes"])
        self.assertTrue(verdict["line"].startswith(
            "Exit criterion (≥ 10 entries/s with the instrument's 40 items per call at the allowed concurrency of 2 "
            "streams): PASS - 2x40: "))
        self.assertIn("Exploratory geometries at ≤ 2 streams (not the instrument's, never deciding): best 2x1",
                      verdict["line"])
        self.assertIn("(no expectation predeclared: --expect-output-tokens or --profile)", verdict["line"])
        self.assertIn(f"served model `{MODEL}`", verdict["line"])
        self.assertTrue(report["allowedConcurrency"]["mayExceed"])
        self.assertIn("Best cell above 2 streams with the instrument's 40 items per call: 3x40",
                      report["allowedConcurrency"]["line"])
        markdown = (self.output / P.MARKDOWN_NAME).read_text()
        self.assertIn(verdict["line"], markdown)
        self.assertIn(report["allowedConcurrency"]["line"], markdown)
        self.assertIn("| Cell | Role | State | Calls answered | Items/call | Window s | Entries/s |", markdown)
        self.assertEqual(3, len(re.findall(r"^\| (2x40|2x1|3x40) \| \w+ \| completed \|", markdown, flags=re.M)))
        on_disk = json.loads((self.output / P.REPORT_NAME).read_text())
        self.assertEqual(report["cells"][1]["entries"], on_disk["cells"][1]["entries"])
        self.assertEqual(sum(c["calls"]["sent"] for c in report["cells"]), len(self.calls()))

    def test_only_the_instruments_geometry_at_the_allowed_concurrency_decides(self):
        settings = P.ProbeSettings(metrics_url="http://127.0.0.1:9/metrics")
        line = lambda report, s=settings: P.exit_criterion(report, s)
        # A faster exploratory geometry never passes the criterion for the instrument's 40 items per call.
        verdict = line({"cells": [decision_cell("60x40", 31.0), decision_cell("60x10", 45.0)]})
        self.assertFalse(verdict["passes"])
        self.assertIn("FAIL - 60x40: 31.0 entries/s over a 240 s steady-state window", verdict["line"])
        self.assertIn("best 60x10 at 45.0 entries/s", verdict["line"])
        self.assertTrue(verdict["line"].endswith("never deciding): best 60x10 at 45.0 entries/s."))
        self.assertIn("the author decides.", verdict["line"])
        passing = line({"cells": [decision_cell("30x40", 30.0), decision_cell("60x40", 44.5)]})
        self.assertTrue(passing["passes"])
        self.assertIn("PASS - 60x40: 44.5 entries/s", passing["line"])
        # The deciding cell skipped (the grid stopped earlier): not assessed, whatever an exploratory cell reached.
        skipped = {"cell": "60x40", "streams": 60, "itemsPerCall": 40, "state": "skipped",
                   "reason": "grid stopped at 30x40 (transport-failures)"}
        verdict = line({"cells": [skipped, decision_cell("30x1", 41.0)]})
        self.assertIsNone(verdict["passes"])
        self.assertIn("not assessed - 60x40: skipped (grid stopped at 30x40 (transport-failures))", verdict["line"])
        self.assertIn("not assessed - the grid has no 60x40 cell", line({"cells": [decision_cell("30x40", 50.0)]})["line"])
        aborted = line({"cells": [decision_cell("60x40", 50.0, state="aborted")]})
        self.assertIn("stopped by the transport-failures rule with its calls cut off", aborted["line"])
        # Answers from another model: the census would pause on model drift.
        drift = line({"cells": [decision_cell("60x40", 462.8, model="some-other-model")]})
        self.assertIsNone(drift["passes"])
        self.assertIn("answered as `some-other-model`, not the instrument's required model", drift["line"])
        self.assertIn("model drift", drift["line"])
        # A saturated cell whose steady-state window is shorter than two p95 call latencies does not decide.
        short = line({"cells": [decision_cell("60x40", 40.4, state="saturated", measured=35.0, p95=52.0)]})
        self.assertIsNone(short["passes"])
        self.assertIn("a steady-state window of 35 s, less than the 104 s needed", short["line"])
        self.assertIn("indicative 40.4 entries/s", short["line"])
        self.assertIn("--cells 60x40 --cell-seconds 900 and a larger --saturation-seconds", short["line"])
        long_enough = line({"cells": [decision_cell("60x40", 41.5, state="saturated", measured=200.0, p95=52.0,
                                                    completed=39.0)]})
        self.assertTrue(long_enough["passes"])
        self.assertIn("41.5 entries/s over a 200 s steady-state window (stopped by the saturation rule", long_enough["line"])
        self.assertIn("completed calls 39.0 entries/s", long_enough["line"])
        # A completed cell needs at least its planned window or two p95 latencies.
        self.assertIsNone(line({"cells": [decision_cell("60x40", 45.0, measured=150.0, p95=100.0)]})["passes"])

    def test_predeclared_tokens_per_entry_warn_and_normalise_the_rate(self):
        settings = P.ProbeSettings(expect_output_tokens=40.0, expect_prompt_tokens=520.0)
        within = P.exit_criterion({"cells": [decision_cell("60x40", 44.5, output=36.0)]}, settings)
        self.assertTrue(within["passes"])
        self.assertIn("tokens per entry: prompt 500.0, output 36.0 against the expected prompt 520 (-4 %), output 40 "
                      "(-10 %)", within["line"])
        self.assertNotIn("WARNING", within["line"])
        departing = P.exit_criterion({"cells": [decision_cell("60x40", 44.5, output=30.0)]}, settings)
        self.assertFalse(departing["passes"])
        self.assertEqual(33.38, departing["assessment"]["normalisedRate"])
        self.assertIn("WARNING: output tokens per entry depart from the expectation by more than 20 %, token-normalised "
                      "rate 33.4 entries/s", departing["line"])
        self.assertIn("FAIL - 60x40: 33.4 entries/s", departing["line"])
        # Synthetic entries with longer answers than real ones: the lower measured rate decides.
        longer = P.exit_criterion({"cells": [decision_cell("60x40", 38.0, output=60.0)]}, settings)
        self.assertFalse(longer["passes"])
        self.assertEqual(57.0, longer["assessment"]["normalisedRate"])
        profiled = P.ProbeSettings(profile_expected={"outputTokensPerEntry": 30.0})
        self.assertEqual({"outputTokensPerEntry": 30.0, "tolerance": 0.2, "source": {"outputTokensPerEntry": "profile"}},
                         profiled.expectation())

    def test_allowed_concurrency_names_the_instruments_geometry_above_the_limit(self):
        settings = P.ProbeSettings(metrics_url="http://127.0.0.1:9/metrics")
        cells = [decision_cell("60x40", 44.5), decision_cell("90x40", 61.0), decision_cell("120x10", 70.0)]
        shared = P.allowed_concurrency({"cells": cells, "server": {"shared": True}}, settings)
        self.assertEqual((60, False), (shared["streams"], shared["mayExceed"]))
        self.assertIn("shared server", shared["line"])
        free = P.allowed_concurrency({"cells": cells, "server": {"shared": False}}, settings)
        self.assertTrue(free["mayExceed"])
        self.assertIn("Best cell above 60 streams with the instrument's 40 items per call: 90x40, 61.0 entries/s.",
                      free["line"])
        self.assertIn("Exploratory geometries above 60 streams: best 120x10 at 70.0 entries/s.", free["line"])
        self.assertEqual("90x40", free["bestAbove"]["cell"])
        unknown = P.allowed_concurrency({"cells": cells, "server": {"shared": None}}, settings)
        self.assertIn("cannot show that the server is not shared", unknown["line"])
        self.assertIn("no metrics endpoint", P.allowed_concurrency({"cells": cells, "server": {}}, P.ProbeSettings())["line"])


class SharedServerTests(ProbeCase):
    def test_requests_beyond_our_own_mark_the_server_as_shared(self):
        self.judge.foreign = 5
        report = self.probe("2x1")
        cell = report["cells"][0]
        self.assertTrue(report["baseline"]["foreign"])
        self.assertTrue(cell["server"]["shared"])
        self.assertGreater(cell["server"]["foreignSamples"], 2)
        self.assertGreaterEqual(cell["server"]["maxExcess"], 3)
        self.assertTrue(report["server"]["shared"])
        self.assertFalse(report["allowedConcurrency"]["mayExceed"])
        self.assertIn("shared server", report["allowedConcurrency"]["line"])

    def test_an_unshared_server_is_shown_as_not_shared(self):
        self.judge.latency = 0.1
        report = self.probe("3x1,2x5")
        for cell in report["cells"]:
            counters = cell["server"]["counters"]
            self.assertEqual(0, cell["server"]["foreignSamples"], cell["server"])
            self.assertTrue(counters["comparable"])
            self.assertEqual((0, 0, 0), (counters["generationExcess"], counters["promptExcess"], counters["requestExcess"]))
            self.assertEqual([], counters["foreignBy"])
            self.assertTrue(cell["server"]["coverage"]["covered"], cell["server"]["coverage"])
            self.assertEqual(1.0, cell["server"]["coverage"]["tokens"])
            self.assertIs(False, cell["server"]["shared"])
            self.assertGreater(cell["server"]["running"]["max"], 0)
        self.assertEqual({"promptTokens": 0, "generationTokens": 0, "requestSuccess": 0},
                         report["baseline"]["countersAdvanced"])
        self.assertIs(False, report["server"]["shared"])
        self.assertEqual([], report["server"]["gaps"])
        self.assertTrue(report["allowedConcurrency"]["mayExceed"])

    def test_metrics_that_see_part_of_our_calls_cannot_show_an_unshared_server(self):
        self.judge.latency, self.judge.visible = 0.1, 0.5          # one replica of two behind a balancer
        report = self.probe("4x1")
        cell = report["cells"][0]
        self.assertEqual(0, cell["server"]["foreignSamples"])
        self.assertFalse(cell["server"]["counters"]["foreign"])
        self.assertLess(cell["server"]["coverage"]["inFlight"], P.COVERAGE_MIN)
        self.assertLess(cell["server"]["coverage"]["tokens"], P.COVERAGE_MIN)
        self.assertIsNone(cell["server"]["shared"])
        self.assertIsNone(report["server"]["shared"])
        self.assertTrue(any("only part of our calls" in gap for gap in report["server"]["gaps"]))
        self.assertFalse(report["allowedConcurrency"]["mayExceed"])
        self.assertIn("- Not shown unshared: 4x1: the metrics account for only part of our calls",
                      (self.output / P.MARKDOWN_NAME).read_text())

    def test_tokens_of_unanswered_calls_are_allowed_for_in_the_counters(self):
        # Server errors that still generated on the server must not look like another client's tokens.
        self.judge.latency, self.judge.fail_every, self.judge.generation_on_failure = 0.1, 4, 2500
        report = self.probe("3x1", abort_failure_share=1.0)
        cell = report["cells"][0]
        counters = cell["server"]["counters"]
        self.assertGreater(cell["calls"]["failed"], 2)
        self.assertEqual(2500 * self.judge.failed, counters["generationExcess"])
        self.assertGreater(counters["generationExcess"], counters["margin"])
        self.assertEqual(3000 * cell["calls"]["failed"], counters["unansweredTokenBound"])
        self.assertEqual(cell["calls"]["failed"], counters["requestBound"])
        self.assertFalse(counters["foreign"])
        self.assertIs(False, cell["server"]["shared"])

    def test_refusals_get_no_allowance_so_tokens_beside_them_show_another_client(self):
        # HTTP 400 refusals generate nothing; tokens generated beside them can only be another client's.
        self.judge.latency, self.judge.fail_every, self.judge.fail_status = 0.05, 10, 400
        self.judge.generation_on_failure = 2500
        report = self.probe("3x1", abort_failure_share=1.0)
        counters = report["cells"][0]["server"]["counters"]
        self.assertGreater(report["cells"][0]["calls"]["failed"], 2)
        self.assertEqual((0, 0), (counters["unansweredMayHaveRun"], counters["unansweredTokenBound"]))
        self.assertEqual(["generation"], counters["foreignBy"])
        self.assertTrue(report["server"]["shared"])

    def test_a_tenant_keeping_one_request_running_is_caught(self):
        # Idle while the baseline is taken; then one request runs throughout the cell and generates tokens.
        self.judge.tenant = {"after": 4, "running": 1, "generation": 60}
        report = self.probe("2x1")
        cell = report["cells"][0]
        self.assertFalse(report["baseline"]["foreign"])
        self.assertGreaterEqual(cell["server"]["foreignSamples"], 2)
        self.assertEqual(1, cell["server"]["maxExcess"])
        self.assertIn("generation", cell["server"]["counters"]["foreignBy"])
        self.assertTrue(report["server"]["shared"])
        self.assertFalse(report["allowedConcurrency"]["mayExceed"])

    def test_requests_another_client_finished_are_caught_by_the_request_counter(self):
        self.judge.tenant = {"after": 4, "success": 1}
        report = self.probe("2x1")
        counters = report["cells"][0]["server"]["counters"]
        self.assertGreater(counters["requestExcess"], 0)
        self.assertEqual(["requests"], counters["foreignBy"])
        self.assertTrue(report["server"]["shared"])
        self.assertTrue(any("more requests than we had answered" in item for item in report["server"]["evidence"]))

    def test_foreign_generation_between_samples_is_caught_by_the_token_counters(self):
        self.judge.extra_generation = 5000
        report = self.probe("2x1")
        cell = report["cells"][0]
        self.assertEqual(0, cell["server"]["foreignSamples"])
        self.assertTrue(cell["server"]["counters"]["foreign"])
        self.assertTrue(report["server"]["shared"])
        self.assertTrue(report["baseline"]["foreign"])
        self.assertGreater(report["baseline"]["countersAdvanced"]["generationTokens"], 0)
        self.assertTrue(any(item.startswith("baseline:") for item in report["server"]["evidence"]))

    def test_without_metrics_the_server_is_not_shown_unshared(self):
        report = self.probe("2x1", metrics=False)
        self.assertIsNone(report["server"]["shared"])
        self.assertEqual(["no metrics endpoint (--metrics-url)"], report["server"]["gaps"])
        self.assertEqual("not-configured", report["cells"][0]["server"]["metrics"])
        self.assertEqual(60, report["allowedConcurrency"]["streams"])
        self.assertFalse(report["allowedConcurrency"]["mayExceed"])

    def test_cells_above_the_allowed_concurrency_wait_for_an_unshared_server(self):
        self.judge.foreign = 5
        report = self.probe("2x1,4x1", allowed_streams=2)
        self.assertEqual(["completed", "skipped"], [c["state"] for c in report["cells"]])
        self.assertIn("the server showed other clients", report["cells"][1]["reason"])
        self.assertIn("--run-above-allowed", report["cells"][1]["reason"])
        self.assertEqual({"2x1"}, {record["cell"] for record in self.calls()})
        self.assertEqual("completed", report["state"])
        forced = self.probe("2x1,4x1", allowed_streams=2, run_above_allowed=True)
        self.assertEqual(["completed", "completed"], [c["state"] for c in forced["cells"]])
        unmeasured = self.probe("2x1,4x1", metrics=False, allowed_streams=2)
        self.assertEqual("skipped", unmeasured["cells"][1]["state"])
        self.assertIn("no --metrics-url", unmeasured["cells"][1]["reason"])
        # Unshared, with server time dominating each call so the metrics demonstrably cover our calls.
        self.judge.foreign, self.judge.latency = 0, 0.1
        free = self.probe("2x1,4x1", allowed_streams=2)
        self.assertEqual(["completed", "completed"], [c["state"] for c in free["cells"]], free["cells"][-1].get("reason"))

    def test_server_prefix_cache_counters_give_the_cached_share_when_usage_has_none(self):
        self.judge.cached = None
        report = self.probe("2x5")
        tokens = report["cells"][0]["tokens"]
        counters = report["cells"][0]["server"]["counters"]
        self.assertEqual("server-prefix-cache", tokens["cachedShareSource"])
        self.assertEqual(counters["prefixCacheHitShare"], tokens["cachedShare"])
        self.assertAlmostEqual(counters["prefixCacheHits"] / counters["prefixCacheQueries"], tokens["cachedShare"], places=3)
        self.assertIn(" (server) |", (self.output / P.MARKDOWN_NAME).read_text())

    def test_abandoned_calls_are_allowed_for_within_the_grace_period(self):
        probe = P.CapacityProbe(P.ProbeSettings())
        now = time.monotonic()
        for ago, tokens in ((100.0, 3000), (5.0, 28000), (1.0, 3500)):
            probe.abandoned(now - ago, tokens, tokens // 10)
        self.assertEqual(2, probe.abandoned_recently(30.0))
        self.assertEqual(31500, probe.abandoned_tokens(30.0))
        self.assertEqual({"calls": 2, "tokens": 31500, "prompt": 3150}, probe.carried(30.0))
        self.assertEqual(0, probe.abandoned_tokens(0.5))

    def test_metrics_parsing(self):
        text = ('# HELP vllm:num_requests_running x\n# TYPE vllm:num_requests_running gauge\n'
                'vllm:num_requests_running{engine="0",model_name="a,b"} 3.0\n'
                'vllm:num_requests_running{engine="1",model_name="a{b}"} 2.0 1700000000000\n'
                'vllm:num_requests_waiting{engine="0"} NaN\n'
                'vllm:gpu_cache_usage_perc{engine="0"} 0.5\nvllm:gpu_cache_usage_perc{engine="1"} 0.75\n'
                'vllm:request_success_total{finished_reason="stop"} 10\n'
                'vllm:request_success_total{finished_reason="length"} 2\n'
                'vllm:generation_tokens_total 120\nvllm:generation_tokens_created 1.7e9\nnot a sample line\n')
        load = P.server_load(P.parse_prometheus(text))
        self.assertEqual((5.0, None, 0.75, 120.0, 12.0), (load["running"], load["waiting"], load["kvCacheUsage"],
                                                        load["generationTokens"], load["requestSuccess"]))
        self.assertTrue(load["recognized"])
        self.assertFalse(P.server_load(P.parse_prometheus("sglang:num_running_reqs 4\n"))["recognized"])
        both = P.server_load(P.parse_prometheus("vllm:kv_cache_usage_perc 0.2\nvllm:gpu_cache_usage_perc 0.9\n"))
        self.assertEqual(0.2, both["kvCacheUsage"])


class SecretTests(ProbeCase):
    def fragments(self, key: str) -> list[str]:
        return [key[i:i + P.KEY_FRAGMENT] for i in range(len(key) - P.KEY_FRAGMENT + 1)]

    def test_the_key_reaches_only_the_judge_and_never_the_report_or_logs(self):
        key = "probe-key-" + secrets.token_hex(16)
        self.judge.fail_every, self.judge.echo_authorization = 9, True
        self.judge.fail_message = "rejected Bearer " + key
        self.judge.fingerprint = "fp-" + key                          # a hostile server echoing the key
        out, err = io.StringIO(), io.StringIO()
        output = self.root / "secret-run"
        argv = ["capacity", "probe", "--base-url", self.url + "/v1", "--api-key-env", "PROBE_JUDGE_KEY",
                "--metrics-url", self.url + "/metrics", "--cells", "2x1", "--output", str(output),
                "--cell-seconds", "0.6", "--warmup-seconds", "0.15", "--ramp-seconds", "0.05", "--drain-seconds", "3",
                "--pause-seconds", "0.02", "--sample-seconds", "0.1", "--abort-min-calls", "1000",
                "--abort-min-failures", "1000"]
        with mock.patch.dict(os.environ, {"PROBE_JUDGE_KEY": key}), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(0, main(argv))
        self.assertEqual({"Bearer " + key}, self.judge.chat_authorization)
        self.assertIn(("/v1/models", "Bearer " + key), self.judge.other_authorization)
        self.assertTrue(all(auth is None for path, auth in self.judge.other_authorization if path == "/metrics"))
        self.assertGreater(self.judge.failed, 0)
        texts = {"stdout": out.getvalue(), "stderr": err.getvalue()}
        texts.update({path.name: path.read_text() for path in output.iterdir()})
        self.assertEqual({"stdout", "stderr", P.REPORT_NAME, P.MARKDOWN_NAME, P.CALLS_NAME}, set(texts))
        for name, text in texts.items():
            with self.subTest(output=name):
                self.assertTrue(text)
                for fragment in self.fragments(key):
                    self.assertNotIn(fragment, text)
        report = json.loads(texts[P.REPORT_NAME])
        self.assertEqual("PROBE_JUDGE_KEY", report["endpoint"]["apiKeyEnv"])
        self.assertTrue(report["endpoint"]["keySent"])
        self.assertEqual({"fp-[redacted]"}, set(report["cells"][0]["served"]["fingerprints"]))
        self.assertEqual("rejected Bearer [redacted]", report["cells"][0]["failureExamples"]["http-500"][0]["detail"])

    def test_a_key_echoed_across_the_cut_of_a_server_message_never_survives_in_part(self):
        key = "pk|" + secrets.token_hex(20)                          # a pipe, which the markdown escapes
        self.judge.fail_every, self.judge.fail_status = 2, 401
        self.judge.fail_message = "e" * 182 + " Bearer " + key + " was refused"
        out, err = io.StringIO(), io.StringIO()
        output = self.root / "cut-run"
        argv = ["capacity", "probe", "--base-url", self.url + "/v1", "--api-key-env", "PROBE_JUDGE_KEY", "--cells", "2x1",
                "--output", str(output), "--cell-seconds", "0.6", "--warmup-seconds", "0.15", "--ramp-seconds", "0.05",
                "--drain-seconds", "3", "--pause-seconds", "0.02", "--sample-seconds", "0.1",
                "--abort-failure-share", "1.0"]
        with mock.patch.dict(os.environ, {"PROBE_JUDGE_KEY": key}), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(0, main(argv))
        texts = {"stdout": out.getvalue(), "stderr": err.getvalue(),
                 **{path.name: path.read_text() for path in output.iterdir()}}
        for name, text in texts.items():
            for fragment in self.fragments(key) + [key.replace("|", "\\|")]:
                with self.subTest(output=name, fragment=fragment):
                    self.assertNotIn(fragment, text)
        detail = json.loads(texts[P.REPORT_NAME])["cells"][0]["failureExamples"]["http-401"][0]["detail"]
        self.assertEqual(("e" * 182 + " Bearer [redacted]")[:P.DETAIL_CHARACTERS], detail)
        self.assertEqual("ab[redacted]", P.redact_key("ab" + key[5:17], key))
        self.assertEqual("ab" + key[5:12] + "z", P.redact_key("ab" + key[5:12] + "z", key))   # 7 characters stay

    def test_refusals_never_echo_the_key(self):
        key = "probe-key-" + secrets.token_hex(16)
        cases = [(["--base-url", "http://user:secret@127.0.0.1:9/v1"], "credentials"),
                 (["--base-url", self.url + "/v1?key=" + key], "credentials, query"),
                 (["--base-url", self.url + "/v1", "--api-key-env", "PROBE_UNSET_KEY"], "unset or empty")]
        os.environ.pop("PROBE_UNSET_KEY", None)
        for extra, message in cases:
            err = io.StringIO()
            with self.subTest(message=message), redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["capacity", "probe", *extra, "--output", str(self.root / "refused"), "--cells", "2x1"])
                self.assertEqual(5, code)
                self.assertIn(message, err.getvalue())
                self.assertNotIn(key, err.getvalue())
        self.assertFalse((self.root / "refused").exists())
        self.assertEqual(0, self.judge.requests)

    def test_a_refused_key_stops_before_any_cell(self):
        self.judge.models_status = 401
        with self.assertRaisesRegex(ValidationError, "HTTP 401"):
            self.probe("2x1", environ={"K": "probe-key-" + secrets.token_hex(8)}, api_key_env="K")
        self.assertEqual(0, self.judge.requests)
        self.assertFalse(self.output.exists())


class DryRunTests(ProbeCase):
    def test_dry_run_prints_the_plan_and_sends_nothing(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(0, main(["capacity", "probe", "--dry-run", "--base-url", self.url + "/v1",
                                      "--metrics-url", self.url + "/metrics", "--max-streams", "90"]))
        text = out.getvalue()
        self.assertIn("nothing is sent", text)
        self.assertIn("(not contacted)", text)
        self.assertEqual(16, len(re.findall(r"^\| \d+ \| \d+x\d+ \|", text, flags=re.M)))
        self.assertEqual(4, text.count("skipped: above --max-streams 90"))
        self.assertEqual(4, text.count("runs if the server can be shown unshared"))
        self.assertIn("exit criterion: cell 60x40 only", text)
        self.assertIn("| 1 | 30x40 | instrument | runs |", text)
        self.assertIn("| 2 | 60x40 | decisive | runs |", text)
        self.assertIn("| 40 | ", text)
        self.assertIn("estimated duration:", text)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(0, main(["--json", "capacity", "probe", "--dry-run", "--cells", "60x40,30x1",
                                      "--base-url", self.url + "/v1"]))
        plan = json.loads(out.getvalue())
        self.assertTrue(plan["dryRun"])
        self.assertEqual(["60x40", "30x1"], [row["cell"] for row in plan["load"]])
        self.assertEqual("60x40", plan["decision"]["cell"])
        sizes = {size["itemsPerCall"]: size for size in plan["requestSizes"]}
        self.assertEqual(28000, sizes[40]["maxTokens"])
        self.assertGreater(sizes[40]["bodyBytes"]["p50"], sizes[1]["bodyBytes"]["p50"] * 10)
        self.assertEqual(round(60 * sizes[40]["meanItems"] / 40, 2), plan["load"][0]["latencyBudgetSeconds"])
        self.assertEqual((0, 0), (self.judge.connections, self.judge.requests))


class ProfileTests(RunnerTestCase):
    def test_a_profile_from_a_census_database_feeds_the_probe(self):
        run = self.run_census()
        self.prepare(run)
        self.assertEqual("completed", run.run_abstract()["state"])
        run.close()
        profile = P.profile_from_database(self.database)
        self.assertEqual(P.PROFILE_FORMAT, profile["format"])
        self.assertEqual(1, profile["source"]["publicReviews"])
        self.assertEqual(15, profile["source"]["entries"])
        # The runner's mock judge reports 20 completion and 100 prompt tokens per call.
        calls = self.rows("SELECT COUNT(*) AS n, SUM(n_items) AS items FROM calls WHERE kind='first' AND error IS NULL")[0]
        self.assertEqual(round(20 * calls["n"] / calls["items"], 1), profile["expected"]["outputTokensPerEntry"])
        self.assertEqual(round(100 * calls["n"] / calls["items"], 1), profile["expected"]["promptTokensPerEntry"])
        self.assertAlmostEqual(1.0, sum(profile["profile"]["citingSentences"]["weights"]), places=3)
        self.assertNotIn("text", json.dumps(profile["profile"]).lower().replace("context", ""))
        path = self.root / "profile.json"
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, main(["capacity", "profile", "--database", str(self.database), "--output", str(path)]))
        loaded, expected, source = P.load_profile(path)
        self.assertEqual(profile["expected"]["outputTokensPerEntry"], expected["outputTokensPerEntry"])
        self.assertEqual("profile.json", source["path"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertNotEqual(0, main(["capacity", "profile", "--database", str(self.database), "--output", str(path)]))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(0, main(["--json", "capacity", "probe", "--dry-run", "--cells", "60x40", "--profile", str(path)]))
        plan = json.loads(out.getvalue())
        self.assertEqual(expected["outputTokensPerEntry"], plan["expectation"]["outputTokensPerEntry"])
        self.assertEqual({"outputTokensPerEntry": "profile", "promptTokensPerEntry": "profile"},
                         plan["expectation"]["source"])
        self.assertEqual("profile.json", plan["synthetic"]["profileSource"]["path"])
        self.assertEqual(loaded["reviewEntries"], plan["synthetic"]["profile"]["reviewEntries"])
        (self.root / "bad.json").write_text(json.dumps({"format": "other"}))
        with self.assertRaisesRegex(ValidationError, "not a capacity profile"):
            P.load_profile(self.root / "bad.json")


if __name__ == "__main__":
    unittest.main()
