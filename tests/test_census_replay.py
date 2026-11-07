# SPDX-License-Identifier: Apache-2.0
"""study replay: census verdicts re-derived from stored broker responses, checked against the receipts, no model calls.

A synthetic census database with broker calls (and one direct call) is replayed from a responses file and from a fake
participant API on a Unix socket; calls the judge refused and entries recorded as invalid replay from the answered calls
that decided them; a census the runner itself recorded through the mock broker replays completely, repair calls,
refusals and invalid outputs included. The output digest is checked against vectors the daemon's own outputDigest
produced.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socketserver
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import uuid
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from ocsl_review import citation_census as C
from ocsl_review import corpus_facts, scores_db
from ocsl_review.census_broker import BrokerRefused, BrokerUnavailable
from ocsl_review.census_replay import (ABSENT, NO_PARSEABLE_OUTPUT, Fetched, ReplayInputError, ResponseItems,
                                       SocketItems, js_json, output_digest, output_text, replay, report_sha256)
from ocsl_review.census_runner import call_id_for, refusal_error
from ocsl_review.cli import main
from ocsl_review.errors import TransientError

from test_census_broker import BrokerTestCase, MockBroker
from test_census_runner import FIXTURES
from test_estimands import build_scores

VECTORS = Path(__file__).resolve().parent / "fixtures" / "broker" / "output-digest-vectors.json"
# The same file is asserted by the daemon's own test (puppenclaw test/fixtures/output-digest-vectors.json): both copies
# must stay byte-identical.
VECTORS_SHA256 = "8650791998aa91a44db66b019c7408a1daf648e7d5549a768e296b3efe93baf1"
MODEL = C.instrument().manifest["requiredModel"]["served"]
JOURNAL = "Synthetic Journal of Examples"
DOI = "10.5555/synthetic.review.1"
RUN = "census-synthetic"
SECRET_TEXT = ("mock rationale", "q2 text", "secret quote", "window words")
# The item fields the lab exports for each call (--responses).
EXPORTED = ("state", "output", "outputDigest", "requestSha256", "servedModel", "errorCode", "httpStatus", "attempts")
# An item that failed in transport: no response, which the census leaves open.
TRANSPORT_FAILURE = {"errorCode": "PROVIDER_UNREACHABLE", "httpStatus": 502, "attempts": 3}
UNREACHABLE = "broker failed: PROVIDER_UNREACHABLE (HTTP 502)"
EMPTY = "empty content; finish=stop"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def line(label: str, category: str = "T", *, escalate: int = 0, quote: str = "", support: str | None = None) -> str:
    return json.dumps({"l": label, "c": category, "v": support or ("direct" if category == "T" else "contra"), "q": quote,
                       "e": escalate, "r": "mock rationale"})


class SyntheticCensus:
    """A census scores database written the way the runner writes it: receipts, transport rows and verdict history
    derived from each call's answer, with the broker's item for every broker call."""

    def __init__(self, path: Path, *, journal: str = JOURNAL, doi: str = DOI, connection: sqlite3.Connection | None = None):
        self.path, self.journal, self.doi = path, journal, doi
        self.connection = connection or scores_db.connect(path, create=True)
        scores_db.set_meta(self.connection, {"instrumentId": C.INSTRUMENT_ID, "instrumentDigest": C.instrument().digest,
                                             "requiredModel": MODEL, "runId": RUN, "seed": "7"})
        if not self.connection.execute("SELECT 1 FROM reviews WHERE journal=? AND doi=?", (journal, doi)).fetchone():
            self.connection.execute("INSERT INTO reviews(journal, doi, review_order, abstract_state, full_text_state) "
                                    "VALUES(?,?,0,'completed','completed')", (journal, doi))
        self.items: dict[tuple[str, str], dict] = {}
        self.jobs: dict[str, str] = {}

    def history(self, label: str, *, tier="abstract", variant="primary", call_id=None, position=None, outcome="accepted",
                record=None, quote: str | None = "") -> None:
        record = record or {}
        self.connection.execute(
            "INSERT INTO history(journal, doi, label, tier, variant, call_id, position, outcome, category, support_verdict,"
            " escalate, quote, reason, record_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'2026-11-01T00:00:00Z')",
            (self.journal, self.doi, label, tier, variant, call_id, position, outcome, record.get("category"),
             record.get("support_verdict"), record.get("escalate"), quote if record else None,
             record.get("reason"), json.dumps(record) if record else None))

    def invalid(self, label: str, *, position=None, variant="primary", basis=NO_PARSEABLE_OUTPUT) -> None:
        """An entry the abstract tier recorded as invalid: a history row naming no call and, in the primary run, the
        citation's basis (no parseable line after the batch's answered calls, or no call answered)."""
        self.history(label, variant=variant, position=position, outcome="invalid")
        if variant == "primary":
            ordinal = self.connection.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
            self.connection.execute(
                "INSERT INTO citations(journal, doi, label, ordinal, cited_inbody, n_loci, identity, abs_class, abs_basis)"
                " VALUES(?,?,?,?,1,1,'resolved','unsure',?)", (self.journal, self.doi, label, ordinal, basis))

    def call(self, call_id: str | None, labels: list[str], output, *, tier="abstract", kind="first", variant="primary",
             batch=0, transport="broker", error=None, item_state="completed", refusal: int | None = None, record=True,
             overrides=None, skip=(), redact=(), item_patch=None, receipt_patch=None) -> dict:
        """A call: its receipt and transport row, the broker's item and the history rows the census derives from its
        answer. Without ``call_id`` the call is numbered among the recorded calls of its batch and kind, as the census
        numbers them; ``refusal`` is the HTTP status of a request the judge server refused."""
        if call_id is None:
            nonce = self.connection.execute(
                "SELECT COUNT(*) FROM calls WHERE run_id=? AND journal=? AND doi=? AND tier=? AND variant=? AND batch=? "
                "AND kind=?", (RUN, self.journal, self.doi, tier, variant, batch, kind)).fetchone()[0]
            call_id = call_id_for(RUN, self.journal, self.doi, tier, variant, batch, kind, nonce)
        if refusal is not None:
            item_state, error = "failed", refusal_error(refusal)
        request = sha("request:" + call_id)
        job = str(uuid.UUID(sha("job:" + call_id)[:32]))
        completed = item_state == "completed"
        self.connection.execute(
            f"INSERT INTO calls({', '.join(scores_db.CALL_COLUMNS)}) VALUES({', '.join('?' * len(scores_db.CALL_COLUMNS))})",
            (call_id, RUN, self.journal, self.doi, tier, variant, batch, kind, len(labels), json.dumps(labels),
             C.INSTRUMENT_ID, C.instrument().digest, "rev", MODEL, MODEL if completed else None, "fp",
             "s" * 64, "a" * 64, "b" * 64, "c" * 64, "d" * 64, 3000, 1, 1, 1, 0, "stop", 0, 1, 0.1, error, 0, 0, 0,
             "2026-11-01T00:00:00Z"))
        broker = transport == "broker"
        route = dict.fromkeys(scores_db.TRANSPORT_COLUMNS)
        route.update({"call_id": call_id, "transport": transport, "request_sha256": request})
        if broker:
            route.update({"broker_principal_id": "principal", "broker_job_id": job, "broker_item_id": call_id,
                          "broker_request_sha256": request, "broker_http_status": refusal or 200,
                          "broker_error_code": "INVALID_REQUEST" if refusal else None})
        route.update(receipt_patch or {})
        self.connection.execute(
            f"INSERT INTO call_transport({', '.join(scores_db.TRANSPORT_COLUMNS)}) "
            f"VALUES({', '.join('?' * len(scores_db.TRANSPORT_COLUMNS))})",
            tuple(route[column] for column in scores_db.TRANSPORT_COLUMNS))
        item = {"id": call_id, "state": item_state, "requestSha256": request,
                "servedModel": None if refusal is not None else MODEL}
        if output is not ABSENT:
            item["output"] = output
        if completed:
            item["outputDigest"] = output_digest(output)[0]
        elif refusal is not None:
            item.update({"errorCode": "INVALID_REQUEST", "httpStatus": refusal, "attempts": 1})
        else:
            item["errorCode"] = "INVALID_REQUEST"
        item.update(item_patch or {})
        if broker:
            self.items[(job, call_id)] = item
            self.jobs[call_id] = job
        if record:
            text = output if isinstance(output, str) and error is None else ""
            records = C.parse_labels(text, labels, stage="abstract" if tier == "abstract" else "fulltext")[0] if text.strip() else {}
            for position, label in enumerate(labels, 1):
                if label in skip:
                    continue
                found = records.get(label)
                if found is not None:
                    found = {**found, **(overrides or {}).get(label, {})}
                    self.history(label, tier=tier, variant=variant, call_id=call_id, position=position, record=found,
                                 quote=None if label in redact else found["quote"])
                elif tier == "full-text":
                    self.history(label, tier=tier, variant=variant, call_id=call_id, position=position, outcome="missing")
        return item

    def commit(self) -> None:
        self.connection.commit()

    def responses(self, path: Path, *, drop=(), omit=None) -> Path:
        """The JSON lines the lab exports; ``omit`` leaves fields of some items out (an export without them)."""
        omit = omit or {}
        with path.open("w", encoding="utf-8") as handle:
            for (job, item_id), item in self.items.items():
                if item_id in drop:
                    continue
                exported = {"jobId": job, "itemId": item_id,
                            **{key: item[key] for key in EXPORTED if key in item and key not in omit.get(item_id, ())}}
                handle.write(json.dumps(exported, ensure_ascii=False) + "\n")
        return path


def synthetic_census(path: Path) -> SyntheticCensus:
    """Calls covering every replay outcome (expected counts in ``EXPECTED``)."""
    census = SyntheticCensus(path)
    census.call("c-match", ["1", "2", "3"], "\n".join([line("1"), line("2", "F", quote="q2 text"), line("3", "N", escalate=1)]))
    census.call("c-repair", ["4"], line("4"), kind="repair")
    census.call("c-mismatch", ["5", "6"], "\n".join([line("5"), line("6")]),
                overrides={"5": {"category": "false", "support_verdict": "contradicted"}})
    census.call("c-unrecorded", ["7", "8"], "\n".join([line("7"), line("8")]), skip={"8"})
    census.call("c-output-digest", ["9", "10"], line("9", support="direct"), tier="full-text", kind="full-text",
                item_patch={"outputDigest": sha("another answer")})
    census.call("c-request-digest", ["11"], line("11"), item_patch={"requestSha256": sha("another request")})
    census.call("c-served", ["12"], line("12"), item_patch={"servedModel": "another/model"})
    census.call("c-missing-item", ["13"], line("13"))
    census.call("c-direct", ["14"], line("14"), transport="direct")
    census.call("c-refused", ["15", "16"], ABSENT, tier="full-text", kind="full-text", refusal=400)
    census.call("c-empty", ["17"], "", tier="full-text", kind="full-text", error=EMPTY)
    census.call("c-redacted", ["18"], line("18", "F", quote="secret quote"), redact={"18"})
    census.call("c-sensitivity", ["1"], line("1"), variant="no-truncation")
    # The census read a refusal; the item the broker now holds failed in transport: no stored response.
    census.call("c-failed", ["22"], ABSENT, tier="full-text", kind="full-text", refusal=400,
                item_patch={**TRANSPORT_FAILURE, "state": "failed"})
    census.history("19", call_id="c-orphan", position=1, record={"category": "true", "support_verdict": "direct-support",
                                                                  "escalate": 0, "quote": ""})
    census.history("20", outcome="deterministic", record={"category": None, "escalate": 0, "quote": ""})
    census.history("21", outcome="invalid", position=3)
    census.commit()
    return census


EXPECTED = {"replayed": 11, "mismatched": 2, "outsideReplay": 2, "unavailable": 4, "digestFailures": 4, "calls": 15,
            "entries": 23}
CALL_OUTCOMES = {"c-match": "replayed", "c-repair": "replayed", "c-mismatch": "mismatched", "c-unrecorded": "mismatched",
                 "c-output-digest": "digest-failure", "c-request-digest": "digest-failure", "c-served": "digest-failure",
                 "c-missing-item": "unavailable", "c-direct": "unavailable", "c-refused": "replayed",
                 "c-empty": "replayed", "c-redacted": "replayed", "c-sensitivity": "replayed", "c-orphan": "unavailable",
                 "c-failed": "unavailable"}


def decided_census(path: Path) -> tuple[SyntheticCensus, dict[str, str]]:
    """Calls that decided entries without a verdict line: full-text calls the judge server refused, and abstract-tier
    batches whose entries ended invalid (expected outcomes in ``DECIDED``). Returns the census and the call ids."""
    census = SyntheticCensus(path)
    ids: dict[str, str] = {}

    def add(name: str, labels: list[str], output, **options) -> None:
        ids[name] = census.call(None, labels, output, **options)["id"]
    # Full-text calls the judge server refused: the census records their entries as missing.
    add("refused", ["f1", "f2"], ABSENT, tier="full-text", kind="full-text", batch=0, refusal=400)
    add("refused-other", ["f3"], ABSENT, tier="full-text", kind="full-text", batch=1, refusal=413,
        receipt_patch={"broker_http_status": 400})
    add("refused-output", ["f4"], ABSENT, tier="full-text", kind="full-text", batch=2, refusal=422,
        item_patch={"output": "window words"})
    # A primary batch: the first call answers a1, the repair call a2; a3 has no line after the repair.
    add("a-first", ["a1", "a2", "a3"], line("a1"), batch=10)
    add("a-repair", ["a2", "a3"], line("a2"), kind="repair", batch=10)
    census.invalid("a3", position=3)
    # The judge server refused the first call; the repair call answers b1 only.
    add("b-first", ["b1", "b2"], ABSENT, batch=11, refusal=400)
    add("b-repair", ["b1", "b2"], line("b1"), kind="repair", batch=11)
    census.invalid("b2", position=2)
    # The repair call for c2 failed in transport until the census closed c2 without an answer: outside replay.
    add("c-first", ["c1", "c2"], line("c1"), batch=12)
    add("c-repair", ["c2"], ABSENT, kind="repair", batch=12, item_state="failed", error=UNREACHABLE,
        item_patch=TRANSPORT_FAILURE)
    census.invalid("c2", position=2, basis="invalid:no-response")
    # The stored repair answer has a line for d2, which the census recorded as invalid.
    add("d-first", ["d1", "d2"], line("d1"), batch=13)
    add("d-repair", ["d2"], line("d2", "F"), kind="repair", batch=13, record=False)
    census.invalid("d2", position=2)
    # No answered repair call carried e2.
    add("e-first", ["e1", "e2"], line("e1"), batch=14)
    census.invalid("e2", position=2)
    # A replicate batch whose first run did not settle (its repair call failed in transport) ran again whole: the
    # discarded run's line for g1 decides nothing.
    add("g-first-0", ["g1", "g2"], line("g1"), batch=15, variant="replicate", record=False)
    add("g-repair-0", ["g2"], ABSENT, kind="repair", batch=15, variant="replicate", item_state="failed",
        error=UNREACHABLE, item_patch=TRANSPORT_FAILURE, record=False)
    add("g-first", ["g1", "g2"], line("g2"), batch=15, variant="replicate")
    add("g-repair", ["g1"], "", kind="repair", batch=15, variant="replicate", error=EMPTY)
    census.invalid("g1", variant="replicate")
    # A no-truncation batch whose last first call has a line for h1, which the census recorded as invalid.
    add("h-first", ["h1", "h2"], "\n".join([line("h1"), line("h2")]), batch=16, variant="no-truncation", skip={"h1"})
    add("h-repair", ["h1"], "", kind="repair", batch=16, variant="no-truncation", error=EMPTY)
    census.invalid("h1", variant="no-truncation")
    census.commit()
    return census, ids


# Entry outcomes by label, with the call each is counted with and its position there.
DECIDED = {"f1": ("replayed", "refused", 1), "f2": ("replayed", "refused", 2),
           "f3": ("digest-failure", "refused-other", 1), "f4": ("digest-failure", "refused-output", 1),
           "a1": ("replayed", "a-first", 1), "a2": ("replayed", "a-repair", 1), "a3": ("replayed", "a-repair", 2),
           "b1": ("replayed", "b-repair", 1), "b2": ("replayed", "b-repair", 2),
           "c1": ("replayed", "c-first", 1), "c2": ("outside-replay", None, 2),
           "d1": ("replayed", "d-first", 1), "d2": ("mismatched", "d-repair", 1),
           "e1": ("replayed", "e-first", 1), "e2": ("mismatched", "e-first", 2),
           "g2": ("replayed", "g-first", 2), "g1": ("replayed", "g-repair", 1),
           "h2": ("replayed", "h-first", 2), "h1": ("mismatched", "h-first", 1)}
DECIDED_CALLS = {"refused": ("replayed", 2), "refused-other": ("digest-failure", 1),
                 "refused-output": ("digest-failure", 1), "a-first": ("replayed", 1), "a-repair": ("replayed", 2),
                 "b-first": ("replayed", 0), "b-repair": ("replayed", 2), "c-first": ("replayed", 1),
                 "d-first": ("replayed", 1), "d-repair": ("mismatched", 1), "e-first": ("mismatched", 2),
                 "g-first": ("replayed", 1), "g-repair": ("replayed", 1), "h-first": ("mismatched", 2),
                 "h-repair": ("replayed", 0)}


def rows(path: Path, sql: str, *params) -> list[dict]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def cli(*argv: str, env: dict | None = None) -> tuple[int, dict | None, str]:
    previous = {key: os.environ.get(key) for key in (env or {})}
    os.environ.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


class FakeParticipantApi:
    """The run principal's socket as far as replay reads it: GET /jobs/<jobId>/results?after&limit for the job ids its
    worker routes accept, one result per page (so paging is exercised), WORKER_NOT_FOUND for a job the principal does
    not hold. ``refusal`` (status, code) answers result pages with that refusal: always, or ``refusals_left`` times."""

    def __init__(self, directory: Path, items: dict[tuple[str, str], dict]):
        self.path = str(directory / "principal.sock")
        self.jobs: dict[str, list[dict]] = {}
        for (job, _), item in sorted(items.items()):
            self.jobs.setdefault(job, []).append(item)
        self.requests: list[str] = []
        self.refusal: tuple[int, str] | None = None
        self.refusals_left: int | None = None
        self.lock = threading.Lock()
        self.server = None

    def start(self) -> "FakeParticipantApi":
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def address_string(self):
                return "unix"

            def do_GET(self):
                api.requests.append(self.path)
                status, value = api.route(self.path)
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                api.requests.append("POST " + self.path)
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            request_queue_size = 64        # replay's parallel fetches, asked again at once without a pause in tests

        self.server = Server(self.path, Handler)
        os.chmod(self.path, 0o600)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def route(self, target: str) -> tuple[int, dict]:
        with self.lock:
            if self.refusal is not None and (self.refusals_left is None or self.refusals_left > 0):
                if self.refusals_left is not None:
                    self.refusals_left -= 1
                status, code = self.refusal
                return status, {"error": "Refused.", "code": code}
        url = urllib.parse.urlsplit(target)
        match = re.fullmatch(r"/jobs/([a-f0-9-]+)/results", url.path)
        if not match:
            return 404, {"error": "A principal socket serves only its worker routes.", "code": "UNKNOWN_PRINCIPAL_ROUTE"}
        results = self.jobs.get(match.group(1))
        if results is None:
            return 404, {"error": "Worker job is not owned by this principal.", "code": "WORKER_NOT_FOUND"}
        after = int(urllib.parse.parse_qs(url.query).get("after", ["0"])[0])
        # Revisions 1..n: an unrelated item first, then the job's items, one per page.
        page = [{"id": "other-item", "state": "completed", "output": "x", "revision": 1}] if after == 0 else []
        if not page:
            index = after - 1
            if index < len(results):
                page = [{**results[index], "revision": after + 1}]
        cursor = page[-1]["revision"] if page else after
        return 200, {"jobId": match.group(1), "results": page, "nextCursor": cursor, "hasMore": cursor <= len(results)}


class OutputDigestTests(unittest.TestCase):
    def test_the_daemons_output_digest_vectors(self):
        data = VECTORS.read_bytes()
        self.assertEqual(VECTORS_SHA256, hashlib.sha256(data).hexdigest(), "the vectors are shared with the daemon's test")
        vectors = json.loads(data)["vectors"]
        self.assertGreaterEqual(len(vectors), 10)
        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                output = json.loads(vector["outputJson"]) if "outputJson" in vector else ABSENT
                self.assertEqual(vector["text"], output_text(output))
                self.assertEqual((vector["sha256"], vector["bytes"]), output_digest(output))

    def test_javascript_property_order_and_values(self):
        self.assertEqual('{"1":0,"10":1,"b":2,"01":3,"a":4}', js_json({"b": 2, "10": 1, "01": 3, "1": 0, "a": 4}))
        self.assertEqual('[1,1.5,"\\u0001",null,true]', js_json([1.0, 1.5, "\u0001", None, True]))
        with self.assertRaises(Exception):
            js_json({"a": object()})


class SyntheticReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocsl-replay-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "census.sqlite3"
        self.census = synthetic_census(self.database)
        self.addCleanup(self.census.connection.close)
        self.responses = self.census.responses(self.root / "responses.jsonl", drop={"c-missing-item"})

    def assert_outcomes(self, database: Path) -> None:
        calls = {row["call_id"]: row for row in rows(database, "SELECT * FROM replay_calls")}
        self.assertEqual(CALL_OUTCOMES, {call_id: row["outcome"] for call_id, row in calls.items()})
        entries = rows(database, "SELECT * FROM replay_entries ORDER BY call_id, position")
        by_label = {}
        for row in entries:
            by_label.setdefault(row["label"], []).append(row)
        self.assertEqual("mismatched", by_label["5"][0]["outcome"])
        self.assertEqual(["category", "support_verdict"], json.loads(by_label["5"][0]["replayed_json"])["differs"])
        self.assertEqual("replayed", by_label["6"][0]["outcome"])
        # A verdict the answer yields that the history lacks.
        unrecorded = by_label["8"][0]
        self.assertEqual(("mismatched", None, None), (unrecorded["outcome"], unrecorded["recorded_json"], unrecorded["history_id"]))
        self.assertEqual(["unrecorded"], json.loads(unrecorded["replayed_json"])["differs"])
        # The redacted quote is not compared; the abstract-tier quote is.
        self.assertFalse(json.loads(by_label["18"][0]["replayed_json"])["quoteCompared"])
        self.assertTrue(json.loads(by_label["2"][0]["replayed_json"])["quoteCompared"])
        self.assertEqual("sha256:" + sha("q2 text"), json.loads(by_label["2"][0]["recorded_json"])["quote"])
        # No answered call decided these: a deterministic outcome, and an invalid entry whose citation records no
        # parseable-output basis.
        self.assertEqual({"outside-replay"}, {row["outcome"] for row in by_label["20"] + by_label["21"]})
        self.assertEqual([None, None], [row["call_id"] for row in by_label["20"] + by_label["21"]])
        # Repair and sensitivity calls replay like first calls; full-text missing entries replay from an empty answer
        # and from the judge server's refusal.
        self.assertEqual("replayed", by_label["4"][0]["outcome"])
        self.assertEqual({"replayed"}, {row["outcome"] for row in by_label["1"]})
        self.assertEqual(["no-truncation", "primary"], sorted(row["variant"] for row in by_label["1"]))
        self.assertEqual("replayed", by_label["17"][0]["outcome"])
        self.assertEqual([("replayed", "missing")] * 2, [(row["outcome"], json.loads(row["replayed_json"])["outcome"])
                                                         for row in by_label["15"] + by_label["16"]])
        detail = {call_id: json.loads(row["detail_json"]) for call_id, row in calls.items()}
        self.assertFalse(detail["c-request-digest"]["checks"]["requestDigest"])
        self.assertFalse(detail["c-output-digest"]["checks"]["outputDigest"])
        self.assertFalse(detail["c-served"]["checks"]["servedModel"])
        self.assertEqual((True, 400), (detail["c-refused"]["checks"]["refusal"], detail["c-refused"]["httpStatus"]))
        self.assertEqual(sha(""), calls["c-refused"]["output_sha256"], "a refusal stores no output")
        self.assertIn("the broker item is failed (PROVIDER_UNREACHABLE)", detail["c-failed"]["reasons"][0])
        self.assertIn("direct call", detail["c-direct"]["reasons"][0])
        self.assertIn("without a receipt", detail["c-orphan"]["reasons"][0])
        self.assertEqual(output_digest(self.census.items[(self.census.jobs["c-match"], "c-match")]["output"])[0],
                         calls["c-match"]["output_sha256"])
        self.assertEqual((3, 3, 0), (calls["c-match"]["n_entries"], calls["c-match"]["n_replayed"],
                                     calls["c-match"]["n_mismatched"]))
        # No output text, rationale or quote reaches the replay tables.
        with closing(sqlite3.connect(database)) as connection:
            for table in ("replay_calls", "replay_entries", "replay_meta"):
                dump = json.dumps([list(row) for row in connection.execute(f"SELECT * FROM {table}")])
                for text in SECRET_TEXT:
                    self.assertNotIn(text, dump, table)

    def history_extent(self) -> list[str]:
        count, last = self.census.connection.execute("SELECT COUNT(*), MAX(history_id) FROM history").fetchone()
        return [str(count), str(last)]

    def test_responses_into_a_separate_file_leave_the_census_unchanged(self):
        before = scores_db.file_sha256(self.database)
        output = self.root / "replay" / "replay.sqlite3"
        code, summary, _ = cli("study", "replay", "--census", str(self.database), "--responses", str(self.responses),
                               "--output", str(output))
        self.assertEqual(3, code)
        self.assertEqual(EXPECTED, {key: summary[key] for key in EXPECTED})
        self.assertEqual(summary["entries"], sum(summary[key] for key in ("replayed", "mismatched", "outsideReplay",
                                                                          "unavailable", "digestFailures")))
        self.assertEqual(("responses", str(output)), (summary["source"], summary["database"]))
        self.assertEqual(before, scores_db.file_sha256(self.database))
        self.assertFalse(scores_db.has_table(self.census.connection, "replay_calls"))
        self.assertFalse(os.access(output, os.W_OK))
        self.assert_outcomes(output)
        with closing(sqlite3.connect(output.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(summary["reportSha256"], report_sha256(connection))
            meta = dict(connection.execute("SELECT key, value FROM replay_meta"))
            self.assertEqual(summary["reportSha256"], meta["reportSha256"])
            self.assertEqual((C.instrument().digest, "responses", RUN),
                             (meta["instrumentDigest"], meta["source"], meta["censusRunId"]))
            # The census database it read and the history it compared.
            self.assertEqual(before, meta["censusSha256"])
            self.assertEqual(self.history_extent(), [meta["historyRows"], meta["maxHistoryId"]])
            documented = {row[0] for row in connection.execute("SELECT DISTINCT table_name FROM data_dictionary")}
            self.assertEqual(set(scores_db.REPLAY_TABLES), documented)
        # An existing output is never overwritten.
        code, _, error = cli("study", "replay", "--census", str(self.database), "--responses", str(self.responses),
                             "--output", str(output))
        self.assertEqual((2, True), (code, "already exists" in error))

    def test_in_place_replay_writes_new_tables_once_and_replaces_them_on_a_second_run(self):
        state = scores_db.freeze_source_state(self.census.connection)
        history = rows(self.database, "SELECT * FROM history ORDER BY history_id")
        result_file = self.root / "result.json"
        code, summary, _ = cli("--result-file", str(result_file), "study", "replay", "--census", str(self.database),
                               "--responses", str(self.responses))
        self.assertEqual(3, code)
        self.assertEqual(EXPECTED, {key: summary[key] for key in EXPECTED})
        recorded = json.loads(result_file.read_text())
        self.assertEqual((3, summary, None), (recorded["exitCode"], recorded["result"], recorded["error"]))
        self.assert_outcomes(self.database)
        self.assertEqual(history, rows(self.database, "SELECT * FROM history ORDER BY history_id"))
        self.assertNotEqual(state, scores_db.freeze_source_state(self.census.connection), "a replay makes a frozen copy stale")
        with closing(scores_db.connect(self.database)) as connection:
            self.assertEqual(summary["reportSha256"], report_sha256(connection))
            meta = dict(connection.execute("SELECT key, value FROM replay_meta"))
            self.assertEqual(self.history_extent(), [meta["historyRows"], meta["maxHistoryId"]])
            self.assertNotIn("censusSha256", meta, "the census database itself holds the replay")
            scores_db.write_dictionary(connection)
            for table in scores_db.REPLAY_TABLES:
                columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
                documented = {r[0] for r in connection.execute(
                    "SELECT column_name FROM data_dictionary WHERE table_name=?", (table,))}
                self.assertEqual(columns, documented, table)
        # The same replay again: the same report, the rows replaced, not added.
        code, again, _ = cli("study", "replay", "--census", str(self.database), "--responses", str(self.responses))
        self.assertEqual((3, summary["reportSha256"]), (code, again["reportSha256"]))
        self.assertEqual(EXPECTED["entries"], len(rows(self.database, "SELECT 1 FROM replay_entries")))
        # A frozen copy is replayed into a file of its own; its redacted quotes are not compared.
        frozen = self.root / "frozen.sqlite3"
        scores_db.freeze(self.database, frozen, allow_incomplete=True)
        code, from_frozen, _ = cli("study", "replay", "--census", str(frozen), "--responses", str(self.responses),
                                   "--output", str(self.root / "frozen-replay.sqlite3"))
        self.assertEqual((3, EXPECTED), (code, {key: from_frozen[key] for key in EXPECTED}))

    def test_an_in_place_replay_needs_a_census_database_it_can_write(self):
        # A census database in a read-only directory (a sealed study volume): refused before any lock file is made.
        sealed = self.root / "sealed"
        sealed.mkdir()
        copy = sealed / "census.sqlite3"
        shutil.copyfile(self.database, copy)
        os.chmod(sealed, 0o555)
        self.addCleanup(os.chmod, sealed, 0o755)
        code, summary, error = cli("study", "replay", "--census", str(copy), "--responses", str(self.responses))
        self.assertEqual((2, None, True), (code, summary, "--output" in error), error)
        self.assertFalse(Path(str(copy) + ".lock").exists())
        code, summary, _ = cli("study", "replay", "--census", str(copy), "--responses", str(self.responses),
                               "--output", str(self.root / "sealed-replay.sqlite3"))
        self.assertEqual((3, EXPECTED), (code, {key: summary[key] for key in EXPECTED}))
        # A read-only file in a writable directory, and a frozen copy: refused the same way.
        readonly = self.root / "readonly.sqlite3"
        shutil.copyfile(self.database, readonly)
        os.chmod(readonly, 0o444)
        frozen = self.root / "frozen.sqlite3"
        scores_db.freeze(self.database, frozen, allow_incomplete=True)
        for database in (readonly, frozen):
            with self.subTest(database=database.name):
                code, _, error = cli("study", "replay", "--census", str(database), "--responses", str(self.responses))
                self.assertEqual((2, True), (code, "--output" in error), error)
                self.assertFalse(Path(str(database) + ".lock").exists())

    def test_socket_mode_reads_each_job_page_by_page(self):
        api = FakeParticipantApi(self.root, {key: item for key, item in self.census.items.items()
                                             if key[1] != "c-missing-item"}).start()
        self.addCleanup(api.stop)
        output = self.root / "socket-replay.sqlite3"
        code, summary, _ = cli("study", "replay", "--census", str(self.database), "--output", str(output),
                               env={"OCSL_BROKER_SOCKET": api.path})
        self.assertEqual((3, EXPECTED, "broker-socket"), (code, {key: summary[key] for key in EXPECTED}, summary["source"]))
        self.assert_outcomes(output)
        self.assertTrue(all(not request.startswith("POST") for request in api.requests), "replay never submits")
        self.assertTrue(any("after=1" in request for request in api.requests), "the result feed is followed")
        detail = json.loads(rows(output, "SELECT detail_json FROM replay_calls WHERE call_id='c-missing-item'")[0]["detail_json"])
        self.assertIn("WORKER_NOT_FOUND", detail["reasons"][0])
        # The same outcomes as from the exported responses.
        exported = self.root / "exported.sqlite3"
        cli("study", "replay", "--census", str(self.database), "--responses", str(self.responses), "--output", str(exported))
        query = "SELECT call_id, position, label, outcome, recorded_json, replayed_json FROM replay_entries ORDER BY 1, 2, 3"
        self.assertEqual(rows(exported, query), rows(output, query))

    def test_a_socket_that_stops_answering_ends_the_replay_without_results(self):
        api = FakeParticipantApi(self.root, self.census.items).start()
        api.stop()                                  # the socket file stays; nothing accepts on it
        output = self.root / "outage.sqlite3"
        for target in (None, output):
            with self.subTest(output=target), self.assertRaises(TransientError):
                replay(self.database, SocketItems(api.path, sleep=lambda seconds: None), output=target)
        self.assertFalse(scores_db.has_table(self.census.connection, "replay_calls"))
        self.assertFalse(output.exists() or Path(str(output.parent / ("." + output.name + ".incoming"))).exists())

    def test_socket_refusals_are_invalid_input_or_an_outage_never_calls_without_a_response(self):
        api = FakeParticipantApi(self.root, self.census.items).start()
        self.addCleanup(api.stop)
        # A revoked principal's socket serves this run no results: invalid input, nothing recorded.
        api.refusal = (409, "PRINCIPAL_REVOKED")
        code, summary, error = cli("study", "replay", "--census", str(self.database), env={"OCSL_BROKER_SOCKET": api.path})
        self.assertEqual((2, None), (code, summary))
        self.assertIn("PRINCIPAL_REVOKED", error)
        self.assertIn("--responses", error)
        self.assertFalse(scores_db.has_table(self.census.connection, "replay_calls"))
        # A result page the broker keeps refusing (a daemon-side failure, a server error): an outage, nothing recorded.
        for status, name in ((400, "INVALID_REQUEST"), (500, "INTERNAL"), (409, "DAEMON_SHUTTING_DOWN")):
            api.refusal = (status, name)
            for target in (None, self.root / f"refused-{status}.sqlite3"):
                with self.subTest(refusal=name, output=target), self.assertRaises(TransientError) as raised:
                    replay(self.database, SocketItems(api.path, sleep=lambda seconds: None), output=target)
                self.assertIn(name, str(raised.exception))
                self.assertFalse(scores_db.has_table(self.census.connection, "replay_calls"))
                self.assertFalse(target is not None and target.exists())
        # A refusal that clears when the page is asked again.
        api.refusal, api.refusals_left = (500, "INTERNAL"), 1
        summary = replay(self.database, SocketItems(api.path, sleep=lambda seconds: None),
                         output=self.root / "after-refusal.sqlite3")
        self.assertEqual({**EXPECTED, "replayed": EXPECTED["replayed"] + 1, "unavailable": EXPECTED["unavailable"] - 1},
                         {key: summary[key] for key in EXPECTED}, "this socket also holds the item of c-missing-item")

    def test_a_census_database_that_changes_while_it_is_replayed_into_a_file_records_nothing(self):
        census, source = self.census, ResponseItems(self.responses)

        class Changing:
            kind = "responses"

            def fetch(self, keys):
                census.history("23", outcome="deterministic", record={"category": None, "escalate": 0, "quote": ""})
                census.commit()
                return source.fetch(keys)
        output = self.root / "changing.sqlite3"
        with self.assertRaises(TransientError):
            replay(self.database, Changing(), output=output)
        self.assertFalse(output.exists() or (self.root / ".changing.sqlite3.incoming").exists())

    def test_every_entry_replays_exit_zero(self):
        database = self.root / "clean.sqlite3"
        census = SyntheticCensus(database)
        self.addCleanup(census.connection.close)
        census.call("a", ["1", "2"], "\n".join([line("1"), line("2", "F", quote="q2 text")]))
        census.call("b", ["2"], line("2", "F", quote="q2 text"), kind="repair", record=False)
        census.history("3", outcome="deterministic", record={"category": None, "escalate": 0, "quote": ""})
        census.commit()
        responses = census.responses(self.root / "clean.jsonl")
        code, summary, _ = cli("study", "replay", "--census", str(database), "--responses", str(responses))
        self.assertEqual(0, code)
        self.assertEqual({"replayed": 2, "mismatched": 0, "outsideReplay": 1, "unavailable": 0, "digestFailures": 0,
                          "calls": 1, "entries": 3}, {key: summary[key] for key in EXPECTED})

    def test_invalid_input_exits_two(self):
        frozen_digest = self.root / "other.sqlite3"
        with closing(scores_db.connect(frozen_digest, create=True)) as connection:
            scores_db.set_meta(connection, {"instrumentDigest": "0" * 64})
            connection.commit()
        bad = self.root / "bad.jsonl"
        bad.write_text('{"jobId": "j", "itemId": "i"}\nnot json\n')
        anonymous = self.root / "anonymous.jsonl"
        anonymous.write_text('{"itemId": "i"}\n')
        cases = [
            (["--census", str(self.root / "missing.sqlite3"), "--responses", str(self.responses)], "missing"),
            (["--census", str(self.database), "--responses", str(bad)], "line 2 is not JSON"),
            (["--census", str(self.database), "--responses", str(anonymous)], "names no jobId"),
            (["--census", str(self.database), "--responses", str(self.root / "absent.jsonl")], "cannot be read"),
            (["--census", str(frozen_digest), "--responses", str(self.responses)], "instrument digest"),
            (["--census", str(self.database)], "--broker-socket"),
            (["--census", str(self.database), "--broker-socket", str(self.root / "no.sock")], "socket"),
            (["--census", str(self.database), "--broker-socket", "relative.sock"], "absolute"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv[2:]):
                code, summary, error = cli("study", "replay", *argv, env={"OCSL_BROKER_SOCKET": ""})
                self.assertEqual((2, None), (code, summary))
                self.assertIn(message, error)
        self.assertFalse(Path(str(frozen_digest) + ".lock").exists(), "refused before the writer lock")
        with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
            main(["study", "replay", "--census", str(self.database), "--responses", str(self.responses),
                  "--broker-socket", "/tmp/x.sock"])
        self.assertEqual(2, raised.exception.code)
        self.assertFalse(scores_db.has_table(self.census.connection, "replay_calls"))

    def test_a_census_process_writing_the_database_refuses_an_in_place_replay(self):
        from ocsl_review.census_runner import writer_lock
        descriptor = writer_lock(self.database)
        try:
            code, _, error = cli("study", "replay", "--census", str(self.database), "--responses", str(self.responses))
        finally:
            os.close(descriptor)
        self.assertEqual((2, True), (code, "another census process" in error))


class DecidedWithoutVerdictTests(unittest.TestCase):
    """Calls the judge server refused, and entries recorded as invalid, replay from the answered calls that decided
    them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocsl-replay-decided-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "census.sqlite3"
        self.census, self.ids = decided_census(self.database)
        self.addCleanup(self.census.connection.close)
        self.names = {call_id: name for name, call_id in self.ids.items()}

    def outcomes(self, database: Path) -> tuple[dict, dict]:
        entries = {row["label"]: row for row in rows(database, "SELECT * FROM replay_entries")}
        calls = {self.names[row["call_id"]]: row for row in rows(database, "SELECT * FROM replay_calls")}
        return entries, calls

    def test_refusals_and_invalid_outputs_replay_from_the_answered_calls_that_decided_them(self):
        output = self.root / "replay.sqlite3"
        responses = self.census.responses(self.root / "responses.jsonl")
        code, summary, _ = cli("study", "replay", "--census", str(self.database), "--responses", str(responses),
                               "--output", str(output))
        self.assertEqual(3, code)
        self.assertEqual({"replayed": 13, "mismatched": 3, "outsideReplay": 1, "unavailable": 0, "digestFailures": 2,
                          "calls": 15, "entries": 19}, {key: summary[key] for key in EXPECTED})
        entries, calls = self.outcomes(output)
        self.assertEqual(DECIDED, {label: (row["outcome"], self.names.get(row["call_id"]), row["position"])
                                   for label, row in entries.items()})
        self.assertEqual(DECIDED_CALLS, {name: (row["outcome"], row["n_entries"]) for name, row in calls.items()})
        self.assertEqual((1, 0, 1), (calls["d-repair"]["n_entries"], calls["d-repair"]["n_replayed"],
                                     calls["d-repair"]["n_mismatched"]))
        # An invalid entry names the answered calls that decided it, first calls before repair calls; the discarded
        # run of the replicate batch decided nothing.
        self.assertEqual([self.ids["a-first"], self.ids["a-repair"]], json.loads(entries["a3"]["replayed_json"])["calls"])
        self.assertEqual([self.ids["b-first"], self.ids["b-repair"]], json.loads(entries["b2"]["replayed_json"])["calls"])
        self.assertEqual([self.ids["g-first"], self.ids["g-repair"]], json.loads(entries["g1"]["replayed_json"])["calls"])
        self.assertFalse({"g-first-0", "g-repair-0", "c-repair"} & set(calls))
        self.assertEqual("invalid", json.loads(entries["a3"]["replayed_json"])["outcome"])
        # What a mismatched invalid entry was re-derived as: an accepted verdict, or an entry left open.
        for label in ("d2", "h1"):
            replayed = json.loads(entries[label]["replayed_json"])
            self.assertEqual(("accepted", ["outcome", "category", "support_verdict", "escalate"]),
                             (replayed["outcome"], replayed["differs"]))
        self.assertEqual((None, ["outcome"]), tuple(json.loads(entries["e2"]["replayed_json"])[key]
                                                    for key in ("outcome", "differs")))
        # The refusals' checks against the receipts.
        detail = {name: json.loads(row["detail_json"]) for name, row in calls.items()}
        self.assertEqual({"requestDigest": True, "outputDigest": True, "refusal": True, "instrumentDigest": True,
                          "tier": True}, detail["refused"]["checks"])
        self.assertFalse(detail["refused-other"]["checks"]["refusal"])
        self.assertEqual(["the refused item stores an output"], detail["refused-output"]["reasons"])
        self.assertTrue(detail["b-first"]["checks"]["refusal"])
        dump = json.dumps([list(row.values()) for row in rows(output, "SELECT * FROM replay_entries")]
                          + [list(row.values()) for row in rows(output, "SELECT * FROM replay_calls")])
        for text in SECRET_TEXT:
            self.assertNotIn(text, dump)

    def test_the_socket_gives_the_same_outcomes(self):
        api = FakeParticipantApi(self.root, self.census.items).start()
        self.addCleanup(api.stop)
        output = self.root / "socket.sqlite3"
        code, summary, _ = cli("study", "replay", "--census", str(self.database), "--output", str(output),
                               env={"OCSL_BROKER_SOCKET": api.path})
        self.assertEqual((3, 13, 0), (code, summary["replayed"], summary["unavailable"]))
        exported = self.root / "exported.sqlite3"
        cli("study", "replay", "--census", str(self.database), "--responses",
            str(self.census.responses(self.root / "responses.jsonl")), "--output", str(exported))
        query = "SELECT call_id, position, label, outcome, recorded_json, replayed_json FROM replay_entries ORDER BY 1, 2, 3"
        self.assertEqual(rows(exported, query), rows(output, query))

    def test_calls_replayed_in_small_chunks_give_the_same_record(self):
        # An invalid entry's deciding calls fall into different chunks of calls: the same report either way.
        from ocsl_review import census_replay
        responses = self.census.responses(self.root / "responses.jsonl")
        whole = replay(self.database, ResponseItems(responses), output=self.root / "whole.sqlite3")
        self.addCleanup(setattr, census_replay, "CALL_CHUNK", census_replay.CALL_CHUNK)
        census_replay.CALL_CHUNK = 2
        chunked = replay(self.database, ResponseItems(responses), output=self.root / "chunked.sqlite3")
        self.assertEqual((whole["reportSha256"], whole["entries"]), (chunked["reportSha256"], chunked["entries"]))

    def test_an_export_without_the_refusal_fields_leaves_a_refused_call_unavailable(self):
        responses = self.census.responses(self.root / "responses.jsonl",
                                          omit={self.ids["refused"]: ("httpStatus", "attempts")})
        output = self.root / "replay.sqlite3"
        code, summary, _ = cli("study", "replay", "--census", str(self.database), "--responses", str(responses),
                               "--output", str(output))
        self.assertEqual((3, 2), (code, summary["unavailable"]))
        entries, calls = self.outcomes(output)
        self.assertEqual(("unavailable", "unavailable"), (entries["f1"]["outcome"], calls["refused"]["outcome"]))


