# SPDX-License-Identifier: Apache-2.0
"""citation-census@1 execution against a local mock judge (no real model endpoint), scores database and facts."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from ocsl_review import citation_census as C
from ocsl_review import estimands, scores_db
from ocsl_review.census_runner import CensusRun, HttpChatTransport
from ocsl_review.errors import ConfidentialityError, ValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "citation_census"
JOURNAL = "Synthetic Journal of Examples"
MODEL = "nvidia/Qwen3.8-Flash-Next-NVFP4"


class MockJudge:
    """Answers chat-completions requests from the labels and abstracts in the user message."""

    def __init__(self):
        self.requests = []
        self.drop_first = set()
        self.never = set()
        self.fail_next = 0
        self.model = MODEL
        self.lock = threading.Lock()

    @staticmethod
    def items(user):
        blocks = re.split(r"^### id=", user, flags=re.M)[1:]
        result = []
        for block in blocks:
            label, _, rest = block.partition("\n")
            evidence = re.search(r"^(?:ABSTRACT: |FULL TEXT OF THE CITED WORK \(\d+ characters\):\n)(.*)$", rest, re.M)
            result.append((label.strip(), evidence.group(1) if evidence else ""))
        return result

    def answer(self, payload):
        user = payload["messages"][1]["content"]
        repair = user.split("\n")[1] == C.repair_note()
        full_text = "FULL TEXT OF THE CITED WORK" in user
        lines = []
        for label, evidence in self.items(user):
            if label in self.never or (label in self.drop_first and not repair and not full_text):
                continue
            number = int(re.sub(r"\D", "", label) or 0)
            if full_text:
                category, escalate, quote = ("T", 0, "") if number % 2 else ("F", 0, " ".join(evidence.split()[:4]))
            elif number % 5 == 0:
                category, escalate, quote = "N", 1, ""
            elif number % 4 == 0:
                category, escalate, quote = "F", 0, " ".join(evidence.split()[:4])
            elif number % 7 == 0:
                category, escalate, quote = "F", 1, " ".join(evidence.split()[:3])
            else:
                category, escalate, quote = "T", 0, ""
            lines.append(json.dumps({"l": label, "c": category, "v": "direct" if category == "T" else "contra",
                                     "q": quote, "e": escalate, "r": "mock rationale"}))
        return "\n".join(lines)


def start_server(judge: MockJudge):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with judge.lock:
                judge.requests.append({"path": self.path, "sha256": hashlib.sha256(body).hexdigest(),
                                       "payload": json.loads(body), "authorization": self.headers.get("Authorization")})
                failing = judge.fail_next > 0
                if failing:
                    judge.fail_next -= 1
            if failing:
                self.send_response(500)
                self.end_headers()
                return
            payload = json.loads(body)
            response = {"model": judge.model, "system_fingerprint": "fp-mock",
                        "choices": [{"message": {"content": judge.answer(payload)}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                  "prompt_tokens_details": {"cached_tokens": 64}}}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def works():
    data = json.loads((FIXTURES / "review_works.json").read_text())["works"]
    return {key: {k: v for k, v in work.items() if k != "label"} for key, work in data.items()}


def record(doi="10.5555/synthetic.review.1", journal=JOURNAL, year=2021, stratum="Review"):
    return {"journal": journal, "doi": doi, "title": "Synthetic review of zeta-kinase signalling in breast tumours",
            "year": year, "stratum": stratum, "provenanceClass": "public",
            "document": {"path": "review.xml", "kind": "jats"}, "works": works()}


def numbered_review(n: int) -> bytes:
    paragraphs = "".join('<p>Claim number %d is fictional <xref ref-type="bibr" rid="B%d">%d</xref>.</p>' % (i, i, i)
                         for i in range(1, n + 1))
    refs = "".join('<ref id="B%d"><label>%d</label><mixed-citation>Author %d. Fictional source %d. '
                   '<pub-id pub-id-type="doi">10.5555/n%d</pub-id></mixed-citation></ref>' % (i, i, i, i, i)
                   for i in range(1, n + 1))
    return ('<article><front><article-meta><title-group><article-title>Numbered synthetic review</article-title>'
            '</title-group></article-meta></front><body><sec><title>Body</title>%s</sec></body><back><ref-list>%s'
            '</ref-list></back></article>' % (paragraphs, refs)).encode()


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.judge = MockJudge()
        self.server = start_server(self.judge)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        self.database = self.root / "census.sqlite3"

    def run_census(self, **kwargs):
        transport = HttpChatTransport(self.base, api_key_env="OCSL_TEST_JUDGE_KEY", on_premises=kwargs.pop("on_premises", False))
        run = CensusRun(self.database, seed=1234, transport=transport, concurrency=4, sleep=lambda s: None, **kwargs)
        self.addCleanup(run.close)
        return run

    def prepare(self, run, *records):
        for item in records or (record(),):
            run.prepare_review(item, (FIXTURES / "review.xml").read_bytes())

    def rows(self, sql, *params):
        with closing(sqlite3.connect(self.database)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, params)]


class PreparationTests(RunnerTestCase):
    def test_prepare_records_entries_loci_identity_and_deterministic_rows(self):
        run = self.run_census()
        result = run.prepare_review(record(), (FIXTURES / "review.xml").read_bytes())
        self.assertEqual(("prepared", 17, 15, 0), (result["state"], result["entries"], result["judgeable"], result["uncovered"]))
        classes = {r["label"]: r["abs_class"] for r in self.rows("SELECT label, abs_class FROM citations")}
        self.assertEqual("not_cited", classes["16"])
        self.assertEqual("not_cited", classes["17"])
        self.assertIsNone(classes["1"])
        identity = {r["label"]: (r["identity"], r["map_method"]) for r in self.rows("SELECT * FROM citations")}
        self.assertEqual(("resolved", "markup-pmid"), identity["3"])
        self.assertEqual(("resolved", "printed-doi"), identity["12"])
        sent = self.rows("SELECT COUNT(*) AS n FROM loci WHERE label='1' AND sent=1")[0]["n"]
        self.assertEqual(3, sent)
        self.assertEqual(4, self.rows("SELECT n_loci FROM citations WHERE label='1'")[0]["n_loci"])
        cut = {r["label"]: r["abs_cut"] for r in self.rows("SELECT label, abs_cut FROM citations WHERE abs_cut IS NOT NULL")}
        self.assertEqual(1, cut["1"])
        self.assertEqual(0, cut["2"])
        deterministic = self.rows("SELECT COUNT(*) AS n FROM history WHERE outcome='deterministic'")[0]["n"]
        self.assertEqual(2, deterministic)
        review = self.rows("SELECT * FROM reviews")[0]
        self.assertEqual(("Italy", "breast", 0, "pending"), (review["country"], review["topic"], review["uncovered"],
                                                             review["abstract_state"]))

    def test_prepare_is_idempotent_and_documents_are_immutable(self):
        run = self.run_census()
        document = (FIXTURES / "review.xml").read_bytes()
        run.prepare_review(record(), document)
        self.assertEqual("already-prepared", run.prepare_review(record(), document)["state"])
        with self.assertRaises(ValidationError):
            run.prepare_review(record(), document + b"\n")
        self.assertEqual("unavailable", run.prepare_review(record(doi="10.5555/unreadable"), None)["state"])
        review = self.rows("SELECT provenance_class, abstract_state FROM reviews WHERE doi='10.5555/unreadable'")[0]
        self.assertEqual(("unavailable", "completed"), (review["provenance_class"], review["abstract_state"]))

    def test_entitled_reviews_reach_only_on_premises_judges(self):
        entitled = {**record(), "provenanceClass": "entitled"}
        run = self.run_census()
        run.prepare_review(entitled, (FIXTURES / "review.xml").read_bytes())
        with self.assertRaises(ConfidentialityError):
            run.run_abstract()
        self.assertEqual([], self.judge.requests)
        run.close()
        local = self.run_census(on_premises=True)
        self.assertEqual("completed", local.run_abstract()["state"])

    def test_run_metadata_is_pinned(self):
        run = self.run_census()
        run.close()
        with self.assertRaises(ValidationError):
            CensusRun(self.database, seed=99)


class AbstractTierTests(RunnerTestCase):
    def test_end_to_end_abstract_tier(self):
        os.environ["OCSL_TEST_JUDGE_KEY"] = "local-test-value"
        self.addCleanup(os.environ.pop, "OCSL_TEST_JUDGE_KEY", None)
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual(("completed", 1, 15, 0), (result["state"], result["reviews"], result["accepted"], result["invalid"]))
        calls = self.rows("SELECT * FROM calls")
        self.assertEqual(1, len(calls))
        call = calls[0]
        self.assertEqual(hashlib.sha256(C.system_prompt(JOURNAL).encode()).hexdigest(), call["system_sha256"])
        self.assertEqual(self.judge.requests[0]["sha256"], call["body_sha256"])
        self.assertEqual("/v1/chat/completions", self.judge.requests[0]["path"])
        self.assertEqual("Bearer local-test-value", self.judge.requests[0]["authorization"])
        payload = self.judge.requests[0]["payload"]
        self.assertEqual((MODEL, 10500, 0.0, "none"), (payload["model"], payload["max_tokens"], payload["temperature"],
                                                        payload["reasoning_effort"]))
        self.assertEqual((MODEL, "fp-mock", 64, 1), (call["served_model"], call["fingerprint"], call["cached_tokens"], call["attempts"]))
        entries = {r["label"]: r for r in self.rows("SELECT * FROM citations")}
        self.assertEqual("sure_supported", entries["1"]["abs_class"])
        self.assertEqual(("false", "sure_not_supported", 1, 1), (entries["4"]["abs_category"], entries["4"]["abs_class"],
                                                                 entries["4"]["abs_quote_present"],
                                                                 entries["4"]["abs_quote_in_evidence"]))
        self.assertEqual(("not_assessable", 1, "unsure"), (entries["5"]["abs_category"], entries["5"]["abs_escalated"],
                                                           entries["5"]["abs_class"]))
        self.assertEqual(("false", 1, "unsure"), (entries["7"]["abs_category"], entries["7"]["abs_escalated"],
                                                  entries["7"]["abs_class"]))
        self.assertEqual(list(range(1, 16)), [entries[str(i)]["abs_position"] for i in range(1, 16)])
        status = run.status()
        self.assertEqual((0, 0, 0.0), (status["invalidOutputs"], status["duplicateJudgments"], status["invalidShare"]))
        self.assertEqual(status["abstractCalls"], status["receiptsWithFingerprint"])
        review = self.rows("SELECT * FROM reviews")[0]
        self.assertEqual(("completed", 15, 2), (review["abstract_state"], review["n_judged"], review["n_not_cited"]))

    def test_repair_pass_recovers_missing_labels(self):
        self.judge.drop_first = {"2", "9"}
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        calls = self.rows("SELECT kind, n_items, labels_json, n_missing FROM calls ORDER BY created_at, kind")
        self.assertEqual(["first", "repair"], sorted(c["kind"] for c in calls))
        repair = next(c for c in calls if c["kind"] == "repair")
        self.assertEqual(["2", "9"], json.loads(repair["labels_json"]))
        user = self.judge.requests[1]["payload"]["messages"][1]["content"]
        self.assertEqual(C.repair_note(), user.split("\n")[1])
        self.assertEqual(3000, self.judge.requests[1]["payload"]["max_tokens"])
        entries = {r["label"]: r for r in self.rows("SELECT * FROM citations")}
        self.assertEqual(1, entries["2"]["model_judged"])
        self.assertEqual(2, entries["2"]["abs_position"])
        positions = {r["label"]: r["position"] for r in self.rows("SELECT label, position FROM history WHERE outcome='accepted'")}
        self.assertEqual((1, 2), (positions["2"], positions["9"]))

    def test_invalid_after_repair(self):
        self.judge.never = {"3"}
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual((14, 1), (result["accepted"], result["invalid"]))
        entry = self.rows("SELECT * FROM citations WHERE label='3'")[0]
        self.assertEqual((0, "unsure", "invalid:no-parseable-output"), (entry["model_judged"], entry["abs_class"], entry["abs_basis"]))
        self.assertAlmostEqual(1 / 15, run.status()["invalidShare"])

    def test_transport_failure_is_retried_then_repaired(self):
        self.judge.fail_next = 3
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual((1, 15), (result["transportFailures"], result["accepted"]))
        calls = {c["kind"]: c for c in self.rows("SELECT * FROM calls")}
        self.assertEqual((3, "HTTP 500"), (calls["first"]["attempts"], calls["first"]["error"]))
        self.assertEqual(15, calls["repair"]["n_items"])
        self.assertIsNone(calls["repair"]["error"])
        self.assertEqual(4, len(self.judge.requests))

    def test_model_drift_pauses_and_resumes(self):
        self.judge.model = "another/model"
        run = self.run_census()
        self.prepare(run)
        result = run.run_abstract()
        self.assertEqual("paused", result["state"])
        self.assertIn("model_drift", result["reason"])
        self.assertEqual("pending", self.rows("SELECT abstract_state FROM reviews")[0]["abstract_state"])
        self.assertEqual(0, self.rows("SELECT COUNT(*) AS n FROM citations WHERE model_judged=1")[0]["n"])
        self.assertEqual("paused:model_drift", run.status()["runState"])
        self.judge.model = MODEL
        result = run.run_abstract()
        self.assertEqual(("completed", 15), (result["state"], result["accepted"]))
        errors = [c["error"] for c in self.rows("SELECT error FROM calls ORDER BY created_at")]
        self.assertIn("model_drift", errors)
        self.assertEqual(0, run.status()["duplicateJudgments"])

    def test_batches_of_forty_in_bibliography_order(self):
        run = self.run_census()
        works = {"n%d" % i: {"doi": "10.5555/n%d" % i, "title": "Fictional source %d" % i, "venue": "Synth", "year": 2020,
                             "abstract": "Abstract %d." % i} for i in range(1, 46)}
        run.prepare_review({"journal": JOURNAL, "doi": "10.5555/numbered", "document": {"kind": "jats"}, "works": works},
                           numbered_review(45))
        run.run_abstract()
        calls = sorted(self.rows("SELECT batch, n_items, labels_json, max_tokens FROM calls"), key=lambda c: c["batch"])
        self.assertEqual([(0, 40, 28000), (1, 5, 3500)], [(c["batch"], c["n_items"], c["max_tokens"]) for c in calls])
        self.assertEqual([str(i) for i in range(1, 41)], json.loads(calls[0]["labels_json"]))
        positions = {r["label"]: (r["abs_batch"], r["abs_position"]) for r in self.rows("SELECT label, abs_batch, abs_position FROM citations")}
        self.assertEqual((1, 5), positions["45"])


def full_text_fetcher(requests):
    def fetch(pmcid):
        requests.append(pmcid)
        return (FIXTURES / "full_texts" / "ft2.xml").read_bytes()
    return fetch


class FullTextTierTests(RunnerTestCase):
    def test_full_text_tier(self):
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        fetched = []
        result = run.run_full_text(fetch_xml=full_text_fetcher(fetched), fetch_workers=2)
        # Escalated: 5, 10, 15 (N, e=1) and 7, 14 (F, e=1). Works with an odd number carry a PMCID.
        self.assertEqual((5, 3, 1), (result["escalated"], result["retrieved"], result["calls"]))
        self.assertEqual(sorted(fetched), ["PMC900005", "PMC900007", "PMC900015"])
        rows = {r["label"]: r for r in self.rows("SELECT * FROM citations WHERE abs_escalated=1")}
        self.assertEqual("no-pmcid", rows["10"]["ft_retrieval"])
        self.assertEqual("no-pmcid", rows["14"]["ft_retrieval"])
        for label in ("5", "7", "15"):
            self.assertEqual(("europe-pmc-fulltextxml", "sure_supported"), (rows[label]["ft_retrieval"], rows[label]["ft_class"]))
            self.assertLessEqual(rows[label]["ft_window_characters"], 11000)
        request = self.judge.requests[-1]["payload"]
        self.assertEqual(("low", 3000), (request["reasoning_effort"], request["max_tokens"]))
        self.assertIn(C.full_text_note(), request["messages"][1]["content"])
        evidence = self.rows("SELECT unit, route, work_key, identity_version FROM evidence WHERE unit='full-text' ORDER BY route")
        self.assertEqual({"source", "window"}, {e["route"] for e in evidence})
        review = self.rows("SELECT ft_read, ft_sure_supported, full_text_state FROM reviews")[0]
        self.assertEqual((3, 3, "completed"), (review["ft_read"], review["ft_sure_supported"], review["full_text_state"]))
        again = run.run_full_text(fetch_xml=full_text_fetcher(fetched))
        self.assertEqual(0, again["escalated"])

    def test_entitled_text_reaches_only_on_premises_judges(self):
        def entitled(work):
            return (b"Entitled synthetic text about zeta-kinase.", "text/plain") if work["work_key"] == "work-10" else None
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        run.run_full_text(fetch_xml=lambda pmcid: None, entitled=entitled)
        rows = {r["label"]: r for r in self.rows("SELECT label, ft_retrieval, ft_class FROM citations WHERE abs_escalated=1")}
        self.assertEqual("entitled-blocked", rows["10"]["ft_retrieval"])
        self.assertEqual("fetch-failed", rows["5"]["ft_retrieval"])

    def test_entitled_text_on_premises(self):
        def entitled(work):
            return (b"Entitled synthetic text about zeta-kinase.", "text/plain") if work["work_key"] == "work-10" else None
        run = self.run_census(on_premises=True)
        self.prepare(run)
        run.run_abstract()
        run.run_full_text(fetch_xml=lambda pmcid: None, entitled=entitled)
        row = self.rows("SELECT ft_retrieval, ft_class, ft_source FROM citations WHERE label='10'")[0]
        self.assertEqual(("entitled", "sure_not_supported", "entitled"), (row["ft_retrieval"], row["ft_class"], row["ft_source"]))
        provenance = self.rows("SELECT provenance_class FROM evidence WHERE unit='full-text' AND route='window'")[0]
        self.assertEqual("entitled", provenance["provenance_class"])


class SensitivityTests(RunnerTestCase):
    def test_no_truncation_reruns_whole_batches(self):
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        self.assertEqual([(JOURNAL, "10.5555/synthetic.review.1", 0)], run.no_truncation_sample(10))
        result = run.run_no_truncation(entries=10)
        self.assertEqual(("completed", 1, 15), (result["state"], result["batches"], result["accepted"]))
        variant = self.rows("SELECT * FROM calls WHERE variant='no-truncation'")
        self.assertEqual(1, len(variant))
        user = self.judge.requests[-1]["payload"]["messages"][1]["content"]
        abstract_line = next(line for line in user.split("\n") if line.startswith("ABSTRACT: Background"))
        self.assertGreater(len(abstract_line) - len("ABSTRACT: "), 2200)
        primary = self.rows("SELECT COUNT(*) AS n FROM history WHERE variant='primary' AND outcome='accepted'")[0]["n"]
        self.assertEqual(15, primary)
        with closing(sqlite3.connect(self.database)) as c:
            comparison = estimands.no_truncation_comparison(c, JOURNAL)
        self.assertEqual(1.0, comparison["all"]["agreement"])
        self.assertEqual(0, run.run_no_truncation(entries=10)["batches"])


class ReplicateTests(RunnerTestCase):
    def test_stratified_replicate(self):
        run = self.run_census()
        self.prepare(run, record(), record(doi="10.5555/second", year=2020))
        run.run_abstract()
        sample = run.replicate_sample(5)
        self.assertEqual({"10.5555/synthetic.review.1", "10.5555/second"}, {doi for _, doi, _ in sample})
        self.assertEqual(sample, run.replicate_sample(5))
        result = run.run_replicate(entries=5)
        self.assertEqual(("completed", 2, 30), (result["state"], result["batches"], result["accepted"]))
        with closing(sqlite3.connect(self.database)) as c:
            agreement = estimands.variant_agreement(c, JOURNAL)
        self.assertEqual((30, 1.0, 1.0, 1.0), (agreement["entries"], agreement["categoryAgreement"],
                                               agreement["classAgreement"], agreement["trueFalseKappa"]))
        self.assertEqual(0, run.run_replicate(entries=5)["batches"])
        primary = self.rows("SELECT COUNT(*) AS n FROM history WHERE variant='primary' AND outcome='accepted'")[0]["n"]
        self.assertEqual(30, primary)
        with self.assertRaises(ValidationError):
            run.replicate_sample(0)


class FreezeExportTests(RunnerTestCase):
    def test_freeze_and_export_a_run(self):
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        run.run_full_text(fetch_xml=full_text_fetcher([]))
        run.close()
        frozen = self.root / "frozen" / "scores.sqlite3"
        report = scores_db.freeze(self.database, frozen)
        self.assertFalse(os.access(frozen, os.W_OK))
        self.assertFalse(Path(str(frozen) + "-wal").exists())
        self.assertGreater(report["scored"]["works"], 0)
        with closing(scores_db.open_frozen(frozen)) as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM evidence WHERE text IS NOT NULL").fetchone()[0])
            self.assertEqual("1", scores_db.meta(c)["frozen"])
            self.assertGreater(c.execute("SELECT COUNT(*) FROM data_dictionary").fetchone()[0], 100)
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM review_scores").fetchone()[0])
        with self.assertRaises(ValidationError):
            scores_db.freeze(self.database, frozen)
        manifest = scores_db.export(frozen, self.root / "export")
        names = {f["path"] for f in manifest["files"]}
        self.assertIn("citations.tsv", names)
        self.assertIn("data_dictionary.tsv", names)
        try:
            import pyarrow  # noqa: F401
            self.assertIn("citations.parquet", names)
        except ImportError:
            self.assertTrue(manifest["parquet"].startswith("unavailable"))


if __name__ == "__main__":
    unittest.main()
