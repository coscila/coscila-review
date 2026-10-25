# SPDX-License-Identifier: Apache-2.0
"""claim-check@1: attribution semantics, 0..n citations per claim, policy, ledger and judges.

Synthetic fixtures only; no model endpoint or network service is contacted.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ocsl_review.claim_check import (
    LITERATURE_SCHEMA,
    ConfidentialPolicy,
    EgressLedger,
    FunctionJudge,
    HttpJudge,
    RunStore,
    ensure_schema,
    extract_claims,
    instrument_definition,
    instrument_digest,
    select_evidence,
)
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.retrieval import EvidenceIndex, load_store
from ocsl_review.screen import run_screen

import claim_check_fixtures as F

FORMATS = {
    "markdown": ("draft.md", F.attribution_markdown),
    "docx": ("draft.docx", F.attribution_docx),
    "jats": ("draft.xml", F.attribution_jats),
}


class AttributionCaseTests(unittest.TestCase):
    """The three attribution cases, each in Markdown, DOCX and JATS."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.works = load_store(F.write_store(root / "store.jsonl"))
        cls.results = {}
        for name, (file_name, build) in FORMATS.items():
            path = root / file_name
            content = build()
            path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content, encoding="utf-8")
            judge = F.QuoteJudge()
            report = run_screen(path, run_root=root / f"run-{name}", works=cls.works, judge=judge, reference_year=2026)
            tasks = [item for call in judge.calls for item in json.loads(call[1]["content"])["items"]]
            cls.results[name] = (report, tasks)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def pairs(self, name):
        report, _ = self.results[name]
        return [(item["sentence"], item["reference"]["entryId"]) for item in report["citations"] if item["reference"]]

    def test_a1_each_citation_is_judged_against_its_own_sentence_only(self):
        for name in FORMATS:
            with self.subTest(format=name):
                pairs = self.pairs(name)
                self.assertIn(("Drug X reduced all-cause mortality in adults.", "ref-00001"), pairs)
                self.assertIn(("Metformin reduces hepatic glucose output.", "ref-00002"), pairs)
                self.assertNotIn(("Drug X reduced all-cause mortality in adults.", "ref-00002"), pairs)
                self.assertNotIn(("Metformin reduces hepatic glucose output.", "ref-00001"), pairs)
                _, tasks = self.results[name]
                claims_for = {task["claim"] for task in tasks if task["identifiers"].get("doi") == "10.5555/attr.m"}
                self.assertEqual({"Metformin reduces hepatic glucose output."}, claims_for)

    def test_a2_inner_and_final_citations_are_each_checked_against_the_whole_sentence(self):
        whole = ("Statins lower LDL cholesterol, an effect mediated by HMG-CoA reductase inhibition, and reduce "
                 "cardiovascular events in primary prevention.")
        for name in FORMATS:
            with self.subTest(format=name):
                pairs = self.pairs(name)
                self.assertIn((whole, "ref-00003"), pairs)
                self.assertIn((whole, "ref-00004"), pairs)
                _, tasks = self.results[name]
                sentence_tasks = [task for task in tasks if task["claim"] == whole]
                self.assertEqual({"10.5555/attr.y", "10.5555/attr.c"}, {task["identifiers"]["doi"] for task in sentence_tasks})
                self.assertFalse(any(task["claim"].startswith("an effect mediated") for task in tasks))

    def test_a3_uncited_sentences_get_no_attribution_and_are_flagged_only_when_needed(self):
        for name in FORMATS:
            with self.subTest(format=name):
                report, tasks = self.results[name]
                judged_x = sorted(task["claim"] for task in tasks if task["identifiers"].get("doi") == "10.5555/attr.x")
                self.assertEqual(["Drug X reduced all-cause mortality in adults.", "Drug X reduced mortality in the trial.",
                                  "Drug X was also well tolerated."], judged_x)
                cited_sentences = {item["sentence"] for item in report["citations"]}
                for uncited in F.A3[1:4]:
                    self.assertNotIn(uncited, cited_sentences)
                flagged = [item["sentence"] for item in report["uncitedClaims"]]
                self.assertEqual(["Metformin is the most widely prescribed oral antidiabetic drug worldwide."], flagged)
                self.assertEqual(1, report["summary"]["integrity"].get("uncited-claim"))

    def test_formats_agree(self):
        reference = self.pairs("markdown")
        for name in ("docx", "jats"):
            self.assertEqual(reference, self.pairs(name), name)


