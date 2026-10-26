# SPDX-License-Identifier: Apache-2.0
"""citation-census@1 through the broker: a mock run-principal socket (threaded Unix-socket HTTP), no daemon, no model.

The mock serves a run principal's socket routes (configs, bulk submit, the changed-job feed, job summaries, result
pages, resume) and the verbatim instrument mode as the contract describes it: the system message must equal the
installed prompt, model and request settings come from the provider and the bundle, and every receipt reports the
sha256 of the canonical JSON of the body sent upstream. It answers like the direct mock judge. No daemon code is used.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import socketserver
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ocsl_review import citation_census as C
from ocsl_review import instruments, scores_db
from ocsl_review.census_broker import (BrokerTransport, canonical_json, canonical_sha256, idempotency_key,
                                       request_sha256)
from ocsl_review.census_runner import CensusRun, HttpChatTransport, census_status
from ocsl_review.claim_check import ConfidentialPolicy
from ocsl_review.cli import main
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.instruments import broker_bundle
from ocsl_review.study_cli import gates

from test_census_runner import FIXTURES, JOURNAL, MODEL, MockJudge, record, start_server
from test_citation_census_fidelity import SYSTEM

POLICY_PREFIX = "Synthetic broker worker policy."
REASONING = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "ultracode")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _integral(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _integral(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_integral(item) for item in value]
    return value


def daemon_canonical(value) -> str:
    """The daemon's canonical JSON for ASCII keys, BMP text and plain numbers: sorted keys, no whitespace, 0.0 as 0."""
    return json.dumps(_integral(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_bundle(bundle) -> None:
    """The install-time checks of the bundle schema (strict keys, types, a verbatim transport)."""
    keys = {"instrumentId", "version", "systemPrompt", "responseSchema", "maxOutputTokens", "temperature", "seed",
            "reasoning", "itemsPerCall", "requiredModelFingerprint"}
    if not keys <= set(bundle) <= keys | {"transport"}:
        raise ValueError(f"bundle keys: {sorted(set(bundle) ^ keys)}")
    if not isinstance(bundle["systemPrompt"], str) or not bundle["systemPrompt"]:
        raise ValueError("systemPrompt")
    if set(bundle["maxOutputTokens"]) != {"perItem", "minimum"} or bundle["maxOutputTokens"]["perItem"] < 1:
        raise ValueError("maxOutputTokens")
    if not 0 <= bundle["temperature"] <= 2 or bundle["reasoning"] not in REASONING or bundle["itemsPerCall"] < 1:
        raise ValueError("settings")
    if bundle["responseSchema"] is not None or bundle.get("transport") not in (None, {"messages": "verbatim"}):
        raise ValueError("schema or transport")


class MockBroker:
    """A run principal's socket with one bulk worker, executing one-item instrument jobs on background threads."""

    def __init__(self, directory: Path):
        self.path = str(directory / "principal" / "broker.sock")
        self.principal, self.run_id, self.worker = "run-principal-census", "run-census-1", "remote_qwen38_none"
        self.model = self.served = MODEL
        self.instruments: dict[str, dict] = {}
        self.granted: set[str] = set()
        self.jobs: dict[str, dict] = {}
        self.keys: dict[str, str] = {}
        self.revision = 0
        self.upstream: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.lock = threading.RLock()
        self.judge = MockJudge()
        self.release = threading.Event()
        self.release.set()
        self.latency = 0.0                      # simulated upstream seconds per call, outside the lock
        self.inject_policy = self.omit_request_digest = self.refuse_items = False
        self.refuse_code = "INSTRUMENT_MESSAGE_MISMATCH"
        self.fail_next: list[str] = []
        self.refuse_next = self.rate_limit_next = 0
        self.resume_after = self.provider_paused = None
        self.budget: int | None = None
        self.dispatched = 0
        self.catalog_patch: dict = {}
        self.server = None

    # ------------------------------------------------------------------ operator side
    def install(self, digest: str, bundle: dict) -> None:
        validate_bundle(bundle)
        if digest in self.instruments and self.instruments[digest] != bundle:
            raise ValueError("INSTRUMENT_DIGEST_CONFLICT")
        self.instruments[digest] = json.loads(json.dumps(bundle))

    def install_census(self, journal: str = JOURNAL, tiers=("abstract", "full-text")) -> dict[str, str]:
        digests = {}
        for tier in tiers:
            exported = broker_bundle("citation-census@1", tier=tier, parameters={"journal": journal})
            self.install(exported["digest"], exported["bundle"])
            self.granted.add(exported["digest"])
            digests[tier] = exported["digest"]
        return digests

    def catalog(self) -> dict:
        installed = [{"digest": digest, "instrumentId": bundle["instrumentId"], "version": bundle["version"],
                      "itemsPerCall": bundle["itemsPerCall"]} for digest, bundle in self.instruments.items()
                     if digest in self.granted]
        value = {"version": 1, "revision": 3,
                 "authorization": {"principalId": self.principal, "kind": "run", "runId": self.run_id,
                                   "attemptId": self.principal, "state": "active", "publicLiteratureAllowed": True,
                                   "instruments": sorted(self.granted)},
                 "instrumentMode": 1, "instruments": installed,
                 "workers": [{"id": self.worker, "providerId": self.worker, "model": self.model,
                              "bulkConcurrencyLimit": 60, "bulkAvailable": True}]}
        for key, patch in self.catalog_patch.items():
            value[key] = {**value[key], **patch} if isinstance(patch, dict) else patch
        return value

    # ------------------------------------------------------------------ server
    def start(self) -> "MockBroker":
        directory = Path(self.path).parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def address_string(self):
                return "unix"

            def do_GET(self):
                broker.handle(self, "GET")

            def do_POST(self):
                broker.handle(self, "POST")

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        if os.path.exists(self.path):
            os.unlink(self.path)
        self.server = Server(self.path, Handler)
        os.chmod(self.path, 0o600)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if os.path.exists(self.path):
            os.unlink(self.path)

    def handle(self, handler, method: str) -> None:
        url = urllib.parse.urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        body = json.loads(handler.rfile.read(length)) if length else None
        with self.lock:
            self.requests.append((method, handler.path, {k.lower(): v for k, v in handler.headers.items()}))
            status, value = self.route(method, url.path, urllib.parse.parse_qs(url.query), body)
        data = json.dumps(value).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    # ------------------------------------------------------------------ routes
    def route(self, method, path, query, body):
        if method == "GET" and path == "/configs":
            return 200, self.catalog()
        if method == "POST" and path == "/bulk/submit":
            return self.submit(body)
        if method == "GET" and path == "/jobs":
            after, limit = int(query.get("after", ["0"])[0]), int(query.get("limit", ["100"])[0])
            changed = sorted((job for job in self.jobs.values() if job["revision"] > after), key=lambda j: j["revision"])
            page = changed[:limit]
            return 200, {"jobs": [self.summary(job) for job in page],
                         "nextCursor": page[-1]["revision"] if page else after, "hasMore": len(changed) > limit}
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "jobs":
            job = self.jobs.get(parts[1])
            if job is None:
                return 404, {"error": "Worker job is not owned by this principal.", "code": "WORKER_NOT_FOUND"}
            if method == "GET" and len(parts) == 2:
                return 200, self.summary(job)
            if method == "GET" and parts[2:] == ["results"]:
                after = int(query.get("after", ["0"])[0])
                rows = sorted((r for r in job["results"].values() if r["revision"] > after), key=lambda r: r["revision"])
                return 200, {"jobId": job["jobId"], "results": [dict(r) for r in rows],
                             "nextCursor": rows[-1]["revision"] if rows else after, "hasMore": False}
            if method == "POST" and parts[2:] == ["resume"]:
                return self.resume(job)
        return 404, {"error": "A principal socket serves only its worker routes.", "code": "UNKNOWN_PRINCIPAL_ROUTE"}

    def bump(self) -> int:
        self.revision += 1
        return self.revision

    def set_state(self, job, **fields) -> None:
        job.update(fields)
        job["revision"] = self.bump()

    def summary(self, job) -> dict:
        states = [r["state"] for r in job["results"].values()]
        out = {"jobId": job["jobId"], "kind": "bulk", "state": job["state"], "parentSessionName": f"principal:{self.principal}",
               "attemptId": self.principal, "configId": job["configId"], "revision": job["revision"],
               "instrumentDigest": job["instrumentDigest"],
               "progress": {"total": len(states), **{s: states.count(s) for s in set(states)}}}
        for key in ("error", "resumeAfter", "resumedFromJobId"):
            if job.get(key):
                out[key] = job[key]
        return out

    def submit(self, body):
        if set(body) - {"configId", "idempotencyKey", "items", "schema", "concurrency", "instrumentDigest"}:
            return 400, {"error": "unknown fields", "code": "INVALID_REQUEST"}
        if body.get("configId") != self.worker:
            return 404, {"error": "Select a configured worker id.", "code": "WORKER_CONFIG_NOT_FOUND"}
        digest = body.get("instrumentDigest")
        if digest not in self.granted:
            return 403, {"error": "This principal is not granted that instrument.", "code": "INSTRUMENT_NOT_GRANTED"}
        if digest not in self.instruments:
            return 400, {"error": "This instrument digest is not installed in the broker.", "code": "INSTRUMENT_NOT_ALLOWED"}
        fingerprint = sha(json.dumps(body, sort_keys=True))
        existing = self.keys.get(body["idempotencyKey"])
        if existing is not None:
            job = self.jobs[existing]
            if job["fingerprint"] != fingerprint:
                return 409, {"error": "Idempotency key was used for different work.", "code": "WORKER_KEY_CONFLICT"}
            return 200, self.summary(job)
        job = self.create(body["idempotencyKey"], fingerprint, digest, body["items"])
        return 200, self.summary(job)

    def create(self, key, fingerprint, digest, items, resumed_from=None) -> dict:
        job = {"jobId": str(uuid.uuid4()), "state": "queued", "idempotencyKey": key, "fingerprint": fingerprint,
               "configId": self.worker, "instrumentDigest": digest, "items": items, "error": None, "resumeAfter": None,
               "resumedFromJobId": resumed_from, "results": {}}
        for item in items:
            job["results"][item["id"]] = {"id": item["id"], "state": "queued", "providerId": self.worker,
                                          "model": self.model, "promptSha256": sha(json.dumps(item["input"])),
                                          "usage": None, "revision": self.bump()}
        job["revision"] = self.bump()
        self.jobs[job["jobId"]] = job
        self.keys[key] = job["jobId"]
        threading.Thread(target=self.execute_job, args=(job["jobId"],), daemon=True).start()
        return job

    def resume(self, job):
        if job["state"] not in ("paused", "interrupted", "failed", "cancelled"):
            return 409, {"error": "Only settled or paused jobs can resume.", "code": "INVALID_REQUEST"}
        if self.budget is not None and self.dispatched >= self.budget:
            return 409, {"error": "Principal admission is closed: budget_exhausted.", "code": "BUDGET_EXHAUSTED"}
        self.provider_paused = None
        key = sha(job["idempotencyKey"])[:48] + "-resume-" + job["jobId"]
        if key in self.keys:
            resumed = self.jobs[self.keys[key]]
        else:
            items = [item for item in job["items"] if job["results"][item["id"]]["state"] != "completed"]
            resumed = self.create(key, job["fingerprint"], job["instrumentDigest"], items, resumed_from=job["jobId"])
        full = self.summary(resumed)
        full["results"] = [dict(r) for r in resumed["results"].values()]
        return 200, full

    # ------------------------------------------------------------------ execution
    def execute_job(self, job_id) -> None:
        self.release.wait(20)
        if self.latency:
            time.sleep(self.latency)
        with self.lock:
            job = self.jobs[job_id]
            if job["state"] != "queued":
                return
            self.set_state(job, state="running")
            for item in job["items"]:
                if job["state"] != "running":
                    break
                self.execute_item(job, item)
            if job["state"] == "running":
                done = all(r["state"] == "completed" for r in job["results"].values())
                self.set_state(job, state="completed" if done else "failed")

    def settle(self, job, result, **fields) -> None:
        result.update(fields)
        result["revision"] = self.bump()

    def execute_item(self, job, item) -> None:
        bundle = self.instruments[job["instrumentDigest"]]
        result = job["results"][item["id"]]
        if self.provider_paused is not None:
            self.set_state(job, state="paused", error=self.provider_paused, resumeAfter=self.resume_after)
            return
        if self.budget is not None and self.dispatched >= self.budget:
            self.settle(result, result, state="paused", errorCode="BUDGET_EXHAUSTED",
                        error="Principal admission is closed: budget_exhausted.")
            self.set_state(job, state="paused", error="budget_exhausted")
            return
        data = item["input"]
        messages = data.get("messages") if isinstance(data, dict) else None
        if self.refuse_items:
            # A refusal before any provider call, with the code the daemon would record (no attempt, no receipt).
            self.settle(job, result, state="failed", errorCode=self.refuse_code,
                        error=f"Refused before any provider call ({self.refuse_code}).")
            return
        if (not isinstance(messages, list) or not messages or set(data) - {"messages", "itemCount"}
                or messages[0].get("role") != "system" or messages[0].get("content") != bundle["systemPrompt"]
                or data.get("itemCount", 1) > bundle["itemsPerCall"]):
            # The daemon refuses every verbatim item that is not the installed instrument's request this way.
            self.settle(job, result, state="failed", errorCode="INSTRUMENT_MESSAGE_MISMATCH",
                        error="Verbatim instrument call refused: the system message differs from the installed "
                              "instrument prompt.")
            return
        count = data.get("itemCount", bundle["itemsPerCall"])
        system = (POLICY_PREFIX + "\n\n" if self.inject_policy else "") + messages[0]["content"]
        sent = [{"role": "system", "content": system}, *messages[1:]]
        body = {"model": self.model, "messages": sent,
                "max_tokens": max(bundle["maxOutputTokens"]["minimum"], bundle["maxOutputTokens"]["perItem"] * count),
                "temperature": bundle["temperature"], "stream": False, "reasoning_effort": bundle["reasoning"]}
        if bundle["seed"] is not None:
            body["seed"] = bundle["seed"]
        self.dispatched += 1
        self.upstream.append(json.loads(json.dumps(body)))
        settings = {"model": body["model"], "temperature": body["temperature"], "seed": body.get("seed"),
                    "reasoning": body["reasoning_effort"], "max_tokens": body["max_tokens"], "response_format_sha256": None,
                    "system_prompt_sha256": sha(system)}
        measured = {"model": self.served, "serverFingerprint": "fp-broker", "settingsSha256": sha(daemon_canonical(settings)),
                    "attempts": 1, "finishReason": "stop", "promptSha256": sha(json.dumps(body)),
                    "usage": {"input": 100, "output": 20, "cacheRead": 64, "cacheWrite": None, "reasoning": None,
                              "total": 120, "quality": "measured"}}
        if not self.omit_request_digest:
            measured["requestSha256"] = sha(daemon_canonical(body))
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            self.provider_paused = f"Provider {self.worker} requires attention (HTTP 429); resume explicitly."
            self.settle(job, result, **measured, state="paused", errorCode="PROVIDER_RATE_LIMITED",
                        error="Provider request failed with HTTP 429.")
            self.set_state(job, state="paused", error=self.provider_paused, resumeAfter=self.resume_after)
            return
        if self.fail_next:
            self.settle(job, result, **{**measured, "attempts": 3}, state="failed", errorCode=self.fail_next.pop(0),
                        error="Provider request failed with HTTP 502.")
            return
        if self.refuse_next:
            self.refuse_next -= 1
            self.settle(job, result, **measured, state="failed", errorCode="INVALID_REQUEST",
                        error="Provider request failed with HTTP 400.")
            return
        required = bundle["requiredModelFingerprint"]
        if required and self.served != required:
            self.provider_paused = f"Model drift: the instrument requires {required}; the provider served {self.served}."
            self.settle(job, result, **measured, state="paused", errorCode="MODEL_DRIFT", error=self.provider_paused)
            self.set_state(job, state="paused", error=self.provider_paused)
            return
        output = self.judge.answer({"messages": sent})
        self.settle(job, result, **measured, state="completed", output=output, outputDigest=sha(output),
                    outputBytes=len(output.encode()))

    def calls_to_route(self, method: str, prefix: str) -> int:
        with self.lock:
            return sum(1 for m, p, _ in self.requests if m == method and p.startswith(prefix))


# ----------------------------------------------------------------------------- canonical JSON
# Inputs and the canonical strings JavaScript's JSON.stringify-based canonical JSON writes for them (recorded once).
VECTORS = [
    ('{"b": 1, "a": [1.5, 0.0, -0.0, 1e21, 1.5e21, 1e-7, 0.000001, 1e16, 123456789012345680000, 2.5, 100.0, 5e-324, 0.1, '
     '-3.25, 0.001234, 1e300, -1e-10, 9007199254740991]}',
     '{"a":[1.5,0,0,1e+21,1.5e+21,1e-7,0.000001,10000000000000000,123456789012345680000,2.5,100,5e-324,0.1,-3.25,0.001234,'
     '1e+300,-1e-10,9007199254740991],"b":1}'),
    ('{"B": 1, "a": 2, "_": 3, "10": 4, "9": 5, "\\u00e9": 6, "\\ud83d\\ude00": 7, "\\uff5e": 8, "": 9}',
     '{"":9,"10":4,"9":5,"B":1,"_":3,"a":2,"\u00e9":6,"\U0001F600":7,"\uff5e":8}'),
    ('["\\u00e9", "\\u2028", "\\u0001", "\\"", "\\\\", "\\n\\t\\r\\b\\f", "\\ud83d\\ude00", "\\u007f", "\\u001f", "/", '
     '"\\ud800", "a\\udc00b"]',
     '["\u00e9","\u2028","\\u0001","\\"","\\\\","\\n\\t\\r\\b\\f","\U0001F600","\x7f","\\u001f","/","\\ud800","a\\udc00b"]'),
    ('{"model": "m", "messages": [{"role": "system", "content": "S\\u00e9"}, {"role": "user", "content": "line\\nnext"}], '
     '"max_tokens": 3000, "temperature": 0.0, "stream": false, "reasoning_effort": "none", "seed": null}',
     '{"max_tokens":3000,"messages":[{"content":"S\u00e9","role":"system"},{"content":"line\\nnext","role":"user"}],'
     '"model":"m","reasoning_effort":"none","seed":null,"stream":false,"temperature":0}'),
]


class CanonicalJsonTests(unittest.TestCase):
    def test_matches_javascript_canonical_json(self):
        for source, expected in VECTORS:
            with self.subTest(source=source[:40]):
                self.assertEqual(expected, canonical_json(json.loads(source)))

    def test_python_surrogate_pairs_join_and_unknown_types_are_refused(self):
        self.assertEqual('"\U0001F600"', canonical_json("\ud83d\ude00"))
        with self.assertRaises(ValidationError):
            canonical_json({1: "a"})
        with self.assertRaises(ValidationError):
            canonical_json({"a": object()})
        with self.assertRaises(ValidationError):
            canonical_json(10 ** 400)

    def test_request_digest_of_an_instrument_call(self):
        messages = C.build_abstract_messages([{"label": "1", "citing_passages": [{"sentence": "Claim \u00e9 [1].", "section": "Intro"}],
                                               "cited_work": {"title": "T", "venue": "V", "year": 2020}, "abstract": "A."}],
                                             "Review", JOURNAL)
        payload = C.request_payload(messages, model=MODEL, items=1)
        self.assertEqual(sha(daemon_canonical(payload)), request_sha256(payload))
        self.assertNotEqual(sha(C.request_body(payload).decode()), request_sha256(payload), "canonical, not the sent bytes")
        self.assertEqual(idempotency_key("call-a", "0" * 64), idempotency_key("call-a", "0" * 64))
        self.assertNotEqual(idempotency_key("call-a", "0" * 64), idempotency_key("call-a", "1" * 64))
        self.assertLessEqual(len(idempotency_key("call-a", "0" * 64)), 128)


# ----------------------------------------------------------------------------- export
class ExportTests(unittest.TestCase):
    def test_bundle_prompt_is_the_builders_prompt_byte_for_byte(self):
        for journal, digest in SYSTEM.items():
            for tier in ("abstract", "full-text"):
                with self.subTest(journal=journal, tier=tier):
                    exported = broker_bundle("citation-census@1", tier=tier, parameters={"journal": journal})
                    self.assertEqual(digest, sha(exported["bundle"]["systemPrompt"]))
                    self.assertEqual(C.system_prompt(journal), exported["bundle"]["systemPrompt"])

    def test_bundle_fields_and_digests(self):
        instrument = instruments.load("citation-census@1")
        abstract = broker_bundle("citation-census@1", tier="abstract", parameters={"journal": JOURNAL})
        full_text = broker_bundle("citation-census@1", tier="full-text", parameters={"journal": JOURNAL})
        other = broker_bundle("citation-census@1", tier="abstract", parameters={"journal": "Another Synthetic Journal"})
        bundle = abstract["bundle"]
        validate_bundle(bundle)
        self.assertEqual({"instrumentId": "citation-census@1", "version": 1, "responseSchema": None,
                          "maxOutputTokens": {"perItem": 700, "minimum": 3000}, "temperature": 0.0, "seed": None,
                          "reasoning": "none", "itemsPerCall": 40, "requiredModelFingerprint": MODEL,
                          "transport": {"messages": "verbatim"}},
                         {k: v for k, v in bundle.items() if k != "systemPrompt"})
        self.assertEqual((5, "low"), (full_text["bundle"]["itemsPerCall"], full_text["bundle"]["reasoning"]))
        self.assertEqual(instrument.digest, abstract["registryDigest"])
        self.assertRegex(abstract["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(3, len({abstract["digest"], full_text["digest"], other["digest"]}))
        self.assertNotIn(instrument.digest, {abstract["digest"], full_text["digest"]})
        self.assertEqual(abstract, broker_bundle("citation-census@1", tier="abstract", parameters={"journal": JOURNAL}))
        self.assertEqual(canonical_sha256(bundle), abstract["bundleSha256"])
        self.assertEqual(f"PUT /v2/instruments/{abstract['digest']}", abstract["install"])

    def test_export_refusals(self):
        for kwargs, message in (({"parameters": {"journal": JOURNAL}}, "choose one with --tier"),
                                ({"tier": "abstract"}, "needs --param journal"),
                                ({"tier": "abstract", "parameters": {"journal": JOURNAL, "x": "y"}}, "no parameter x"),
                                ({"tier": "abstract", "parameters": {"journal": "two\nlines"}}, "one non-empty line"),
                                ({"tier": "abstract", "parameters": {"journal": "{{journal}}"}}, "one non-empty line"),
                                ({"tier": "summary", "parameters": {"journal": JOURNAL}}, "has no summary tier")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValidationError, message):
                broker_bundle("citation-census@1", **kwargs)
        with self.assertRaisesRegex(ValidationError, "no verbatim message transport"):
            broker_bundle("citation-support@2", tier="abstract")

    def test_export_command(self):
        def run(*argv):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["instruments", "export", "citation-census@1", *argv])
            return code, buffer.getvalue()
        code, output = run("--tier", "full-text", "--param", f"journal={JOURNAL}")
        self.assertEqual(0, code)
        exported = json.loads(output)
        self.assertEqual(broker_bundle("citation-census@1", tier="full-text", parameters={"journal": JOURNAL}), exported)
        code, output = run("--tier", "full-text", "--param", f"journal={JOURNAL}", "--body")
        self.assertEqual((0, exported["bundle"]), (code, json.loads(output)))
        self.assertNotEqual(0, run("--param", f"journal={JOURNAL}")[0])
        self.assertNotEqual(0, run("--tier", "abstract", "--param", "journal")[0])


# ----------------------------------------------------------------------------- runs through the broker
class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocsl-b-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = MockBroker(self.root).start()
        self.addCleanup(self.broker.stop)
        self.database = self.root / "census.sqlite3"
        self.now = [time.time()]
        instrument = instruments.load("citation-census@1")
        self.bundle = instruments.study_bundle_digest(
            protocol={"seed": 1234, "window": ["2001-01-01", "2002-12-31"]}, study_prompt="Synthetic study prompt.",
            instruments={"citation-census@1": instrument.digest}, dataset_versions={"synthetic": "v1"},
            package_tags={"ocsl-review": "test"})

    def transport(self, **kwargs) -> BrokerTransport:
        options = {"poll_interval": 0.01, "wait_seconds": 15.0, "outage_seconds": 0.3, "wall": lambda: self.now[0]}
        return BrokerTransport(self.broker.path, **{**options, **kwargs})

    def run_census(self, transport=None, **kwargs) -> CensusRun:
        run = CensusRun(self.database, seed=1234, transport=transport or self.transport(), concurrency=4,
                        sleep=lambda s: None, **kwargs)
        self.addCleanup(run.close)
        return run

    def prepare(self, run, *records, bundle=True):
        if bundle:
            run.pin_study_bundle(self.bundle["sha256"])
        for item in records or (record(),):
            run.prepare_review(item, (FIXTURES / "review.xml").read_bytes())

    def rows(self, sql, *params):
        with closing(sqlite3.connect(self.database)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, params)]

    def status(self):
        with closing(scores_db.open_readonly(self.database)) as connection:
            return census_status(connection, code_instrument_digest=instruments.load("citation-census@1").digest)


class BrokerAbstractTierTests(BrokerTestCase):
    def test_end_to_end_through_the_principal_socket(self):
        digests = self.broker.install_census()
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("completed", 1, 15, 0), (result["state"], result["reviews"], result["accepted"], result["invalid"]))
        self.assertEqual(("broker", "run-principal-census", "remote_qwen38_none"),
                         (result["transport"]["kind"], result["transport"]["principalId"], result["transport"]["worker"]))
        call = self.rows("SELECT * FROM calls")[0]
        self.assertEqual(("broker", digests["abstract"], call["call_id"], self.bundle["sha256"], "fp-broker", MODEL),
                         (call["transport"], call["broker_instrument_digest"], call["broker_item_id"],
                          call["study_bundle_sha256"], call["fingerprint"], call["served_model"]))
        self.assertEqual(call["request_sha256"], call["broker_request_sha256"])
        self.assertIn(call["broker_job_id"], self.broker.jobs)
        self.assertEqual("run-principal-census", call["broker_principal_id"])
        self.assertRegex(call["broker_settings_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual((100, 20, 120, 64, 1, 10500), (call["prompt_tokens"], call["completion_tokens"], call["total_tokens"],
                                                        call["cached_tokens"], call["attempts"], call["max_tokens"]))
        # What went upstream is the instrument's request: the builder's system prompt, settings from the bundle.
        sent = self.broker.upstream[0]
        self.assertEqual(SYSTEM[JOURNAL], sha(sent["messages"][0]["content"]))
        self.assertEqual(call["system_sha256"], sha(sent["messages"][0]["content"]))
        self.assertEqual(call["request_sha256"], sha(daemon_canonical(sent)))
        self.assertEqual((MODEL, 10500, "none", False), (sent["model"], sent["max_tokens"], sent["reasoning_effort"],
                                                         sent["stream"]))
        # The socket is the identity: no credential or bearer token ever travels, and the catalog is read once.
        self.assertTrue(all("authorization" not in headers for _, _, headers in self.broker.requests))
        self.assertEqual(1, self.broker.calls_to_route("GET", "/configs"))
        job = self.broker.jobs[call["broker_job_id"]]
        self.assertEqual(idempotency_key(call["call_id"], call["request_sha256"]), job["idempotencyKey"])
        self.assertEqual({"messages", "itemCount"}, set(job["items"][0]["input"]))
        self.assertEqual(15, job["items"][0]["input"]["itemCount"])
        status = self.status()
        self.assertEqual((1, 0, 0, 0, {"broker": 1}), (status["brokerCalls"], status["brokerRequestMismatches"],
                                                       status["brokerVerdictsFromUnverifiedRequests"],
                                                       status["receiptsWithoutStudyBundle"], status["callsByTransport"]))
        report = gates(status)
        self.assertTrue(report["checks"]["everyBrokerVerdictFromAVerifiedRequest"])
        self.assertTrue(report["checks"]["everyReceiptCarriesTheStudyBundle"])
        self.assertTrue(report["checks"]["everyReceiptHasFingerprint"])

    def test_broker_and_direct_requests_are_the_same_request(self):
        self.broker.install_census()
        judge = MockJudge()
        server = start_server(judge)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        direct_db = self.root / "direct.sqlite3"
        direct = CensusRun(direct_db, seed=1234, concurrency=4, sleep=lambda s: None,
                           transport=HttpChatTransport("http://127.0.0.1:%d/v1" % server.server_address[1]))
        self.addCleanup(direct.close)
        for run in (direct, self.run_census()):
            judge.drop_first = self.broker.judge.drop_first = {"2", "9"}
            self.prepare(run)
            self.assertEqual(15, run.run_abstract()["accepted"])
        with closing(sqlite3.connect(direct_db)) as c:
            direct_calls = c.execute("SELECT kind, request_sha256, body_sha256, transport FROM calls ORDER BY kind").fetchall()
        broker_calls = [tuple(r.values()) for r in self.rows("SELECT kind, request_sha256, body_sha256, transport FROM calls "
                                                             "ORDER BY kind")]
        self.assertEqual([(k, r, b) for k, r, b, _ in direct_calls], [(k, r, b) for k, r, b, _ in broker_calls])
        self.assertEqual(({"direct"}, {"broker"}), ({t for *_, t in direct_calls}, {t for *_, t in broker_calls}))
        self.assertEqual([daemon_canonical(_integral(request["payload"])) for request in judge.requests],
                         [daemon_canonical(body) for body in self.broker.upstream])
        repair = self.broker.upstream[1]
        self.assertEqual((3000, ["2", "9"]), (repair["max_tokens"],
                                              [line[len("### id="):] for line in repair["messages"][1]["content"].split("\n")
                                               if line.startswith("### id=")]))

    def test_request_digest_mismatch_invalidates_the_call_and_stops_the_run(self):
        self.broker.install_census()
        self.broker.inject_policy = True
        run = self.run_census()
        self.prepare(run, record(), record(doi="10.5555/second"))
        result = run.run_abstract()
        self.assertEqual(("paused", 0, 0), (result["state"], result["accepted"], result["invalid"]))
        self.assertTrue(result["reason"].startswith("fidelity: call "), result["reason"])
        self.assertIn("verbatim instrument calls unchanged", result["reason"])
        self.assertEqual("paused:fidelity", self.status()["runState"])
        calls = self.rows("SELECT error, request_sha256, broker_request_sha256, n_parsed FROM calls")
        self.assertTrue(calls and all(c["error"] == "request_mismatch" and c["n_parsed"] == 0 for c in calls))
        self.assertTrue(all(c["request_sha256"] != c["broker_request_sha256"] for c in calls))
        self.assertEqual(0, self.rows("SELECT COUNT(*) AS n FROM citations WHERE abs_basis IS NOT NULL AND "
                                      "abs_basis != 'deterministic:not-cited'")[0]["n"], "no label decided, none invalid")
        report = gates(self.status())
        self.assertFalse(report["ok"])
        self.assertTrue(any("another request than the instrument built" in note for note in report["notes"]))
        self.assertLessEqual(len(self.broker.upstream), 4, "the run stops dispatching at the first mismatch")
        self.broker.inject_policy = False
        resumed = run.run_abstract()
        self.assertEqual(("completed", 30), (resumed["state"], resumed["accepted"]))
        status = self.status()
        self.assertEqual((0, "active"), (status["brokerVerdictsFromUnverifiedRequests"], status["runState"]))
        self.assertGreaterEqual(status["brokerRequestMismatches"], 1)
        self.assertTrue(gates(status)["checks"]["everyBrokerVerdictFromAVerifiedRequest"])

    def test_a_receipt_without_a_request_digest_is_a_mismatch(self):
        self.broker.install_census()
        self.broker.omit_request_digest = True
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual("paused", result["state"])
        self.assertIn("no request digest", result["reason"])
        self.assertEqual("request_mismatch", self.rows("SELECT error FROM calls")[0]["error"])

    def test_a_broker_that_refuses_verbatim_calls_stops_the_run(self):
        # Every refusal before a provider call stops the run: nothing reached a judge and the same request would be
        # refused again, so no repair call follows and no entry becomes an invalid output.
        self.broker.install_census()
        self.broker.refuse_items = True
        for seed, code in enumerate(("INSTRUMENT_MESSAGE_MISMATCH", "PRINCIPAL_REQUIRED", "INVALID_REQUEST"), 1):
            with self.subTest(code=code):
                self.broker.refuse_code = code
                database = self.root / f"refused-{code}.sqlite3"
                # Another seed is another run (other call ids), so no call meets a job of an earlier subtest.
                run = CensusRun(database, seed=seed, transport=self.transport(), concurrency=4, sleep=lambda s: None)
                self.addCleanup(run.close)
                self.prepare(run, record(), record(doi="10.5555/second"))
                result = run.run_abstract()
                self.assertEqual(("paused", 0, 0), (result["state"], result["accepted"], result["invalid"]))
                self.assertTrue(result["reason"].startswith(f"fidelity: call call-"), result["reason"])
                self.assertIn(code, result["reason"])
                with closing(sqlite3.connect(database)) as c:
                    calls = c.execute("SELECT kind, error, attempts, served_model, n_parsed FROM calls").fetchall()
                    state = c.execute("SELECT value FROM meta WHERE key='runState'").fetchone()[0]
                self.assertEqual("paused:fidelity", state)
                self.assertTrue(calls and all(kind == "first" and error.startswith(f"broker refused the instrument call: {code}")
                                              and (attempts, served, parsed) == (0, None, 0)
                                              for kind, error, attempts, served, parsed in calls), calls)
        self.assertEqual([], self.broker.upstream)

    def test_a_bundle_installed_wrongly_under_the_census_digest_stops_the_run_before_any_call(self):
        digests = self.broker.install_census()
        installed = self.broker.instruments[digests["abstract"]]
        installed["systemPrompt"] += "\nAlso consider the review's tone."
        run = self.run_census()
        self.prepare(run, record(), record(doi="10.5555/second"))
        result = run.run_abstract()
        self.assertEqual(("paused", 0, 0), (result["state"], result["accepted"], result["invalid"]))
        self.assertIn("INSTRUMENT_MESSAGE_MISMATCH", result["reason"])
        self.assertIn(f"the bundle installed under {digests['abstract']}", result["reason"])
        self.assertIn("ocsl-review instruments export", result["reason"])
        self.assertEqual([], self.broker.upstream, "the broker refused every item before a provider call")
        status = self.status()
        self.assertEqual(("paused:fidelity", len(self.rows("SELECT call_id FROM calls")), 0),
                         (status["runState"], status["brokerRefusals"], status["brokerRequestMismatches"]))
        self.assertTrue(any("refused before any provider call" in note for note in gates(status)["notes"]))
        self.assertEqual({"first"}, {c["kind"] for c in self.rows("SELECT kind FROM calls")}, "no repair call")
        self.assertLessEqual(len(self.rows("SELECT call_id FROM calls")), 2, "the run stops at the first refusal")
        installed["systemPrompt"] = C.system_prompt(JOURNAL)
        resumed = run.run_abstract()
        self.assertEqual(("completed", 30, 0), (resumed["state"], resumed["accepted"], self.status()["duplicateJudgments"]))

    def test_repair_failures_and_refusals_follow_the_direct_semantics(self):
        self.broker.install_census()
        self.broker.fail_next = ["PROVIDER_UNREACHABLE"]
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("completed", 15, 1), (result["state"], result["accepted"], result["transportFailures"]))
        calls = {c["kind"]: c for c in self.rows("SELECT * FROM calls")}
        self.assertEqual(("broker failed: PROVIDER_UNREACHABLE", 3, None),
                         (calls["first"]["error"], calls["first"]["attempts"], calls["first"]["served_model"]))
        self.assertEqual((15, None), (calls["repair"]["n_items"], calls["repair"]["error"]))
        self.assertEqual(1, self.status()["transportFailures"])

    def test_upstream_refusal_is_an_answer_without_labels(self):
        self.broker.install_census()
        self.broker.refuse_next = 2
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("completed", 0, 15), (result["state"], result["accepted"], result["invalid"]))
        self.assertEqual(["refused: broker INVALID_REQUEST"] * 2, [c["error"] for c in self.rows("SELECT error FROM calls")])


