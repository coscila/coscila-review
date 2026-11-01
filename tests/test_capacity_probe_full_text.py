# SPDX-License-Identifier: Apache-2.0
"""Capacity probe, full-text tier: real loci from a label-free projection, prepared, retrieved and sent by the census's
own code, against the local mock judge (no real model endpoint and no Europe PMC request is made)."""
from __future__ import annotations

import http.server
import io
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ocsl_review import capacity_probe as P
from ocsl_review import citation_census as C
from ocsl_review import study_cli
from ocsl_review.census_inputs import SEARCH
from ocsl_review.census_runner import CensusRun, HttpChatTransport, full_text_call
from ocsl_review.cli import main
from ocsl_review.corpus_seed import import_legacy_evidence
from ocsl_review.corpus_store import CorpusRepository
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.evidence import abstract_candidate
from ocsl_review.scholarly import normalize_europe_pmc
from ocsl_review.store_projection import build_projection

from test_capacity_probe import FAST, ProbeCase
from test_census_inputs import epmc, lookup

JOURNAL = "Synthetic Journal of Full Texts"
PREFIX = "10.5555/ftreview."
OTHER_REVIEW = "10.5555/otherjournal.1"          # a retained review of another journal: never selected
REVIEWS = {1: 13, 2: 8, 3: 11}                   # review number -> cited works
STORED_FULL_TEXT = (2, 1)                        # a cited work whose fullTextXML the store holds


def work_doi(review: int, k: int) -> str:
    return f"10.5555/ftwork.{review}.{k}"


def pmcid_of(review: int, k: int) -> str | None:
    """Three works in four carry a PMCID; of those, the ones whose number ends in 5 have no open-access full text."""
    return None if k % 4 == 2 else f"PMC{review}{k:03d}"


def open_access(pmcid: str) -> bool:
    return not pmcid.endswith("5")


def review_jats(review: int, works: int, doi: str | None = None) -> bytes:
    doi = doi or PREFIX + str(review)
    paragraphs = "".join(f'<p>Zeta kinase signalling drives fictional tumour growth in cohort {review}.{k} of the '
                         f'example population <xref ref-type="bibr" rid="B{k}">{k}</xref>.</p>' for k in range(1, works + 1))
    refs = "".join(f'<ref id="B{k}"><label>{k}</label><mixed-citation>Author {k}. Fictional source {k}. '
                   f'<pub-id pub-id-type="doi">{work_doi(review, k)}</pub-id></mixed-citation></ref>'
                   for k in range(1, works + 1))
    return (f'<article><front><article-meta><article-id pub-id-type="doi">{doi}</article-id><title-group>'
            f'<article-title>Full-text synthetic review {review}</article-title></title-group></article-meta></front>'
            f'<body><sec><title>Body</title>{paragraphs}</sec></body><back><ref-list>{refs}</ref-list></back>'
            f'</article>').encode()


def full_text_xml(pmcid: str, doi: str) -> bytes:
    """A cited work's fullTextXML, long enough that the claim-targeted window cuts it."""
    filler = " ".join(["unrelated methods text about buffers and incubation"] * 6)
    paragraphs = "".join(f"<p>Paragraph {n}: {'zeta kinase signalling and tumour growth' if n % 7 == 0 else filler}.</p>"
                         for n in range(80))
    return (f'<article><front><article-meta><article-id pub-id-type="doi">{doi}</article-id>'
            f'<article-id pub-id-type="pmcid">{pmcid}</article-id><title-group><article-title>Cited work {pmcid}'
            f'</article-title></title-group><abstract><p>Abstract of {pmcid}.</p></abstract></article-meta></front>'
            f'<body><sec><title>Results</title>{paragraphs}</sec></body></article>').encode()


