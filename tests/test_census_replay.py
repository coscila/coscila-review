# SPDX-License-Identifier: Apache-2.0
"""study replay: census verdicts re-derived from stored broker responses, checked against the receipts, no model calls.

A synthetic census database with broker calls (and one direct call) is replayed from a responses file and from a fake
participant API on a Unix socket; a census the runner itself recorded through the mock broker replays completely,
repair calls included. The output digest is checked against vectors the daemon's own outputDigest produced.
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
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from ocsl_review import citation_census as C
from ocsl_review import corpus_facts, scores_db
from ocsl_review.census_replay import (ABSENT, Fetched, ReplayInputError, ResponseItems, SocketItems, js_json,
                                       output_digest, output_text, replay, report_sha256)
from ocsl_review.cli import main
from ocsl_review.errors import TransientError

from test_census_broker import BrokerTestCase
from test_census_runner import FIXTURES
from test_estimands import build_scores

VECTORS = Path(__file__).resolve().parent / "fixtures" / "broker" / "output-digest-vectors.json"
# The same file is asserted by the daemon's own test (puppenclaw test/fixtures/output-digest-vectors.json): both copies
# must stay byte-identical.
VECTORS_SHA256 = "8650791998aa91a44db66b019c7408a1daf648e7d5549a768e296b3efe93baf1"
MODEL = C.instrument().manifest["requiredModel"]["served"]
JOURNAL = "Synthetic Journal of Examples"
DOI = "10.5555/synthetic.review.1"
SECRET_TEXT = ("mock rationale", "q2 text", "secret quote", "window words")


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
                                             "requiredModel": MODEL, "runId": "census-synthetic", "seed": "7"})
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

    def call(self, call_id: str, labels: list[str], output, *, tier="abstract", kind="first", variant="primary",
             transport="broker", error=None, item_state="completed", record=True, overrides=None, skip=(),
             redact=(), item_patch=None) -> dict:
        request = sha("request:" + call_id)
        job = "job-" + sha(call_id)[:12]
        self.connection.execute(
            f"INSERT INTO calls({', '.join(scores_db.CALL_COLUMNS)}) VALUES({', '.join('?' * len(scores_db.CALL_COLUMNS))})",
            (call_id, "census-synthetic", self.journal, self.doi, tier, variant, 0, kind, len(labels), json.dumps(labels),
             C.INSTRUMENT_ID, C.instrument().digest, "rev", MODEL, MODEL if item_state == "completed" else None, "fp",
             "s" * 64, "a" * 64, "b" * 64, "c" * 64, "d" * 64, 3000, 1, 1, 1, 0, "stop", 0, 1, 0.1, error, 0, 0, 0,
             "2026-11-01T00:00:00Z"))
        broker = transport == "broker"
        self.connection.execute(
            f"INSERT INTO call_transport({', '.join(scores_db.TRANSPORT_COLUMNS)}) "
            f"VALUES({', '.join('?' * len(scores_db.TRANSPORT_COLUMNS))})",
            (call_id, transport, None, request, "principal" if broker else None, job if broker else None,
             call_id if broker else None, None, None, request if broker else None, None, None, 200 if broker else None))
        item = {"id": call_id, "state": item_state, "requestSha256": request, "servedModel": MODEL}
        if output is not ABSENT:
            item["output"] = output
        if item_state == "completed":
            item["outputDigest"] = output_digest(output)[0]
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

    def responses(self, path: Path, *, drop=()) -> Path:
        with path.open("w", encoding="utf-8") as handle:
            for (job, item_id), item in self.items.items():
                if item_id in drop:
                    continue
                exported = {"jobId": job, "itemId": item_id,
                            **{key: item[key] for key in ("state", "output", "outputDigest", "requestSha256",
                                                           "servedModel") if key in item}}
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
    census.call("c-refused", ["15", "16"], ABSENT, tier="full-text", kind="full-text", item_state="failed",
                error="refused: broker INVALID_REQUEST (HTTP 400)")
    census.call("c-empty", ["17"], "", tier="full-text", kind="full-text", error="empty content; finish=stop")
    census.call("c-redacted", ["18"], line("18", "F", quote="secret quote"), redact={"18"})
    census.call("c-sensitivity", ["1"], line("1"), variant="no-truncation")
    census.history("19", call_id="c-orphan", position=1, record={"category": "true", "support_verdict": "direct-support",
                                                                  "escalate": 0, "quote": ""})
    census.history("20", outcome="deterministic", record={"category": None, "escalate": 0, "quote": ""})
    census.history("21", outcome="invalid", position=3)
    census.commit()
    return census


EXPECTED = {"replayed": 9, "mismatched": 2, "outsideReplay": 2, "unavailable": 5, "digestFailures": 4, "calls": 14,
            "entries": 22}
CALL_OUTCOMES = {"c-match": "replayed", "c-repair": "replayed", "c-mismatch": "mismatched", "c-unrecorded": "mismatched",
                 "c-output-digest": "digest-failure", "c-request-digest": "digest-failure", "c-served": "digest-failure",
                 "c-missing-item": "unavailable", "c-direct": "unavailable", "c-refused": "unavailable",
                 "c-empty": "replayed", "c-redacted": "replayed", "c-sensitivity": "replayed", "c-orphan": "unavailable"}


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
    """The run principal's socket as far as replay reads it: GET /jobs/<jobId>/results?after&limit, one result per page
    (so paging is exercised), WORKER_NOT_FOUND for a job the principal does not hold."""

    def __init__(self, directory: Path, items: dict[tuple[str, str], dict]):
        self.path = str(directory / "principal.sock")
        self.jobs: dict[str, list[dict]] = {}
        for (job, _), item in sorted(items.items()):
            self.jobs.setdefault(job, []).append(item)
        self.requests: list[str] = []
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

        self.server = Server(self.path, Handler)
        os.chmod(self.path, 0o600)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def route(self, target: str) -> tuple[int, dict]:
        url = urllib.parse.urlsplit(target)
        parts = url.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "jobs" or parts[2] != "results":
            return 404, {"error": "A principal socket serves only its worker routes.", "code": "UNKNOWN_PRINCIPAL_ROUTE"}
        results = self.jobs.get(urllib.parse.unquote(parts[1]))
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
        return 200, {"jobId": parts[1], "results": page, "nextCursor": cursor, "hasMore": cursor <= len(results)}


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
        self.assertEqual({"outside-replay"}, {row["outcome"] for row in by_label["20"] + by_label["21"]})
        self.assertEqual([None, None], [row["call_id"] for row in by_label["20"] + by_label["21"]])
        # Repair and sensitivity calls replay like first calls; full-text missing entries replay from an empty answer.
        self.assertEqual("replayed", by_label["4"][0]["outcome"])
        self.assertEqual({"replayed"}, {row["outcome"] for row in by_label["1"]})
        self.assertEqual(["no-truncation", "primary"], sorted(row["variant"] for row in by_label["1"]))
        self.assertEqual("replayed", by_label["17"][0]["outcome"])
        detail = {call_id: json.loads(row["detail_json"]) for call_id, row in calls.items()}
        self.assertFalse(detail["c-request-digest"]["checks"]["requestDigest"])
        self.assertFalse(detail["c-output-digest"]["checks"]["outputDigest"])
        self.assertFalse(detail["c-served"]["checks"]["servedModel"])
        self.assertIn("the broker item is failed", detail["c-refused"]["reasons"][0])
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
            self.assertEqual((C.instrument().digest, "responses", "census-synthetic"),
                             (meta["instrumentDigest"], meta["source"], meta["censusRunId"]))
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
        code, _, error = cli("study", "replay", "--census", str(frozen), "--responses", str(self.responses))
        self.assertEqual((2, True), (code, "--output" in error))
        code, from_frozen, _ = cli("study", "replay", "--census", str(frozen), "--responses", str(self.responses),
                                   "--output", str(self.root / "frozen-replay.sqlite3"))
        self.assertEqual((3, EXPECTED), (code, {key: from_frozen[key] for key in EXPECTED}))

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

    def test_socket_failures_are_unavailable_items(self):
        from ocsl_review.census_broker import BrokerRefused, BrokerUnavailable

        class Client:
            def __init__(self):
                self.calls = 0

            def request(self, method, route):
                self.calls += 1
                if "gone" in route:
                    raise BrokerRefused(404, "WORKER_NOT_FOUND")
                if "down" in route:
                    raise BrokerUnavailable("broker socket: ConnectionRefusedError")
                return {"results": [{"id": "i", "state": "completed"}], "nextCursor": 1, "hasMore": False}

        client = Client()
        items = SocketItems(client=client, workers=1, sleep=lambda seconds: None)
        fetched = items.fetch([("gone", "i"), ("ok", "i"), ("ok", "missing")])
        self.assertIn("WORKER_NOT_FOUND", fetched[("gone", "i")].reason)
        self.assertEqual("completed", fetched[("ok", "i")].item["state"])
        self.assertEqual(Fetched(None, "the broker job holds no item of the call"), fetched[("ok", "missing")])
        self.assertEqual(3, client.calls, "a refusal is not asked again")
        # A socket that stops answering is an outage, never a call without a stored response.
        with self.assertRaises(TransientError):
            items.fetch([("down", "i")])
        self.assertEqual(3 + 3, client.calls)


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
            corpus_facts.export_facts(frozen, root / "facts", ["J1", "J2"], b=50, seed=3, code_revision="rev-test")
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
            self.assertTrue(corpus_facts.verify_export(root / "facts" / "facts.json")["ok"])
            bundle = json.loads((root / "facts" / "facts.json").read_text())
            bundle["facts"]["census.j1.replay.replayed"]["value"] = 3
            mutated = root / "facts" / "mutated.json"
            mutated.write_text(json.dumps(bundle))
            with self.assertRaises(Exception):
                corpus_facts.verify_export(mutated)

    def test_no_replay_facts_without_a_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_scores(root / "work.sqlite3")
            scores_db.freeze(root / "work.sqlite3", root / "scores.sqlite3")
            corpus_facts.export_facts(root / "scores.sqlite3", root / "facts", ["J1"], b=20, seed=1, code_revision="r")
            facts = json.loads((root / "facts" / "facts.json").read_text())["facts"]
            self.assertFalse([key for key in facts if ".replay." in key])


class RunnerReplayTests(BrokerTestCase):
    """A census the runner recorded through the mock broker replays from the items the broker holds."""

    def census(self):
        self.broker.install_census()
        run = self.run_census()
        self.broker.judge.drop_first = {"2", "9"}      # these labels are decided by repair calls
        self.broker.judge.never = {"11"}              # no parseable line after the repair: invalid, without a call
        self.prepare(run)
        self.assertEqual("completed", run.run_abstract()["state"])
        self.assertEqual("completed", run.run_full_text(
            fetch_xml=lambda pmcid: (FIXTURES / "full_texts" / "ft2.xml").read_bytes())["state"])
        self.assertEqual("completed", run.run_replicate(entries=5)["state"])
        run.close()

    def replay(self, *extra):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["study", "replay", "--census", str(self.database), "--broker-socket", self.broker.path, *extra])
        return code, json.loads(buffer.getvalue())

    def test_every_verdict_of_a_broker_run_replays_repair_calls_included(self):
        self.census()
        history = self.rows("SELECT * FROM history")
        with_call = [row for row in history if row["call_id"]]
        self.assertTrue(any(row["tier"] == "full-text" for row in with_call))
        self.assertTrue(any(row["variant"] == "replicate" for row in with_call))
        repairs = {row["call_id"] for row in self.rows("SELECT call_id FROM calls WHERE kind='repair'")}
        self.assertTrue(repairs and repairs & {row["call_id"] for row in with_call})
        before, posts = len(self.broker.upstream), self.broker.calls_to_route("POST", "/")
        code, summary = self.replay()
        self.assertEqual(0, code, summary)
        self.assertEqual((len(with_call), 0, 0, 0), (summary["replayed"], summary["mismatched"], summary["unavailable"],
                                                     summary["digestFailures"]))
        self.assertEqual(len(history) - len(with_call), summary["outsideReplay"])
        self.assertEqual(len({row["call_id"] for row in with_call}), summary["calls"])
        self.assertEqual(before, len(self.broker.upstream), "no model call")
        self.assertEqual(posts, self.broker.calls_to_route("POST", "/"), "replay submits and resumes nothing")
        invalid = self.rows("SELECT outcome FROM replay_entries WHERE label='11' AND tier='abstract' AND variant='primary'")
        self.assertEqual([{"outcome": "outside-replay"}], invalid)

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
        entries = len(self.rows("SELECT 1 FROM history WHERE call_id=?", call["call_id"]))
        self.assertEqual((3, entries, 0), (code, summary["digestFailures"], summary["mismatched"]))
        with self.broker.lock:
            result["outputDigest"] = output_digest(result["output"])[0]
        code, summary = self.replay("--output", str(self.root / "rewritten.sqlite3"))
        self.assertEqual((3, 0, 1), (code, summary["digestFailures"], summary["mismatched"]))
        differs = rows(self.root / "rewritten.sqlite3", "SELECT replayed_json FROM replay_entries WHERE outcome='mismatched'")
        self.assertIn("category", json.loads(differs[0]["replayed_json"])["differs"])


if __name__ == "__main__":
    unittest.main()
