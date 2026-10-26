# SPDX-License-Identifier: Apache-2.0
"""Census inputs from byte caches: a label-free projection and a provider response cache, live fallback, provenance
per item in the scores database and the facts, and fresh enumeration compared with a stored manifest afterwards."""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.parse
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from ocsl_review import census_inputs, corpus_facts, scores_db
from ocsl_review.census_inputs import (FULL_TEXT, SEARCH, InputCaches, LiveSources, ReviewAcquirer, acquire_records,
                                       acquired_record, compare_manifests, enumerate_reviews, load_enumeration,
                                       parse_routes, verify_claim)
from ocsl_review.census_abstracts import fill_record
from ocsl_review.census_runner import CensusRun, RetryableFetch, census_status, record_comparison
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
from ocsl_review.study_cli import gates, prepare

from test_census_runner import FIXTURES, JOURNAL, RunnerTestCase, record

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
        db.execute("INSERT INTO papers VALUES(?,?,?,?)", (JATS_REVIEW, "Synthetic review", 2021, "work/corpus/review.xml"))
        db.execute("INSERT INTO bib_refs VALUES(?,?,?,?)", (JATS_REVIEW, "1", "Printed reference", "doi:10.5555/w1"))
    (legacy / "work/corpus/review.xml").write_bytes((FIXTURES / "review.xml").read_bytes())
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

    def test_acquire_records_writes_documents_and_resumes_transient_failures(self):
        source = self.root / "input" / "reviews.jsonl"
        source.parent.mkdir()
        rows = [{"journal": JOURNAL, "doi": JATS_REVIEW}, {"journal": JOURNAL, "doi": HTML_REVIEW},
                {"journal": SECOND, "doi": "10.5555/flaky.review"}, {"journal": JOURNAL, "doi": "10.5555/absent.review"},
                {**record(doi="10.5555/supplied.review"), "document": {"path": "review.xml", "kind": "jats"}}]
        source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        attempts = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) == 1:
                raise RetryableFetch("HTTP 503")
            return html_review("10.5555/flaky.review"), PUBLISHER + "flaky"
        acquirer = self.acquirer(LiveSources(publisher=flaky), routes=(f"{JOURNAL}=public", f"{SECOND}=entitled:html"))
        output = self.root / "acquired" / "reviews.jsonl"
        first = acquire_records(source, output, acquirer)
        self.assertEqual({"cache-hit-public": 2, "retry": 1, "unavailable": 1, "supplied": 1}, first["provenance"])
        lines = [json.loads(line) for line in output.read_text().splitlines()]
        jats = lines[0]
        self.assertEqual(("public", "cache-hit-public"), (jats["provenanceClass"], jats["acquisition"]["provenance"]))
        document = output.parent / jats["document"]["path"]
        self.assertEqual(census_inputs.sha256_bytes(document.read_bytes()), jats["document"]["sha256"])
        self.assertEqual(1, len(lines[1]["document"]["tables"]))
        self.assertEqual("retry", lines[2]["acquisition"]["state"])
        self.assertEqual("unavailable", lines[3]["provenanceClass"])
        self.assertNotIn("acquisition", lines[4])
        second = acquire_records(output, self.root / "acquired" / "reviews.2.jsonl", acquirer)
        self.assertEqual((1, 4), (second["acquired"], second["passedThrough"]))
        self.assertEqual("live-entitled", json.loads((self.root / "acquired" / "reviews.2.jsonl").read_text()
                                                     .splitlines()[2])["acquisition"]["provenance"])


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
        self.assertEqual(("store", "doi+pmcid"), (items["work-7"]["cache"], items["work-7"]["identity_check"]))
        self.assertEqual("identity-mismatch", json.loads(items["work-15"]["detail_json"])["rejected"][0]["reason"])
        with closing(scores_db.open_readonly(self.database)) as connection:
            status = census_status(connection)
        self.assertEqual({"cache-hit-public": 2, "live-public": 1}, status["itemProvenance"]["full-text"])
        self.assertEqual(caches.store.version["merkleRoot"], status["inputs"]["inputStore"]["merkleRoot"])


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
        with self.assertRaisesRegex(ValidationError, "already exists"):
            enumerate_reviews(self.SPEC, manifest, self.provider()[0])
        page = next(Path(str(manifest) + ".pages").iterdir())
        page.chmod(0o644)
        page.write_bytes(page.read_bytes() + b" ")
        with self.assertRaisesRegex(ValidationError, "differs from its digest"):
            load_enumeration(receipt)

    def test_incomplete_or_changing_populations_are_refused(self):
        def short(params):
            return search_page([enumerated(JATS_REVIEW, "review")], 5, params["cursorMark"])
        with self.assertRaisesRegex(ValidationError, "exhausted"):
            enumerate_reviews(self.SPEC, self.root / "a.jsonl", short)
        calls = []

        def changing(params):
            calls.append(1)
            return search_page([enumerated(JATS_REVIEW, "review")], 2 + len(calls), "next%d" % len(calls))
        with self.assertRaisesRegex(ValidationError, "changed during paging"):
            enumerate_reviews(self.SPEC, self.root / "b.jsonl", changing)
        with self.assertRaises(ValidationError):
            enumerate_reviews({"groups": []}, self.root / "c.jsonl", short)

    def test_fresh_study_prepares_only_enumerated_acquired_reviews_and_compares_afterwards(self):
        manifest, result, _ = self.enumerate()
        receipt = Path(result["receipt"])
        enumeration = load_enumeration(receipt)
        caches = self.caches()
        acquirer = ReviewAcquirer(caches, routes=parse_routes([f"{JOURNAL}=public", f"{SECOND}=entitled"]))
        acquired = manifest.parent / "acquired.jsonl"
        acquire_records(manifest, acquired, acquirer)
        rows = [json.loads(line) for line in acquired.read_text().splitlines()]
        self.assertEqual(["cache-hit-public", "cache-hit-public", "cache-hit-entitled"],
                         [row["acquisition"]["provenance"] for row in rows])
        # The study's own cited-work records (identifiers and metadata, no abstracts yet), then the abstract lookups:
        # the caches first, Europe PMC (mocked here) for the rest.
        works = {key: {k: v for k, v in work.items() if k not in ("abstract", "abstractSource")}
                 for key, work in record()["works"].items()}

        def get(params):
            results = [epmc(doi=part.split('"')[1], abstract="Live abstract.") for part in params["query"].split(" OR ")
                       if '"' in part]
            if params["query"].startswith("EXT_ID:"):
                results.append(epmc(pmid=params["query"].split(":")[1].split()[0], abstract="Live abstract by PMID."))
            return {"resultList": {"result": results}}
        withworks = [fill_record({**row, "works": works}, get, cache=caches) for row in rows]
        labels = {work["abstractProvenance"]["provenance"] for work in withworks[0]["works"].values()}
        self.assertEqual({"cache-hit-public", "live-public"}, labels)
        extra = [{**withworks[0], "doi": "10.5555/not.enumerated"},
                 {**withworks[0], "stratum": "Mini Review"},
                 {key: value for key, value in withworks[1].items() if key != "works"},
                 {key: value for key, value in withworks[2].items() if key != "enumeration"}]
        source = manifest.parent / "prepared-input.jsonl"
        source.write_text("".join(json.dumps(row) + "\n" for row in withworks + extra))
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
        self.assertTrue(all(verdict[name] for name in ("freshEnumerationPinned", "reviewsComeFromTheEnumeration",
                                                        "everyReviewHasAcquisitionProvenance", "cacheHitsVerified",
                                                        "everyAbstractFromARecordedLookup")), verdict)
        self.assertEqual(0, status["inputChecks"]["abstractsWithoutLookup"])
        self.assertEqual({"sha256": enumeration.sha256, "reviews": 3, "inStudy": 3, "notInStudy": 0,
                          "reviewsOutsideEnumeration": 0}, status["enumeration"])
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
        self.assertEqual({"fresh": 2, "stored": 2, "inBoth": 1, "freshOnly": 1, "storedOnly": 1}, early["byJournal"][JOURNAL])
        self.assertEqual(1, early["byJournal"]["(unattributed)"]["storedOnly"])
        report = json.loads((manifest.parent / "early.json").read_text())
        self.assertEqual((["10.5555/elsewhere", "10.5555/stored.only"], 0, 1), (report["storedOnly"], report["imported"],
                                                                                report["stored"]["invalid"]))
        reviews = {row["doi"] for row in self.rows("SELECT doi FROM reviews")}
        self.assertEqual({JATS_REVIEW, HTML_REVIEW, CACHED_REVIEW}, reviews, "nothing of the stored manifest is imported")
        with self.assertRaisesRegex(ValidationError, "already recorded"):
            record_comparison(self.database, stored, manifest.parent / "again.json", label="early", allow_incomplete=True)
        # Finish the run (the entitled review needs an on-premises judge), then compare, freeze and export facts.
        run = self.run_census(on_premises=True)
        self.assertEqual("completed", run.run_abstract()["state"])
        run.pin_inputs(caches)
        self.assertEqual("completed", run.run_full_text(fetch_xml=lambda pmcid: None, cache=caches)["state"])
        run.close()
        final = record_comparison(self.database, stored, manifest.parent / "final.json", label="final")
        self.assertTrue(final["runComplete"])
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
        self.assertGreater(value(f"{j}.provenance.full_text.unavailable"), 0)
        self.assertEqual((2, 2, 0), (value(f"{j}.enumeration.reviews"), value(f"{j}.enumeration.in_study"),
                                     value(f"{j}.enumeration.outside")))
        self.assertEqual(1, value(f"{j}.enumeration.stratum.systematic-review.reviews"))
        self.assertEqual((2, 1, 1), (value(f"{j}.manifest_comparison.final.all.stored"),
                                     value(f"{j}.manifest_comparison.final.all.in_both"),
                                     value(f"{j}.manifest_comparison.final.review.stored_only")))
        self.assertEqual((3, 2), (value("census.manifest_comparison.final.stored"),
                                  value("census.manifest_comparison.final.stored_only")))
        inputs = facts[f"{j}.provenance.reviews.cache-hit-public"]["inputs"]
        self.assertEqual((caches.store.version["merkleRoot"], enumeration.sha256),
                         (inputs["inputStoreMerkleRoot"], inputs["enumerationSha256"]))
        self.assertEqual({"ok": True, "facts": export["count"]}, corpus_facts.verify_export(self.root / "facts" / "facts.json"))

    def test_compare_manifests_counts_by_journal_and_stratum(self):
        fresh = [{"journal": "A", "doi": "1", "stratum": "x"}, {"journal": "A", "doi": "2", "stratum": "y"}]
        stored = [{"journal": "a", "doi": "2", "stratum": "y"}, {"journal": "A ", "doi": "3", "stratum": "x"},
                  {"journal": None, "doi": "4", "stratum": None}]
        result = compare_manifests(fresh, stored)
        self.assertEqual({"fresh": 2, "stored": 3, "inBoth": 1, "freshOnly": 1, "storedOnly": 2}, result["totals"])
        self.assertEqual({"fresh": 1, "stored": 1, "inBoth": 0, "freshOnly": 1, "storedOnly": 1},
                         result["byJournal"]["A"]["strata"]["x"])
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
            census_inputs.search_pages(spacing=0, attempts=2, opener=failing, sleep=lambda s: None)({"query": "q"})
        self.assertEqual(2, len(seen))
        self.assertEqual({"Accept", "User-agent"}, set(dict(seen[0].header_items())))
        ok = census_inputs.search_pages(spacing=0, opener=lambda request, timeout: FakeResponse(200, b"{}"),
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
        result = self.run_cli(["study", "acquire", "--input", str(manifest), "--output", str(acquired), *caches,
                               "--route", f"{JOURNAL}=public", "--route", f"{SECOND}=entitled"])
        self.assertEqual({"cache-hit-entitled": 1, "cache-hit-public": 2}, result["provenance"])
        self.assertEqual("literature-evidence@2", result["inputs"]["store"]["id"])
        rows = [{**json.loads(line), "works": record()["works"]} for line in acquired.read_text().splitlines()]
        filled = manifest.parent / "with-works.jsonl"
        filled.write_text("".join(json.dumps(row) + "\n" for row in rows))
        prepared = self.run_cli(["study", "prepare", "--database", str(self.database), "--seed", "1234", "--input",
                                 str(filled), *caches, "--require-fresh-enumeration", "--enumeration",
                                 enumerated_result["receipt"]])
        self.assertEqual(3, prepared["prepared"])
        checks = self.run_cli(["study", "gates", "--database", str(self.database), "--require-fresh-enumeration"])["checks"]
        self.assertTrue(checks["freshEnumerationPinned"] and checks["reviewsComeFromTheEnumeration"])
        # The fixture works carry abstracts that no recorded lookup vouches for.
        self.assertFalse(checks["everyAbstractFromARecordedLookup"])
        stored = self.root / "stored.txt"
        stored.write_text(f"# one DOI per line\n{JATS_REVIEW}\n10.5555/stored.only\n")
        compared = self.run_cli(["study", "compare-manifest", "--database", str(self.database), "--stored", str(stored),
                                 "--output", str(self.root / "comparison.json"), "--allow-incomplete"])
        self.assertEqual({"fresh": 3, "stored": 2, "inBoth": 1, "freshOnly": 2, "storedOnly": 1}, compared["totals"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertNotEqual(0, main(["study", "prepare", "--database", str(self.database), "--seed", "1234",
                                         "--input", str(filled), "--require-fresh-enumeration"]))
        unpinned = self.root / "unpinned.sqlite3"
        self.run_cli(["study", "prepare", "--database", str(unpinned), "--seed", "1234", "--input", str(filled)])
        checks = self.run_cli(["study", "gates", "--database", str(unpinned), "--require-fresh-enumeration"])["checks"]
        self.assertFalse(checks["freshEnumerationPinned"])


if __name__ == "__main__":
    unittest.main()
