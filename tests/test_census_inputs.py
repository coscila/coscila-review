# SPDX-License-Identifier: Apache-2.0
"""Census inputs from byte caches: a label-free projection and a provider response cache, live fallback, provenance
per item in the scores database and the facts, and fresh enumeration compared with a stored manifest afterwards."""
from __future__ import annotations

import csv
import http.client
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ocsl_review import census_abstracts, census_inputs, corpus_facts, scores_db
from ocsl_review.census_inputs import (FULL_TEXT, QUERY_RULE, SEARCH, InputCaches, LiveSources, ReviewAcquirer,
                                       acquire_records, acquired_record, compare_manifests, enumerate_reviews,
                                       load_enumeration, parse_routes, verify_claim)
from ocsl_review.census_abstracts import fill_record
from ocsl_review.census_runner import (CensusRun, HttpChatTransport, RetryableFetch, census_status, record_comparison,
                                       record_exclusions)
from ocsl_review.census_works import WORKS_DERIVATION, derive_works, fill_works, metadata_label
from ocsl_review.cli import main
from ocsl_review.corpus_acquisition import CorpusResponseCache
from ocsl_review.corpus_library import now
from ocsl_review.corpus_seed import import_legacy_evidence
from ocsl_review.corpus_store import CorpusRepository, canonical, identity
from ocsl_review.errors import ValidationError
from ocsl_review.evidence import abstract_candidate
from ocsl_review.scholarly import RawResponse, normalize_europe_pmc
from ocsl_review.store_client import (ByteCacheError, EvidenceStore, GuardedReader, ProviderCache, TableAllowlistError)
from ocsl_review.store_projection import KEPT_TABLES, build_projection
from ocsl_review.study_cli import fill_abstracts, gates, prepare

from test_census_runner import FIXTURES, JOURNAL, RunnerTestCase, record, unpaced

SECOND = "Second Synthetic Journal"
JATS_REVIEW = "10.5555/synthetic.review.1"          # retained capture in the store (JATS)
HTML_REVIEW = "10.5555/synthetic.review.2"          # publisher page with a table page in the store
CACHED_REVIEW = "10.5555/synthetic.review.3"        # publisher page in the provider cache only
TAMPERED_REVIEW = "10.5555/synthetic.review.4"      # provider cache bytes changed after caching
MISMATCHED_REVIEW = "10.5555/synthetic.review.5"    # the cached page declares another DOI
PUBLISHER = "https://publisher.example/articles/"


def html_review(doi: str) -> bytes:
    """The synthetic publisher page with its linked table, declaring ``doi``."""
    return (FIXTURES / "review_table_caption.html").read_bytes().replace(JATS_REVIEW.encode(), doi.encode())


TABLE_PAGE = (FIXTURES / "review_table_1.html").read_bytes()
TABLE_URL = "https://publisher.example/articles/synthetic-review-1/tables/1"
PAYWALL = (b'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="citation_doi" content="%s"></head><body>'
           b'<article><section data-title="Abstract"><p>Only the abstract is shown.</p></section>'
           b'<section data-title="References"><ol class="c-article-references"><li id="ref-CR1">Author A. A source. '
           b'2020.</li></ol></section></article></body></html>')


def cited_jats(pmcid: str | None, doi: str | None, sentence: str) -> bytes:
    ids = "".join(f'<article-id pub-id-type="{kind}">{value}</article-id>' for kind, value in (("doi", doi), ("pmcid", pmcid))
                  if value)
    return (f'<article><front><article-meta>{ids}<title-group><article-title>Cited work</article-title></title-group>'
            f'<abstract><p>Abstract of the cited work.</p></abstract></article-meta></front><body><sec><title>Results'
            f'</title><p>{sentence}</p></sec></body></article>').encode()


def epmc(doi=None, pmid=None, abstract=None, title="A cited work", pmcid=None):
    raw = {"id": pmid or "PPR1", "source": "MED" if pmid else "PPR", "title": title, "pubYear": "2020"}
    raw.update({key: value for key, value in (("doi", doi), ("pmid", pmid), ("pmcid", pmcid), ("abstractText", abstract))
                if value})
    return raw


def lookup(doi=None, pmid=None):
    query = f'DOI:"{doi}"' if doi else f"EXT_ID:{pmid} AND SRC:MED"
    return {"query": query, "pageSize": 5, "resultType": "core", "format": "json"}


def build_caches(root: Path) -> dict:
    """An earlier corpus (with a legacy score row) projected into literature-evidence@2, and a provider cache."""
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
        # A legacy normalised abstract column: never an abstract of record.
        db.execute("INSERT INTO works VALUES(?,?,?,?,?,?,NULL,NULL,NULL)",
                   ("doi:10.5555/w6", "10.5555/w6", "Fictional cohort six", 2019, "Legacy normalised text.", "europepmc"))
        # A full text the earlier census selected for one of its own escalations (legacy-original-source).
        db.execute("INSERT INTO works VALUES(?,?,?,?,NULL,NULL,?,?,?)",
                   ("doi:10.5555/w9", "10.5555/w9", "Range member nine and box source", 2016, "work/corpus/w9.xml",
                    FULL_TEXT % "PMC900009", "jats"))
        db.execute("INSERT INTO papers VALUES(?,?,?,?)", (JATS_REVIEW, "Synthetic review", 2021, "work/corpus/review.xml"))
        db.execute("INSERT INTO bib_refs VALUES(?,?,?,?)", (JATS_REVIEW, "1", "Printed reference", "doi:10.5555/w1"))
    (legacy / "work/corpus/review.xml").write_bytes((FIXTURES / "review.xml").read_bytes())
    (legacy / "work/corpus/w9.xml").write_bytes(cited_jats("PMC900009", "10.5555/w9", "Selected full text of work nine."))
    import_legacy_evidence(seed, database, source_root=legacy)
    with seed.connect(write=True) as connection:
        method = connection.execute("SELECT sha256 FROM source_artifacts LIMIT 1").fetchone()[0]
        analysis = connection.execute("SELECT analysis_id FROM analyses WHERE analysis_id LIKE 'legacy-evidence:%'").fetchone()[0]
        connection.execute("INSERT INTO work_observations VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "old-score", analysis, "work-" + identity("doi:10.5555/w6"), "legacy.citation_score",
            canonical({"row": {"abs_class": "sure_not_supported"}}), "legacy", method, None, "unresolved", now()))
    # A publisher review page as served, its table page and the combined page the acquisition stored.
    page, url = html_review(HTML_REVIEW), PUBLISHER + "synthetic-review-2"
    work = seed.work({"doi": HTML_REVIEW, "title": "Synthetic review in HTML"})
    original = seed.artifact(page, "raw-publisher-source", "text/html")
    table = seed.artifact(TABLE_PAGE, "raw-provider-response", "text/html")
    seed.document(work, {"body": page, "mediaType": "text/html", "sourceUrl": url, "complete": True, "text": "page",
                         "provider": "publisher", "originalArtifactSha256": original,
                         "components": [{"sourceUrl": url, "sha256": original}, {"sourceUrl": url + "/tables/1",
                                                                                  "sha256": table}]},
                  "full-text", "publisher")
    # A cited work's abstract of record (a complete Europe PMC candidate) and a cited work's fullTextXML.
    raw = epmc(doi="10.5555/w5", abstract="Stored provider abstract of work five.")
    source = SEARCH + "?" + urllib.parse.urlencode(lookup(doi="10.5555/w5"))
    candidate = abstract_candidate(normalize_europe_pmc(raw), source_url=source, retrieved_at="2024-05-01T00:00:00Z")
    work = seed.work({"doi": "10.5555/w5", "title": "Fictional cohort five"})
    seed.document(work, {"text": candidate["text"], "complete": True, "sourceUrl": source, "provenance": candidate},
                  "abstract", "europe-pmc")
    work = seed.work({"doi": "10.5555/w7", "pmcid": "PMC900007", "title": "Fictional cohort seven"})
    seed.document(work, {"body": cited_jats("PMC900007", "10.5555/w7", "Stored full text of work seven."),
                         "mediaType": "application/xml", "sourceUrl": FULL_TEXT % "PMC900007", "complete": True,
                         "text": "x", "provider": "europe-pmc"}, "full-text", "europe-pmc")
    seed.library.snapshot("corpus-snapshot")
    mount = root / "datasets"
    mount.mkdir()
    projection = mount / "literature-evidence-2"
    build_projection(seed_root / "corpus-snapshot", projection, workers=1)

    provider = root / "provider" / "cache.sqlite3"
    cache = CorpusResponseCache(provider, transport=None)

    def keep(url, body, *, params=None, document=False, final=None, media="application/json"):
        cache.store(url, RawResponse(body, 200, {"Content-Type": media}, final or url), params=params, document=document)
    keep(SEARCH, json.dumps({"resultList": {"result": [epmc(doi="10.5555/w1", abstract="Cached abstract of work one.")]}})
         .encode(), params=lookup(doi="10.5555/w1"))
    # The first record carrying the DOI has no abstract; the live lookup decides, as it would without a cache.
    keep(SEARCH, json.dumps({"resultList": {"result": [epmc(doi="10.5555/w2"), epmc(doi="10.5555/w2", pmid="200002",
                                                                                     abstract="A second record.")]}})
         .encode(), params=lookup(doi="10.5555/w2"))
    # Same DOI, conflicting PMID: refused.
    keep(SEARCH, json.dumps({"resultList": {"result": [epmc(doi="10.5555/w4", pmid="999999", abstract="Wrong work.")]}})
         .encode(), params=lookup(doi="10.5555/w4"))
    keep(FULL_TEXT % "PMC900005", cited_jats("PMC900005", "10.5555/w5", "Cached full text of work five."),
         document=True, media="application/xml")
    keep(FULL_TEXT % "PMC900015", cited_jats("PMC900099", None, "A different article."), document=True,
         media="application/xml")
    keep("https://doi.org/" + CACHED_REVIEW, html_review(CACHED_REVIEW), document=True,
         final=PUBLISHER + "synthetic-review-3", media="text/html")
    keep(TABLE_URL, TABLE_PAGE, document=True, media="text/html")
    keep("https://doi.org/" + TAMPERED_REVIEW, html_review(TAMPERED_REVIEW), document=True,
         final=PUBLISHER + "synthetic-review-4", media="text/html")
    keep("https://doi.org/" + MISMATCHED_REVIEW, html_review("10.5555/another.review"), document=True,
         final=PUBLISHER + "synthetic-review-5", media="text/html")
    keep("https://legacy.example/verdicts", b'{"abs_class": "sure_not_supported"}', document=True)
    tampered = provider.parent / "cache-artifacts" / census_inputs.sha256_bytes(html_review(TAMPERED_REVIEW))
    tampered.write_bytes(html_review(TAMPERED_REVIEW) + b"<!-- changed -->")
    return {"projection": projection, "snapshot": seed_root / "corpus-snapshot", "provider": provider}


class CacheTestCase(RunnerTestCase):
    """A judge mock (from the runner tests) plus caches built once per class."""

    @classmethod
    def setUpClass(cls):
        cls.cache_temp = tempfile.TemporaryDirectory()
        cls.cache_root = Path(cls.cache_temp.name)
        cls.paths = build_caches(cls.cache_root)

    @classmethod
    def tearDownClass(cls):
        cls.cache_temp.cleanup()

    def caches(self, *, store=True, provider=True) -> InputCaches:
        caches = InputCaches.open(store=self.paths["projection"] if store else None,
                                  provider_cache=self.paths["provider"] if provider else None)
        self.addCleanup(caches.close)
        return caches