class BrokerPauseTests(BrokerTestCase):
    def test_provider_limit_with_resume_after_pauses_and_the_next_invocation_resumes_the_job(self):
        self.broker.install_census()
        later = datetime.fromtimestamp(self.now[0], timezone.utc) + timedelta(minutes=5)
        self.broker.rate_limit_next = 1
        self.broker.resume_after = later.isoformat().replace("+00:00", "Z")
        run = self.run_census()
        self.prepare(run)
        paused = run.run_abstract()
        self.assertEqual(("paused", 0, self.broker.resume_after), (paused["state"], paused["accepted"], paused["resumeAfter"]))
        self.assertIn("transport: broker halted", paused["reason"])
        status = self.status()
        self.assertEqual(("paused:transport", self.broker.resume_after), (status["runState"], status["runPause"]["resumeAfter"]))
        self.assertEqual([], self.rows("SELECT * FROM calls"), "a paused call keeps its call id for the resume")
        self.assertEqual(0, self.broker.calls_to_route("POST", "/jobs/"), "no resume before resumeAfter")
        self.now[0] = later.timestamp() + 1
        resumed = run.run_abstract()
        self.assertEqual(("completed", 15), (resumed["state"], resumed["accepted"]))
        self.assertEqual(1, self.broker.calls_to_route("POST", "/jobs/"))
        call = self.rows("SELECT * FROM calls")[0]
        job = self.broker.jobs[call["broker_job_id"]]
        self.assertIsNotNone(job["resumedFromJobId"], "the call settled in the resumed job of the same lineage")
        self.assertEqual(2, len(self.broker.upstream))
        self.assertEqual((0, "active", None), (self.status()["duplicateJudgments"], self.status()["runState"],
                                               self.status()["runPause"]))

    def test_a_stale_provider_pause_is_resumed_once(self):
        self.broker.install_census()
        self.broker.provider_paused = "Provider requires attention (HTTP 429); resume explicitly."
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("completed", 15), (result["state"], result["accepted"]))
        self.assertEqual(1, result["transport"]["resumed"])

    def test_a_pause_that_returns_after_the_resume_halts(self):
        self.broker.install_census()
        self.broker.rate_limit_next = 2
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("paused", 1, 2), (result["state"], result["transport"]["resumed"], len(self.broker.upstream)))
        self.assertEqual("PROVIDER_RATE_LIMITED", result["transport"]["halted"]["code"])
        self.assertEqual([], self.rows("SELECT * FROM calls"))
        result = run.run_abstract()
        self.assertEqual(("completed", 15), (result["state"], result["accepted"]))

    def test_budget_exhaustion_pauses_until_the_budget_is_raised(self):
        self.broker.install_census()
        self.broker.budget = 0
        run = self.run_census()
        self.prepare(run, record(), record(doi="10.5555/second"))
        paused = run.run_abstract()
        self.assertEqual(("paused", 0), (paused["state"], paused["accepted"]))
        self.assertEqual("BUDGET_EXHAUSTED", paused["transport"]["halted"]["code"])
        self.assertIn("budget_exhausted", paused["reason"])
        self.assertEqual([], self.broker.upstream)
        again = run.run_abstract()
        self.assertEqual("paused", again["state"], "the resume is refused while the budget stays exhausted")
        self.broker.budget = None
        done = run.run_abstract()
        self.assertEqual(("completed", 30), (done["state"], done["accepted"]))
        self.assertEqual(0, self.status()["duplicateJudgments"])

    def test_model_drift_at_the_broker_pauses_the_run(self):
        self.broker.install_census()
        self.broker.served = "another/model"
        run = self.run_census()
        self.prepare(run)
        paused = run.run_abstract()
        self.assertEqual("paused", paused["state"])
        self.assertIn("model_drift", paused["reason"])
        call = self.rows("SELECT error, served_model, broker_error_code FROM calls")[0]
        self.assertEqual(("model_drift", "another/model", "MODEL_DRIFT"), tuple(call.values()))
        self.assertEqual("paused:model_drift", self.status()["runState"])
        self.broker.served = MODEL
        resumed = run.run_abstract()
        self.assertEqual(("completed", 15), (resumed["state"], resumed["accepted"]))

    def test_an_outage_leaves_entries_open_and_the_next_invocation_finishes(self):
        self.broker.install_census()
        run = self.run_census(breaker=2)
        self.prepare(run, record(), record(doi="10.5555/second"), record(doi="10.5555/third"))
        run.transport.open()
        self.broker.stop()
        paused = run.run_abstract(transport_retries=1, cooldown=0)
        self.assertEqual(("paused", 0, 0), (paused["state"], paused["accepted"], paused["invalid"]))
        self.assertEqual("paused:transport", self.status()["runState"])
        self.assertEqual([], self.rows("SELECT * FROM calls"))
        self.broker.start()
        done = run.run_abstract(cooldown=0)
        self.assertEqual(("completed", 45, 0), (done["state"], done["accepted"], done["invalid"]))

    def test_a_call_repeated_after_a_crash_meets_its_job_and_never_judges_twice(self):
        self.broker.install_census()
        self.broker.release.clear()            # the broker holds the job: the executor gives up waiting ("crash")
        run = self.run_census(transport=self.transport(wait_seconds=0.3))
        self.prepare(run)
        paused = run.run_abstract(transport_retries=0)
        self.assertEqual(("paused", 0), (paused["state"], paused["accepted"]))
        self.assertEqual([], self.rows("SELECT * FROM calls"))
        self.assertEqual(1, len(self.broker.jobs), "no repair call while the first call may still run")
        self.broker.release.set()
        deadline = time.time() + 10
        while self.broker.dispatched < 1 and time.time() < deadline:
            time.sleep(0.01)
        done = run.run_abstract()
        self.assertEqual(("completed", 15), (done["state"], done["accepted"]))
        self.assertEqual((1, 1, 1), (len(self.broker.upstream), len(self.broker.jobs), done["transport"]["replayed"]))
        self.assertEqual(0, self.status()["duplicateJudgments"])


