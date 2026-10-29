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
from ocsl_review.census_broker import (BrokerTransport, canonical_json, canonical_sha256, generation_key,
                                       idempotency_key, request_sha256)
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
# Call receipts with their transport row (a call has one row in each).
CALLS = "calls LEFT JOIN call_transport USING(call_id)"


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
    """The daemon's canonical JSON for ASCII keys, BMP text and plain numbers: sorted keys, no whitespace, 0.0 as 0,
    lone surrogates escaped as JSON.stringify writes them."""
    text = json.dumps(_integral(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "".join("\\u%04x" % ord(ch) if "\ud800" <= ch <= "\udfff" else ch for ch in text)


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


def load_legacy_scores_db():
    """dev 1b355a9's scores_db module (tests/fixtures/legacy), loaded as a module of the ocsl_review package."""
    import importlib.util
    path = Path(__file__).resolve().parent / "fixtures" / "legacy" / "scores_db_1b355a9.py"
    spec = importlib.util.spec_from_file_location("ocsl_review._legacy_scores_db_1b355a9", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "ocsl_review"
    spec.loader.exec_module(module)
    return module


class MockBroker:
    """A run principal's socket with one bulk worker, executing one-item instrument jobs on background threads.

    As the daemon: a job's fingerprint covers the worker configuration and the request (a key reused under another
    configuration is WORKER_KEY_CONFLICT naming the job that holds it), a resume of a job whose worker configuration
    changed is WORKER_CONFIG_CHANGED, a resume of a job resumed before answers that resume and clears no provider
    pause, an exhausted budget pauses the job and leaves its item queued, and a verbatim item reports the served model
    and system_fingerprint the upstream named (null when it named none) and the HTTP status."""

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
        self.omit_served_model = self.omit_fingerprint = self.empty_answers = False
        self.refuse_code = "INSTRUMENT_MESSAGE_MISMATCH"
        self.fail_next: list[str] = []
        self.status_next: list[int] = []        # upstream statuses answered to the next calls (4xx: INVALID_REQUEST)
        self.refuse_next = self.rate_limit_next = 0
        self.worker_config = {"baseUrl": "http://127.0.0.1:9/v1", "label": "Synthetic judge"}
        self.hold_repairs = threading.Event()   # set: repair calls wait until it is cleared
        self.hold = None                        # callable(job) -> bool: the job waits while it answers True
        self.settle_early_jobs = 0              # the next jobs report failed once while their item still runs
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
                      "itemsPerCall": bundle["itemsPerCall"],
                      "transport": {"messages": (bundle.get("transport") or {}).get("messages", "policy-wrapped")}}
                     for digest, bundle in self.instruments.items() if digest in self.granted]
        value = {"version": 1, "revision": 3,
                 "authorization": {"principalId": self.principal, "kind": "run", "runId": self.run_id,
                                   "attemptId": self.principal, "state": "active", "publicLiteratureAllowed": True,
                                   "instruments": sorted(self.granted)},
                 "instrumentMode": 1, "instrumentVerbatim": 1, "instruments": installed,
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
        fingerprint = sha(json.dumps({"config": self.worker_config, "body": body}, sort_keys=True))
        existing = self.keys.get(body["idempotencyKey"])
        if existing is not None:
            job = self.jobs[existing]
            if job["fingerprint"] != fingerprint:
                return 409, {"error": "Idempotency key was used for different work.", "code": "WORKER_KEY_CONFLICT",
                             "jobId": job["jobId"]}
            return 200, self.summary(job)
        job = self.create(body["idempotencyKey"], fingerprint, digest, body["items"])
        return 200, self.summary(job)

    def create(self, key, fingerprint, digest, items, resumed_from=None) -> dict:
        job = {"jobId": str(uuid.uuid4()), "state": "queued", "idempotencyKey": key, "fingerprint": fingerprint,
               "configId": self.worker, "instrumentDigest": digest, "items": items, "error": None, "resumeAfter": None,
               "resumedFromJobId": resumed_from, "results": {}, "config": dict(self.worker_config)}
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
        if job["config"] != self.worker_config:
            return 409, {"error": "Worker configuration changed since this job was submitted.", "code": "WORKER_CONFIG_CHANGED"}
        key = sha(job["idempotencyKey"])[:48] + "-resume-" + job["jobId"]
        if key in self.keys:
            resumed = self.jobs[self.keys[key]]         # an earlier resume: answered as it is, no pause cleared
        else:
            self.provider_paused = None
            items = [item for item in job["items"] if job["results"][item["id"]]["state"] != "completed"]
            resumed = self.create(key, job["fingerprint"], job["instrumentDigest"], items, resumed_from=job["jobId"])
        full = self.summary(resumed)
        full["results"] = [dict(r) for r in resumed["results"].values()]
        return 200, full

    def restart(self, *, relaunch: bool = False) -> None:
        """A daemon restart: running and queued jobs are requeued and relaunched, or interrupted for a resume."""
        with self.lock:
            for job in self.jobs.values():
                if job["state"] in ("queued", "running"):
                    for result in job["results"].values():
                        if result["state"] == "running":
                            result["state"] = "queued"
                    self.set_state(job, state="queued" if relaunch else "interrupted",
                                   error=None if relaunch else "Runner restarted; resume the job.")
                    if relaunch:
                        threading.Thread(target=self.execute_job, args=(job["jobId"],), daemon=True).start()

    # ------------------------------------------------------------------ execution
    def execute_job(self, job_id) -> None:
        self.release.wait(20)
        if self.latency:
            time.sleep(self.latency)
        with self.lock:
            job = self.jobs[job_id]
            repair = any(self.is_repair(item) for item in job["items"])
        while (repair and self.hold_repairs.is_set()) or (self.hold is not None and self.hold(job)):
            time.sleep(0.01)
        with self.lock:
            if job["state"] != "queued":
                return
            self.set_state(job, state="running")
            early = list(job["items"]) if self.settle_early_jobs > 0 else []
            if early:
                # A second launch of the job (as across a restart) settles it failed while the first still runs the item.
                self.settle_early_jobs -= 1
                for item in early:
                    job["results"][item["id"]]["state"] = "running"
                self.set_state(job, state="failed", error="settled by a second launch")
        if early:
            time.sleep(0.3)
            with self.lock:
                for item in early:
                    self.execute_item(job, item)
                job["revision"] = self.bump()
            return
        with self.lock:
            for item in job["items"]:
                if job["state"] != "running":
                    break
                self.execute_item(job, item)
            if job["state"] == "running":
                done = all(r["state"] == "completed" for r in job["results"].values())
                self.set_state(job, state="completed" if done else "failed")

    @staticmethod
    def is_repair(item) -> bool:
        data = item.get("input") if isinstance(item, dict) else None
        messages = data.get("messages") if isinstance(data, dict) else None
        return bool(messages) and C.repair_note() in str(messages[-1].get("content"))

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
            # As the daemon: the principal refuses admission, the item stays queued and the job pauses.
            self.set_state(job, state="paused", error="budget_exhausted")
            return
        data = item["input"]
        messages = data.get("messages") if isinstance(data, dict) else None
        if self.refuse_items:
            # A refusal before any provider call, with the code the daemon would record (no attempt, no receipt).
            self.settle(job, result, state="failed", errorCode=self.refuse_code,
                        error=f"Refused before any provider call ({self.refuse_code}).")
            return
        count = data.get("itemCount", bundle["itemsPerCall"]) if isinstance(data, dict) else 0
        max_tokens = max(bundle["maxOutputTokens"]["minimum"], bundle["maxOutputTokens"]["perItem"] * count)
        # Settings an item may repeat must equal what is sent (the worker's model and the bundle), as in the daemon.
        repeated = {"model": self.model, "max_tokens": max_tokens, "temperature": bundle["temperature"],
                    "seed": bundle["seed"], "reasoning_effort": bundle["reasoning"], "stream": False}
        if (not isinstance(messages, list) or not messages or set(data) - {"messages", "itemCount", *repeated}
                or messages[0].get("role") != "system" or messages[0].get("content") != bundle["systemPrompt"]
                or not 1 <= count <= bundle["itemsPerCall"]
                or any(name in data and data[name] != value for name, value in repeated.items())):
            # The daemon refuses every verbatim item that is not the installed instrument's request this way.
            self.settle(job, result, state="failed", errorCode="INSTRUMENT_MESSAGE_MISMATCH",
                        error="Verbatim instrument call refused: the messages or settings differ from the installed "
                              "instrument.")
            return
        system = (POLICY_PREFIX + "\n\n" if self.inject_policy else "") + messages[0]["content"]
        sent = [{"role": "system", "content": system}, *messages[1:]]
        body = {"model": self.model, "messages": sent, "max_tokens": max_tokens,
                "temperature": bundle["temperature"], "stream": False, "reasoning_effort": bundle["reasoning"]}
        if bundle["seed"] is not None:
            body["seed"] = bundle["seed"]
        self.dispatched += 1
        self.upstream.append(json.loads(json.dumps(body)))
        settings = {"model": body["model"], "temperature": body["temperature"], "seed": body.get("seed"),
                    "reasoning": body["reasoning_effort"], "max_tokens": body["max_tokens"], "response_format_sha256": None,
                    "system_prompt_sha256": sha(system)}
        served = None if self.omit_served_model else self.served
        measured = {"model": served or self.model, "servedModel": served,
                    "systemFingerprint": None if self.omit_fingerprint else "fp-broker",
                    "serverFingerprint": (f"{served or self.model}@0123456789abcdef" if self.omit_fingerprint else "fp-broker"),
                    "httpStatus": 200, "settingsSha256": sha(daemon_canonical(settings)),
                    "attempts": 1, "finishReason": "stop", "promptSha256": hashlib.sha256(json.dumps(body).encode("ascii")).hexdigest(),
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
            self.settle(job, result, **{**measured, "attempts": 3, "httpStatus": 502}, state="failed",
                        errorCode=self.fail_next.pop(0), error="Provider request failed with HTTP 502.")
            return
        if self.status_next:
            status = self.status_next.pop(0)
            self.settle(job, result, **{**measured, "httpStatus": status}, state="failed", errorCode="INVALID_REQUEST",
                        error=f"Provider request failed with HTTP {status}.")
            return
        if self.refuse_next:
            self.refuse_next -= 1
            self.settle(job, result, **{**measured, "httpStatus": 400}, state="failed", errorCode="INVALID_REQUEST",
                        error="Provider request failed with HTTP 400.")
            return
        required = bundle["requiredModelFingerprint"]
        if required and served != required and measured["systemFingerprint"] != required:
            self.provider_paused = f"Model drift: the instrument requires {required}; the provider served {served}."
            self.settle(job, result, **measured, state="paused", errorCode="MODEL_DRIFT", error=self.provider_paused)
            self.set_state(job, state="paused", error=self.provider_paused)
            return
        output = "" if self.empty_answers else self.judge.answer({"messages": sent})
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
        call = self.rows(f"SELECT * FROM {CALLS}")[0]
        self.assertEqual(("broker", digests["abstract"], call["call_id"], self.bundle["sha256"], "fp-broker", MODEL, 200),
                         (call["transport"], call["broker_instrument_digest"], call["broker_item_id"],
                          call["study_bundle_sha256"], call["fingerprint"], call["served_model"], call["broker_http_status"]))
        self.assertEqual("fp-broker", call["broker_server_fingerprint"])
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
        # The item is the request the instrument built (messages and settings) and its entry count.
        sent_item = job["items"][0]["input"]
        self.assertEqual({"messages", "itemCount", "model", "max_tokens", "temperature", "stream", "reasoning_effort"},
                         set(sent_item))
        self.assertEqual((15, MODEL, 10500, 0.0, False, "none"),
                         (sent_item["itemCount"], sent_item["model"], sent_item["max_tokens"], sent_item["temperature"],
                          sent_item["stream"], sent_item["reasoning_effort"]))
        self.assertEqual(call["request_sha256"], request_sha256({k: v for k, v in sent_item.items() if k != "itemCount"}))
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
            direct_calls = c.execute(f"SELECT kind, request_sha256, body_sha256, transport FROM {CALLS} ORDER BY kind").fetchall()
        broker_calls = [tuple(r.values()) for r in self.rows(f"SELECT kind, request_sha256, body_sha256, transport FROM {CALLS} "
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
        calls = self.rows(f"SELECT error, request_sha256, broker_request_sha256, n_parsed FROM {CALLS}")
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

    def test_settings_that_differ_from_the_installed_bundle_are_refused_before_any_call(self):
        # A bundle whose output-token rule differs from the instrument's: the item repeats the max_tokens the
        # instrument built, so the broker refuses it instead of sending its own value upstream.
        digests = self.broker.install_census()
        self.broker.instruments[digests["abstract"]]["maxOutputTokens"] = {"perItem": 701, "minimum": 3000}
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("paused", 0), (result["state"], result["accepted"]))
        self.assertIn("INSTRUMENT_MESSAGE_MISMATCH", result["reason"])
        self.assertEqual([], self.broker.upstream, "no request other than the instrument's reached the judge")
        self.assertEqual(["broker refused the instrument call: INSTRUMENT_MESSAGE_MISMATCH"],
                         [c["error"].split(" (")[0] for c in self.rows("SELECT error FROM calls")])

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
        self.assertEqual(("broker failed: PROVIDER_UNREACHABLE (HTTP 502)", 3, None),
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
        self.assertEqual(["refused: broker INVALID_REQUEST (HTTP 400)"] * 2, [c["error"] for c in self.rows("SELECT error FROM calls")])


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
        call = self.rows(f"SELECT * FROM {CALLS}")[0]
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
        call = self.rows(f"SELECT error, served_model, broker_error_code FROM {CALLS}")[0]
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

    def test_a_broker_without_verbatim_mode_or_a_policy_wrapped_install_is_refused_before_any_job(self):
        self.broker.install_census(tiers=("full-text",))
        exported = broker_bundle("citation-census@1", tier="abstract", parameters={"journal": JOURNAL})
        # The daemon stores a policy-wrapped bundle without transport and lists it as policy-wrapped.
        self.broker.install(exported["digest"], {k: v for k, v in exported["bundle"].items() if k != "transport"})
        self.broker.granted.add(exported["digest"])
        run = self.run_census()
        self.prepare(run)
        with self.assertRaisesRegex(ValidationError, rf"without transport.messages verbatim: abstract tier, .*{exported['digest']}"):
            run.run_abstract()
        self.broker.catalog_patch = {"instrumentVerbatim": None}
        with self.assertRaisesRegex(ValidationError, r"verbatim instrument mode \(instrumentVerbatim\)"):
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
        self.assertEqual([None], [r["study_bundle_sha256"] for r in self.rows(f"SELECT study_bundle_sha256 FROM {CALLS}")])
        self.assertIsNone(self.status()["receiptsWithoutStudyBundle"])
        with self.assertRaisesRegex(ValidationError, "before the first call"):
            run.pin_study_bundle(self.bundle["sha256"])
        run.close()
        self.database.unlink()
        run = self.run_census(transport=transport)
        self.prepare(run)
        run.run_abstract()
        self.assertEqual([(self.bundle["sha256"], "direct")],
                         [tuple(r.values()) for r in self.rows(f"SELECT study_bundle_sha256, transport FROM {CALLS}")])
        self.assertTrue(gates(self.status())["checks"]["everyReceiptCarriesTheStudyBundle"])

    def test_a_database_this_version_wrote_opens_and_takes_receipts_with_the_previous_version(self):
        # The broker deploy can roll back: calls keeps exactly the columns every earlier version writes by position,
        # and the transport, study-bundle and broker fields live in tables the earlier version ignores.
        legacy = load_legacy_scores_db()
        self.broker.install_census()
        run = self.run_census()
        self.prepare(run)
        self.assertEqual("completed", run.run_abstract()["state"])
        run.close()
        with closing(sqlite3.connect(self.database)) as c:
            self.assertEqual(list(scores_db.CALL_COLUMNS), [r[1] for r in c.execute("PRAGMA table_info(calls)")])
        with closing(legacy.connect(self.database, create=True)) as c:
            self.assertEqual(legacy.SCHEMA_VERSION, scores_db.SCHEMA_VERSION)
            documented = {r[0] for r in c.execute("SELECT column_name FROM data_dictionary WHERE table_name='calls'")}
            self.assertEqual(set(scores_db.CALL_COLUMNS), documented)
            # The earlier version's receipt insert, by position.
            row = list(c.execute("SELECT * FROM calls LIMIT 1").fetchone())
            row[0] = "call-recorded-by-the-earlier-version"
            c.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            c.commit()
            self.assertEqual(0, legacy.completeness(c)["abstractPending"])
        # And forward again: the earlier version's call counts as a direct call without the study bundle.
        status = self.status()
        self.assertEqual(({"broker": 1, "direct": 1}, 1), (status["callsByTransport"], status["receiptsWithoutStudyBundle"]))
        self.assertFalse(gates(status)["checks"]["everyReceiptCarriesTheStudyBundle"])

    def test_old_working_databases_gain_the_transport_tables(self):
        legacy = load_legacy_scores_db()
        with closing(legacy.connect(self.database, create=True)) as c:
            self.assertFalse(scores_db.has_table(c, "call_transport"))
        with closing(scores_db.open_readonly(self.database)) as c:
            self.assertEqual((0, {}), (census_status(c)["brokerCalls"], census_status(c)["callsByTransport"]))
        with closing(scores_db.connect(self.database, create=True)) as c:
            self.assertEqual(list(scores_db.CALL_COLUMNS), [r[1] for r in c.execute("PRAGMA table_info(calls)")])
            for table in ("call_transport", "pending_calls"):
                documented = {r[0] for r in c.execute("SELECT column_name FROM data_dictionary WHERE table_name=?", (table,))}
                self.assertEqual(scores_db.table_columns(c, table), documented, table)


class BrokerFullTextTests(BrokerTestCase):
    def test_full_text_tier_through_the_broker(self):
        digests = self.broker.install_census()
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        result = run.run_full_text(fetch_xml=lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes())
        self.assertEqual(("completed", 3, 1), (result["state"], result["retrieved"], result["calls"]))
        call = self.rows(f"SELECT * FROM {CALLS} WHERE tier='full-text'")[0]
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
        self.assertEqual({"broker"}, {r["transport"] for r in self.rows(f"SELECT transport FROM {CALLS}")})

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


class Crash(Exception):
    """Stands for the census process dying in the middle of a call."""


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


class BrokerAnswerTests(BrokerTestCase):
    """What a broker item counts as: the judge's own response, classified as for a direct call."""

    def test_a_fingerprint_the_server_did_not_offer_is_not_the_judge_fingerprint(self):
        self.broker.install_census()
        self.broker.omit_fingerprint = True
        run = self.run_census()
        self.prepare(run)
        self.assertEqual("completed", run.run_abstract()["state"])
        call = self.rows(f"SELECT fingerprint, broker_server_fingerprint FROM {CALLS}")[0]
        self.assertIsNone(call["fingerprint"])
        self.assertRegex(call["broker_server_fingerprint"], r"@[0-9a-f]{16}$", "the daemon's identity is kept apart")
        status = self.status()
        self.assertEqual((1, 0, False), (status["receiptsWithResponse"], status["receiptsWithFingerprint"],
                                         status["fingerprintOffered"]))
        report = gates(status)
        self.assertFalse(report["checks"]["everyReceiptHasFingerprint"])
        self.assertTrue(any("offered no system_fingerprint" in note for note in report["notes"]))

    def test_a_response_naming_no_model_is_drift_whether_the_broker_or_the_executor_catches_it(self):
        self.broker.install_census()
        self.broker.omit_served_model = True
        for required, broker_checks in ((MODEL, True), (None, False)):
            with self.subTest(broker_checks=broker_checks):
                # Without requiredModelFingerprint the broker completes the item; the executor's own check pauses.
                for digest, bundle in self.broker.instruments.items():
                    bundle["requiredModelFingerprint"] = required
                database = self.root / f"no-model-{broker_checks}.sqlite3"
                run = CensusRun(database, seed=1234 + broker_checks, transport=self.transport(), concurrency=4,
                                sleep=lambda s: None)
                self.addCleanup(run.close)
                self.prepare(run)
                paused = run.run_abstract()
                self.assertEqual("paused", paused["state"])
                self.assertIn("model_drift: served '(no model named in the response)'", paused["reason"])
                with closing(sqlite3.connect(database)) as c:
                    call = c.execute("SELECT error, served_model, n_parsed FROM calls").fetchone()
                    undecided = c.execute("SELECT COUNT(*) FROM citations WHERE cited_inbody=1 AND abs_basis IS NULL "
                                          "AND identity IN ('resolved','resolved_unverified')").fetchone()[0]
                    state = c.execute("SELECT value FROM meta WHERE key='runState'").fetchone()[0]
                self.assertEqual(("model_drift", None, 0), tuple(call))
                self.assertEqual((15, "paused:model_drift"), (undecided, state), "no entry became an invalid output")
                self.broker.provider_paused = None

    def test_a_direct_response_naming_no_model_pauses_as_drift_instead_of_invalidating_entries(self):
        judge = MockJudge()
        judge.model = None
        server = start_server(judge)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        run = self.run_census(transport=HttpChatTransport("http://127.0.0.1:%d/v1" % server.server_address[1]))
        self.prepare(run)
        paused = run.run_abstract()
        self.assertEqual("paused", paused["state"])
        self.assertIn("no model named in the response", paused["reason"])
        self.assertEqual(0, self.rows("SELECT COUNT(*) AS n FROM citations WHERE abs_basis LIKE 'invalid%'")[0]["n"])

    def test_a_transient_upstream_status_leaves_entries_open_while_400_413_422_are_answers(self):
        self.broker.install_census()
        self.broker.status_next = [408, 425]
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract(transport_retries=1, cooldown=0)
        self.assertEqual(("completed", 15, 0), (result["state"], result["accepted"], result["invalid"]))
        errors = [r["error"] for r in self.rows("SELECT error FROM calls ORDER BY created_at, rowid")]
        self.assertEqual(["broker failed: INVALID_REQUEST (HTTP 408)", "broker failed: INVALID_REQUEST (HTTP 425)", None],
                         errors)
        self.assertEqual((2, 2), (result["transportFailures"], self.status()["transportFailures"]))
        for status in (400, 413, 422):
            with self.subTest(status=status):
                database = self.root / f"refused-{status}.sqlite3"
                self.broker.status_next = [status, status]
                other = CensusRun(database, seed=status, transport=self.transport(), concurrency=4, sleep=lambda s: None)
                self.addCleanup(other.close)
                self.prepare(other)
                refused = other.run_abstract(transport_retries=0)
                self.assertEqual(("completed", 0, 15), (refused["state"], refused["accepted"], refused["invalid"]))

    def test_an_empty_completion_is_an_answer_without_labels(self):
        self.broker.install_census()
        self.broker.empty_answers = True
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("completed", 0, 15), (result["state"], result["accepted"], result["invalid"]))
        self.assertEqual(["empty content; finish=stop"] * 2, [r["error"] for r in self.rows("SELECT error FROM calls")])

    def test_a_call_without_a_usable_answer_stays_open(self):
        self.broker.install_census()
        self.broker.fail_next = ["NO_FINAL_MESSAGE", "NO_FINAL_MESSAGE"]
        run = self.run_census()
        self.prepare(run)
        paused = run.run_abstract(transport_retries=0)
        self.assertEqual(("paused", 0, 0), (paused["state"], paused["accepted"], paused["invalid"]))
        self.assertEqual(15, self.status()["openEntries"])
        done = run.run_abstract()
        self.assertEqual(("completed", 15), (done["state"], done["accepted"]))

    def test_a_lone_surrogate_travels_as_an_escape_and_keeps_the_request_digest(self):
        self.broker.install_census()
        digest = broker_bundle("citation-census@1", tier="abstract", parameters={"journal": JOURNAL})["digest"]
        transport = self.transport()
        transport.open()
        item = {"label": "1", "citing_passages": [{"sentence": "A claim [1].", "section": "Intro"}],
                "cited_work": {"title": "T", "venue": "V", "year": 2020}, "abstract": "Broken \ud800 surrogate."}
        payload = C.request_payload(C.build_abstract_messages([item], "Review", JOURNAL), model=MODEL, items=1)
        outcome = transport.call(item_id="call-surrogate", messages=payload["messages"], item_count=1, digest=digest,
                                 key=idempotency_key("call-surrogate", request_sha256(payload)),
                                 settings={k: v for k, v in payload.items() if k != "messages"})
        self.assertEqual("settled", outcome.kind)
        self.assertEqual(request_sha256(payload), outcome.item["requestSha256"])
        self.assertIn("\ud800", self.broker.upstream[0]["messages"][1]["content"])


class BrokerStudyBundleStatusTests(BrokerTestCase):
    def test_a_bare_sha256_pin_is_reported_as_unverified(self):
        self.broker.install_census()
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        status = self.status()
        self.assertEqual((self.bundle["sha256"], False), (status["studyBundleSha256"], status["studyBundleVerified"]))
        self.assertTrue(any("pinned by its sha256 alone" in note for note in gates(status)["notes"]))
        path = self.root / "bundle.json"
        path.write_text(json.dumps(self.bundle))
        run.pin_study_bundle(path)
        status = self.status()
        self.assertTrue(status["studyBundleVerified"])
        self.assertFalse(any("pinned by its sha256 alone" in note for note in gates(status)["notes"]))

    def test_broker_bundles_refuse_request_fields_the_executor_does_not_send(self):
        import dataclasses
        from unittest import mock
        instrument = instruments.load("citation-census@1")
        for change, message in ((lambda request: request.pop("reasoningEffort"), "declares no reasoningEffort"),
                                (lambda request: request.update(seed=7), "declares a seed")):
            manifest = json.loads(json.dumps(instrument.manifest))
            change(manifest["tiers"]["abstract"]["request"])
            changed = dataclasses.replace(instrument, manifest=manifest)
            with self.subTest(message=message), mock.patch("ocsl_review.instruments.load", return_value=changed), \
                    self.assertRaisesRegex(ValidationError, message):
                broker_bundle("citation-census@1", tier="abstract", parameters={"journal": JOURNAL})


class BrokerLineageTests(BrokerTestCase):
    def test_a_lineage_longer_than_the_resume_bound_still_resumes_once_the_provider_recovers(self):
        self.broker.install_census()
        self.broker.rate_limit_next = 10 ** 6       # a sustained 429 without a reset header
        run = self.run_census()
        self.prepare(run)
        for _ in range(10):
            self.assertEqual("paused", run.run_abstract()["state"])
        self.assertGreater(len(self.broker.jobs), 16, "every invocation added to the call's lineage")
        self.broker.rate_limit_next = 0
        before = dict(run.transport.counters)
        done = run.run_abstract()
        self.assertEqual(("completed", 15), (done["state"], done["accepted"]))
        self.assertGreater(done["transport"]["walked"] - before["walked"], 16, "the lineage was walked to its tail")
        self.assertEqual(1, done["transport"]["resumed"] - before["resumed"], "and resumed once, there")
        self.assertEqual(0, self.status()["duplicateJudgments"])

    def test_walking_a_lineage_clears_no_provider_pause_before_the_tail_may_resume(self):
        self.broker.install_census()
        later = datetime.fromtimestamp(self.now[0], timezone.utc) + timedelta(minutes=5)
        self.broker.rate_limit_next = 10 ** 6
        run = self.run_census()
        self.prepare(run)
        self.assertEqual("paused", run.run_abstract()["state"])
        jobs = len(self.broker.jobs)
        # The tail now carries a future resumeAfter: a re-run walks to it and stops there, sending nothing.
        self.broker.resume_after = later.isoformat().replace("+00:00", "Z")
        tail = max(self.broker.jobs.values(), key=lambda job: job["revision"])
        tail["resumeAfter"] = self.broker.resume_after
        upstream = len(self.broker.upstream)
        paused = run.run_abstract()
        self.assertEqual("paused", paused["state"])
        self.assertEqual(self.broker.resume_after, paused["resumeAfter"])
        self.assertEqual((jobs, upstream), (len(self.broker.jobs), len(self.broker.upstream)))
        self.assertIsNotNone(self.broker.provider_paused, "the walk cleared no provider pause")


class BrokerWorkerChangeTests(BrokerTestCase):
    def test_a_changed_worker_configuration_moves_a_paused_call_to_its_next_key(self):
        self.broker.install_census()
        later = datetime.fromtimestamp(self.now[0], timezone.utc) + timedelta(minutes=5)
        self.broker.rate_limit_next = 1
        self.broker.resume_after = later.isoformat().replace("+00:00", "Z")
        run = self.run_census()
        self.prepare(run)
        self.assertEqual("paused", run.run_abstract()["state"])
        # The operator fixes the provider in the daemon's configuration while the call is paused.
        self.broker.worker_config = {**self.broker.worker_config, "baseUrl": "http://127.0.0.1:10/v1"}
        self.now[0] = later.timestamp() + 1
        done = run.run_abstract()
        self.assertEqual(("completed", 15), (done["state"], done["accepted"]))
        self.assertEqual(1, done["transport"]["keyGenerations"])
        call = self.rows(f"SELECT call_id, request_sha256, broker_job_id FROM {CALLS}")[0]
        key = idempotency_key(call["call_id"], call["request_sha256"])
        settled = self.broker.jobs[call["broker_job_id"]]
        root = self.broker.jobs[settled["resumedFromJobId"]] if settled["resumedFromJobId"] else settled
        self.assertEqual(generation_key(key, 1), root["idempotencyKey"], "the call's second key started its new lineage")
        self.assertEqual(2, len(self.broker.upstream), "the paused call and its successor; nothing judged twice")
        jobs = len(self.broker.jobs)
        # A later invocation finds nothing open, and the jobs stay as they are.
        self.assertEqual("completed", run.run_abstract()["state"])
        self.assertEqual(jobs, len(self.broker.jobs))

    def test_a_key_conflict_takes_the_answer_of_the_job_that_settled_the_call(self):
        self.broker.install_census()
        self.broker.release.clear()
        run = self.run_census(transport=self.transport(wait_seconds=0.3))
        self.prepare(run)
        self.assertEqual("paused", run.run_abstract(transport_retries=0)["state"])
        self.broker.release.set()
        wait_until(lambda: any(job["state"] == "completed" for job in self.broker.jobs.values()))
        self.broker.worker_config = {**self.broker.worker_config, "label": "Renamed judge"}
        done = run.run_abstract()
        self.assertEqual(("completed", 15), (done["state"], done["accepted"]))
        self.assertEqual((1, 1, 0), (len(self.broker.upstream), len(self.broker.jobs), done["transport"]["keyGenerations"]))


class BrokerLostAnswerTests(BrokerTestCase):
    def test_a_lost_full_text_answer_is_collected_from_its_job_and_never_judged_again(self):
        self.broker.install_census()
        run = self.run_census()
        self.prepare(run, record(), record(doi="10.5555/second"), record(doi="10.5555/third"))
        run.run_abstract()
        before = set(self.broker.jobs)
        held: list[str] = []

        def hold(job):
            if job["jobId"] in before or self.broker.hold is None:
                return False
            if not held:
                held.append(job["jobId"])
            return job["jobId"] == held[0]
        self.broker.hold = hold
        fetch = lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes()
        run.transport.wait_seconds = 0.5
        paused = run.run_full_text(fetch_xml=fetch, transport_retries=0)
        self.assertEqual("paused", paused["state"])
        pending = self.rows("SELECT call_id, labels_json FROM pending_calls")
        self.assertEqual(1, len(pending), "the call that got no answer is kept with its exact request")
        self.broker.hold = None
        wait_until(lambda: self.broker.jobs[held[0]]["state"] == "completed")
        jobs, upstream = len(self.broker.jobs), len(self.broker.upstream)
        run.transport.wait_seconds = 15.0
        done = run.run_full_text(fetch_xml=fetch)
        self.assertEqual(("completed", 1), (done["state"], done["replayed"]))
        self.assertEqual((jobs, upstream), (len(self.broker.jobs), len(self.broker.upstream)),
                         "the pending call met its job: no second job, no second judgment")
        self.assertEqual([], self.rows("SELECT * FROM pending_calls"))
        judged = self.rows("SELECT doi, label, COUNT(*) AS n FROM history WHERE tier='full-text' GROUP BY doi, label")
        self.assertTrue(judged and all(row["n"] == 1 for row in judged))
        call = self.rows(f"SELECT broker_job_id FROM {CALLS} WHERE call_id=?", pending[0]["call_id"])[0]
        self.assertEqual(held[0], call["broker_job_id"])

    def test_a_crash_while_a_repair_runs_resumes_with_that_repair_and_keeps_the_first_answers(self):
        self.broker.install_census()
        self.broker.judge.drop_first = {"2", "9"}
        self.broker.hold_repairs.set()
        transport = self.transport()
        original = transport._await

        def crash_on_repair(job_id, deadline):
            if MockBroker.is_repair(self.broker.jobs[job_id]["items"][0]):
                raise Crash()
            return original(job_id, deadline)
        transport._await = crash_on_repair
        run = self.run_census(transport=transport)
        self.prepare(run)
        with self.assertRaises(Crash):
            run.run_abstract()
        run.close()
        # The first call's answers were written with its receipt; the repair job runs on in the broker.
        self.assertEqual(13, self.rows("SELECT COUNT(*) AS n FROM citations WHERE abs_basis='model'")[0]["n"])
        self.broker.hold_repairs.clear()
        wait_until(lambda: len(self.broker.upstream) == 2)
        again = self.run_census()
        done = again.run_abstract()
        self.assertEqual(("completed", 2, 0), (done["state"], done["accepted"], done["invalid"]))
        self.assertEqual((2, 2), (len(self.broker.jobs), len(self.broker.upstream)),
                         "the next invocation sent exactly the repair that was due and met its job")
        self.assertEqual(0, self.status()["duplicateJudgments"])

    def test_a_job_settled_while_its_item_still_runs_is_not_an_answer(self):
        self.broker.install_census()
        self.broker.settle_early_jobs = 1
        run = self.run_census()
        self.prepare(run)
        paused = run.run_abstract(transport_retries=0)
        self.assertEqual(("paused", 0), (paused["state"], paused["accepted"]))
        self.assertEqual([], self.rows("SELECT * FROM calls"), "a failed job whose item still runs is no answer")
        wait_until(lambda: all(result["state"] == "completed" for job in self.broker.jobs.values()
                               for result in job["results"].values()))
        done = run.run_abstract()
        self.assertEqual(("completed", 15), (done["state"], done["accepted"]))
        self.assertEqual((1, 1), (len(self.broker.jobs), len(self.broker.upstream)))
        self.assertEqual([None], [r["error"] for r in self.rows("SELECT error FROM calls")])


class BrokerStopTests(BrokerTestCase):
    def test_a_fidelity_stop_sends_none_of_the_batches_already_queued(self):
        self.broker.install_census()
        self.broker.inject_policy = True
        run = self.run_census()                     # concurrency 4: eight batches queued at a time
        self.prepare(run, *[record(doi=f"10.5555/review.{index}") for index in range(12)])
        result = run.run_abstract()
        self.assertEqual("paused", result["state"])
        self.assertLessEqual(len(self.broker.upstream), 4, "only the calls already in flight reached the judge")
        self.assertEqual("FIDELITY", result["transport"]["halted"]["code"])
        status = self.status()
        self.assertEqual(len(self.rows("SELECT call_id FROM calls")), status["brokerRequestMismatches"])
        self.assertEqual((0, 0), (status["transportFailures"], result["transportFailures"]),
                         "a fidelity stop is reported apart from transport failures")

    def test_an_outage_names_a_possible_revocation_and_a_second_principal_is_noted(self):
        self.broker.install_census()
        run = self.run_census(breaker=2)
        self.prepare(run, record(), record(doi="10.5555/second"))
        run.transport.open()
        self.broker.stop()                          # the principal's socket is gone
        paused = run.run_abstract(transport_retries=0)
        self.assertEqual(("paused", 0), (paused["state"], paused["transportFailures"]))
        self.assertIn("may have been revoked", paused["transport"]["lastUnavailable"])
        run.close()
        with self.assertRaisesRegex(ValidationError, "may have been revoked"):
            self.transport()
        # A new principal takes over the run.
        self.broker.principal = "run-principal-census-2"
        self.broker.start()
        again = self.run_census()
        self.assertEqual("completed", again.run_abstract()["state"])
        again.close()
        self.broker.principal = "run-principal-census-3"
        third = self.run_census()
        third.prepare_review(record(doi="10.5555/third"), (FIXTURES / "review.xml").read_bytes())
        self.assertEqual("completed", third.run_abstract()["state"])
        status = self.status()
        self.assertEqual(2, status["brokerPrincipals"])
        self.assertTrue(any("2 run principals" in note for note in gates(status)["notes"]))


class BrokerCursorTests(unittest.TestCase):
    """The feed cursor and a job that settles between its own summary reading and the cursor's start."""

    class Feed:
        def __init__(self):
            self.lock = threading.Lock()
            self.jobs = {"A": {"jobId": "A", "state": "running", "revision": 5},
                         "B": {"jobId": "B", "state": "running", "revision": 10}}
            self.gate = threading.Event()
            self.feed_reads = 0
            self.summary_reads = {"A": 0, "B": 0}

        def request(self, method, route, payload=None):
            if route.startswith("/jobs?"):
                after = int(route.split("after=")[1].split("&")[0])
                with self.lock:
                    self.feed_reads += 1
                    changed = sorted((dict(j) for j in self.jobs.values() if j["revision"] > after),
                                     key=lambda j: j["revision"])
                return {"jobs": changed, "nextCursor": changed[-1]["revision"] if changed else after, "hasMore": False}
            job_id = route.split("/")[2].split("?")[0]
            with self.lock:
                self.summary_reads[job_id] += 1
                first = self.summary_reads[job_id] == 1
                value = dict(self.jobs[job_id])
            if job_id == "A" and first:
                value = {"jobId": "A", "state": "running", "revision": 5}
                self.gate.wait(5)           # A's first reading is delayed until B has started the cursor
            return value

    def test_a_job_awaited_before_the_cursor_started_moves_it_back_and_settles(self):
        feed = self.Feed()
        transport = BrokerTransport("/unused", client=feed, poll_interval=0.01, recheck_seconds=3600)
        self.addCleanup(transport.close)
        results = {}
        first = threading.Thread(target=lambda: results.setdefault("A", transport._await("A", time.monotonic() + 3)))
        first.start()
        wait_until(lambda: feed.summary_reads["A"] == 1)
        with feed.lock:
            feed.jobs["A"] = {"jobId": "A", "state": "completed", "revision": 7}   # A settles before B's reading
        second = threading.Thread(target=lambda: results.setdefault("B", transport._await("B", time.monotonic() + 3)))
        second.start()
        wait_until(lambda: (transport._cursor or 0) >= 9)                         # B started the cursor past A's 7
        wait_until(lambda: feed.feed_reads >= 2)
        feed.gate.set()
        first.join(5)
        self.assertEqual("completed", results["A"]["state"], results)
        with feed.lock:
            feed.jobs["B"] = {"jobId": "B", "state": "completed", "revision": 12}
        second.join(5)
        self.assertEqual("completed", results["B"]["state"])

    def test_a_waiting_call_reads_its_job_again_when_the_feed_never_reports_it(self):
        feed = self.Feed()
        feed.gate.set()
        transport = BrokerTransport("/unused", client=feed, poll_interval=0.01, recheck_seconds=0.2)
        self.addCleanup(transport.close)
        with feed.lock:
            feed.jobs["B"]["revision"] = 3                                         # the feed will never list A again
        started = time.monotonic()
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("A", transport._await("A", time.monotonic() + 5)))
        thread.start()
        wait_until(lambda: feed.summary_reads["A"] == 1)
        with feed.lock:
            transport._cursor = 100                                                # as if the feed had passed A
            feed.jobs["A"] = {"jobId": "A", "state": "completed", "revision": 6}
        thread.join(5)
        self.assertEqual("completed", result["A"]["state"])
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