class GuardTests(CacheTestCase):
    def test_reader_refuses_every_table_outside_the_projection_allowlist(self):
        store = self.caches(provider=False).store
        self.assertGreater(len(store.rows("SELECT work_id FROM evidence.works")), 0)
        for table in ("work_observations", "assessments", "claim_loci", "bibliography_entries", "model_receipts",
                      "reviews", "corpus_tasks", "sqlite_master"):
            with self.subTest(table=table), self.assertRaisesRegex(TableAllowlistError, table):
                store.rows(f"SELECT count(*) FROM evidence.{table}")
        with self.assertRaisesRegex(TableAllowlistError, "work_observations"):
            store.rows("SELECT w.work_id FROM evidence.works w WHERE w.work_id IN "
                       "(SELECT work_id FROM evidence.work_observations)")
        for statement in ("DELETE FROM evidence.works", "PRAGMA evidence.table_info(works)",
                          "ATTACH DATABASE ':memory:' AS other", "CREATE TEMP TABLE scratch(x)"):
            with self.subTest(statement=statement), self.assertRaises(ValidationError):
                store.rows(statement)
        # The same guard over the unprojected snapshot, which still holds a legacy score row: refused, never empty.
        source = self.paths["snapshot"] / "corpus.sqlite3"
        reader = GuardedReader(source.as_uri() + "?mode=ro&immutable=1", KEPT_TABLES, label="snapshot", alias="evidence")
        self.addCleanup(reader.close)
        with sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True) as raw:
            self.assertEqual(1, raw.execute("SELECT count(*) FROM work_observations WHERE property='legacy.citation_score'")
                             .fetchone()[0])
        raw.close()
        with self.assertRaisesRegex(TableAllowlistError, "work_observations"):
            reader.rows("SELECT value_json FROM evidence.work_observations WHERE property LIKE 'legacy.%'")
        cache = self.caches(store=False).provider_cache
        for table in ("failures", "request_leases", "provider_slots"):
            with self.subTest(table=table), self.assertRaisesRegex(TableAllowlistError, table):
                cache.reader.rows(f"SELECT count(*) FROM provider.{table}")

    def test_only_the_label_free_projection_is_read_and_its_version_is_pinned(self):
        with self.assertRaisesRegex(ValidationError, "label-free projection"):
            EvidenceStore(self.paths["snapshot"])
        with self.assertRaisesRegex(ValidationError, "pinned"):
            InputCaches.open(store=self.paths["projection"], store_root="0" * 64)
        caches = self.caches()
        pinned = InputCaches.open(store=self.paths["projection"], store_root=caches.store.version["merkleRoot"])
        self.addCleanup(pinned.close)
        self.assertEqual(caches.store.version, pinned.store.version)
        self.assertIsNone(InputCaches.open())
        with self.assertRaisesRegex(ValidationError, "give --store"):
            InputCaches.open(store_root="0" * 64)
        with self.assertRaisesRegex(ByteCacheError, "legacy verdict vocabulary"):
            caches.provider_cache.get("https://legacy.example/verdicts", document=True)
        journal = Path(str(self.paths["provider"]) + "-journal")
        journal.write_bytes(b"x")
        self.addCleanup(journal.unlink)
        with self.assertRaisesRegex(ValidationError, "quiescent"):
            ProviderCache(self.paths["provider"])

    def test_store_bytes_that_fail_verification_are_refused(self):
        copy = self.cache_root / "tampered-store"
        shutil.copytree(self.paths["projection"], copy)
        self.addCleanup(shutil.rmtree, copy, True)
        caches = InputCaches.open(store=copy)
        self.addCleanup(caches.close)
        capture = caches.store.retained_captures(JATS_REVIEW)[0]["capture_sha256"]
        with closing(sqlite3.connect((copy / "packs" / "index.sqlite3").as_uri() + "?mode=ro", uri=True)) as index:
            name, offset, length = index.execute("SELECT p.name, b.offset, b.stored_length FROM blobs b JOIN packs p "
                                                 "ON p.id=b.pack_id WHERE b.sha256=?", (capture,)).fetchone()
        pack = copy / "packs" / name
        os.chmod(pack, 0o644)
        data = bytearray(pack.read_bytes())
        data[offset + length // 2] ^= 0xFF
        pack.write_bytes(bytes(data))
        with self.assertRaises(ByteCacheError):
            caches.store.read(capture)
        result = ReviewAcquirer(caches, routes=parse_routes([f"{JOURNAL}=public"])).acquire(
            {"journal": JOURNAL, "doi": JATS_REVIEW})
        self.assertEqual("unavailable", result.state)
        self.assertIn("sha256-mismatch", {item["reason"] for item in result.detail["reasons"]})


class ReviewAcquisitionTests(CacheTestCase):
    def acquirer(self, live=None, routes=(f"{JOURNAL}=public", f"{SECOND}=entitled")):
        return ReviewAcquirer(self.caches(), routes=parse_routes(routes), live=live)

    def test_store_hits_are_accepted_after_digest_and_identity_checks(self):
        acquirer = self.acquirer()
        jats = acquirer.acquire({"journal": JOURNAL, "doi": JATS_REVIEW})
        self.assertEqual(("acquired", "cache-hit-public", "jats"), (jats.state, jats.provenance, jats.kind))
        self.assertEqual((FIXTURES / "review.xml").read_bytes(), jats.body)
        self.assertEqual(("store", "retained-capture", "doi"), (jats.detail["cache"], jats.detail["source"],
                                                                 jats.detail["identity"]))
        self.assertEqual(acquirer.caches.store.version["merkleRoot"], jats.detail["cacheVersion"])
        # A journal without a route takes the conservative class: a retained capture is entitled text.
        unrouted = ReviewAcquirer(self.caches()).acquire({"journal": "Unrouted Journal", "doi": JATS_REVIEW})
        self.assertEqual("cache-hit-entitled", unrouted.provenance)
        html = acquirer.acquire({"journal": JOURNAL, "doi": HTML_REVIEW})
        self.assertEqual(("cache-hit-public", "html", "store-document"), (html.provenance, html.kind, html.detail["source"]))
        self.assertEqual(html_review(HTML_REVIEW), html.body, "the page as served, not the combined page")
        self.assertEqual([(1, TABLE_PAGE)], html.tables)
        self.assertEqual("store", html.detail["tables"][0]["source"])

    def test_provider_cache_hits_resolve_table_pages_from_the_cache(self):
        result = self.acquirer().acquire({"journal": SECOND, "doi": CACHED_REVIEW})
        self.assertEqual(("acquired", "cache-hit-entitled", "provider-cache"), (result.state, result.provenance,
                                                                                result.detail["cache"]))
        self.assertEqual([(1, TABLE_PAGE)], result.tables)
        self.assertEqual("provider-cache", result.detail["tables"][0]["source"])

    def test_digest_mismatch_and_identity_mismatch_fall_back_to_the_live_route(self):
        fetched = []

        def publisher(url):
            fetched.append(url)
            doi = urllib.parse.unquote(url.rsplit("doi.org/", 1)[-1]) if "doi.org/" in url else None
            if url == TABLE_URL:
                return TABLE_PAGE, url
            return (html_review(doi), PUBLISHER + "live") if doi else "http-404"
        live = LiveSources(publisher=publisher)
        tampered = self.acquirer(live).acquire({"journal": SECOND, "doi": TAMPERED_REVIEW})
        self.assertEqual(("acquired", "live-entitled"), (tampered.state, tampered.provenance))
        self.assertEqual([("provider-cache", "sha256-mismatch")],
                         [(item["cache"], item["reason"]) for item in tampered.detail["rejected"]])
        self.assertEqual("https://doi.org/" + TAMPERED_REVIEW, fetched[0])
        mismatched = self.acquirer(live).acquire({"journal": JOURNAL, "doi": MISMATCHED_REVIEW})
        self.assertEqual(("acquired", "live-public"), (mismatched.state, mismatched.provenance))
        self.assertEqual("identity-mismatch", mismatched.detail["rejected"][0]["reason"])
        without_live = self.acquirer().acquire({"journal": JOURNAL, "doi": MISMATCHED_REVIEW})
        self.assertEqual(("unavailable", "unavailable"), (without_live.state, without_live.provenance))
        self.assertEqual(["identity-mismatch", "live-acquisition-off"],
                         [item["reason"] for item in without_live.detail["reasons"]])

    def test_paywall_pages_transient_failures_and_missing_routes(self):
        paywall_doi = "10.5555/paywalled.review"
        live = LiveSources(publisher=lambda url: (PAYWALL % paywall_doi.encode(), PUBLISHER + "paywalled"))
        result = self.acquirer(live).acquire({"journal": SECOND, "doi": paywall_doi})
        self.assertEqual("unavailable", result.state)
        self.assertEqual("not-the-article", result.detail["reasons"][0]["reason"])

        def flaky(url):
            raise RetryableFetch("HTTP 503")
        retry = self.acquirer(LiveSources(publisher=flaky)).acquire({"journal": SECOND, "doi": "10.5555/flaky.review"})
        self.assertEqual("retry", retry.state)
        unrouted = self.acquirer(live).acquire({"journal": "Unrouted Journal", "doi": paywall_doi})
        self.assertEqual(["no-live-route"], [item["reason"] for item in unrouted.detail["reasons"]])
        self.assertEqual("unavailable", ReviewAcquirer().acquire({"journal": JOURNAL, "doi": "not a doi"}).state)

    def test_a_route_reads_only_its_document_kinds(self):
        routes = parse_routes([f"{JOURNAL}=entitled:html"])
        result = ReviewAcquirer(self.caches(), routes=routes).acquire({"journal": JOURNAL, "doi": JATS_REVIEW})
        self.assertEqual("unavailable", result.state)
        self.assertEqual("kind-not-read-by-route", result.detail["reasons"][0]["reason"])
        for bad in ("J", "J=open", "J=public:pdf", "J=public:html,html"):
            with self.subTest(route=bad), self.assertRaises(ValidationError):
                parse_routes([bad])
        with self.assertRaises(ValidationError):
            parse_routes(["J=public", "J=entitled"])

    def acquire_source(self):
        source = self.root / "input" / "reviews.jsonl"
        source.parent.mkdir(exist_ok=True)
        rows = [{"journal": JOURNAL, "doi": JATS_REVIEW}, {"journal": JOURNAL, "doi": HTML_REVIEW},
                {"journal": SECOND, "doi": "10.5555/flaky.review"}, {"journal": JOURNAL, "doi": "10.5555/absent.review"},
                {**record(doi="10.5555/supplied.review"), "document": {"path": "review.xml", "kind": "jats"}}]
        source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return source

    def flaky_acquirer(self, failures):
        attempts = []

        def flaky(url):
            if "flaky" in url:
                attempts.append(url)
                if len(attempts) <= failures:
                    raise RetryableFetch("HTTP 503")
            return html_review("10.5555/flaky.review"), PUBLISHER + "flaky"
        return self.acquirer(LiveSources(publisher=flaky), routes=(f"{JOURNAL}=public", f"{SECOND}=entitled:html")), attempts

    def test_acquire_records_writes_documents_and_resumes_transient_failures(self):
        source = self.acquire_source()
        # The publisher fails through the main pass and all four sweeps.
        acquirer, attempts = self.flaky_acquirer(failures=5)
        output = self.root / "acquired" / "reviews.jsonl"
        waits = []
        first = acquire_records(source, output, acquirer, sleep=waits.append)
        self.assertEqual([60.0, 300.0, 900.0, 3600.0], waits)
        self.assertEqual(5, len(attempts))
        self.assertEqual([(60.0, 1, 1, 0), (300.0, 1, 1, 0), (900.0, 1, 1, 0), (3600.0, 1, 1, 0)],
                         [(s["waitSeconds"], s["records"], s["items"], s["recovered"]) for s in first["sweeps"]])
        self.assertEqual({"cache-hit-public": 2, "retry": 1, "unavailable": 1, "supplied": 1}, first["provenance"])
        self.assertIsNotNone(first["next"])
        lines = [json.loads(line) for line in output.read_text().splitlines()]
        jats = lines[0]
        self.assertEqual(("public", "cache-hit-public"), (jats["provenanceClass"], jats["acquisition"]["provenance"]))
        document = output.parent / jats["document"]["path"]
        self.assertEqual(census_inputs.sha256_bytes(document.read_bytes()), jats["document"]["sha256"])
        self.assertEqual(1, len(lines[1]["document"]["tables"]))
        self.assertEqual("retry", lines[2]["acquisition"]["state"])
        self.assertEqual("unavailable", lines[3]["provenanceClass"])
        self.assertNotIn("acquisition", lines[4])
        second = acquire_records(output, self.root / "acquired" / "reviews.2.jsonl", acquirer, sleep=waits.append)
        self.assertEqual((1, 4, []), (second["acquired"], second["passedThrough"], second["sweeps"]))
        self.assertEqual("live-entitled", json.loads((self.root / "acquired" / "reviews.2.jsonl").read_text()
                                                     .splitlines()[2])["acquisition"]["provenance"])

    def test_acquisitions_left_in_state_retry_are_swept_and_the_output_is_written_once(self):
        source = self.acquire_source()
        acquirer, attempts = self.flaky_acquirer(failures=1)
        output = self.root / "acquired" / "reviews.jsonl"
        waits = []
        with mock.patch("os.replace", wraps=os.replace) as replaced:
            result = acquire_records(source, output, acquirer, sleep=waits.append)
        self.assertEqual([60.0], waits, "the first sweep acquired it; no further sweep waits")
        self.assertEqual([(60.0, 1, 1, 1)], [(s["waitSeconds"], s["records"], s["items"], s["recovered"])
                                             for s in result["sweeps"]])
        self.assertEqual({"cache-hit-public": 2, "live-entitled": 1, "unavailable": 1, "supplied": 1}, result["provenance"])
        self.assertIsNone(result["next"])
        self.assertEqual(1, sum(1 for call in replaced.call_args_list if Path(call.args[1]) == output))
        self.assertEqual([output.name], [path.name for path in output.parent.iterdir() if path.is_file()])
        lines = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([row["doi"] for row in map(json.loads, source.read_text().splitlines())],
                         [line["doi"] for line in lines])
        self.assertEqual("live-entitled", lines[2]["acquisition"]["provenance"])
        self.assertTrue((output.parent / lines[2]["document"]["path"]).is_file())


class CitedWorkTests(CacheTestCase):
    def test_abstracts_come_from_the_caches_first_and_record_their_provenance(self):
        queries = []

        def get(params):
            queries.append(params["query"])
            results = []
            for doi in [part.split('"')[1] for part in params["query"].split(" OR ") if '"' in part]:
                results.append(epmc(doi=doi, abstract=f"Live abstract of {doi}."))
            if params["query"].startswith("EXT_ID:"):
                results.append(epmc(pmid=params["query"].split(":")[1].split()[0], abstract="Live abstract by PMID."))
            return {"resultList": {"result": results}}
        works = {"w1": {"doi": "10.5555/w1"}, "w2": {"doi": "10.5555/w2"}, "w4": {"doi": "10.5555/w4", "pmid": "400004"},
                 "w5": {"doi": "10.5555/w5"}, "w6": {"doi": "10.5555/w6"}, "w9": {"pmid": "900009"}}
        caches = self.caches()
        filled = fill_record({"journal": JOURNAL, "doi": JATS_REVIEW, "works": works}, get, cache=caches)["works"]
        self.assertEqual(["10.5555/w2", "10.5555/w4", "10.5555/w6"], sorted(
            part.split('"')[1] for query in queries for part in query.split(" OR ") if '"' in part))
        self.assertIn("EXT_ID:900009 AND SRC:MED", queries)
        one, five = filled["w1"], filled["w5"]
        self.assertEqual(("Cached abstract of work one.", "europe-pmc-core"), (one["abstract"], one["abstractSource"]))
        self.assertEqual(("cache-hit-public", "provider-cache", caches.provider_cache.version["sha256"]),
                         (one["abstractProvenance"]["provenance"], one["abstractProvenance"]["cache"],
                          one["abstractProvenance"]["cacheVersion"]))
        self.assertEqual(("Stored provider abstract of work five.", "store", caches.store.version["merkleRoot"]),
                         (five["abstract"], five["abstractProvenance"]["cache"], five["abstractProvenance"]["cacheVersion"]))
        self.assertEqual("2024-05-01T00:00:00Z", five["abstractRetrievedAt"])
        for key in ("w2", "w6"):
            self.assertEqual(("live-public", f"Live abstract of {works[key]['doi']}."),
                             (filled[key]["abstractProvenance"]["provenance"], filled[key]["abstract"]))
        self.assertEqual("identity-mismatch", filled["w4"]["abstractProvenance"]["rejected"][0]["reason"])
        self.assertEqual("europe-pmc-core-pmid", filled["w9"]["abstractSource"])
        self.assertNotEqual("Legacy normalised text.", filled["w6"]["abstract"])

    def test_full_text_tier_reads_cached_fulltextxml_and_records_provenance(self):
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        fetched = []

        def fetch(pmcid):
            fetched.append(pmcid)
            return (FIXTURES / "full_texts" / "ft2.xml").read_bytes()
        caches = self.caches()
        run.pin_inputs(caches)
        result = run.run_full_text(fetch_xml=fetch, fetch_workers=2, cache=caches)
        self.assertEqual("completed", result["state"])
        self.assertEqual(["PMC900015"], fetched, "works five and seven come from the caches")
        provenance = {r["label"]: (r["ft_provenance"], r["ft_retrieval"]) for r in self.rows(
            "SELECT label, ft_provenance, ft_retrieval FROM citations WHERE abs_escalated=1")}
        self.assertEqual({"5": ("cache-hit-public", "europe-pmc-fulltextxml"), "7": ("cache-hit-public", "europe-pmc-fulltextxml"),
                          "15": ("live-public", "europe-pmc-fulltextxml"), "10": ("unavailable", "no-pmcid"),
                          "14": ("unavailable", "no-pmcid")}, provenance)
        items = {r["work_key"]: r for r in self.rows("SELECT * FROM input_provenance WHERE kind='full-text'")}
        self.assertEqual(("provider-cache", 1), (items["work-5"]["cache"], items["work-5"]["verified"]))
        self.assertTrue(all(json.loads(item["detail_json"])["liveRetrieval"] for item in items.values()))
        self.assertEqual(("store", "doi+pmcid"), (items["work-7"]["cache"], items["work-7"]["identity_check"]))
        self.assertEqual("identity-mismatch", json.loads(items["work-15"]["detail_json"])["rejected"][0]["reason"])
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        self.assertEqual({"cache-hit-public": 2, "live-public": 1}, status["itemProvenance"]["full-text"])
        self.assertEqual(caches.store.version["merkleRoot"], status["inputs"]["inputStore"]["merkleRoot"])


    def test_full_text_caches_stand_in_only_for_live_downloads_and_never_offer_earlier_selections(self):
        caches = self.caches()
        # The source store held a full text an earlier census selected for its own escalation; the label-free
        # projection drops it (with every legacy document), and a full-text lookup never offers it.
        store = caches.store
        work_id, _ = store.find_work({"doi": "10.5555/w9"})
        if work_id is not None:
            self.assertNotIn("legacy-original-source",
                             {document["provider"] for document in store.documents(work_id, "full-text")})
        self.assertEqual((None, []), caches.full_text({"doi": "10.5555/w9", "pmcid": "PMC900009"}))
        self.assertIsNotNone(caches.full_text({"doi": "10.5555/w7", "pmcid": "PMC900007"})[0])   # a Europe PMC document
        run = self.run_census()
        self.prepare(run)
        run.run_abstract()
        run.pin_inputs(caches)
        with self.assertRaisesRegex(ValidationError, "stand in only for downloads"):
            run.run_full_text(cache=caches)
        self.assertEqual([], self.rows("SELECT * FROM input_provenance WHERE kind='full-text'"))
        run.close()
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = main(["study", "full-text", "--database", str(self.database), "--seed", "1234", "--base-url", self.base,
                         "--store", str(self.paths["projection"])])
        self.assertNotEqual(0, code)
        self.assertIn("--europe-pmc", buffer.getvalue())
        # A full text taken from the cache while retrieving live is a stand-in; one without (an older run) fails the gate.
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        self.assertEqual(0, status["inputChecks"]["fullTextCacheHitsWithoutLiveRetrieval"])

    def test_works_derived_from_the_bibliography_take_their_metadata_from_the_abstract_lookup(self):
        document = (FIXTURES / "review.xml").read_bytes()
        works, counts = derive_works(document, "jats")
        self.assertEqual({"entries": 17, "withoutIdentifier": 1, "works": 16}, counts)
        self.assertEqual({"doi": "10.5555/w2", "pmid": "100002"}, works["doi:10.5555/w2"])
        self.assertEqual({"pmid": "100003"}, works["pmid:100003"])
        self.assertIn("doi:10.5555/s0140-6736(14)00012-5", works)          # a DOI printed in the entry
        folder = self.root / "derive"
        folder.mkdir()
        (folder / "review.xml").write_bytes(document)
        base = {"journal": JOURNAL, "doi": JATS_REVIEW, "document": {"path": "review.xml", "kind": "jats"}}
        (folder / "in.jsonl").write_text(json.dumps(base) + "\n")
        result = fill_works(folder / "in.jsonl", folder / "works.jsonl")
        self.assertEqual((1, 16, 1), (result["derived"], result["works"], result["entriesWithoutIdentifier"]))
        row = json.loads((folder / "works.jsonl").read_text())
        provenance = row["works"]["doi:10.5555/w1"]["workProvenance"]
        self.assertEqual((WORKS_DERIVATION, census_inputs.sha256_bytes(document)),
                         (provenance["derivation"], provenance["documentSha256"]))
        missing = {**base, "doi": "10.5555/missing.review", "document": {"path": "missing.xml", "kind": "jats"}}
        (folder / "two.jsonl").write_text(json.dumps(base) + "\n" + json.dumps(missing) + "\n")
        two = fill_works(folder / "two.jsonl", folder / "two-works.jsonl")
        self.assertEqual((1, 1, 0), (two["derived"], two["documentUnreadable"], two["passedThrough"]))
        unreadable = json.loads((folder / "two-works.jsonl").read_text().splitlines()[1])
        self.assertEqual(({}, "document-unreadable"), (unreadable["works"], unreadable["worksDerivation"]["state"]))
        (folder / "carried.jsonl").write_text(json.dumps({**base, "works": record()["works"]}) + "\n")
        with self.assertRaisesRegex(ValidationError, "already carries works"):
            fill_works(folder / "carried.jsonl", folder / "refused.jsonl")
        filled = fill_record(row, lookups, cache=self.caches())["works"]
        one, three, seven = filled["doi:10.5555/w1"], filled["pmid:100003"], filled["doi:10.5555/w7"]
        self.assertEqual(("cache-hit-public", "A cited work", 2020), (one["metadataProvenance"]["provenance"], one["title"],
                                                                      one["year"]))
        self.assertEqual(("live-public", "PMC900007"), (seven["metadataProvenance"]["provenance"], seven["pmcid"]))
        self.assertEqual("live-public", three["metadataProvenance"]["provenance"])
        self.assertEqual("cache-hit-public", metadata_label(one))
        self.assertEqual("supplied", metadata_label({**one, "title": "Changed after the lookup"}))
        self.assertIsNone(metadata_label({"doi": "10.5555/x", "title": "A supplied work"}))
        # Works an input supplied keep their metadata: only derived works take it from the lookup.
        supplied = fill_record({"journal": JOURNAL, "doi": JATS_REVIEW, "works": {"w1": {"doi": "10.5555/w1", "title": "Kept"}}},
                               lookups, cache=self.caches())["works"]["w1"]
        self.assertEqual(("Kept", None), (supplied["title"], supplied.get("metadataProvenance")))


class PreparationProvenanceTests(CacheTestCase):
    def acquired(self, folder: Path, *rows) -> Path:
        acquirer = ReviewAcquirer(self.caches(), routes=parse_routes([f"{JOURNAL}=public", f"{SECOND}=entitled"]))
        source = folder / "reviews.jsonl"
        source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        output = folder / "acquired.jsonl"
        acquire_records(source, output, acquirer)
        return output

    def test_prepare_records_review_and_abstract_provenance_and_rechecks_cache_claims(self):
        folder = self.root / "inputs"
        folder.mkdir()
        works = record()["works"]
        works["work-1"] = {**works["work-1"], "abstractProvenance": {
            "provenance": "cache-hit-public", "cache": "store", "cacheVersion": "0" * 64,
            "textSha256": census_inputs.sha256_bytes(works["work-1"]["abstract"].encode())}}
        output = self.acquired(folder, {"journal": JOURNAL, "doi": JATS_REVIEW, "year": 2021, "stratum": "Review",
                                        "works": works},
                               {"journal": SECOND, "doi": CACHED_REVIEW, "year": 2020, "stratum": "Review", "works": works})
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        # A forged claim: the store holds no capture of these bytes for this review.
        forged = {**rows[0], "doi": "10.5555/forged.review"}
        forged["acquisition"] = {**forged["acquisition"]}
        with output.open("a") as handle:
            handle.write(json.dumps(forged) + "\n")
            handle.write(json.dumps({**record(doi="10.5555/supplied.review"),
                                     "document": {"path": "../review.xml", "kind": "jats"}}) + "\n")
        (folder / "review.xml").write_bytes((FIXTURES / "review.xml").read_bytes())
        run = self.run_census()
        caches = self.caches()
        result = prepare(run, output, caches=caches)
        self.assertEqual({"prepared": 2, "skipped-provenance-unverified": 1, "unavailable": 1}, result["states"])
        reviews = {r["doi"]: r for r in self.rows("SELECT doi, provenance, provenance_class FROM reviews")}
        self.assertEqual(("cache-hit-public", "public"), (reviews[JATS_REVIEW]["provenance"],
                                                          reviews[JATS_REVIEW]["provenance_class"]))
        self.assertEqual(("cache-hit-entitled", "entitled"), (reviews[CACHED_REVIEW]["provenance"],
                                                              reviews[CACHED_REVIEW]["provenance_class"]))
        self.assertNotIn("10.5555/forged.review", reviews)
        items = {r["doi"]: r for r in self.rows("SELECT * FROM input_provenance WHERE kind='review'")}
        self.assertEqual((1, "store"), (items[JATS_REVIEW]["verified"], items[JATS_REVIEW]["cache"]))
        self.assertEqual(1, items[CACHED_REVIEW]["verified"])
        abstracts = {r["work_key"]: r for r in self.rows("SELECT * FROM input_provenance WHERE kind='abstract'")}
        self.assertEqual(("cache-hit-public", 0), (abstracts["work-1"]["provenance"], abstracts["work-1"]["verified"]))
        self.assertEqual(("supplied", None), (abstracts["work-2"]["provenance"], abstracts["work-2"]["verified"]))
        meta = scores_db.meta(run.connection)
        self.assertEqual(caches.store.version, json.loads(meta["inputStore"]))
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        self.assertEqual(1, status["inputChecks"]["unverifiedAbstractCacheHits"])
        self.assertEqual({"cache-hit-public": 1, "unavailable": 1}, status["reviewProvenanceByJournal"][JOURNAL])
        run.close()
        # A study pinned to one store version refuses another.
        other = mock.Mock(store=mock.Mock(version={**caches.store.version, "merkleRoot": "1" * 64}), provider_cache=None)
        again = CensusRun(self.database, seed=1234)
        self.addCleanup(again.close)
        with self.assertRaisesRegex(ValidationError, "inputStore is pinned"):
            again.pin_inputs(other)

    def test_abstract_cache_claims_are_read_again_at_preparation(self):
        caches = self.caches()
        works = {"w1": {"doi": "10.5555/w1", "title": "One"}, "w5": {"doi": "10.5555/w5", "title": "Five"},
                 "w6": {"doi": "10.5555/w6", "title": "Six"}}
        filled = fill_record({"journal": JOURNAL, "doi": "x", "works": works}, lookups, cache=caches)["works"]
        self.assertEqual(("provider-cache", "store"), (filled["w1"]["abstractProvenance"]["cache"],
                                                       filled["w5"]["abstractProvenance"]["cache"]))
        claim = lambda work: {"provenance": "cache-hit-public", **work["abstractProvenance"]}
        verify = census_inputs.verify_abstract_claim
        self.assertTrue(verify(filled["w1"], claim(filled["w1"]), filled["w1"]["abstract"], caches))
        self.assertTrue(verify(filled["w5"], claim(filled["w5"]), filled["w5"]["abstract"], caches))
        # Replaced text with its digest recomputed: the cache record says otherwise.
        self.assertFalse(verify(filled["w1"], claim(filled["w1"]), "A replaced abstract.", caches))
        # A pointer to another record, or a claim for another work, is refuted; without the cache nothing is shown.
        self.assertFalse(verify(filled["w1"], {**claim(filled["w1"]), "pointer": "/resultList/result/3"},
                                filled["w1"]["abstract"], caches))
        self.assertFalse(verify(filled["w6"], claim(filled["w1"]), filled["w1"]["abstract"], caches))
        self.assertIsNone(verify(filled["w1"], claim(filled["w1"]), filled["w1"]["abstract"], None))
        # At preparation: the tampered abstract is recorded as an unverified cache hit.
        tampered = {**filled["w1"], "abstract": "A replaced abstract."}
        tampered["abstractProvenance"] = {**filled["w1"]["abstractProvenance"],
                                          "textSha256": census_inputs.sha256_bytes(b"A replaced abstract.")}
        data = record()
        data["works"] = {**data["works"], "work-1": {**data["works"]["work-1"], **{k: tampered[k] for k in (
            "abstract", "abstractSource", "abstractProvenance", "europePmcOutcome")}},
                         "work-5": {**data["works"]["work-5"], **{k: filled["w5"][k] for k in (
                             "abstract", "abstractSource", "abstractProvenance", "europePmcOutcome")}}}
        run = self.run_census()
        run.pin_inputs(caches)
        run.prepare_review(data, (FIXTURES / "review.xml").read_bytes())
        verified = {r["work_key"]: r["verified"] for r in self.rows("SELECT work_key, verified FROM input_provenance "
                                                                    "WHERE kind='abstract' AND provenance='cache-hit-public'")}
        self.assertEqual({"work-1": 0, "work-5": 1}, verified)

    def test_review_claims_name_this_reviews_own_request_and_document(self):
        caches = self.caches()
        body = html_review(CACHED_REVIEW)
        acquired = ReviewAcquirer(caches, routes=parse_routes([f"{SECOND}=entitled"])).acquire(
            {"journal": SECOND, "doi": CACHED_REVIEW})
        claim = {"journal": SECOND, "doi": CACHED_REVIEW, "acquisition": {"provenance": "cache-hit-entitled", **acquired.detail}}
        self.assertEqual((True, "provider cache holds these bytes for this review's request"),
                         verify_claim(claim, body, caches))
        other = {**claim, "doi": "10.5555/another.review"}
        self.assertEqual((False, "the cached response answers another request than this review's own"),
                         verify_claim(other, body, caches))
        # A JATS document that declares only a PMCID must declare this review's PMCID when prepared.
        document = (FIXTURES / "review.xml").read_bytes().replace(
            b'<article-id pub-id-type="doi">10.5555/synthetic.review.1</article-id>',
            b'<article-id pub-id-type="pmcid">PMC111</article-id>')
        acquisition = {"provenance": "live-public", "sourceSha256": census_inputs.sha256_bytes(document),
                       "retrievedAt": "2026-01-01T00:00:00Z"}
        run = self.run_census()
        wrong = {**record(doi="10.5555/pmcid.review"), "pmcid": "PMC222", "acquisition": acquisition}
        with self.assertRaisesRegex(ValidationError, "do not confirm this review"):
            run.prepare_review(wrong, document)
        right = {**record(doi="10.5555/pmcid.review"), "pmcid": "PMC111", "acquisition": acquisition}
        self.assertEqual("prepared", run.prepare_review(right, document)["state"])

    def test_verify_claim_refutes_changed_bytes_and_other_versions(self):
        caches = self.caches()
        body = (FIXTURES / "review.xml").read_bytes()
        claim = {"journal": JOURNAL, "doi": JATS_REVIEW, "acquisition": {
            "provenance": "cache-hit-public", "cache": "store", "cacheVersion": caches.store.version["merkleRoot"],
            "sourceSha256": census_inputs.sha256_bytes(body)}}
        self.assertEqual((True, "store holds these bytes for this review"), verify_claim(claim, body, caches))
        self.assertFalse(verify_claim(claim, body + b" ", caches)[0])
        other = {**claim, "acquisition": {**claim["acquisition"], "cacheVersion": "1" * 64}}
        self.assertFalse(verify_claim(other, body, caches)[0])
        self.assertIsNone(verify_claim(claim, body, None)[0])
        self.assertIsNone(verify_claim({"acquisition": {"provenance": "live-public"}}, body, caches)[0])
        acquisition = census_inputs.Acquisition("acquired", "cache-hit-public", body, "jats", detail={"sourceSha256": "x"})
        written = acquired_record({"journal": JOURNAL, "doi": JATS_REVIEW, "document": {"path": "old"}}, acquisition,
                                  self.root / "documents")
        self.assertTrue((self.root / written["document"]["path"]).is_file())


def search_page(results, total, cursor):
    return json.dumps({"hitCount": total, "nextCursorMark": cursor, "resultList": {"result": results}}).encode()


def enumerated(doi, kind, first="2021-03-01", pmcid=None):
    return {"id": doi[-1], "source": "MED", "doi": doi, "pmcid": pmcid, "title": "Review " + doi[-1],
            "firstPublicationDate": first, "pubYear": first[:4], "pubTypeList": {"pubType": [kind]}}


def refuse_live(*args):
    raise AssertionError("the caches hold every review: nothing is fetched live")


# The journals' live routes are on (as a fresh study requires); every review is in the caches, so none is used.
LIVE = LiveSources(europe_pmc=refuse_live, publisher=refuse_live)
LIVE_PMCIDS = {"10.5555/w7": "PMC900007"}


def lookups(params):
    """Europe PMC search answers for the abstract and metadata lookups (a record with an abstract for every DOI)."""
    results = []
    for part in params["query"].split(" OR "):
        if '"' in part:
            doi = part.split('"')[1]
            results.append(epmc(doi=doi, abstract="Live abstract.", title="Live title " + doi, pmcid=LIVE_PMCIDS.get(doi)))
    if params["query"].startswith("EXT_ID:"):
        results.append(epmc(pmid=params["query"].split(":")[1].split()[0], abstract="Live abstract by PMID."))
    return {"resultList": {"result": results}}


class FreshEnumerationTests(CacheTestCase):
    SPEC = {"window": {"from": "2020-01-01", "until": "2022-12-31"}, "pageSize": 2,
            "groups": [{"journal": JOURNAL, "query": 'ISSN:"0000-0001"', "stratum": "Review",
                        "strata": {"systematic-review": "Systematic Review"}},
                       {"journal": SECOND, "query": 'ISSN:"0000-0002"', "stratum": "Review"}]}

    def provider(self):
        pages = {('ISSN:"0000-0001"', "*"): search_page([enumerated(JATS_REVIEW, "review"),
                                                         enumerated(HTML_REVIEW, "systematic-review")], 3, "c2"),
                 ('ISSN:"0000-0001"', "c2"): search_page([{"id": "77", "source": "MED", "title": "No DOI"}], 3, "c3"),
                 ('ISSN:"0000-0002"', "*"): search_page([enumerated(CACHED_REVIEW, "review", "2020-06-01")], 1, "c9")}
        requests = []

        def get(params):
            requests.append(dict(params))
            query = params["query"].split(") AND ")[0].lstrip("(")
            return pages[(query, params["cursorMark"])]
        return get, requests

    def enumerate(self):
        get, requests = self.provider()
        manifest = self.root / "study" / "enumeration.jsonl"
        result = enumerate_reviews(self.SPEC, manifest, get, code_revision="test")
        return manifest, result, requests

    def fresh_inputs(self):
        """Enumerate, acquire with the live routes on, derive the works, look up abstracts and metadata."""
        manifest, result, _ = self.enumerate()
        enumeration = load_enumeration(Path(result["receipt"]))
        caches = self.caches()
        acquirer = ReviewAcquirer(caches, routes=parse_routes([f"{JOURNAL}=public", f"{SECOND}=entitled"]), live=LIVE)
        acquired = manifest.parent / "acquired.jsonl"
        acquire_records(manifest, acquired, acquirer)
        derived = manifest.parent / "works.jsonl"
        fill_works(acquired, derived)
        looked_up = manifest.parent / "abstracts.jsonl"
        fill_abstracts(derived, looked_up, lookups, cache=caches)
        return manifest, enumeration, caches, [json.loads(line) for line in looked_up.read_text().splitlines()]

    def census(self, database: Path, **kwargs) -> CensusRun:
        transport = HttpChatTransport(self.base, on_premises=kwargs.pop("on_premises", False))
        run = CensusRun(database, seed=1234, transport=transport, concurrency=4, sleep=lambda s: None, **kwargs)
        self.addCleanup(run.close)
        return run

    def test_enumerating_again_returns_the_finished_enumeration_or_starts_over(self):
        manifest, result, requests = self.enumerate()
        get, _ = self.provider()
        # The same spec again (a step submitted again after its process stopped): the finished enumeration, its kept
        # pages verified, and no new search.
        again = enumerate_reviews(self.SPEC, manifest, get, code_revision="test")
        self.assertEqual((True, result["records"], result["groups"]), (again["alreadyEnumerated"], again["records"],
                                                                       again["groups"]))
        # Another spec into the same output is refused.
        other = json.loads(json.dumps(self.SPEC))
        other["window"]["until"] = "2021-12-31"
        with self.assertRaisesRegex(ValidationError, "already exists"):
            enumerate_reviews(other, manifest, get, code_revision="test")
        # A manifest without its receipt (stopped before it finished) is enumerated again.
        Path(str(manifest) + ".receipt.json").unlink()
        restarted = enumerate_reviews(self.SPEC, manifest, get, code_revision="test")
        self.assertNotIn("alreadyEnumerated", restarted)
        self.assertEqual(result["records"], restarted["records"])
        load_enumeration(Path(restarted["receipt"]))

    def test_enumeration_keeps_its_pages_and_verifies_them(self):
        manifest, result, requests = self.enumerate()
        self.assertEqual(3, result["records"])
        self.assertEqual('(ISSN:"0000-0001") AND FIRST_PDATE:[2020-01-01 TO 2022-12-31]', requests[0]["query"])
        self.assertEqual(["*", "c2", "*"], [r["cursorMark"] for r in requests])
        records = [json.loads(line) for line in manifest.read_text().splitlines()]
        self.assertEqual([(JOURNAL, JATS_REVIEW, "Review"), (JOURNAL, HTML_REVIEW, "Systematic Review"),
                          (SECOND, CACHED_REVIEW, "Review")], [(r["journal"], r["doi"], r["stratum"]) for r in records])
        receipt = Path(result["receipt"])
        enumeration = load_enumeration(receipt)
        self.assertEqual(3, len(enumeration.reviews))
        self.assertEqual(1, json.loads(receipt.read_text())["groups"][0]["withoutDoi"])
        self.assertEqual(QUERY_RULE, json.loads(receipt.read_text())["queryRule"])
        # Enumerating the same spec again returns the finished enumeration (see the test above).
        self.assertTrue(enumerate_reviews(self.SPEC, manifest, self.provider()[0])["alreadyEnumerated"])
        page = next(Path(str(manifest) + ".pages").iterdir())
        page.chmod(0o644)
        page.write_bytes(page.read_bytes() + b" ")
        with self.assertRaisesRegex(ValidationError, "differs from its digest"):
            load_enumeration(receipt)

    def test_a_paging_glitch_restarts_the_group_from_its_first_page_and_the_receipt_records_it(self):
        from ocsl_review.errors import ProviderError
        get, requests = self.provider()
        glitches = {('ISSN:"0000-0001"', "c2"): [search_page([], 4, "c3")],   # the hit count changed between pages
                    ('ISSN:"0000-0002"', "*"): [ProviderError("Europe PMC search failed: HTTP 503"),
                                                b"<html>maintenance</html>"]}

        def glitching(params):
            pending = glitches.get((params["query"].split(") AND ")[0].lstrip("("), params["cursorMark"]))
            if pending:
                outcome = pending.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return get(params)
        waits = []
        manifest = self.root / "study" / "enumeration.jsonl"
        result = enumerate_reviews(self.SPEC, manifest, glitching, code_revision="test", sleep=waits.append)
        self.assertEqual([30.0, 30.0, 60.0], waits, "each group's restarts wait 30 s, then 60 s")
        self.assertEqual([1, 2], [group["restarts"] for group in result["groups"]])
        clean = self.root / "clean" / "enumeration.jsonl"
        expected = enumerate_reviews(self.SPEC, clean, self.provider()[0], code_revision="test")
        self.assertEqual(expected["records"], result["records"])
        self.assertEqual(clean.read_bytes(), manifest.read_bytes(), "a restarted group keeps no record of its glitch")
        receipt = json.loads(Path(result["receipt"]).read_text())
        first, second = (group["restarts"] for group in receipt["groups"])
        self.assertEqual([("enumeration population changed during paging", 30.0)],
                         [(item["reason"], item["waitSeconds"]) for item in first])
        self.assertEqual([("Europe PMC search failed: HTTP 503", 30.0),
                          ("enumeration page is not a Europe PMC result list", 60.0)],
                         [(item["reason"], item["waitSeconds"]) for item in second])
        self.assertTrue(all(item["failedAt"] <= item["restartedAt"] for item in first + second))
        self.assertEqual(["*", "c2"], [page["cursorMark"] for page in receipt["groups"][0]["pages"]],
                         "the receipt keeps the pages of the paging that completed")
        self.assertEqual(3, len(load_enumeration(Path(result["receipt"])).reviews))
        again = enumerate_reviews(self.SPEC, manifest, get, code_revision="test")
        self.assertEqual((True, [1, 2]), (again["alreadyEnumerated"], [g["restarts"] for g in again["groups"]]))

    def test_glitches_that_outlast_three_restarts_end_transient_and_write_no_receipt(self):
        from ocsl_review.errors import ProviderError, TransientError

        def short(params):
            return search_page([enumerated(JATS_REVIEW, "review")], 5, params["cursorMark"])
        calls = []

        def changing(params):
            calls.append(1)
            return search_page([enumerated(JATS_REVIEW, "review")], 2 + len(calls), "next%d" % len(calls))

        def down(params):
            raise ProviderError("Europe PMC search failed: URLError")
        for name, get, reason in (("short", short, "exhausted"), ("changing", changing, "changed during paging"),
                                  ("down", down, "search failed")):
            with self.subTest(name=name):
                waits = []
                output = self.root / name / "enumeration.jsonl"
                with self.assertRaisesRegex(TransientError, f"after 3 restarts.*{reason}"):
                    enumerate_reviews(self.SPEC, output, get, sleep=waits.append)
                self.assertEqual([30.0, 60.0, 120.0], waits)
                self.assertFalse(Path(str(output) + ".receipt.json").exists())
                self.assertFalse(output.exists())
        # A spec the enumeration cannot run is refused at once.
        waits = []
        with self.assertRaises(ValidationError):
            enumerate_reviews({"groups": []}, self.root / "c.jsonl", short, sleep=waits.append)
        self.assertEqual([], waits)

    def test_an_enumeration_that_stays_unavailable_exits_with_error_kind_transient(self):
        from ocsl_review.errors import EXIT_TRANSIENT, ProviderError

        def down(params):
            raise ProviderError("Europe PMC search failed: HTTP 503")
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(self.SPEC))
        result_file = self.root / "enumerate.result.json"
        with mock.patch.object(census_inputs, "search_pages", return_value=down), \
                mock.patch.object(census_inputs, "RESTART_WAITS", (0.0, 0.0, 0.0)), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--result-file", str(result_file), "study", "enumerate", "--spec", str(spec),
                         "--output", str(self.root / "study" / "enumeration.jsonl")])
        record = json.loads(result_file.read_text())
        self.assertEqual((EXIT_TRANSIENT, EXIT_TRANSIENT), (code, record["exitCode"]))
        self.assertEqual("transient", record["error"]["kind"])
        self.assertIn("3 restarts", record["error"]["message"])

    def test_enumeration_queries_select_a_population_never_single_reviews(self):
        for query in ('DOI:"10.5555/a" OR DOI:"10.5555/b"', '"10.5555/a" OR "10.5555/b"', "EXT_ID:1 AND SRC:MED",
                      'ISSN:"1" or ISSN:"2"', "pmcid:PMC1", 'ISSN:"1" AND (PUB_TYPE:"review"'):
            spec = {**self.SPEC, "groups": [{"journal": JOURNAL, "query": query, "stratum": "Review"}]}
            with self.subTest(query=query), self.assertRaisesRegex(ValidationError, "not population-level"):
                enumerate_reviews(spec, self.root / "refused.jsonl", self.provider()[0])
        self.assertEqual([], census_inputs.query_problems(
            '(ISSN:"0000-0001" OR JOURNAL:"Synthetic Journal") AND PUB_TYPE:"review" AND NOT PUB_TYPE:"erratum"'))
        with self.assertRaisesRegex(ValidationError, "window field"):
            enumerate_reviews({**self.SPEC, "window": {"from": "2020-01-01", "until": "2021-01-01", "field": "DOI"}},
                              self.root / "window.jsonl", self.provider()[0])
        self.assertFalse((self.root / "refused.jsonl").exists())
        manifest, result, _ = self.enumerate()
        # A receipt whose spec lists reviews one by one is refused when a study would pin it.
        receipt = json.loads(Path(result["receipt"]).read_text())
        receipt["spec"]["groups"][0]["query"] = 'DOI:"%s"' % JATS_REVIEW
        forged = manifest.parent / "forged.receipt.json"
        forged.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValidationError, "not population-level"):
            load_enumeration(forged)
        # The rule is pinned with the enumeration and checked by the gates.
        run = self.run_census()
        empty = manifest.parent / "empty.jsonl"
        empty.write_text("")
        prepare(run, empty, enumeration=load_enumeration(Path(result["receipt"])))
        run.close()
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        self.assertEqual(QUERY_RULE, status["enumeration"]["queryRule"])
        self.assertTrue(gates(status)["checks"]["enumerationQueriesPopulationLevel"])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM meta WHERE key='enumerationQueryRule'")
        with closing(scores_db.open_readonly(self.database)) as connection:
            self.assertFalse(gates(census_status(connection))["checks"]["enumerationQueriesPopulationLevel"])

    def test_fresh_study_prepares_only_enumerated_acquired_reviews_and_compares_afterwards(self):
        manifest, enumeration, caches, rows = self.fresh_inputs()
        self.assertEqual(["cache-hit-public", "cache-hit-public", "cache-hit-entitled"],
                         [row["acquisition"]["provenance"] for row in rows])
        self.assertTrue(all(row["acquisition"]["liveRoute"] for row in rows))
        # Works derived from the review's own bibliography, metadata and abstracts from recorded lookups.
        self.assertEqual((WORKS_DERIVATION, "derived", 17, 1),
                         tuple(rows[0]["worksDerivation"][k] for k in ("derivation", "state", "entries", "withoutIdentifier")))
        one, seven = rows[0]["works"]["doi:10.5555/w1"], rows[0]["works"]["doi:10.5555/w7"]
        self.assertEqual(("cache-hit-public", "provider-cache", "A cited work"),
                         (one["metadataProvenance"]["provenance"], one["metadataProvenance"]["cache"], one["title"]))
        self.assertEqual(("live-public", "Live title 10.5555/w7", "PMC900007"),
                         (seven["metadataProvenance"]["provenance"], seven["title"], seven["pmcid"]))
        labels = {work["abstractProvenance"]["provenance"] for work in rows[0]["works"].values()}
        self.assertEqual({"cache-hit-public", "live-public"}, labels)
        extra = [{**rows[0], "doi": "10.5555/not.enumerated"},
                 {**rows[0], "stratum": "Mini Review"},
                 {key: value for key, value in rows[1].items() if key != "works"},
                 {key: value for key, value in rows[2].items() if key != "enumeration"}]
        source = manifest.parent / "prepared-input.jsonl"
        source.write_text("".join(json.dumps(row) + "\n" for row in rows + extra))
        run = self.run_census()
        # Cache-hit claims that cannot be re-checked (no caches given) are refused in a fresh-enumeration study.
        blind = prepare(run, source, enumeration=enumeration)
        self.assertEqual({"skipped-provenance-unverified": 3, "skipped-not-enumerated": 3, "skipped-no-works": 1},
                         blind["states"])
        self.assertEqual(0, run.connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0])
        out = prepare(run, source, caches=caches, enumeration=enumeration)
        self.assertEqual({"prepared": 3, "skipped-not-enumerated": 3, "skipped-no-works": 1}, out["states"])
        reasons = sorted(problem.get("reason", "")[:20] for problem in out["problems"])
        self.assertIn("stratum differs from", reasons)
        self.assertEqual(3, run.connection.execute("SELECT COUNT(*) FROM enumeration").fetchone()[0])
        with self.assertRaisesRegex(ValidationError, "pinned to a fresh enumeration"):
            prepare(run, source, caches=caches)
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        verdict = gates(status, require_fresh_enumeration=True)["checks"]
        for name in ("freshEnumerationPinned", "reviewsComeFromTheEnumeration", "everyReviewHasAcquisitionProvenance",
                     "cacheHitsVerified", "everyAbstractFromARecordedLookup", "noCarriedIdentityMapsOrCountries",
                     "everyWorkDerivedFromItsReview", "everyWorkMetadataFromARecordedLookup",
                     "reviewsAcquiredThroughLiveRoutes", "fullTextCacheHitsBackedByLiveRetrieval",
                     "enumerationQueriesPopulationLevel", "everyEnumeratedReviewInStudyOrExcluded"):
            self.assertTrue(verdict[name], name)
        self.assertEqual(0, status["inputChecks"]["abstractsWithoutLookup"])
        self.assertEqual({"sha256": enumeration.sha256, "reviews": 3, "inStudy": 3, "notInStudy": 0, "excluded": 0,
                          "unexplained": 0, "reviewsOutsideEnumeration": 0, "queryRule": QUERY_RULE},
                         {k: v for k, v in status["enumeration"].items() if k not in ("populationSha256", "settled")})
        self.assertEqual((0, None), (status["enumeration"]["settled"]["total"],
                                     status["enumeration"]["settled"]["maxPercent"]))
        self.assertTrue(verdict["settledReviewsWithinBound"])
        stored = manifest.parent / "stored.csv"
        with stored.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["doi", "journal", "stratum", "abs_class"])
            writer.writerows([[JATS_REVIEW, JOURNAL, "Review", "x"], ["10.5555/stored.only", JOURNAL, "Review", "x"],
                              ["10.5555/elsewhere", "Another Journal", "Review", "x"], ["not-a-doi", JOURNAL, "", ""]])
        run.close()
        with self.assertRaisesRegex(ValidationError, "after the census run is complete"):
            record_comparison(self.database, stored, manifest.parent / "early.json")
        early = record_comparison(self.database, stored, manifest.parent / "early.json", label="early", allow_incomplete=True)
        self.assertEqual({"fresh": 3, "stored": 3, "inBoth": 1, "freshOnly": 2, "storedOnly": 2}, early["totals"])
        self.assertFalse(early["runComplete"])
        self.assertEqual({"fresh": 2, "stored": 2, "inBoth": 1, "freshOnly": 1, "storedOnly": 1, "inStudy": 2, "excluded": 0},
                         early["byJournal"][JOURNAL])
        self.assertEqual(1, early["byJournal"]["(unattributed)"]["storedOnly"])
        report = json.loads((manifest.parent / "early.json").read_text())
        self.assertEqual((["10.5555/elsewhere", "10.5555/stored.only"], 0, 1), (report["storedOnly"], report["imported"],
                                                                                report["stored"]["invalid"]))
        self.assertEqual((3, 0, 0), (report["population"]["inStudy"], report["population"]["excluded"],
                                     report["population"]["unexplained"]))
        reviews = {row["doi"] for row in self.rows("SELECT doi FROM reviews")}
        self.assertEqual({JATS_REVIEW, HTML_REVIEW, CACHED_REVIEW}, reviews, "nothing of the stored manifest is imported")
        with self.assertRaisesRegex(ValidationError, "already recorded"):
            record_comparison(self.database, stored, manifest.parent / "again.json", label="early", allow_incomplete=True)
        # The first comparison froze the population: nothing is prepared or excluded after it.
        run = self.run_census()
        with self.assertRaisesRegex(ValidationError, "population is frozen"):
            prepare(run, source, caches=caches, enumeration=enumeration)
        run.close()
        with self.assertRaisesRegex(ValidationError, "population is frozen"):
            record_exclusions(self.database, [{"journal": JOURNAL, "doi": HTML_REVIEW, "reason": "late"}])
        # Finish the run (the entitled review needs an on-premises judge), then compare, freeze and export facts.
        run = self.run_census(on_premises=True)
        self.assertEqual("completed", run.run_abstract()["state"])
        run.pin_inputs(caches)
        with self.assertRaisesRegex(ValidationError, "stand in only for downloads"):
            run.run_full_text(cache=caches)
        self.assertEqual("completed", run.run_full_text(fetch_xml=lambda pmcid: None, cache=caches)["state"])
        run.close()
        final = record_comparison(self.database, stored, manifest.parent / "final.json", label="final")
        self.assertTrue(final["runComplete"])
        # The same comparison again returns what it recorded, also when the earlier process stopped between recording
        # it and renaming its report into place; another stored manifest under the label is refused.
        again = record_comparison(self.database, stored, manifest.parent / "final.json", label="final")
        self.assertEqual((True, final["reportSha256"], final["totals"]), (again["alreadyRecorded"], again["reportSha256"],
                                                                           again["totals"]))
        (manifest.parent / "final.json").rename(manifest.parent / ".final.json.incoming")
        renamed = record_comparison(self.database, stored, manifest.parent / "final.json", label="final")
        self.assertEqual(final["byJournal"], renamed["byJournal"])
        self.assertTrue((manifest.parent / "final.json").is_file())
        other = manifest.parent / "other-stored.txt"
        other.write_text(JATS_REVIEW + "\n")
        with self.assertRaisesRegex(ValidationError, "already recorded"):
            record_comparison(self.database, other, manifest.parent / "final.json", label="final")
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        checks = gates(status, require_fresh_enumeration=True)["checks"]
        self.assertTrue(checks["populationUnchangedSinceComparison"])
        self.assertTrue(checks["fullTextCacheHitsBackedByLiveRetrieval"])
        self.assertEqual(1, status["itemProvenance"]["full-text"]["cache-hit-public"])
        frozen = self.root / "frozen.sqlite3"
        scores_db.freeze(self.database, frozen)
        export = corpus_facts.export_facts(frozen, self.root / "facts", [JOURNAL, SECOND], b=50, seed=3,
                                           code_revision="test")
        facts = json.loads((self.root / "facts" / "facts.json").read_text())["facts"]
        j, s = "census.synthetic-journal-of-examples", "census.second-synthetic-journal"

        def value(key):
            return facts[key]["value"]
        self.assertEqual((2, 0, 2), (value(f"{j}.provenance.reviews.cache-hit-public"),
                                     value(f"{j}.provenance.reviews.live-public"),
                                     value(f"{j}.provenance.reviews_read.cache-hit-public")))
        self.assertEqual((1, 1), (value(f"{j}.provenance.stratum.review.reviews.cache-hit-public"),
                                  value(f"{j}.provenance.stratum.systematic-review.reviews.cache-hit-public")))
        self.assertEqual(1, value(f"{s}.provenance.reviews.cache-hit-entitled"))
        self.assertEqual(2, value(f"{j}.coverage.acquisition.cache-hit-public.reviews"))
        self.assertGreater(value(f"{j}.provenance.abstracts.cache-hit-public"), 0)
        self.assertGreater(value(f"{j}.provenance.abstracts.live-public"), 0)
        self.assertEqual((0, 0, 0), (value(f"{j}.provenance.abstracts.supplied"),
                                     value(f"{j}.provenance.abstracts.cache-hit-entitled"),
                                     value(f"{j}.provenance.abstracts.unrecorded")))
        # Work seven's cached full text (retrieved live as well) gave a window to its escalated entries in both reviews.
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM citations WHERE journal=? AND ft_provenance='cache-hit-public' "
                                   "AND cited_work_key='doi:10.5555/w7'", JOURNAL)[0]["n"],
                         value(f"{j}.provenance.full_text.cache-hit-public"))
        self.assertGreaterEqual(value(f"{j}.provenance.full_text.cache-hit-public"), 1)
        self.assertGreater(value(f"{j}.provenance.full_text.unavailable"), 0)
        self.assertEqual((0, 0), (value(f"{j}.provenance.full_text.unrecorded"),
                                  value(f"{j}.provenance.reviews_read.unrecorded")))
        self.assertEqual((2, 2, 0, 0, 0), (value(f"{j}.enumeration.reviews"), value(f"{j}.enumeration.in_study"),
                                           value(f"{j}.enumeration.outside"), value(f"{j}.enumeration.excluded"),
                                           value(f"{j}.enumeration.unexplained")))
        self.assertEqual(1, value(f"{j}.enumeration.stratum.systematic-review.reviews"))
        self.assertEqual((2, 1, 1, 2), (value(f"{j}.manifest_comparison.final.all.stored"),
                                        value(f"{j}.manifest_comparison.final.all.in_both"),
                                        value(f"{j}.manifest_comparison.final.review.stored_only"),
                                        value(f"{j}.manifest_comparison.final.all.in_study")))
        self.assertEqual((3, 2, 3), (value("census.manifest_comparison.final.stored"),
                                     value("census.manifest_comparison.final.stored_only"),
                                     value("census.manifest_comparison.final.in_study")))
        inputs = facts[f"{j}.provenance.reviews.cache-hit-public"]["inputs"]
        self.assertEqual((caches.store.version["merkleRoot"], enumeration.sha256),
                         (inputs["inputStoreMerkleRoot"], inputs["enumerationSha256"]))
        self.assertEqual({"ok": True, "facts": export["count"]}, corpus_facts.verify_export(self.root / "facts" / "facts.json"))

    def test_a_review_without_a_pmcid_is_prepared_from_its_pmc_copy_and_its_enumeration_still_verifies(self):
        spec = {"pageSize": 10, "groups": [{"journal": JOURNAL, "query": 'ISSN:"0000-0001"', "stratum": "Review"}]}
        review = {"id": "42920861", "source": "MED", "pmid": "42920861", "doi": COPY_REVIEW, "title": COPY_TITLE,
                  "firstPublicationDate": "2021-03-01", "pubYear": "2021", "pubTypeList": {"pubType": ["review"]}}
        copy = pmc_record(COPY_PMCID)                 # Europe PMC lists the review's copy in PMC without a DOI
        manifest = self.root / "study" / "enumeration.jsonl"
        result = enumerate_reviews(spec, manifest, lambda params: search_page([review, copy], 2, "c2"),
                                   code_revision="test")
        group = json.loads(Path(result["receipt"]).read_text())["groups"][0]
        self.assertEqual((1, 1, [COPY_PMCID]), (result["records"], group["withoutDoi"], group["withoutDoiIds"]))
        caches = self.caches()
        copies = PmcCopies([copy], {COPY_PMCID: copy_jats(COPY_PMCID, COPY_REVIEW, "42920861")})
        live = LiveSources(europe_pmc=copies.fetch, publisher=refuse_live, search=copies.search)
        acquired = manifest.parent / "acquired.jsonl"
        self.assertEqual(1, acquire_records(manifest, acquired, ReviewAcquirer(
            caches, routes=parse_routes([f"{JOURNAL}=public"]), live=live))["pmcCopies"])
        derived = manifest.parent / "works.jsonl"
        fill_works(acquired, derived)
        looked_up = manifest.parent / "abstracts.jsonl"
        fill_abstracts(derived, looked_up, lookups, cache=caches)
        # Every prepare derives the population again from the kept pages, which the acquisition left unchanged.
        enumeration = load_enumeration(Path(result["receipt"]))
        run = self.run_census()
        self.assertEqual({"prepared": 1}, prepare(run, looked_up, caches=caches, enumeration=enumeration)["states"])
        run.close()
        with closing(scores_db.open_readonly(self.database)) as connection:
            checks = gates(census_status(connection), require_fresh_enumeration=True)["checks"]
        for name in ("freshEnumerationPinned", "reviewsComeFromTheEnumeration", "everyReviewHasAcquisitionProvenance",
                     "reviewsAcquiredThroughLiveRoutes", "everyWorkDerivedFromItsReview",
                     "everyEnumeratedReviewInStudyOrExcluded"):
            self.assertTrue(checks[name], name)
        row = self.rows("SELECT doi, provenance, provider, source_url, detail_json FROM input_provenance "
                        "WHERE kind='review'")[0]
        self.assertEqual((COPY_REVIEW, "live-public", "europe-pmc", FULL_TEXT % COPY_PMCID),
                         (row["doi"], row["provenance"], row["provider"], row["source_url"]))
        self.assertEqual({"by": "title+jats-doi", "pmcid": COPY_PMCID, "search": COPY_QUERY},
                         json.loads(row["detail_json"])["link"])

    def test_a_trimmed_or_empty_population_never_counts_as_complete(self):
        manifest, enumeration, caches, rows = self.fresh_inputs()
        stored = manifest.parent / "stored.txt"
        stored.write_text(f"{JATS_REVIEW}\n")
        empty = manifest.parent / "empty.jsonl"
        empty.write_text("")
        run = self.run_census()
        prepare(run, empty, caches=caches, enumeration=enumeration)       # pins the enumeration, prepares nothing
        run.close()
        with self.assertRaisesRegex(ValidationError, "3 enumerated reviews are neither in the study nor excluded"):
            record_comparison(self.database, stored, manifest.parent / "empty.json")
        # Only the review the stored manifest also lists, judged to the end: still a trimmed population.
        only = manifest.parent / "only.jsonl"
        only.write_text(json.dumps(rows[0]) + "\n")
        run = self.run_census()
        self.assertEqual(1, prepare(run, only, caches=caches, enumeration=enumeration)["prepared"])
        self.assertEqual("completed", run.run_abstract()["state"])
        run.pin_inputs(caches)
        self.assertEqual("completed", run.run_full_text(fetch_xml=lambda pmcid: None, cache=caches)["state"])
        run.close()
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        checks = gates(status, require_fresh_enumeration=True)
        self.assertFalse(checks["checks"]["everyEnumeratedReviewInStudyOrExcluded"])
        self.assertFalse(checks["ok"])
        self.assertIn("2 enumerated reviews are neither in the study nor excluded", checks["notes"])
        with self.assertRaisesRegex(ValidationError, "2 enumerated reviews are neither in the study nor excluded"):
            record_comparison(self.database, stored, manifest.parent / "trimmed.json")
        with self.assertRaisesRegex(ValidationError, "incomplete"):
            scores_db.freeze(self.database, self.root / "refused.sqlite3")
        # Protocol exclusions, each with its reason, complete the population and are counted.
        for bad, message in (({"journal": JOURNAL, "doi": JATS_REVIEW, "reason": "x"}, "already in the study"),
                             ({"journal": JOURNAL, "doi": "10.5555/stranger", "reason": "x"}, "not a review of the pinned"),
                             ({"journal": JOURNAL, "doi": HTML_REVIEW, "reason": " "}, "needs journal, doi and a reason")):
            with self.subTest(message=message), self.assertRaisesRegex(ValidationError, message):
                record_exclusions(self.database, [bad])
        exclusions = manifest.parent / "exclusions.jsonl"
        exclusions.write_text(json.dumps({"journal": JOURNAL, "doi": HTML_REVIEW, "reason": "stratum outside the protocol"})
                              + "\n" + json.dumps({"journal": SECOND, "doi": CACHED_REVIEW, "reason": "journal outside "
                                                   "the protocol"}) + "\n")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(0, main(["study", "exclude", "--database", str(self.database), "--input", str(exclusions)]))
        result = json.loads(buffer.getvalue())
        self.assertEqual((2, 0, 2, 0), (result["excluded"], result["already"], result["population"]["excluded"],
                                        result["population"]["unexplained"]))
        again = record_exclusions(self.database, [{"journal": SECOND, "doi": CACHED_REVIEW, "reason": "journal outside the protocol"}])
        self.assertEqual((0, 1), (again["excluded"], again["already"]))
        with self.assertRaisesRegex(ValidationError, "another reason"):
            record_exclusions(self.database, [{"journal": SECOND, "doi": CACHED_REVIEW, "reason": "changed my mind"}])
        final = record_comparison(self.database, stored, manifest.parent / "final.json")
        self.assertTrue(final["runComplete"])
        self.assertEqual((1, 2, 0), (final["population"]["inStudy"], final["population"]["excluded"],
                                     final["population"]["unexplained"]))
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        checks = gates(status, require_fresh_enumeration=True)["checks"]
        self.assertTrue(checks["everyEnumeratedReviewInStudyOrExcluded"] and checks["populationUnchangedSinceComparison"])
        frozen = self.root / "frozen.sqlite3"
        scores_db.freeze(self.database, frozen)
        corpus_facts.export_facts(frozen, self.root / "facts", [JOURNAL], b=20, seed=3, code_revision="test")
        facts = json.loads((self.root / "facts" / "facts.json").read_text())["facts"]
        j = "census.synthetic-journal-of-examples"
        self.assertEqual((1, 1, 0), (facts[f"{j}.enumeration.in_study"]["value"], facts[f"{j}.enumeration.excluded"]["value"],
                                     facts[f"{j}.enumeration.unexplained"]["value"]))
        self.assertEqual(1, facts[f"{j}.manifest_comparison.stored.all.excluded"]["value"])
        # Changing the population after the comparison fails the gates.
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM exclusions WHERE doi=?", (CACHED_REVIEW,))
        with closing(scores_db.open_readonly(self.database)) as connection:
            checks = gates(census_status(connection), require_fresh_enumeration=True)["checks"]
        self.assertFalse(checks["populationUnchangedSinceComparison"])
        self.assertFalse(checks["everyEnumeratedReviewInStudyOrExcluded"])

    def test_settling_reviews_a_run_cannot_prepare_completes_the_population(self):
        manifest, enumeration, caches, rows = self.fresh_inputs()
        # One review is prepared; one whose abstract lookups failed in transport cannot be; one is not in the input.
        failed = json.loads(json.dumps(rows[1]))
        first = sorted(failed["works"])[0]
        failed["works"][first]["europePmcOutcome"] = "europe-pmc-failed"
        source = manifest.parent / "settle.jsonl"
        source.write_text(json.dumps(rows[0]) + "\n" + json.dumps(failed) + "\n")
        run = self.run_census()
        with self.assertRaisesRegex(ValidationError, "needs a fresh enumeration"):
            prepare(run, source, caches=caches, settle=True)
        out = prepare(run, source, caches=caches, enumeration=enumeration, settle=True)
        self.assertEqual({"prepared": 1, "skipped-abstract-lookup-failed": 1}, out["states"])
        self.assertEqual({"absent-from-input": 1, "skipped-abstract-lookup-failed": 1}, out["settled"])
        reviews = {row["doi"]: row for row in self.rows("SELECT doi, provenance, provenance_class, extraction_json, "
                                                         "abstract_state, full_text_state, stratum FROM reviews")}
        self.assertEqual({JATS_REVIEW, HTML_REVIEW, CACHED_REVIEW}, set(reviews))
        for doi, state in ((HTML_REVIEW, "skipped-abstract-lookup-failed"), (CACHED_REVIEW, "absent-from-input")):
            row = reviews[doi]
            self.assertEqual(("unavailable", "unavailable", "completed", "completed"),
                             (row["provenance"], row["provenance_class"], row["abstract_state"], row["full_text_state"]))
            self.assertEqual(state, json.loads(row["extraction_json"])["notPrepared"]["state"])
        # The settled review keeps the enumeration's own attributes.
        self.assertEqual("Systematic Review", reviews[HTML_REVIEW]["stratum"])
        self.assertIn("cited work(s) failed", json.loads(reviews[HTML_REVIEW]["extraction_json"])["notPrepared"]["reason"])
        # Preparing again settles nothing more.
        again = prepare(run, source, caches=caches, enumeration=enumeration, settle=True)
        self.assertEqual({}, again["settled"])
        run.close()
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        verdict = gates(status, require_fresh_enumeration=True)["checks"]
        for name in ("everyEnumeratedReviewInStudyOrExcluded", "reviewsAcquiredThroughLiveRoutes",
                     "everyReviewHasAcquisitionProvenance", "reviewsComeFromTheEnumeration", "freshEnumerationPinned"):
            self.assertTrue(verdict[name], name)
        self.assertEqual((3, 0, 0), (status["enumeration"]["inStudy"], status["enumeration"]["excluded"],
                                     status["enumeration"]["unexplained"]))
        self.assertEqual(2, status["inputChecks"]["reviewsSettledAsNotPrepared"])
        # Settled reviews without a pinned bound never pass the bound check.
        self.assertFalse(verdict["settledReviewsWithinBound"])
        self.assertEqual((2, None, {"absent-from-input": 1, "skipped-abstract-lookup-failed": 1}),
                         (status["enumeration"]["settled"]["total"], status["enumeration"]["settled"]["maxPercent"],
                          status["enumeration"]["settled"]["byReason"]))
        # The command line: settling needs a fresh enumeration.
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(["study", "prepare", "--database", str(self.database), "--seed", "1234", "--input", str(source),
                         "--settle-unprepared"])
        self.assertNotEqual(0, code)

    def test_the_settled_share_stays_within_the_protocols_bound(self):
        manifest, enumeration, caches, rows = self.fresh_inputs()
        source = manifest.parent / "bound.jsonl"
        source.write_text(json.dumps(rows[0]) + "\n")       # two of three enumerated reviews are not in the input
        outcomes = {}
        for percent in (50.0, 100.0):
            database = self.root / f"bound-{percent:g}.sqlite3"
            run = self.census(database)
            out = prepare(run, source, caches=caches, enumeration=enumeration, settle=True, max_settled_percent=percent)
            self.assertEqual({"absent-from-input": 2}, out["settled"])
            # A later prepare repeats the bound and never changes it.
            prepare(run, source, caches=caches, enumeration=enumeration, settle=True, max_settled_percent=percent)
            with self.assertRaisesRegex(ValidationError, "never changed"):
                prepare(run, source, caches=caches, enumeration=enumeration, settle=True, max_settled_percent=10)
            run.close()
            with closing(scores_db.open_readonly(database)) as connection:
                status = census_status(connection)
            outcomes[percent] = gates(status, require_fresh_enumeration=True)
            settled = status["enumeration"]["settled"]
            self.assertEqual((2, percent), (settled["total"], settled["maxPercent"]))
            # The journal of the prepared review settled one of two; the other journal settled its only review.
            self.assertEqual({JOURNAL: {"settled": 1, "enumerated": 2, "share": 0.5},
                              SECOND: {"settled": 1, "enumerated": 1, "share": 1.0}}, settled["byJournal"])
        self.assertFalse(outcomes[50.0]["checks"]["settledReviewsWithinBound"])
        self.assertTrue(outcomes[100.0]["checks"]["settledReviewsWithinBound"])
        self.assertTrue(any("the protocol's bound is 50 %" in note for note in outcomes[50.0]["notes"]))
        # A bound belongs to settling, from code and from the command line.
        run = self.census(self.root / "no-settle.sqlite3")
        with self.assertRaisesRegex(ValidationError, "belongs to settling"):
            prepare(run, source, caches=caches, enumeration=enumeration, max_settled_percent=5)
        run.close()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["study", "prepare", "--database", str(self.root / "cli.sqlite3"), "--seed", "1234", "--input",
                         str(source), "--require-fresh-enumeration", "--enumeration", str(manifest) + ".receipt.json",
                         "--max-settled-percent", "5"])
        self.assertNotEqual(0, code)

    def test_fresh_inputs_are_derived_in_the_run_never_carried(self):
        manifest, enumeration, caches, rows = self.fresh_inputs()
        jats = rows[0]
        one = jats["works"]["doi:10.5555/w1"]
        cache_only = ReviewAcquirer(caches, routes=parse_routes([f"{JOURNAL}=public", f"{SECOND}=entitled"]))
        acquire_records(manifest, manifest.parent / "cache-only.jsonl", cache_only)
        cached = json.loads((manifest.parent / "cache-only.jsonl").read_text().splitlines()[0])
        self.assertFalse(cached["acquisition"]["liveRoute"])
        variants = {
            "skipped-carried-inputs": {**jats, "identities": {"1": {"workKey": "doi:10.5555/w1", "identity": "resolved"}},
                                       "country": "Carried Over"},
            "skipped-works-not-derived": {**jats, "works": record()["works"]},
            "skipped-cache-only-acquisition": {**cached, "works": jats["works"]},
        }
        changed = {**jats, "works": {**jats["works"], "doi:10.5555/w1": {**one, "pmid": "999999"}}}
        for index, (state, row) in enumerate([*variants.items(), ("skipped-works-not-derived", changed)]):
            source = manifest.parent / f"variant-{index}.jsonl"
            source.write_text(json.dumps(row) + "\n")
            run = self.census(self.root / f"variant-{index}.sqlite3")
            with self.subTest(state=state, index=index):
                result = prepare(run, source, caches=caches, enumeration=enumeration)
                self.assertEqual({state: 1}, result["states"])
                if index == 0:
                    self.assertIn("identities, country", result["problems"][0]["reason"])
                if index == 3:
                    self.assertIn("changed its pmid", result["problems"][0]["reason"])
            run.close()
        # Metadata edited after its lookup counts as supplied, which the gates refuse in a fresh study.
        edited = {**jats, "works": {**jats["works"], "doi:10.5555/w1": {**one, "title": "Copied from elsewhere"}}}
        source = manifest.parent / "edited.jsonl"
        source.write_text(json.dumps(edited) + "\n")
        run = self.census(self.root / "edited.sqlite3")
        self.assertEqual(1, prepare(run, source, caches=caches, enumeration=enumeration)["prepared"])
        self.assertEqual("supplied", run.connection.execute(
            "SELECT metadata_provenance FROM works WHERE work_key='doi:10.5555/w1'").fetchone()[0])
        status = census_status(run.connection)
        run.close()
        checks = gates(status)["checks"]
        self.assertFalse(checks["everyWorkMetadataFromARecordedLookup"])
        self.assertTrue(checks["everyWorkDerivedFromItsReview"] and checks["noCarriedIdentityMapsOrCountries"])
        status["inputChecks"]["suppliedIdentityMaps"] = 17      # a database prepared around these rules
        self.assertFalse(gates(status)["checks"]["noCarriedIdentityMapsOrCountries"])

    def test_stored_manifests_are_read_for_identities_only(self):
        folder = self.root / "stored"
        folder.mkdir()
        rows = [{"doi": "https://doi.org/10.5555/A", "journal": "J", "stratum": "Review", "abs_class": "x"},
                {"doi": "10.5555/a", "journal": "J"}, {"doi": "not a doi"}, ["not", "a", "record"]]
        (folder / "m.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        (folder / "m.json").write_text(json.dumps({"reviews": rows}))
        (folder / "m.tsv").write_text("DOI\tJournal\tStratum\n10.5555/a\tJ\tReview\n\tJ\t\n")
        for name, invalid in (("m.jsonl", 2), ("m.json", 2), ("m.tsv", 1)):
            records, info = census_inputs.load_stored_manifest(folder / name)
            self.assertEqual(([{"doi": "10.5555/a", "journal": "J", "stratum": "Review"}], invalid, 1),
                             (records, info["invalid"], info["records"]), name)
            self.assertEqual(census_inputs.sha256_bytes((folder / name).read_bytes()), info["sha256"])
        (folder / "bad.json").write_text("{")
        with self.assertRaises(ValidationError):
            census_inputs.load_stored_manifest(folder / "bad.json")
        with self.assertRaisesRegex(ValidationError, "label"):
            record_comparison(self.database, folder / "m.tsv", folder / "out.json", label="Not A Label")

    def test_compare_manifests_counts_by_journal_and_stratum(self):
        fresh = [{"journal": "A", "doi": "1", "stratum": "x", "inStudy": True},
                 {"journal": "A", "doi": "2", "stratum": "y", "excluded": True}]
        stored = [{"journal": "a", "doi": "2", "stratum": "y"}, {"journal": "A ", "doi": "3", "stratum": "x"},
                  {"journal": None, "doi": "4", "stratum": None}]
        result = compare_manifests(fresh, stored)
        self.assertEqual({"fresh": 2, "stored": 3, "inBoth": 1, "freshOnly": 1, "storedOnly": 2}, result["totals"])
        self.assertEqual({"fresh": 1, "stored": 1, "inBoth": 0, "freshOnly": 1, "storedOnly": 1, "inStudy": 1, "excluded": 0},
                         result["byJournal"]["A"]["strata"]["x"])
        self.assertEqual((1, 1), (result["byJournal"]["A"]["inStudy"], result["byJournal"]["A"]["excluded"]))
        self.assertEqual(1, result["byJournal"]["(unattributed)"]["storedOnly"])


class FakeResponse:
    def __init__(self, status, body=b"", url=None):
        self.status, self.body, self.url = status, body, url

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


LICENSED = "Licensed Synthetic Journal"
LICENSED_REVIEW = "10.5555/licensed.review"
ARTICLE = "https://www.nature.com/articles/synthetic-review-1"
SESSION_VALUE = "session-value-kept-in-memory"


class FakeHTTPResponse:
    """What http.client returns for one hop: status, headers (Set-Cookie may repeat) and a bounded body."""

    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = http.client.HTTPMessage()
        for name, value in headers:
            self.headers[name] = value
        self.body = io.BytesIO(body)

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, site, host):
        self.site, self.host, self.sock = site, host, None

    def request(self, method, target, headers=None):
        self.target, self.headers = target, dict(headers or {})
        self.site.requests.append((self.host, target, dict(self.headers)))

    def getresponse(self):
        return FakeHTTPResponse(*self.site.serve(self.host, self.target, self.headers))

    def close(self):
        pass