class BrokerPreflightTests(BrokerTestCase):
    def test_the_socket_must_belong_to_an_active_run_principal(self):
        for patch, message in (({"authorization": {"principalId": None}}, "names no principal"),
                               ({"authorization": {"kind": "human"}}, "not a run principal"),
                               ({"authorization": {"state": "revoked"}}, "revoked"),
                               ({"instrumentMode": None}, "instrument mode")):
            with self.subTest(patch=patch):
                self.broker.catalog_patch = patch
                with self.assertRaisesRegex(ValidationError, message):
                    self.transport().open()
        self.broker.catalog_patch = {"workers": []}
        with self.assertRaisesRegex(ValidationError, "no bulk worker"):
            self.transport().open()
        self.broker.catalog_patch = {}
        with self.assertRaisesRegex(ValidationError, "has no bulk worker 'other'"):
            self.transport(worker="other").open()

    def test_missing_bundles_model_or_study_bundle_refuse_before_any_job(self):
        run = self.run_census()
        self.prepare(run, bundle=False)
        with self.assertRaisesRegex(ValidationError, "pin it first with study prepare --study-bundle"):
            run.run_abstract()
        run.pin_study_bundle(self.bundle["sha256"])
        with self.assertRaisesRegex(ValidationError, "instruments export"):
            run.run_abstract()
        exported = broker_bundle("citation-census@1", tier="abstract", parameters={"journal": JOURNAL})
        self.broker.granted.add(exported["digest"])
        with self.assertRaisesRegex(ValidationError, "granted, not installed"):
            run.run_abstract()
        self.broker.install(exported["digest"], exported["bundle"])
        self.broker.model = "served-under-another-name"
        transport = self.transport()
        run.close()
        run = self.run_census(transport=transport)
        with self.assertRaisesRegex(ValidationError, "pass --model to match"):
            run.run_abstract()
        self.assertEqual(0, self.broker.calls_to_route("POST", "/bulk/submit"))

    def test_entitled_reviews_need_an_on_premises_broker_provider(self):
        self.broker.install_census()
        run = self.run_census()
        self.prepare(run, {**record(), "provenanceClass": "entitled"})
        with self.assertRaises(ConfidentialityError):
            run.run_abstract()
        self.assertEqual(0, self.broker.calls_to_route("POST", "/bulk/submit"))
        run.transport.on_premises = True
        self.assertEqual("completed", run.run_abstract()["state"])