def build_store(root: Path) -> dict:
    """An earlier corpus with retained review captures and Europe PMC records of the cited works, projected into
    literature-evidence@2 (the only store the full-text tier opens)."""
    seed_root = root / "operator"
    seed_root.mkdir()
    seed = CorpusRepository.create(seed_root, "corpus", "Earlier question", "earlier", {})
    legacy = seed_root / "legacy"
    (legacy / "work/reviews").mkdir(parents=True)
    (legacy / "work/corpus").mkdir(parents=True)
    database = legacy / "work/reviews/earlier.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("CREATE TABLE works(work_key TEXT,doi TEXT,title TEXT,year INT,abstract TEXT,abstract_source TEXT,"
                         "local_path TEXT,origin_url TEXT,fulltext_format TEXT);"
                         "CREATE TABLE papers(doi TEXT,title TEXT,year INT,path_xml TEXT);"
                         "CREATE TABLE bib_refs(doi TEXT,label TEXT,printed TEXT,work_key TEXT);")
        for review, works in REVIEWS.items():
            path = f"work/corpus/review{review}.xml"
            (legacy / path).write_bytes(review_jats(review, works))
            db.execute("INSERT INTO papers VALUES(?,?,?,?)", (PREFIX + str(review), f"Review {review}", 2021, path))
        (legacy / "work/corpus/other.xml").write_bytes(review_jats(9, 4, doi=OTHER_REVIEW))
        db.execute("INSERT INTO papers VALUES(?,?,?,?)", (OTHER_REVIEW, "Other review", 2021, "work/corpus/other.xml"))
    import_legacy_evidence(seed, database, source_root=legacy)
    for review, works in [*REVIEWS.items(), (9, 4)]:
        for k in range(1, works + 1):
            doi, pmcid = work_doi(review, k), pmcid_of(review, k)
            raw = epmc(doi=doi, abstract=f"Abstract of cited work {review}.{k} on zeta kinase signalling.",
                       title=f"Cited work {review}.{k}", pmcid=pmcid)
            source = SEARCH + "?" + urllib.parse.urlencode(lookup(doi=doi))
            candidate = abstract_candidate(normalize_europe_pmc(raw), source_url=source, retrieved_at="2024-05-01T00:00:00Z")
            work = seed.work({"doi": doi, "title": f"Cited work {review}.{k}", **({"pmcid": pmcid} if pmcid else {})})
            seed.document(work, {"text": candidate["text"], "complete": True, "sourceUrl": source, "provenance": candidate},
                          "abstract", "europe-pmc")
            if (review, k) == STORED_FULL_TEXT:
                seed.document(work, {"body": full_text_xml(pmcid, doi), "mediaType": "application/xml",
                                     "sourceUrl": "https://www.ebi.ac.uk/europepmc/webservices/rest/%s/fullTextXML" % pmcid,
                                     "complete": True, "text": "x", "provider": "europe-pmc"}, "full-text", "europe-pmc")
    seed.library.snapshot("corpus-snapshot")
    projection = root / "datasets" / "literature-evidence-2"
    projection.parent.mkdir()
    build_projection(seed_root / "corpus-snapshot", projection, workers=1)
    return {"projection": projection, "snapshot": seed_root / "corpus-snapshot"}


class Fetcher:
    """Europe PMC stand-in: a fullTextXML for open-access PMCIDs, none (a 404) for the rest; records every request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[str] = []

    def __call__(self, pmcid: str) -> bytes | None:
        with self.lock:
            self.requests.append(pmcid)
        review, k = int(pmcid[3]), int(pmcid[4:])
        return full_text_xml(pmcid, work_doi(review, k)) if open_access(pmcid) else None


class FullTextCase(ProbeCase):
    @classmethod
    def setUpClass(cls):
        cls.store_temp = tempfile.TemporaryDirectory()
        cls.paths = build_store(Path(cls.store_temp.name))

    @classmethod
    def tearDownClass(cls):
        cls.store_temp.cleanup()

    def full_text_settings(self, cells: str = "4x5", **overrides) -> P.ProbeSettings:
        values = {**FAST, "cell_seconds": 30.0, "drain_seconds": 10.0, "tier": "full-text",
                  "store": str(self.paths["projection"]), "review_prefixes": (PREFIX,), "journal": JOURNAL,
                  "review_route": "public", "full_text_entries": 1000, "fetch_workers": 2, **overrides}
        return P.ProbeSettings(base_url=self.url + "/v1", metrics_url=values.pop("metrics_url", None),
                               cells=P.parse_cells([cells]), order="as-given", **values)

    def run_full_text(self, cells: str = "4x5", *, fetcher=None, **overrides) -> dict:
        self.runs += 1
        self.fetcher = fetcher or Fetcher()
        probe = P.CapacityProbe(self.full_text_settings(cells, **overrides), progress=self.events.append, environ={},
                                fetch_xml=self.fetcher)
        self.output = self.root / ("run-%d" % self.runs)
        return probe.run(self.output)

    def first_payloads(self) -> list[dict]:
        return [p for p in self.judge.payloads if len(re.findall(r"^### id=", p["messages"][1]["content"], re.M)) >= 1]


def window_of(user: str, label: str) -> str:
    """The FULL TEXT window of one item in a full-text call's user message."""
    block = re.split(r"^### id=", user, flags=re.M)
    text = next(part for part in block[1:] if part.split("\n", 1)[0] == label)
    return text.split("characters):\n", 1)[1].rsplit("\n\n", 1)[0]