class SourceTests(unittest.TestCase):
    def test_responses_keep_the_last_line_of_an_item_and_an_absent_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.jsonl"
            path.write_text("\n".join([json.dumps({"jobId": "j", "itemId": "i", "state": "running"}),
                                       "",
                                       json.dumps({"jobId": "j", "itemId": "i", "state": "completed", "outputDigest": "d"}),
                                       json.dumps({"jobId": "k", "itemId": "i", "state": "completed", "output": None})]) + "\n")
            items = ResponseItems(path)
            fetched = items.fetch([("j", "i"), ("k", "i"), ("x", "i")])
            self.assertEqual({"id": "i", "jobId": "j", "state": "completed", "outputDigest": "d"}, fetched[("j", "i")].item)
            self.assertNotIn("output", fetched[("j", "i")].item)
            self.assertIn("output", fetched[("k", "i")].item)
            self.assertIsNone(fetched[("x", "i")].item)
            with self.assertRaises(ReplayInputError):
                ResponseItems(Path(directory) / "missing.jsonl")

    def test_only_a_job_the_broker_does_not_hold_is_an_item_without_a_response(self):
        class Client:
            def __init__(self):
                self.calls: list[str] = []
                self.flaky = 1

            def request(self, method, route):
                job = route.split("/")[2]
                self.calls.append(job)
                if job == "a1":
                    raise BrokerRefused(404, "WORKER_NOT_FOUND")
                if job == "c3":
                    raise BrokerUnavailable("broker socket: ConnectionRefusedError")
                if job == "d4":
                    raise BrokerRefused(409, "PRINCIPAL_REVOKED")
                if job == "0e":
                    raise BrokerRefused(404, "UNKNOWN_PRINCIPAL_ROUTE")
                if job == "e5":
                    raise BrokerRefused(400, "INVALID_REQUEST")
                if job == "f6" and self.flaky:
                    self.flaky -= 1
                    raise BrokerRefused(500, "INTERNAL")
                return {"results": [{"id": "i", "state": "completed"}], "nextCursor": 1, "hasMore": False}

        client = Client()
        items = SocketItems(client=client, workers=1, sleep=lambda seconds: None)
        fetched = items.fetch([("a1", "i"), ("b2", "i"), ("b2", "missing"), ("f6", "i"), ("job-x", "i")])
        self.assertIn("WORKER_NOT_FOUND", fetched[("a1", "i")].reason)
        self.assertEqual(1, client.calls.count("a1"), "WORKER_NOT_FOUND is not asked again")
        self.assertEqual("completed", fetched[("b2", "i")].item["state"])
        self.assertEqual(Fetched(None, "the broker job holds no item of the call"), fetched[("b2", "missing")])
        self.assertEqual(("completed", 2), (fetched[("f6", "i")].item["state"], client.calls.count("f6")),
                         "a refusal that clears on the next attempt")
        self.assertIn("no job id the broker issues", fetched[("job-x", "i")].reason)
        self.assertNotIn("job-x", client.calls)
        # A socket that stops answering, or keeps refusing a page, is an outage, never a call without a response.
        for job, words in (("c3", "ConnectionRefusedError"), ("e5", "INVALID_REQUEST (HTTP 400)")):
            with self.subTest(job=job), self.assertRaises(TransientError) as raised:
                items.fetch([(job, "i")])
            self.assertIn(words, str(raised.exception))
            self.assertEqual(3, client.calls.count(job))
        # A socket that cannot serve this run's results: invalid input, not asked again.
        for job in ("d4", "0e"):
            with self.subTest(job=job), self.assertRaises(ReplayInputError):
                items.fetch([(job, "i")])
            self.assertEqual(1, client.calls.count(job))