class StudyBundleTests(BrokerTestCase):
    def test_a_bundle_file_is_checked_and_pinned_once(self):
        path = self.root / "bundle.json"
        path.write_text(json.dumps(self.bundle))
        run = self.run_census()
        self.assertEqual({"studyBundleSha256": self.bundle["sha256"], "contentVerified": True}, run.pin_study_bundle(path))
        self.assertEqual({"studyBundleSha256": self.bundle["sha256"], "contentVerified": False},
                         run.pin_study_bundle(self.bundle["sha256"]))
        with self.assertRaisesRegex(ValidationError, "pinned and differs"):
            run.pin_study_bundle("0" * 64)
        meta = self.rows("SELECT key, value FROM meta WHERE key LIKE 'studyBundle%'")
        self.assertEqual({"studyBundle", "studyBundleSha256"}, {row["key"] for row in meta})

    def test_bundle_files_are_verified(self):
        run = self.run_census()
        tampered = {**self.bundle, "protocolSha256": "0" * 64}
        foreign = instruments.study_bundle_digest(protocol={}, study_prompt="", instruments={"citation-census@1": "1" * 64},
                                                  dataset_versions={}, package_tags={})
        for value, message in ((tampered, "differs from its content"), (foreign, "the installed instrument is"),
                               ({"format": "other"}, "no ocsl-study-bundle-v1 record")):
            path = self.root / "bundle.json"
            path.write_text(json.dumps(value))
            with self.subTest(message=message), self.assertRaisesRegex(ValidationError, message):
                run.pin_study_bundle(path)
        with self.assertRaisesRegex(ValidationError, "takes a study bundle file"):
            run.pin_study_bundle(self.root / "missing.json")

    def test_the_bundle_is_pinned_before_the_first_call_and_recorded_in_every_receipt(self):
        judge = MockJudge()
        server = start_server(judge)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        transport = HttpChatTransport("http://127.0.0.1:%d/v1" % server.server_address[1])
        run = self.run_census(transport=transport)
        self.prepare(run, bundle=False)
        run.run_abstract()
        self.assertEqual([None], [r["study_bundle_sha256"] for r in self.rows("SELECT study_bundle_sha256 FROM calls")])
        self.assertIsNone(self.status()["receiptsWithoutStudyBundle"])
        with self.assertRaisesRegex(ValidationError, "before the first call"):
            run.pin_study_bundle(self.bundle["sha256"])
        run.close()
        self.database.unlink()
        run = self.run_census(transport=transport)
        self.prepare(run)
        run.run_abstract()
        self.assertEqual([(self.bundle["sha256"], "direct")],
                         [tuple(r.values()) for r in self.rows("SELECT study_bundle_sha256, transport FROM calls")])
        self.assertTrue(gates(self.status())["checks"]["everyReceiptCarriesTheStudyBundle"])

    def test_old_working_databases_gain_the_receipt_columns(self):
        with closing(sqlite3.connect(self.database)) as c:
            c.executescript(scores_db.DDL.replace(
                ",\n  transport TEXT, study_bundle_sha256 TEXT, request_sha256 TEXT, broker_principal_id TEXT, broker_job_id TEXT,\n"
                "  broker_item_id TEXT, broker_instrument_digest TEXT, broker_settings_sha256 TEXT, broker_request_sha256 TEXT,\n"
                "  broker_error_code TEXT", ""))
            self.assertNotIn("transport", scores_db.table_columns(c, "calls"))
            c.execute("INSERT INTO meta VALUES('schemaVersion', '1')")
            c.commit()
        with closing(scores_db.open_readonly(self.database)) as c:
            self.assertEqual(0, census_status(c)["brokerCalls"])
        with closing(scores_db.connect(self.database, create=True)) as c:
            self.assertLessEqual(set(scores_db.CALL_COLUMNS_ADDED), scores_db.table_columns(c, "calls"))
            documented = {r[0] for r in c.execute("SELECT column_name FROM data_dictionary WHERE table_name='calls'")}
            self.assertEqual(scores_db.table_columns(c, "calls"), documented)