class RequestTests(FullTextCase):
    def test_calls_are_the_census_full_text_calls_over_real_loci(self):
        report = self.run_full_text(review_prefixes=(PREFIX + "1",))
        inputs = report["fullTextInputs"]
        # Review 1 cites 13 works: 10 carry a PMCID, one of those has no open-access full text.
        self.assertEqual((4, 1, 1, 13, 10, 10, 9, 1), (
            inputs["reviews"]["retainedInStore"], inputs["reviews"]["withPrefix"], inputs["reviews"]["used"],
            inputs["entries"]["judgeable"], inputs["entries"]["candidates"], inputs["entries"]["tried"],
            inputs["entries"]["retrieved"], inputs["entries"]["withoutFullText"]))
        self.assertEqual(10, len(self.fetcher.requests))
        self.assertEqual({"5": 1, "4": 1}, inputs["calls"]["byItems"])
        payloads = self.judge.payloads
        self.assertEqual(2, len(payloads))
        for payload in payloads:
            labels = re.findall(r"^### id=(.+)$", payload["messages"][1]["content"], re.M)
            self.assertEqual(C.max_tokens(len(labels), "full-text"), payload["max_tokens"])
            self.assertEqual(("low", 0.0, False, P.ProbeSettings().requested_model()),
                             (payload["reasoning_effort"], payload["temperature"], payload["stream"], payload["model"]))
            self.assertEqual(C.system_prompt(JOURNAL), payload["messages"][0]["content"])
            self.assertEqual(len(labels), payload["messages"][1]["content"].count("FULL TEXT OF THE CITED WORK ("))
        # The census's own full-text pass over the same prepared review sends byte-identical calls.
        probe_bodies = sorted(record["bodySha256"] for record in self.calls())
        copy = self.root / "census-copy.sqlite3"
        shutil.copyfile(self.output / P.FULL_TEXT_INPUTS_NAME, copy)
        with closing(sqlite3.connect(copy)) as db, db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM history WHERE outcome='accepted'").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM citations WHERE model_judged=1").fetchone()[0])
            db.execute("UPDATE citations SET model_judged=1, abs_escalated=1, ft_retrieval=NULL, ft_provenance=NULL "
                       "WHERE cited_pmcid IS NOT NULL AND cited_inbody=1")
        self.judge.bodies.clear()
        run = CensusRun(copy, seed=1, transport=HttpChatTransport(self.url + "/v1"), concurrency=4, sleep=lambda s: None)
        try:
            result = run.run_full_text(fetch_xml=Fetcher(), fetch_workers=1)
        finally:
            run.close()
        self.assertEqual(("completed", 9, 2), (result["state"], result["retrieved"], result["calls"]))
        self.assertEqual(probe_bodies, sorted(self.judge.bodies))

    def test_the_store_first_then_europe_pmc_and_a_cache_for_later_runs(self):
        cache = self.root / "full-text-cache"
        first = self.run_full_text(full_text_cache=str(cache))
        inputs = first["fullTextInputs"]
        self.assertNotIn("PMC2001", self.fetcher.requests, "the store holds that work's fullTextXML")
        self.assertEqual(1, inputs["entries"]["bySource"]["cache-hit-public"])
        fetched, absent = inputs["fullTexts"]["fetched"], inputs["fullTexts"]["absent"]
        self.assertEqual(len(self.fetcher.requests), fetched + absent)
        self.assertEqual(sorted(path.name for path in cache.iterdir()),
                         sorted(f"{p}.xml" if open_access(p) else f"{p}.absent" for p in self.fetcher.requests))
        self.assertTrue(all(oct(path.stat().st_mode)[-3:] == "600" for path in cache.iterdir()))
        bodies = sorted(record["bodySha256"] for record in self.calls())
        second = self.run_full_text(full_text_cache=str(cache))
        self.assertEqual([], self.fetcher.requests, "a later run fetches nothing it asked for before")
        self.assertEqual((fetched, absent), (second["fullTextInputs"]["fullTexts"]["cacheHits"],
                                             second["fullTextInputs"]["fullTexts"]["cachedAbsent"]))
        self.assertEqual(bodies, sorted(record["bodySha256"] for record in self.calls()))

    def test_the_store_must_be_the_label_free_projection(self):
        with self.assertRaisesRegex(ValidationError, "label-free projection"):
            self.run_full_text(store=str(self.paths["snapshot"]))
        self.assertEqual([], self.judge.payloads)
        report = json.loads((self.output / P.REPORT_NAME).read_text())
        self.assertEqual(("failed", "ValidationError"), (report["state"], report["error"]))
        self.assertIn("label-free projection", report["errorMessage"])