class LicensedSite:
    """nature.com's sign-in flow behind fake HTTPS connections, with lookalike hosts beside it.

    A licensed page redirects to idp.nature.com, which sets the session cookie for .nature.com and sends the client
    back; the page is served under the licence only when the cookie comes back with it. A client that does not return
    it ends on ``?error=cookies_not_supported`` with the page outside the licence (a paywall page).
    """

    def __init__(self):
        self.requests = []

    def connection(self, host, port=443, timeout=None, context=None):
        return FakeConnection(self, host)

    def sent_cookies(self, host):
        return [headers.get("Cookie") for seen, _, headers in self.requests if seen == host]

    def serve(self, host, target, headers):
        path, _, query = target.partition("?")
        signed_in = "idp_session=" + SESSION_VALUE in headers.get("Cookie", "")
        sign_in = ("Location", "https://idp.nature.com/authorize?response_type=cookie&client_id=synthetic&redirect_uri="
                   + urllib.parse.quote("https://www.nature.com" + path, safe=""))
        if host == "doi.org":
            return 302, [("Location", ARTICLE)]
        if host == "www.nature.com" and path == "/articles/synthetic-review-1":
            if signed_in:
                # A host-only cookie: RFC 6265 returns it to www.nature.com alone.
                return 200, [("Set-Cookie", "page_view=1; Path=/; Secure")], html_review(LICENSED_REVIEW)
            if "error=cookies_not_supported" in query:
                return 200, [], PAYWALL % LICENSED_REVIEW.encode()
            return 303, [sign_in]
        if host == "www.nature.com" and path == "/articles/synthetic-review-1/tables/1":
            return (200, [], TABLE_PAGE) if signed_in else (303, [sign_in])
        if host == "idp.nature.com" and path == "/authorize":
            back = dict(urllib.parse.parse_qsl(query))["redirect_uri"]
            return 302, [("Location", "/transit?code=transit-1&redirect_uri=" + urllib.parse.quote(back, safe="")),
                         ("Set-Cookie", f"idp_session={SESSION_VALUE}; Domain=.nature.com; Path=/; Secure; HttpOnly"),
                         # A sibling subdomain and another site: neither may be set from idp.nature.com.
                         ("Set-Cookie", "sibling=1; Domain=www2.nature.com; Path=/; Secure"),
                         ("Set-Cookie", "foreign=1; Domain=example.org; Path=/; Secure")]
        if host == "idp.nature.com" and path == "/transit":
            back = dict(urllib.parse.parse_qsl(query))["redirect_uri"]
            if "idp_session=" + SESSION_VALUE in headers.get("Cookie", ""):
                return 302, [("Location", back)]
            return 302, [("Location", back + "?error=cookies_not_supported&code=transit-1")]
        if host in ("evilnature.com", "nature.com.example"):
            # Lookalike hosts claim nature.com for their cookies.
            return 200, [("Set-Cookie", "lookalike=1; Domain=nature.com; Path=/; Secure"),
                         ("Set-Cookie", "suffix=1; Domain=.nature.com; Path=/; Secure")], b"<html/>"
        return 404, []