class BrokerFullTextTests(BrokerTestCase):
    def test_full_text_tier_through_the_broker(self):
        digests = self.broker.install_census()
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        result = run.run_full_text(fetch_xml=lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes())
        self.assertEqual(("completed", 3, 1), (result["state"], result["retrieved"], result["calls"]))
        call = self.rows("SELECT * FROM calls WHERE tier='full-text'")[0]
        self.assertEqual((digests["full-text"], 3000), (call["broker_instrument_digest"], call["max_tokens"]))
        self.assertEqual(call["request_sha256"], call["broker_request_sha256"])
        sent = self.broker.upstream[-1]
        self.assertEqual(("low", 3000), (sent["reasoning_effort"], sent["max_tokens"]))
        self.assertIn(C.full_text_note(), sent["messages"][1]["content"])
        rows = self.rows("SELECT label, ft_class FROM citations WHERE ft_class IS NOT NULL")
        self.assertEqual(3, len(rows))

    def test_a_mismatch_in_the_full_text_tier_stops_it(self):
        self.broker.install_census()
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        self.broker.inject_policy = True
        result = run.run_full_text(fetch_xml=lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes())
        self.assertEqual("paused", result["state"])
        self.assertTrue(result["reason"].startswith("fidelity:"))
        self.assertEqual(0, len(self.rows("SELECT label FROM citations WHERE ft_class IS NOT NULL")))
        self.broker.inject_policy = False
        self.assertEqual("completed", run.run_full_text(
            fetch_xml=lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes())["state"])