class SchemaTests(unittest.TestCase):
    def test_claims_carry_zero_to_n_citations_and_are_append_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "draft.md"
            path.write_text(F.attribution_markdown(), encoding="utf-8")
            run_screen(path, run_root=root / "run", works=load_store(F.write_store(root / "store.jsonl")), judge=F.QuoteJudge())
            store = RunStore(root / "run")
            connection = store.connect()
            counts = dict(connection.execute("SELECT citation_count, count(*) FROM claims GROUP BY citation_count").fetchall())
            self.assertEqual({0: 3, 1: 4, 2: 1}, counts)
            self.assertEqual(6, connection.execute("SELECT count(*) FROM claim_citations").fetchone()[0])
            self.assertEqual(6, connection.execute("SELECT count(*) FROM claim_loci").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM claim_citations WHERE resolution='resolved' AND locus_id IS NULL").fetchone()[0])
            connection.close()
            writable = store.connect(write=True)
            with self.assertRaises(sqlite3.IntegrityError):
                writable.execute("UPDATE claims SET claim_text='changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                writable.execute("DELETE FROM claim_citations")
            writable.close()

    def test_extension_applies_additively_to_an_existing_library(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "corpus.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(LITERATURE_SCHEMA.read_text(encoding="utf-8"))
            before = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ensure_schema(connection)
            ensure_schema(connection)
            after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
            connection.close()
        self.assertTrue(before < after)
        self.assertTrue({"claims", "claim_citations", "retrieval_receipts", "integrity_findings", "work_status_observations",
                         "registry_checks", "sanitization_records", "integrity_datasets"} <= after - before)


class ClaimExtractionTests(unittest.TestCase):
    def test_markers_are_removed_and_context_stays_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text(F.attribution_markdown(), encoding="utf-8")
            claims = extract_claims(ingest_manuscript(path))
        first = claims[0]
        self.assertEqual("Drug X reduced all-cause mortality in adults.", first.text)
        self.assertEqual(["Metformin reduces hepatic glucose output."], first.context)
        self.assertEqual([("ref-00001", "resolved")], [(c.entry_id, c.resolution) for c in first.citations])
        self.assertTrue(all(claim.claim_id.startswith("claim-") for claim in claims))


class EvidenceSelectionTests(unittest.TestCase):
    def test_full_text_window_abstract_and_unavailable(self):
        works = load_store(F.write_store(Path(tempfile.mkdtemp()) / "store.jsonl"))
        with tempfile.TemporaryDirectory() as directory:
            with EvidenceIndex.build(Path(directory) / "index.sqlite3", [u for w in works for u in w.units]) as index:
                full = select_evidence(index, "work-c", "statins reduced cardiovascular events")
                self.assertEqual(("full-text", True), (full.tier, full.complete))
                window = select_evidence(index, "work-c", "statins reduced cardiovascular events", max_characters=120)
                self.assertEqual(("full-text", False), (window.tier, window.complete))
                self.assertEqual(["work-c#p1"], [unit.unit_id for unit in window.units])
                abstract = select_evidence(index, "work-x", "drug X mortality")
                self.assertEqual(("abstract", True), (abstract.tier, abstract.complete))
                missing = select_evidence(index, "work-none", "anything at all")
                self.assertEqual((None, "source evidence is unavailable"), (missing.tier, missing.reason))


class PolicyAndLedgerTests(unittest.TestCase):
    def test_identifiers_only_leave_in_confidential_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EgressLedger(Path(directory) / "egress.jsonl")
            policy = ConfidentialPolicy()
            event = policy.authorize_provider(ledger, "doi.org", "doi_registry_check", {"doi": "10.5555/a"})
            self.assertEqual(("allowed", "identifiers-only"), (event["decision"], event["contentClass"]))
            for arguments in ({"title": "A manuscript title"}, {"query": "statins cholesterol"}, {"doi": "not a doi"}):
                with self.subTest(arguments=arguments), self.assertRaises(ConfidentialityError):
                    policy.authorize_provider(ledger, "api.crossref.org", "search", arguments)
            events = ledger.events()
            self.assertEqual(["allowed", "refused", "refused", "refused"], [e["decision"] for e in events])
            self.assertNotIn("manuscript title", ledger.path.read_text(encoding="utf-8"))
            self.assertTrue(ledger.verify()["valid"])

    def test_only_listed_on_prem_endpoints_receive_text_in_confidential_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EgressLedger(Path(directory) / "egress.jsonl")
            relay = HttpJudge("http://127.0.0.1:18161/v1/chat/completions", "m")
            # An address alone is not a reason to trust a judge: a loopback relay may forward to a hosted service.
            with self.assertRaises(ConfidentialityError):
                ConfidentialPolicy().authorize_model(ledger, relay, payload_sha256="0" * 64, items=1)
            policy = ConfidentialPolicy.from_mapping({"onPremEndpoints": [
                {"url": "http://127.0.0.1:18160/v1/chat/completions", "models": ["local-model"]},
                "https://gpu-node.internal/judges/"]})
            listed = HttpJudge("http://127.0.0.1:18160/v1/chat/completions", "local-model")
            below = HttpJudge("https://gpu-node.internal:443/judges/a/chat/completions", "m")
            lan = HttpJudge("http://10.20.30.40/v1/chat/completions", "m")
            external = HttpJudge("http://127.0.0.1:18160/v1/chat/completions", "m", location="external")
            in_process = FunctionJudge(lambda m, s: "{}", location="on-prem")
            for judge in (listed, below, in_process):
                self.assertEqual("on-prem-model", policy.authorize_model(ledger, judge, payload_sha256="0" * 64,
                                                                         items=1)["destinationClass"])
            for judge in (relay, lan, external):
                with self.subTest(judge=judge.url), self.assertRaises(ConfidentialityError):
                    policy.authorize_model(ledger, judge, payload_sha256="0" * 64, items=1)
            self.assertTrue(policy.served_model_allowed(listed, "local-model"))
            self.assertFalse(policy.served_model_allowed(listed, "external-model"))
            self.assertTrue(policy.served_model_allowed(below, "anything"))
            public = ConfidentialPolicy("public-literature")
            self.assertEqual("external-model", public.authorize_model(ledger, external, payload_sha256="0" * 64,
                                                                      items=1)["destinationClass"])
            events = ledger.events()
            self.assertEqual(["refused", "allowed", "allowed", "allowed", "refused", "refused", "refused", "allowed"],
                             [e["decision"] for e in events])
            self.assertEqual("http://127.0.0.1:18160/v1/chat/completions", events[1]["destination"])
            self.assertEqual("local-model", events[1]["requestedModel"])

    def test_policy_file_digest_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"version": 2, "mode": "confidential-manuscript",
                                        "onPremEndpoints": ["https://gpu-node.internal/v1/"]}), encoding="utf-8")
            policy = ConfidentialPolicy.load(path)
            self.assertTrue(policy.endpoint_on_prem("https://GPU-node.internal:443/v1/chat/completions"))
            self.assertFalse(policy.endpoint_on_prem("http://gpu-node.internal/v1/chat/completions"))
            self.assertFalse(policy.endpoint_on_prem("https://gpu-node.internal/v2/chat/completions"))
            self.assertEqual(64, len(policy.digest))
            self.assertNotEqual(ConfidentialPolicy().digest, policy.digest)
            self.assertEqual(["https://gpu-node.internal:443/v1/"], [e["url"] for e in policy.record()["onPremEndpoints"]])
            for invalid in ({"mode": "anything"}, {"mode": "public-literature", "extra": 1},
                            {"mode": "confidential-manuscript", "onPremHosts": ["gpu-node.internal"]},
                            {"version": 1}, {"onPremEndpoints": ["https://user:secret@host/v1"]},
                            {"onPremEndpoints": ["https://host/v1?key=1"]}, {"onPremEndpoints": ["ftp://host/v1"]}):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(ValidationError):
                        ConfidentialPolicy.load(path)

    def test_ledger_hash_chain_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EgressLedger(Path(directory) / "egress.jsonl")
            for index in range(3):
                ledger.append(destination="doi.org", decision="allowed", index=index)
            self.assertEqual({"valid": True, "events": 3}, {k: v for k, v in ledger.verify().items() if k != "head"})
            reopened = EgressLedger(ledger.path)
            reopened.append(destination="doi.org", decision="allowed", index=3)
            self.assertTrue(reopened.verify()["valid"])
            lines = ledger.path.read_text(encoding="utf-8").splitlines()
            lines[1] = lines[1].replace('"index":1', '"index":9')
            ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual({"valid": False, "brokenAt": 2}, {k: v for k, v in EgressLedger(ledger.path).verify().items() if k != "events"})