class ShareTests(FullTextCase):
    def test_missing_and_malformed_entries_per_call_and_overall(self):
        self.judge.drop_multi_labels = {"3"}
        self.judge.malformed_labels = {"4"}
        report = self.run_full_text()
        cell = report["cells"][0]
        full_text = cell["fullText"]
        entries = full_text["entries"]
        self.assertEqual((self.judge.dropped_lines, self.judge.malformed_lines), (entries["missing"], entries["malformed"]))
        self.assertGreater(entries["missing"], 0)
        self.assertGreater(entries["malformed"], 0)
        self.assertEqual(entries["entries"], entries["valid"] + entries["missing"] + entries["malformed"])
        self.assertEqual(round((entries["missing"] + entries["malformed"]) / entries["entries"], 5), entries["invalidShare"])
        self.assertEqual(entries["invalidShare"], cell["invalidOutputShare"])
        rows = full_text["byItemsPerCall"]
        for key in ("entries", "valid", "missing", "malformed"):
            self.assertEqual(entries[key], sum(row[key] for row in rows.values()))
        records = self.calls()
        self.assertTrue(all(record["kind"] == "first" for record in records))
        self.assertEqual(len(records), report["fullTextInputs"]["calls"]["count"])
        self.assertEqual(len(records), self.judge.requests)
        calls = full_text["calls"]
        self.assertEqual(sum(1 for r in records if r["missing"] > r["malformed"]), calls["withMissing"])
        self.assertEqual(sum(1 for r in records if r["malformed"]), calls["withMalformed"])
        self.assertEqual(len(records), sum(calls["byInvalidEntries"].values()))
        self.assertFalse(full_text["census"]["repairPass"])
        self.assertIn("no repair pass at the full-text tier", full_text["census"]["statement"])
        self.assertTrue(cell["timing"]["inputs"]["exhausted"])
        exit_line = report["exitCriterion"]["line"]
        self.assertFalse(report["exitCriterion"]["passes"])
        self.assertIn("FAIL - 4x5: invalid", exit_line)
        self.assertTrue(exit_line.endswith("the author decides in D1."))
        self.assertNotIn("hypothetical", exit_line)
        self.assertIn("a probe setting and no census one", report["allowedConcurrency"]["line"])
        markdown = (self.output / P.MARKDOWN_NAME).read_text()
        self.assertIn("| 4x5 | all |", markdown)
        self.assertIn("Malformed (rejected line)", markdown)
        self.assertIn("no repair pass at the full-text tier", markdown)
        self.assertIn("Inputs: real loci from the store", markdown)

    def test_a_hypothetical_single_item_resend_is_reported_apart(self):
        self.judge.drop_multi_labels = {"3"}
        self.judge.malformed_labels = {"4"}
        report = self.run_full_text(hypothetical_repair=True)
        full_text = report["cells"][0]["fullText"]
        first = full_text["entries"]
        repair = full_text["hypotheticalRepair"]
        self.assertTrue(repair["hypothetical"])
        self.assertIn("hypothetical: citation-census@1 sends no such call", repair["label"])
        self.assertIn("census_runner.full_text_call([entry]", repair["resend"])
        calls = repair["calls"]
        self.assertEqual((first["invalid"], first["invalid"], 0), (repair["entriesNeedingResend"], calls["sent"],
                                                                    calls["notSent"]))
        self.assertEqual((first["missing"], first["malformed"]), (calls["recovered"], calls["stillInvalid"]))
        self.assertEqual((first["malformed"], 0), (repair["afterRepair"]["invalid"], repair["afterRepair"]["open"]))
        self.assertEqual(first["invalidShare"], repair["beforeRepair"]["share"])
        self.assertEqual(round(first["malformed"] / first["entries"], 5), repair["afterRepair"]["share"])
        # The census's own shares stay those of the first calls.
        self.assertEqual(first["invalidShare"], report["exitCriterion"]["invalidShare"])
        self.assertIn("hypothetical single-item resend (not part of citation-census@1)", report["exitCriterion"]["line"])
        resends = [record for record in self.calls() if record["kind"] == P.HYPOTHETICAL_REPAIR]
        self.assertEqual(calls["sent"], len(resends))
        self.assertTrue(all(record["items"] == 1 for record in resends))
        singles = [p for p in self.judge.payloads if p["messages"][1]["content"].count("### id=") == 1
                   and "ITEMS (1):" in p["messages"][1]["content"]]
        multi = [p for p in self.judge.payloads if p["messages"][1]["content"].count("### id=") > 1]
        self.assertTrue(singles and multi)
        single = singles[0]
        label = re.findall(r"^### id=(.+)$", single["messages"][1]["content"], re.M)[0]
        origin = next(p for p in multi if f"### id={label}\n" in p["messages"][1]["content"])
        self.assertEqual(window_of(origin["messages"][1]["content"], label),
                         window_of(single["messages"][1]["content"], label))
        self.assertEqual((C.max_tokens(1, "full-text"), "low"), (single["max_tokens"], single["reasoning_effort"]))
        markdown = (self.output / P.MARKDOWN_NAME).read_text()
        self.assertIn("Hypothetical single-item resend", markdown)

    def test_calls_the_judge_refuses_count_as_missing_in_the_census_share(self):
        self.judge.fail_every, self.judge.fail_status = 4, 400
        report = self.run_full_text()
        entries = report["cells"][0]["fullText"]["entries"]
        refused = [record for record in self.calls() if record.get("failure") == "http-400"]
        self.assertTrue(refused)
        self.assertEqual((len(refused), sum(r["items"] for r in refused)), (entries["refusedCalls"], entries["refusedEntries"]))
        self.assertEqual(0.0, entries["invalidShare"], "answered calls were all valid")
        self.assertEqual(round(entries["refusedEntries"] / (entries["entries"] + entries["refusedEntries"]), 5),
                         entries["censusInvalidShare"])
        self.assertEqual(entries["censusInvalidShare"], report["exitCriterion"]["invalidShare"])
        self.assertIn("calls the judge refused, which the census counts as missing", report["exitCriterion"]["line"])
        self.assertFalse(report["exitCriterion"]["passes"])

    def test_malformed_entries_are_those_with_a_rejected_line(self):
        self.assertEqual(1, P.malformed_entries(["bad category for 2: 'Z'"], ["2", "3"]))
        self.assertEqual(2, P.malformed_entries(["bad category for 2: 'Z'", "unparseable: {x"], ["2", "3", "4"]))
        self.assertEqual(1, P.malformed_entries(["unexpected label '9'"], ["3"]))
        self.assertEqual(0, P.malformed_entries(["bad category for 7: 'Z'"], ["3"]), "a later valid line decided 7")
        self.assertEqual(0, P.malformed_entries([], ["1", "2"]))
        line = lambda label, category="T": json.dumps({"l": label, "c": category, "v": "direct", "q": "", "e": 0, "r": "x"})
        body = json.dumps({"model": "m", "choices": [{"message": {"content": line("1") + "\n" + line("2", "Z")},
                                                      "finish_reason": "stop"}]}).encode()
        result = P.classify_response(200, body, ["1", "2", "3"], stage=P.FULL_TEXT_STAGE)
        self.assertEqual((1, 2, 1), (result["valid"], result["missing"], result["malformed"]))