class BrokerCommandLineTests(BrokerTestCase):
    def cli(self, *argv, env=None):
        previous = {key: os.environ.get(key) for key in (env or {})}
        os.environ.update(env or {})
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(list(argv))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, (json.loads(buffer.getvalue()) if buffer.getvalue().strip() else None)

    def test_study_commands_through_the_broker(self):
        self.broker.install_census()
        manifest = self.root / "input" / "reviews.jsonl"
        manifest.parent.mkdir()
        (manifest.parent / "review.xml").write_bytes((FIXTURES / "review.xml").read_bytes())
        manifest.write_text(json.dumps(record()) + "\n")
        bundle = self.root / "bundle.json"
        bundle.write_text(json.dumps(self.bundle))
        database = str(self.database)
        code, prepared = self.cli("study", "prepare", "--database", database, "--seed", "5", "--input", str(manifest),
                                  "--study-bundle", str(bundle))
        self.assertEqual((0, 1, self.bundle["sha256"]), (code, prepared["prepared"], prepared["studyBundle"]["studyBundleSha256"]))
        code, judged = self.cli("study", "judge", "--database", database, "--seed", "5", "--broker-socket", self.broker.path)
        self.assertEqual((0, "completed", "broker"), (code, judged["state"], judged["transport"]["kind"]))
        code, full_text = self.cli("study", "full-text", "--database", database, "--seed", "5",
                                   env={"OCSL_BROKER_SOCKET": self.broker.path})
        self.assertEqual((0, "completed"), (code, full_text["state"]))
        code, report = self.cli("study", "gates", "--database", database)
        self.assertEqual(0, code)
        self.assertTrue(report["ok"], report["checks"])
        self.assertEqual({"broker"}, {r["transport"] for r in self.rows("SELECT transport FROM calls")})

    def test_transport_options_are_exclusive_and_hold_no_credentials(self):
        from ocsl_review.study_cli import _transport
        import argparse
        base = {"base_url": None, "api_key_env": None, "on_premises": False, "policy": None, "broker_socket": None,
                "broker_worker": None}
        with self.assertRaisesRegex(ValidationError, "not both"):
            _transport(argparse.Namespace(**{**base, "base_url": "http://127.0.0.1:9/v1", "broker_socket": self.broker.path}))
        previous = os.environ.pop("OCSL_BROKER_SOCKET", None)
        try:
            with self.assertRaisesRegex(ValidationError, "OCSL_BROKER_SOCKET"):
                _transport(argparse.Namespace(**base))
        finally:
            if previous is not None:
                os.environ["OCSL_BROKER_SOCKET"] = previous
        with self.assertRaisesRegex(ValidationError, "drop --api-key-env"):
            _transport(argparse.Namespace(**{**base, "broker_socket": self.broker.path, "api_key_env": "JUDGE_KEY"}))
        with self.assertRaisesRegex(ValidationError, "absolute path"):
            _transport(argparse.Namespace(**{**base, "broker_socket": "relative.sock"}))
        with self.assertRaisesRegex(ValidationError, "not a Unix socket"):
            _transport(argparse.Namespace(**{**base, "broker_socket": str(self.database.parent)}))
        transport = _transport(argparse.Namespace(**{**base, "broker_socket": self.broker.path}))
        self.assertEqual(("run-principal-census", False), (transport.principal_id, transport.on_premises))

    def test_on_premises_needs_a_policy_that_lists_the_broker_provider(self):
        from ocsl_review.study_cli import _transport
        import argparse
        args = {"base_url": None, "api_key_env": None, "on_premises": True, "policy": None,
                "broker_socket": self.broker.path, "broker_worker": None}
        with self.assertRaisesRegex(ConfidentialityError, "needs --policy"):
            _transport(argparse.Namespace(**args))
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"version": 2, "onPremBrokerProviders": [{"provider": "another_provider"}]}))
        with self.assertRaisesRegex(ConfidentialityError, "not an on-prem broker provider"):
            _transport(argparse.Namespace(**{**args, "policy": str(policy)}))
        policy.write_text(json.dumps({"version": 2, "onPremBrokerProviders": [
            {"provider": "remote_qwen38_none", "models": ["another/model"]}]}))
        with self.assertRaises(ConfidentialityError):
            _transport(argparse.Namespace(**{**args, "policy": str(policy)}))
        policy.write_text(json.dumps({"version": 2, "onPremBrokerProviders": [
            {"provider": "remote_qwen38_none", "models": [MODEL]}]}))
        self.assertTrue(_transport(argparse.Namespace(**{**args, "policy": str(policy)})).on_premises)

    def test_policies_without_broker_providers_keep_their_digest(self):
        plain = ConfidentialPolicy.from_mapping({"version": 2, "onPremEndpoints": ["http://127.0.0.1:1/v1/"]})
        self.assertNotIn("onPremBrokerProviders", plain.record())
        broker = ConfidentialPolicy.from_mapping({"version": 2, "onPremEndpoints": ["http://127.0.0.1:1/v1/"],
                                                  "onPremBrokerProviders": ["remote_qwen38_none"]})
        self.assertEqual([{"provider": "remote_qwen38_none", "models": []}], broker.record()["onPremBrokerProviders"])
        self.assertNotEqual(plain.digest, broker.digest)
        self.assertEqual(broker.on_prem_broker_providers, broker.with_mode("public-literature").on_prem_broker_providers)
        self.assertTrue(broker.broker_provider_on_prem("remote_qwen38_none", "any/model"))
        for value in (["bad id!"], [{"provider": "a", "models": "m"}], ["a", "a"]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ConfidentialPolicy.from_mapping({"version": 2, "onPremBrokerProviders": value})


if __name__ == "__main__":
    unittest.main()