class LicensedPageTests(unittest.TestCase):
    """Entitled nature.com pages through the licence: one in-memory session per acquisition, nature.com hosts only."""

    def setUp(self):
        self.site = LicensedSite()
        patcher = mock.patch("ocsl_review.fulltext._PublicHTTPSConnection", side_effect=self.site.connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("PUPPENCLAW_ORCHESTRATION_SOCKET", None)
        self.waits = []

    def live(self, session=True):
        return LiveSources(publisher=census_inputs.page_fetcher(spacing=1.0, sleep=self.waits.append),
                           session=census_inputs.session_cookies() if session else None)

    def acquire(self, live, route="entitled"):
        acquirer = ReviewAcquirer(None, routes=parse_routes([f"{LICENSED}={route}:html"]), live=live)
        return acquirer.acquire({"journal": LICENSED, "doi": LICENSED_REVIEW})

    def test_an_entitled_page_and_its_table_pages_keep_the_session_cookie_through_the_sign_in_redirects(self):
        live = self.live()
        result = self.acquire(live)
        self.assertEqual(("acquired", "live-entitled", "html"), (result.state, result.provenance, result.kind))
        self.assertEqual(html_review(LICENSED_REVIEW), result.body)
        self.assertEqual(ARTICLE, result.detail["sourceUrl"])
        self.assertEqual([(1, TABLE_PAGE)], result.tables)
        self.assertEqual(("live", ARTICLE + "/tables/1"), (result.detail["tables"][0]["source"],
                                                          result.detail["tables"][0]["url"]))
        hosts = [(host, target.partition("?")[0]) for host, target, _ in self.site.requests]
        self.assertEqual([("doi.org", "/" + LICENSED_REVIEW), ("www.nature.com", "/articles/synthetic-review-1"),
                          ("idp.nature.com", "/authorize"), ("idp.nature.com", "/transit"),
                          ("www.nature.com", "/articles/synthetic-review-1"),
                          ("www.nature.com", "/articles/synthetic-review-1/tables/1")], hosts,
                         "one sign-in; the table page reuses the session")
        self.assertEqual([None], self.site.sent_cookies("doi.org"))
        article, transit, table = (self.site.requests[4][2], self.site.requests[3][2], self.site.requests[5][2])
        self.assertEqual(f"idp_session={SESSION_VALUE}", transit["Cookie"])
        self.assertEqual(f"idp_session={SESSION_VALUE}", article["Cookie"])
        self.assertIn(f"idp_session={SESSION_VALUE}", table["Cookie"])
        self.assertIn("page_view=1", table["Cookie"])
        self.assertEqual(["idp_session", "page_view"], sorted(cookie.name for cookie in live.session))
        # The page and its table page were fetched one second apart; the redirects of one fetch are one fetch.
        self.assertEqual(1, len(self.waits))
        self.assertTrue(0.5 < self.waits[0] <= 1.0, self.waits)
        # The session is never written: not into the record, the acquisition detail or the documents.
        with tempfile.TemporaryDirectory() as root:
            written = acquired_record({"journal": LICENSED, "doi": LICENSED_REVIEW}, result, Path(root) / "documents")
            stored = b"".join(path.read_bytes() for path in Path(root).rglob("*") if path.is_file())
        for text in (json.dumps(written), json.dumps(result.detail), stored.decode("utf-8", "replace")):
            self.assertNotIn(SESSION_VALUE, text)
            self.assertNotIn("idp_session", text)

    def test_without_the_session_the_sign_in_ends_on_cookies_not_supported(self):
        for live, route in ((self.live(session=False), "entitled"), (self.live(), "public")):
            with self.subTest(route=route, session=live.session is not None):
                self.site.requests.clear()
                result = self.acquire(live, route)
                self.assertEqual("unavailable", result.state)
                self.assertEqual(["not-the-article"], [item["reason"] for item in result.detail["reasons"]])
                self.assertIn("error=cookies_not_supported", self.site.requests[-1][1])
                self.assertEqual({None}, {headers.get("Cookie") for _, _, headers in self.site.requests})
                if live.session is not None:
                    self.assertEqual([], list(live.session), "a public route never fills the session")

    def test_cookies_stay_on_nature_com_hosts_by_domain_matching(self):
        jar = census_inputs.session_cookies()
        fetch = census_inputs.page_fetcher(spacing=0, sleep=lambda seconds: None)
        body, final = fetch("https://doi.org/" + LICENSED_REVIEW, cookies=jar)
        self.assertEqual((html_review(LICENSED_REVIEW), ARTICLE), (body, final))
        # Neither the sibling subdomain's nor another site's cookie was taken from idp.nature.com.
        self.assertEqual({"idp_session": ".nature.com", "page_view": "www.nature.com"},
                         {cookie.name: cookie.domain for cookie in jar})
        self.site.requests.clear()
        for url in ("https://evilnature.com/page", "https://nature.com.example/page", "https://doi.org/10.5555/other"):
            fetch(url, cookies=jar)
        outside = {host: headers.get("Cookie") for host, _, headers in self.site.requests if host != "www.nature.com"}
        self.assertEqual({"evilnature.com": None, "nature.com.example": None, "doi.org": None}, outside)
        self.assertIn(f"idp_session={SESSION_VALUE}", self.site.sent_cookies("www.nature.com")[0],
                      "the doi.org redirect to www.nature.com carries the session on its nature.com hop only")
        self.assertEqual({"idp_session", "page_view"}, {cookie.name for cookie in jar},
                         "lookalike hosts set no cookie for nature.com")
        # The host-only cookie goes back to www.nature.com only; the domain cookie to every nature.com host.
        self.site.requests.clear()
        fetch("https://idp.nature.com/authorize?redirect_uri=" + urllib.parse.quote(ARTICLE, safe=""), cookies=jar)
        cookies = self.site.sent_cookies("idp.nature.com")
        self.assertIn(f"idp_session={SESSION_VALUE}", cookies[0])
        self.assertNotIn("page_view", cookies[0])
        self.assertIn("page_view=1", self.site.sent_cookies("www.nature.com")[0])


class LiveFetchTests(unittest.TestCase):
    def test_page_fetcher_tells_definitive_misses_from_transient_failures(self):
        from ocsl_review.errors import ProviderError
        script, requests = [], []

        def opener(request, timeout, allow_cross_origin):
            requests.append((request.full_url, dict(request.header_items())))
            outcome = script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        fetch = census_inputs.page_fetcher(spacing=0, opener=opener, sleep=lambda seconds: None)
        script[:] = [FakeResponse(200, b"<html/>", "https://publisher.example/final")]
        self.assertEqual((b"<html/>", "https://publisher.example/final"), fetch("https://doi.org/10.5555/x"))
        self.assertNotIn("@", json.dumps(requests), "requests carry no contact address")
        script[:] = [FakeResponse(404)]
        self.assertEqual("http-404", fetch("https://doi.org/10.5555/x"))
        script[:] = [FakeResponse(403)]
        self.assertEqual("http-403", fetch("https://doi.org/10.5555/x"))
        script[:] = [FakeResponse(503), FakeResponse(429), ProviderError("provider response exceeded the absolute timeout")]
        with self.assertRaises(RetryableFetch):
            fetch("https://doi.org/10.5555/x")
        script[:] = [ProviderError("document destination is not public")]
        self.assertTrue(fetch("https://doi.org/10.5555/x").startswith("refused:"))
        script[:] = [FakeResponse(503), FakeResponse(200, b"late", "https://publisher.example/late")]
        self.assertEqual((b"late", "https://publisher.example/late"), fetch("https://doi.org/10.5555/x"))

    def test_search_pages_never_guess_and_send_identifiers_only(self):
        import urllib.error
        from ocsl_review.errors import ProviderError
        seen = []

        def failing(request, timeout):
            seen.append(request)
            raise urllib.error.URLError("down")
        with self.assertRaisesRegex(ProviderError, "search failed"):
            census_inputs.search_pages(attempts=2, opener=failing, sleep=lambda s: None,
                                       limit=unpaced())({"query": "q"})
        self.assertEqual(2, len(seen))
        self.assertEqual({"Accept", "User-agent"}, set(dict(seen[0].header_items())))
        ok = census_inputs.search_pages(limit=unpaced(), opener=lambda request, timeout: FakeResponse(200, b"{}"),
                                        sleep=lambda s: None)
        self.assertEqual(b"{}", ok({"query": "q", "cursorMark": "*"}))

    def test_live_sources_follow_the_route_order(self):
        live = LiveSources(europe_pmc=lambda pmcid: b"<article/>", publisher=lambda url: (b"page", url))
        both = live.attempts(census_inputs.Route("entitled", ("html", "jats")), "10.5555/x", {"pmcid": "PMC1"})
        self.assertEqual([("html", "entitled", "publisher"), ("jats", "public", "europe-pmc")],
                         [item[:3] for item in both])
        self.assertEqual((b"<article/>", FULL_TEXT % "PMC1"), both[1][4]())
        only = live.attempts(census_inputs.Route("public", ("jats",)), "10.5555/x", {})
        self.assertEqual([], only, "no PMCID and the route reads JATS only")


COPY_REVIEW = "10.5555/pmc.copy.review"                 # a review whose Europe PMC record has a DOI and no PMCID
COPY_PMCID = "PMC13691543"                              # its copy in PMC: a record with the PMCID and without the DOI
COPY_TITLE = "Synthetic review of zeta-kinase signalling in breast tumours"
COPY_QUERY = f'(TITLE:"{COPY_TITLE}" AND SRC:PMC) OR DOI:"{COPY_REVIEW}"'


def copy_jats(pmcid: str, doi: str | None, pmid: str | None = None) -> bytes:
    """The synthetic JATS review as Europe PMC serves a PMC copy: its PMCID and the given DOI (and PMID) in
    article-meta."""
    ids = f'<article-id pub-id-type="pmcid">{pmcid}</article-id>' + "".join(
        f'<article-id pub-id-type="{kind}">{value}</article-id>' for kind, value in (("doi", doi), ("pmid", pmid))
        if value)
    return (FIXTURES / "review.xml").read_bytes().replace(
        b'<article-id pub-id-type="doi">10.5555/synthetic.review.1</article-id>', ids.encode())


def pmc_record(pmcid: str, title: str = COPY_TITLE, doi: str | None = None) -> dict:
    """A lite record of Europe PMC's search for a record in PMC."""
    return {"id": pmcid, "source": "PMC", "pmcid": pmcid, "title": title, "pubYear": "2021",
            **({"doi": doi} if doi else {})}


class PmcCopies:
    """Europe PMC for reviews without a PMCID: the copy search answers ``records`` (lite) for the title and the
    review's own record (a MEDLINE record with the DOI) for the DOI it names, fullTextXML serves ``full_texts`` by
    PMCID (None for a PMCID without an open-access full text). The first ``empty`` searches get the answer of an empty
    index: a result list without any hit."""

    def __init__(self, records=(), full_texts=None, failure=None, empty=0):
        self.records, self.full_texts, self.failure = list(records), dict(full_texts or {}), failure
        self.empty, self.searches, self.fetched = empty, [], []

    def search(self, params):
        self.searches.append(dict(params))
        if self.failure is not None:
            if isinstance(self.failure, Exception):
                raise self.failure
            return self.failure
        if len(self.searches) <= self.empty:
            return json.dumps({"hitCount": 0, "resultList": {"result": []}}).encode()
        own = [{"id": "42920861", "source": "MED", "pmid": "42920861", "doi": doi, "title": "Its record " + doi}
               for doi in re.findall(r'DOI:"([^"]+)"', params["query"])]
        results = self.records + own
        return json.dumps({"hitCount": len(results), "resultList": {"result": results}}).encode()

    def fetch(self, pmcid):
        self.fetched.append(pmcid)
        return self.full_texts.get(pmcid)


class PmcCopyTests(RunnerTestCase):
    """A review whose Europe PMC record has a DOI and no PMCID, acquired from its copy in PMC: one search by its title
    among PMC's records, and a candidate is its document only when the candidate's JATS article-id names its DOI."""

    def acquire(self, copies, publisher=refuse_live, record=None):
        live = LiveSources(europe_pmc=copies.fetch, publisher=publisher, search=copies.search)
        acquirer = ReviewAcquirer(None, routes=parse_routes([f"{JOURNAL}=public"]), live=live)
        return acquirer.acquire(record or {"journal": JOURNAL, "doi": COPY_REVIEW, "enumeratedTitle": COPY_TITLE,
                                           "pmid": "42920861"})

    def test_a_true_copy_is_acquired_live_public_and_read_as_any_jats(self):
        copies = PmcCopies(
            [pmc_record("PMC2", "Corrigendum: " + COPY_TITLE),        # another title: never fetched
             pmc_record("PMC3", doi="10.5555/another.review"),        # names another DOI: never fetched
             pmc_record(COPY_PMCID, COPY_TITLE.upper() + ".")],       # the copy: the same title, no DOI
            {COPY_PMCID: copy_jats(COPY_PMCID, COPY_REVIEW, "42920861")})
        result = self.acquire(copies)
        self.assertEqual(("acquired", "live-public", "jats"), (result.state, result.provenance, result.kind))
        self.assertEqual([{"query": COPY_QUERY, "resultType": "lite", "pageSize": 10, "format": "json"}],
                         copies.searches)
        self.assertEqual([COPY_PMCID], copies.fetched)
        self.assertEqual(("europe-pmc", FULL_TEXT % COPY_PMCID, "doi+pmid"),
                         (result.detail["provider"], result.detail["sourceUrl"], result.detail["identity"]))
        self.assertEqual({"by": "title+jats-doi", "pmcid": COPY_PMCID, "search": COPY_QUERY}, result.detail["link"])
        # The reader does not change: the copy is prepared as any Europe PMC JATS document of the review.
        record = acquired_record({"journal": JOURNAL, "doi": COPY_REVIEW, "pmid": "42920861", "works": {}}, result,
                                 self.root / "documents")
        self.assertEqual(("public", "live-public"), (record["provenanceClass"], record["acquisition"]["provenance"]))
        self.assertNotIn("pmcid", record, "the record keeps its own identifiers")
        document = (self.root / record["document"]["path"]).read_bytes()
        self.assertEqual(copy_jats(COPY_PMCID, COPY_REVIEW, "42920861"), document)
        prepared = self.run_census().prepare_review(record, document)
        self.assertEqual("prepared", prepared["state"])
        self.assertGreater(prepared["entries"], 0)

    def test_a_copy_is_refused_unless_its_jats_names_the_reviews_doi(self):
        copies = PmcCopies([pmc_record("PMC7"), pmc_record("PMC8"), pmc_record("PMC9")],
                           {"PMC7": copy_jats("PMC7", "10.5555/another.review"),
                            # Only the PMID agrees: a PMC copy is the review's only by its DOI.
                            "PMC8": copy_jats("PMC8", None, "42920861")})
        result = self.acquire(copies, publisher=lambda url: "http-404")
        self.assertEqual(("unavailable", ["PMC7", "PMC8", "PMC9"]), (result.state, copies.fetched))
        self.assertEqual([("europe-pmc", SEARCH + "?" + urllib.parse.urlencode({"query": COPY_QUERY}),
                           "pmc-copy-unconfirmed: PMC7's JATS names DOI 10.5555/another.review; PMC8's JATS names no "
                           "DOI; PMC9 has no open-access full text"),
                          ("publisher", "https://doi.org/" + COPY_REVIEW, "http-404")],
                         [(item["provider"], item["url"], item["reason"]) for item in result.detail["reasons"]])

    def test_without_a_copy_the_publisher_page_is_tried_next(self):
        copies = PmcCopies([pmc_record("PMC2", "Corrigendum: " + COPY_TITLE)])
        result = self.acquire(copies, publisher=lambda url: (html_review(COPY_REVIEW), PUBLISHER + "copy"))
        self.assertEqual(("acquired", "live-public", "html", "publisher"),
                         (result.state, result.provenance, result.kind, result.detail["provider"]))
        self.assertEqual([], copies.fetched)
        self.assertNotIn("link", result.detail)
        self.assertEqual(["no-pmc-copy"], [item["reason"] for item in result.detail["rejected"]])

    def test_a_failed_search_leaves_the_review_for_a_later_pass(self):
        from ocsl_review.errors import ProviderError
        for failure in (ProviderError("Europe PMC search failed: HTTP 503"), b"<html>maintenance</html>"):
            with self.subTest(failure=str(failure)[:30]):
                copies = PmcCopies(failure=failure)
                result = self.acquire(copies, publisher=lambda url: "http-404")
                self.assertEqual("retry", result.state)
                self.assertEqual([("publisher", "http-404"), ("europe-pmc", "transient")],
                                 [(item["provider"], item["reason"]) for item in result.detail["reasons"]])
                self.assertIn("PMC copy search", result.detail["reasons"][1]["detail"])

    def test_an_answer_without_the_reviews_own_record_shows_no_absence_of_a_copy(self):
        landing = lambda url: (PAYWALL % COPY_REVIEW.encode(), PUBLISHER + "landing")
        # An empty index, and a partial one that lacks the review's own record, answer without a copy: that is no
        # answer, so the review stays for a later pass instead of closing as unavailable.
        partial = json.dumps({"hitCount": 1, "resultList": {"result": [pmc_record("PMC2", "Corrigendum: " + COPY_TITLE)]}})
        for index, copies in (("empty", PmcCopies(empty=1)), ("partial", PmcCopies(failure=partial.encode()))):
            with self.subTest(index=index):
                result = self.acquire(copies, publisher=landing)
                self.assertEqual("retry", result.state)
                self.assertEqual([("publisher", "not-the-article"), ("europe-pmc", "transient")],
                                 [(item["provider"], item["reason"]) for item in result.detail["reasons"]])
                self.assertIn("neither a copy nor the review's own record", result.detail["reasons"][1]["detail"])
        # The first sweep, once the index answers again, reads the review from its copy.
        copies = PmcCopies([pmc_record(COPY_PMCID)], {COPY_PMCID: copy_jats(COPY_PMCID, COPY_REVIEW)}, empty=1)
        live = LiveSources(europe_pmc=copies.fetch, publisher=landing, search=copies.search)
        source = self.root / "reviews.jsonl"
        source.write_text(json.dumps({"journal": JOURNAL, "doi": COPY_REVIEW, "enumeratedTitle": COPY_TITLE}) + "\n")
        waits = []
        result = acquire_records(source, self.root / "acquired" / "reviews.jsonl", ReviewAcquirer(
            None, routes=parse_routes([f"{JOURNAL}=public"]), live=live), sleep=waits.append)
        self.assertEqual(({"live-public": 1}, 1, [60.0], None),
                         (result["provenance"], result["pmcCopies"], waits, result["next"]))
        self.assertEqual(([COPY_QUERY, COPY_QUERY], [COPY_PMCID]),
                         ([search["query"] for search in copies.searches], copies.fetched))

    def test_a_review_with_a_pmcid_or_without_a_title_gets_no_search(self):
        copies = PmcCopies([pmc_record(COPY_PMCID)], {"PMC5": copy_jats("PMC5", COPY_REVIEW)})
        with_pmcid = self.acquire(copies, record={"journal": JOURNAL, "doi": COPY_REVIEW, "pmcid": "PMC5",
                                                  "enumeratedTitle": COPY_TITLE})
        self.assertEqual(("acquired", FULL_TEXT % "PMC5"), (with_pmcid.state, with_pmcid.detail["sourceUrl"]))
        self.assertNotIn("link", with_pmcid.detail)
        untitled = self.acquire(copies, publisher=lambda url: "http-404",
                                record={"journal": JOURNAL, "doi": COPY_REVIEW})
        self.assertEqual("unavailable", untitled.state)
        self.assertEqual(([], ["PMC5"]), (copies.searches, copies.fetched))
        # A route that reads HTML only never searches for a JATS copy.
        live = LiveSources(europe_pmc=copies.fetch, publisher=lambda url: "http-404", search=copies.search)
        self.assertEqual([("html", "public", "publisher")],
                         [item[:3] for item in live.attempts(parse_routes([f"{JOURNAL}=public:html"])[JOURNAL],
                                                             COPY_REVIEW, {}, COPY_TITLE)])
        self.assertIsNotNone(LiveSources.default().search, "study acquire searches for copies")

    def test_acquire_records_counts_the_reviews_read_from_their_copy(self):
        copies = PmcCopies([pmc_record(COPY_PMCID)], {COPY_PMCID: copy_jats(COPY_PMCID, COPY_REVIEW)})
        live = LiveSources(europe_pmc=copies.fetch, search=copies.search,
                           publisher=lambda url: (html_review("10.5555/page.review"), PUBLISHER + "page"))
        source = self.root / "reviews.jsonl"
        source.write_text("".join(json.dumps(row) + "\n" for row in (
            {"journal": JOURNAL, "doi": COPY_REVIEW, "enumeratedTitle": COPY_TITLE},
            {"journal": JOURNAL, "doi": "10.5555/page.review", "enumeratedTitle": "A review read from its page"})))
        output = self.root / "acquired" / "reviews.jsonl"
        result = acquire_records(source, output, ReviewAcquirer(None, routes=parse_routes([f"{JOURNAL}=public"]),
                                                                live=live))
        self.assertEqual(({"live-public": 2}, 1), (result["provenance"], result["pmcCopies"]))
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([COPY_PMCID, None], [(row["acquisition"].get("link") or {}).get("pmcid") for row in rows])


class EuropePmcRateTests(unittest.TestCase):
    """One Europe PMC rate per command: every request of every thread takes its turn at 8 a second, and a 429 or 503
    answer's Retry-After holds them all."""

    def test_threads_take_turns_at_eight_requests_a_second(self):
        import threading
        starts, lock = [], threading.Lock()

        def sleep(seconds):                       # the clock stands still: a request starts when its sleep ends
            with lock:
                starts.append(seconds)
        rate = census_inputs.RateLimit(census_inputs.EUROPE_PMC_REQUESTS_PER_SECOND, clock=lambda: 0.0, sleep=sleep)
        threads = [threading.Thread(target=lambda: [rate.wait() for _ in range(10)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Forty requests from four threads: the first at once, every other one 1/8 s after the one before it.
        self.assertEqual([number * 0.125 for number in range(1, 40)], sorted(starts))

    def test_a_retry_after_holds_every_request_also_those_waiting_their_turn(self):
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                # While this request waits its turn, another thread's request gets a 429 that names 30 s.
                self.assertEqual(30.0, rate.hold(429, {"Retry-After": "30"}))
        rate = census_inputs.RateLimit(8.0, clock=lambda: 0.0, sleep=sleep)
        rate.wait()
        rate.wait()
        rate.wait()
        self.assertEqual([0.125, 30.0, 30.125], sleeps,
                         "the waiting request starts when the wait ends, the next one a turn later")

    def test_retry_after_names_seconds_or_a_date_and_holds_at_most_fifteen_minutes(self):
        import email.utils
        from datetime import datetime, timedelta, timezone
        rate = census_inputs.RateLimit(8.0, clock=lambda: 0.0, sleep=lambda seconds: None)
        self.assertIsNone(rate.hold(500, {"Retry-After": "30"}), "only 429 and 503 answers hold the requests")
        self.assertIsNone(rate.hold(429, {}))
        self.assertIsNone(rate.hold(429, None))
        self.assertIsNone(rate.hold(503, {"Retry-After": "soon"}))
        self.assertEqual(900.0, rate.hold(503, {"Retry-After": "86400"}))
        when = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
        self.assertAlmostEqual(120.0, census_inputs.retry_after_seconds({"Retry-After": when}), delta=2.0)
        message = http.client.HTTPMessage()
        message["retry-after"] = "7"
        self.assertEqual(7.0, census_inputs.retry_after_seconds(message), "header names are case-insensitive")
        gone = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(seconds=60), usegmt=True)
        self.assertEqual(0.0, census_inputs.retry_after_seconds({"Retry-After": gone}))

    def test_a_retry_after_from_one_lookup_holds_the_next_request_of_another(self):
        import urllib.error
        from ocsl_review.study_cli import europe_pmc_fetcher
        from test_census_runner import EuropePmcStandIn, FakeClock
        clock, requests = FakeClock(100.0), []
        rate = census_inputs.RateLimit(8.0, clock=clock, sleep=clock.sleep)
        europe_pmc = EuropePmcStandIn(full_texts={"PMC5": b"<article/>"})

        def busy(request, timeout=None):
            requests.append((clock(), "search"))
            raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {"Retry-After": "60"},
                                         io.BytesIO(b""))

        def serving(request, timeout=None):
            requests.append((clock(), "fullTextXML"))
            return europe_pmc(request, timeout)
        # The abstracts step's lookup gets a 503 that names 60 s at its last attempt and gives up; the full-text
        # fetcher of the same command then waits for the end of that wait before its first request.
        self.assertIsNone(census_abstracts.http_get(attempts=1, opener=busy, sleep=clock.sleep, limit=rate)(
            {"query": 'DOI:"10.5555/x"'}))
        fetch = europe_pmc_fetcher(opener=serving, sleep=clock.sleep, clock=clock, limit=rate)
        self.assertEqual(b"<article/>", fetch("PMC5"))
        self.assertEqual([(100.0, "search"), (160.0, "fullTextXML")], requests)
        # An enumeration page answered 429 with a wait of 5 s is asked again after that wait, not after its 3 s.
        requests.clear()
        pages = iter([urllib.error.HTTPError(SEARCH, 429, "Too Many Requests", {"Retry-After": "5"}, io.BytesIO(b"")),
                      FakeResponse(200, b"{}")])

        def enumeration(request, timeout=None):
            requests.append((clock(), "page"))
            answer = next(pages)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.assertEqual(b"{}", census_inputs.search_pages(opener=enumeration, sleep=clock.sleep, limit=rate)(
            {"query": "q", "cursorMark": "*"}))
        (first, _), (second, _) = requests
        self.assertEqual(5.0, second - first)

    def test_the_census_fetchers_share_one_europe_pmc_rate_by_default(self):
        from ocsl_review.study_cli import europe_pmc_fetcher
        turns = []

        class Recording:
            def wait(self):
                turns.append("wait")

            def hold(self, status, headers):
                return None

        def answer(request, timeout=None):
            turns.append(urllib.parse.urlsplit(request.full_url).path.rsplit("/", 1)[-1])
            return FakeResponse(200, b'{"resultList": {"result": []}}' if "search" in request.full_url else b"<a/>")
        with mock.patch.object(census_inputs, "EUROPE_PMC_LIMIT", Recording()):
            census_inputs.search_pages(opener=answer)({"query": "q"})
            census_abstracts.http_get(opener=answer)({"query": "q"})
            europe_pmc_fetcher(opener=answer)("PMC1")
        self.assertEqual(["wait", "search", "wait", "search", "wait", "fullTextXML"], turns)


class CommandLineInputTests(CacheTestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        self.assertEqual(0, code, (argv, buffer.getvalue()))
        return json.loads(buffer.getvalue())

    def test_study_commands_for_fresh_inputs(self):
        get, _ = FreshEnumerationTests.provider(self)
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(FreshEnumerationTests.SPEC))
        manifest = self.root / "study" / "enumeration.jsonl"
        with mock.patch.object(census_inputs, "search_pages", return_value=get):
            enumerated_result = self.run_cli(["study", "enumerate", "--spec", str(spec), "--output", str(manifest)])
        self.assertEqual(3, enumerated_result["records"])
        acquired = manifest.parent / "acquired.jsonl"
        caches = ["--store", str(self.paths["projection"]), "--provider-cache", str(self.paths["provider"])]
        with mock.patch.object(census_inputs.LiveSources, "default", return_value=LIVE):
            result = self.run_cli(["study", "acquire", "--input", str(manifest), "--output", str(acquired), *caches,
                                   "--route", f"{JOURNAL}=public", "--route", f"{SECOND}=entitled", "--live"])
        self.assertEqual({"cache-hit-entitled": 1, "cache-hit-public": 2}, result["provenance"])
        self.assertEqual("literature-evidence@2", result["inputs"]["store"]["id"])
        derived = manifest.parent / "works.jsonl"
        works = self.run_cli(["study", "works", "--input", str(acquired), "--output", str(derived)])
        self.assertEqual((3, 3, 0), (works["records"], works["derived"], works["extractionFailed"]))
        filled = manifest.parent / "abstracts.jsonl"
        with mock.patch.object(census_abstracts, "http_get", return_value=lookups):
            self.run_cli(["study", "abstracts", "--input", str(derived), "--output", str(filled), *caches])
        prepared = self.run_cli(["study", "prepare", "--database", str(self.database), "--seed", "1234", "--input",
                                 str(filled), *caches, "--require-fresh-enumeration", "--enumeration",
                                 enumerated_result["receipt"]])
        self.assertEqual(3, prepared["prepared"])
        checks = self.run_cli(["study", "gates", "--database", str(self.database), "--require-fresh-enumeration"])["checks"]
        for name in ("freshEnumerationPinned", "reviewsComeFromTheEnumeration", "everyAbstractFromARecordedLookup",
                     "everyWorkDerivedFromItsReview", "everyWorkMetadataFromARecordedLookup",
                     "reviewsAcquiredThroughLiveRoutes", "everyEnumeratedReviewInStudyOrExcluded"):
            self.assertTrue(checks[name], name)
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            self.assertNotEqual(0, main(["study", "full-text", "--database", str(self.database), "--seed", "1234",
                                         "--base-url", self.base, *caches]))
        stored = self.root / "stored.txt"
        stored.write_text(f"# one DOI per line\n{JATS_REVIEW}\n10.5555/stored.only\n")
        compared = self.run_cli(["study", "compare-manifest", "--database", str(self.database), "--stored", str(stored),
                                 "--output", str(self.root / "comparison.json"), "--allow-incomplete"])
        self.assertEqual({"fresh": 3, "stored": 2, "inBoth": 1, "freshOnly": 2, "storedOnly": 1}, compared["totals"])
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            self.assertNotEqual(0, main(["study", "prepare", "--database", str(self.database), "--seed", "1234",
                                         "--input", str(filled), "--require-fresh-enumeration"]))
            self.assertNotEqual(0, main(["study", "prepare", "--database", str(self.database), "--seed", "1234",
                                         "--input", str(filled), *caches, "--require-fresh-enumeration", "--enumeration",
                                         enumerated_result["receipt"]]))
        self.assertIn("population is frozen", buffer.getvalue())
        unpinned = self.root / "unpinned.sqlite3"
        self.run_cli(["study", "prepare", "--database", str(unpinned), "--seed", "1234", "--input", str(filled)])
        checks = self.run_cli(["study", "gates", "--database", str(unpinned), "--require-fresh-enumeration"])["checks"]
        self.assertFalse(checks["freshEnumerationPinned"])


if __name__ == "__main__":
    unittest.main()