class SettingsTests(FullTextCase):
    def test_settings_of_the_full_text_tier_are_checked(self):
        cases = [
            ({"store": None}, "needs --store"),
            ({"store": "relative/store"}, "absolute path"),
            ({"review_prefixes": ()}, "needs --review-doi-prefix"),
            ({"review_prefixes": ("ftreview",)}, "DOI prefix"),
            ({"journal": P.DEFAULT_JOURNAL}, "journal label"),
            ({"repair": True}, "no repair pass at the full-text tier"),
            ({"expect_output_tokens": 50.0}, "synthetic entries"),
            ({"review_route": "entitled"}, "on-premises judge"),
            ({"on_premises": True}, "needs --policy"),
            ({"full_text_cache": "cache"}, "absolute path"),
            ({"fetch_workers": 0}, "--fetch-workers"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValidationError, message):
                self.full_text_settings(**overrides).validate(live=True)
        for cells, message in (("4x5,8x5", "one cell"), ("4x4", "5 items per call")):
            with self.subTest(cells=cells), self.assertRaisesRegex(ValidationError, message):
                self.full_text_settings(cells).validate(live=True)
        with self.assertRaisesRegex(ValidationError, "belong to --tier full-text"):
            P.ProbeSettings(base_url=self.url + "/v1", store=str(self.paths["projection"])).validate(live=True)
        default = P.ProbeSettings(tier="full-text")
        self.assertEqual([(P.FULL_TEXT_STREAMS, 5)], default.grid_cells())
        self.assertEqual(P.FULL_TEXT, P.cell_role(20, 5, default))

    def test_entitled_reviews_reach_only_a_judge_the_policy_names_on_premises(self):
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"policy": "claim-check-egress", "version": 2,
                                      "onPremEndpoints": [{"url": "http://judge.example:8000/v1/chat/completions"}]}))
        with self.assertRaises(ConfidentialityError):
            self.run_full_text(review_route="entitled", on_premises=True, policy=str(policy))
        self.assertEqual([], self.judge.payloads)
        policy.write_text(json.dumps({"policy": "claim-check-egress", "version": 2,
                                      "onPremEndpoints": [{"url": self.url + "/v1/chat/completions"}]}))
        report = self.run_full_text(review_route="entitled", on_premises=True, policy=str(policy),
                                    review_prefixes=(PREFIX + "1",))
        self.assertEqual({"cache-hit-entitled": 1}, report["fullTextInputs"]["reviews"]["acquisitions"])
        self.assertEqual(True, report["onPremises"]["endpointOnPremises"])
        self.assertEqual("completed", report["state"])