class JudgeTests(unittest.TestCase):
    def test_http_judge_talks_to_a_local_mock_endpoint(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"model": "served-model", "choices": [{"message": {"content": '{"items": []}'},
                                                                          "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 7}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            judge = HttpJudge(f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions", "local-model")
            response = judge.complete([{"role": "user", "content": "x"}], settings={"temperature": 0, "max_tokens": 3000})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(("served-model", '{"items": []}', "stop"), (response.model, response.text, response.finish_reason))
        self.assertEqual({"model": "local-model", "messages": [{"role": "user", "content": "x"}], "temperature": 0,
                          "max_tokens": 3000}, received[0])
        self.assertEqual("127.0.0.1", judge.host)
        with self.assertRaises(ValidationError):
            HttpJudge("ftp://host/x", "m")

    def test_instrument_digest_is_stable_and_names_its_parts(self):
        definition = instrument_definition()
        self.assertEqual(instrument_digest(definition), instrument_digest(instrument_definition()))
        self.assertNotEqual(instrument_digest(definition), instrument_digest(instrument_definition(k=7)))
        self.assertEqual("claim-check@1", definition["id"])
        self.assertEqual("citation-support-v2", definition["verdictProtocol"])

    def test_registered_bundle_mirrors_the_run_definition(self):
        from ocsl_review import instruments
        bundle = instruments.load("claim-check@1")
        manifest, definition = bundle.manifest, instrument_definition()
        self.assertEqual(manifest["unit"], definition["unit"])
        self.assertEqual(manifest["files"]["systemPrompt"]["sha256"], definition["promptSha256"])
        self.assertEqual(manifest["files"]["outputSchema"]["sha256"], definition["schemaSha256"])
        self.assertEqual(manifest["validator"]["id"], definition["verdictProtocol"])
        self.assertEqual(manifest["classMap"]["errorCategories"], definition["errorCategories"])
        self.assertEqual(manifest["deterministicRows"]["integrity"], definition["integrity"])
        for tier in manifest["tiers"].values():
            self.assertEqual(tier["evidencePolicy"]["retrieval"], definition["retrieval"])
            self.assertEqual(tier["evidencePolicy"]["sanitizer"], definition["sanitizer"])
            self.assertEqual((tier["geometry"]["itemsPerCall"]["max"], tier["geometry"]["groupedBy"]),
                             (definition["batch"]["maxItems"], definition["batch"]["groupedBy"]))
            self.assertEqual((tier["geometry"]["extractor"], tier["geometry"]["authorYearResolver"]),
                             (definition["extractor"], definition["authorYearResolver"]))
            self.assertEqual({key: tier["request"][key] for key in ("temperature", "maxTokens")}, definition["settings"])
            self.assertEqual(tier["repair"]["passes"], definition["repairPasses"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "draft.md"
            path.write_text(F.attribution_markdown(), encoding="utf-8")
            report = run_screen(path, run_root=root / "run", works=load_store(F.write_store(root / "store.jsonl")),
                                judge=F.QuoteJudge(), reference_year=2026)
        self.assertEqual(report["instrument"]["registryDigest"], bundle.digest)


class VerdictTests(unittest.TestCase):
    def test_repair_pass_contradiction_category_and_unresolved_identity(self):
        markdown = ("# Introduction\n\nDrug X increases all-cause mortality in adults [1]. Metformin reduces hepatic glucose "
                    "output [2]. An unidentified work is cited here [3].\n\n## References\n\n"
                    f"1. {F.REFERENCES['x']}\n2. {F.REFERENCES['m']}\n3. Nobody, Z. (2001). Not in the store. J, 1, 2.\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "draft.md"
            path.write_text(markdown, encoding="utf-8")
            judge = F.QuoteJudge(drop_first=True)
            report = run_screen(path, run_root=root / "run", works=load_store(F.write_store(root / "store.jsonl")), judge=judge)
        by_entry = {item["reference"]["entryId"]: item for item in report["citations"]}
        contradicted = by_entry["ref-00001"]["verdict"]
        self.assertEqual(("contradicted", "contradicts", "check suggested"),
                         (contradicted["supportVerdict"], contradicted["errorCategory"], contradicted["flag"]))
        self.assertEqual("direct-support", by_entry["ref-00002"]["verdict"]["supportVerdict"])
        self.assertEqual(("not judged", "cited work identity is unresolved"),
                         (by_entry["ref-00003"]["verdict"]["state"], by_entry["ref-00003"]["verdict"]["reason"]))
        self.assertEqual(2, len(judge.calls))  # the dropped item was asked again, once
        self.assertEqual(1, report["summary"]["integrity"]["identity-unresolved"])
        self.assertEqual(2, report["summary"]["modelCalls"])


if __name__ == "__main__":
    unittest.main()