class FactsTests(unittest.TestCase):
    def test_replay_facts_are_exported_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work.sqlite3"
            build_scores(work)
            with closing(scores_db.connect(work)) as connection:
                census = SyntheticCensus(work, journal="J1", doi="r1", connection=connection)
                census.call("j1-a", ["1", "2"], "\n".join([line("1"), line("2")]))
                census.call("j1-b", ["3"], line("3"), overrides={"3": {"category": "false"}})
                j2 = SyntheticCensus(work, journal="J2", doi="s1", connection=connection)
                j2.call("j2-a", ["1"], line("1"), item_patch={"outputDigest": sha("x")})
                census.items.update(j2.items)
                census.commit()
                responses = census.responses(root / "responses.jsonl")
            code, summary, _ = cli("study", "replay", "--census", str(work), "--responses", str(responses))
            self.assertEqual(3, code)
            frozen = root / "scores.sqlite3"
            scores_db.freeze(work, frozen)
            result = corpus_facts.export_facts(frozen, root / "facts", ["J1", "J2"], b=50, seed=3, code_revision="rev-test")
            self.assertTrue(result["replay"]["exported"])
            facts = json.loads((root / "facts" / "facts.json").read_text())["facts"]
            # build_scores records its no-truncation rows and deterministic outcomes without calls.
            outside = {journal: len(rows(work, "SELECT 1 FROM history WHERE journal=? AND call_id IS NULL", journal))
                       for journal in ("J1", "J2")}
            expected = {"census.j1.replay.replayed": 2, "census.j1.replay.mismatched": 1,
                        "census.j1.replay.digest_failures": 0, "census.j1.replay.unavailable": 0,
                        "census.j1.replay.outside_replay": outside["J1"], "census.j1.replay.entries": 3 + outside["J1"],
                        "census.j1.replay.share_replayed": 2 / 3,
                        "census.j2.replay.replayed": 0, "census.j2.replay.digest_failures": 1,
                        "census.j2.replay.share_replayed": 0.0,
                        "census.replay.entries": summary["entries"], "census.replay.replayed": summary["replayed"],
                        "census.replay.share_replayed": 2 / 4}
            self.assertEqual(expected, {key: facts[key]["value"] for key in expected})
            self.assertIn("replay_meta", facts["census.replay.share_replayed"]["sql"], "the derivation checks coverage")
            self.assertTrue(corpus_facts.verify_export(root / "facts" / "facts.json")["ok"])
            bundle = json.loads((root / "facts" / "facts.json").read_text())
            bundle["facts"]["census.j1.replay.replayed"]["value"] = 3
            mutated = root / "facts" / "mutated.json"
            mutated.write_text(json.dumps(bundle))
            with self.assertRaises(Exception):
                corpus_facts.verify_export(mutated)

    def test_replay_facts_need_a_replay_that_covers_the_frozen_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work.sqlite3"
            build_scores(work)
            with closing(scores_db.connect(work)) as connection:
                census = SyntheticCensus(work, journal="J1", doi="r1", connection=connection)
                census.call("j1-a", ["1", "2"], "\n".join([line("1"), line("2")]))
                census.commit()
                code, _, _ = cli("study", "replay", "--census", str(work), "--responses",
                                 str(census.responses(root / "responses.jsonl")))
                self.assertEqual(0, code)
                replayed = connection.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                # A verdict recorded after the replay: the replay no longer covers the history.
                census.call("j1-b", ["3"], line("3"))
                census.commit()
                responses = census.responses(root / "responses.jsonl")
            stale = root / "stale.sqlite3"
            scores_db.freeze(work, stale)
            result = corpus_facts.export_facts(stale, root / "stale-facts", ["J1"], b=20, seed=1, code_revision="r")
            facts = json.loads((root / "stale-facts" / "facts.json").read_text())["facts"]
            self.assertFalse([key for key in facts if ".replay." in key])
            self.assertEqual((False, replayed, replayed + 1), tuple(result["replay"][key] for key in (
                "exported", "replayedHistoryRows", "historyRows")))
            self.assertIn("replay the census again", result["replay"]["reason"])
            # Replayed again before freezing: the facts are exported and verified.
            code, _, _ = cli("study", "replay", "--census", str(work), "--responses", str(responses))
            self.assertEqual(0, code)
            frozen = root / "scores.sqlite3"
            scores_db.freeze(work, frozen)
            result = corpus_facts.export_facts(frozen, root / "facts", ["J1"], b=20, seed=1, code_revision="r")
            self.assertTrue(result["replay"]["exported"])
            facts = json.loads((root / "facts" / "facts.json").read_text())["facts"]
            self.assertEqual((3, 1.0), (facts["census.j1.replay.replayed"]["value"],
                                        facts["census.replay.share_replayed"]["value"]))
            self.assertTrue(corpus_facts.verify_export(root / "facts" / "facts.json")["ok"])

    def test_no_replay_facts_without_a_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_scores(root / "work.sqlite3")
            scores_db.freeze(root / "work.sqlite3", root / "scores.sqlite3")
            result = corpus_facts.export_facts(root / "scores.sqlite3", root / "facts", ["J1"], b=20, seed=1,
                                               code_revision="r")
            facts = json.loads((root / "facts" / "facts.json").read_text())["facts"]
            self.assertFalse([key for key in facts if ".replay." in key])
            self.assertNotIn("replay", result)