class DryRunTests(FullTextCase):
    def test_the_dry_run_reads_the_store_and_sends_and_fetches_nothing(self):
        def refuse(pmcid):
            raise AssertionError("the dry run fetched " + pmcid)
        probe = P.CapacityProbe(self.full_text_settings(), environ={}, fetch_xml=refuse)
        plan = probe.plan()
        self.assertEqual(("full-text", 3), (plan["tier"], plan["fullTextInputs"]["reviews"]["withPrefix"]))
        self.assertEqual(1, plan["fullTextInputs"]["entries"]["retrieved"], "only the full text the store holds")
        self.assertEqual((23, 0), (plan["fullTextInputs"]["entries"]["notFetched"],
                                   plan["fullTextInputs"]["entries"]["withoutFullText"]))
        self.assertEqual(3, plan["fullTextInputs"]["sample"]["reviews"])
        self.assertEqual(0, self.judge.requests)
        self.assertFalse(plan["census"]["repairPass"])
        text = P.render_plan(plan)
        self.assertIn("nothing is sent and nothing is fetched", text)
        self.assertIn("no repair pass at the full-text tier", text)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(["capacity", "probe", "--tier", "full-text", "--store", str(self.paths["projection"]),
                         "--review-doi-prefix", PREFIX, "--journal", JOURNAL, "--review-route", "public",
                         "--cells", "20x5", "--full-text-entries", "300", "--hypothetical-repair", "--dry-run"])
        self.assertEqual(0, code)
        self.assertIn("Capacity probe plan, full-text tier", out.getvalue())
        self.assertIn("hypothetical single-item resend", out.getvalue())
        self.assertEqual(0, self.judge.requests)


class EuropePmcTests(FullTextCase):
    def test_the_census_fetcher_is_the_default_with_its_headers(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("User-Agent"), self.headers.get("Accept")))
                pmcid = self.path.strip("/").split("/")[0]
                if not open_access(pmcid):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = full_text_xml(pmcid, work_doi(int(pmcid[3]), int(pmcid[4:])))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/%%s/fullTextXML" % server.server_address[1]
        with mock.patch.object(study_cli, "EUROPE_PMC_FULL_TEXT", url):
            probe = P.CapacityProbe(self.full_text_settings(review_prefixes=(PREFIX + "2",)), environ={})
            self.output = self.root / "default-fetcher"
            report = probe.run(self.output)
        self.assertEqual("completed", report["state"])
        self.assertEqual({("ocsl-review census inputs", "application/xml")}, {(agent, accept) for _, agent, accept in seen})
        self.assertNotIn("PMC2001/fullTextXML", " ".join(path for path, _, _ in seen))
        self.assertEqual(len(seen), report["fullTextInputs"]["fullTexts"]["fetched"]
                         + report["fullTextInputs"]["fullTexts"]["absent"])


if __name__ == "__main__":
    unittest.main()