class RunnerReplayTests(BrokerTestCase):
    """A census the runner recorded through the mock broker replays from the items the broker holds."""

    def census(self, *, refuse=False):
        self.broker.install_census()
        run = self.run_census()
        self.broker.judge.drop_first = {"2", "9"}      # these labels are decided by repair calls
        self.broker.judge.never = {"11"}              # no parseable line after the repair: invalid
        if refuse:
            # The judge server refuses the first repair call: its entries are invalid, decided by that refusal.
            original, refused = self.broker.execute_item, []

            def execute_item(job, item):
                if not refused and MockBroker.is_repair(item):
                    refused.append(item["id"])
                    self.broker.status_next.append(400)
                return original(job, item)
            self.broker.execute_item = execute_item
        self.prepare(run)
        self.assertEqual("completed", run.run_abstract()["state"])
        if refuse:
            self.broker.status_next = [413]          # the judge server refuses the first full-text call
        self.assertEqual("completed", run.run_full_text(
            fetch_xml=lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes())["state"])
        self.assertEqual("completed", run.run_replicate(entries=5)["state"])
        run.close()

    def replay(self, *extra):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["study", "replay", "--census", str(self.database), "--broker-socket", self.broker.path, *extra])
        return code, json.loads(buffer.getvalue())

    def test_every_verdict_of_a_broker_run_replays_repair_calls_and_invalid_outputs_included(self):
        self.census()
        history = self.rows("SELECT * FROM history")
        with_call = [row for row in history if row["call_id"]]
        invalid = [row for row in history if not row["call_id"] and row["outcome"] == "invalid"]
        self.assertTrue(any(row["tier"] == "full-text" for row in with_call))
        self.assertTrue(any(row["variant"] == "replicate" for row in with_call))
        self.assertTrue(any(row["label"] == "11" and row["variant"] == "primary" for row in invalid))
        repairs = {row["call_id"] for row in self.rows("SELECT call_id FROM calls WHERE kind='repair'")}
        self.assertTrue(repairs and repairs & {row["call_id"] for row in with_call})
        before, posts = len(self.broker.upstream), self.broker.calls_to_route("POST", "/")
        code, summary = self.replay()
        self.assertEqual(0, code, summary)
        self.assertEqual((len(with_call) + len(invalid), 0, 0, 0), (summary["replayed"], summary["mismatched"],
                                                                    summary["unavailable"], summary["digestFailures"]))
        self.assertEqual(len(history) - len(with_call) - len(invalid), summary["outsideReplay"])
        replayed_calls = {row["call_id"] for row in self.rows("SELECT call_id FROM replay_calls")}
        self.assertEqual(len(replayed_calls), summary["calls"])
        self.assertTrue({row["call_id"] for row in with_call} <= replayed_calls)
        self.assertEqual(before, len(self.broker.upstream), "no model call")
        self.assertEqual(posts, self.broker.calls_to_route("POST", "/"), "replay submits and resumes nothing")
        # The invalid output is counted with the repair call that closed it.
        closed = self.rows("SELECT r.outcome, k.kind FROM replay_entries r JOIN calls k ON k.call_id=r.call_id "
                           "WHERE r.label='11' AND r.tier='abstract'")
        self.assertTrue(closed)
        self.assertEqual({("replayed", "repair")}, {(row["outcome"], row["kind"]) for row in closed})

    def test_calls_the_judge_refused_replay_as_answers_without_labels(self):
        self.census(refuse=True)
        refused = self.rows("SELECT call_id, tier, kind FROM calls WHERE error LIKE 'refused:%'")
        self.assertEqual({("abstract", "repair"), ("full-text", "full-text")}, {(row["tier"], row["kind"]) for row in refused})
        code, summary = self.replay()
        self.assertEqual(0, code, summary)
        self.assertEqual((0, 0, 0), (summary["mismatched"], summary["unavailable"], summary["digestFailures"]))
        outcomes = {row["call_id"]: row for row in self.rows("SELECT * FROM replay_calls")}
        for row in refused:
            with self.subTest(tier=row["tier"]):
                replayed = outcomes[row["call_id"]]
                self.assertEqual("replayed", replayed["outcome"])
                self.assertGreater(replayed["n_entries"], 0)
                self.assertTrue(json.loads(replayed["detail_json"])["checks"]["refusal"])

    def test_a_changed_stored_answer_is_a_digest_failure_and_a_rewritten_one_a_mismatch(self):
        self.census()
        call = self.rows("SELECT c.call_id, t.broker_job_id FROM calls c JOIN call_transport t USING(call_id) "
                         "WHERE c.tier='abstract' AND c.kind='first' AND c.variant='primary' ORDER BY c.call_id LIMIT 1")[0]
        with self.broker.lock:
            result = self.broker.jobs[call["broker_job_id"]]["results"][call["call_id"]]
            original = result["output"]
            first = original.splitlines()[0]
            changed = json.dumps({**json.loads(first), "c": "U"})
            result["output"] = original.replace(first, changed)
        code, summary = self.replay("--output", str(self.root / "changed.sqlite3"))
        failed = rows(self.root / "changed.sqlite3", "SELECT outcome FROM replay_entries WHERE call_id=?", call["call_id"])
        self.assertEqual((3, len(failed), 0), (code, summary["digestFailures"], summary["mismatched"]))
        self.assertEqual({"digest-failure"}, {row["outcome"] for row in failed})
        self.assertGreaterEqual(len(failed), len(self.rows("SELECT 1 FROM history WHERE call_id=?", call["call_id"])))
        with self.broker.lock:
            result["outputDigest"] = output_digest(result["output"])[0]
        code, summary = self.replay("--output", str(self.root / "rewritten.sqlite3"))
        self.assertEqual((3, 0, 1), (code, summary["digestFailures"], summary["mismatched"]))
        differs = rows(self.root / "rewritten.sqlite3", "SELECT replayed_json FROM replay_entries WHERE outcome='mismatched'")
        self.assertIn("category", json.loads(differs[0]["replayed_json"])["differs"])


if __name__ == "__main__":
    unittest.main()
