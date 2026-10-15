# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ocsl_review.confidentiality import EgressPlan
from ocsl_review.corpus_acquisition import CacheIntegrityError, CorpusAcquirer, CorpusResponseCache, failure_kind
from ocsl_review.errors import ConfidentialityError, ProviderError
from ocsl_review.scholarly import EUROPE_PMC, RawResponse, ScholarlyClient
from test_fulltext import SourceTransport


HTML = b'''<html><head><meta name="citation_doi" content="10.1234/example"></head>
<body><article><h2>Introduction</h2><p>The claim has a source <a href="#r1">1</a>.</p>
<figure><figcaption>Table 1</figcaption><a href="/articles/example/tables/1">View table</a></figure>
<ol class="references"><li id="r1" data-label="1">Author A. Source. doi:10.1234/source</li></ol>
</article></body></html>'''
TABLE = b'''<html><head><meta name="citation_doi" content="10.1234/example"></head><body>
<table><tr><td>The table finding</td><td><a href="/articles/example#r1">1</a></td></tr></table>
</body></html>'''


class PublisherTransport(SourceTransport):
    def __init__(self, *, page=HTML, table=TABLE, epmc_failure=False):
        super().__init__(open_access=False)
        self.page, self.table, self.epmc_failure = page, table, epmc_failure

    def get(self, url, *, params=None, headers=None):
        if url.startswith("https://doi.org/"):
            self.calls.append((url, params or {}, headers or {}))
            return RawResponse(self.page, 200, {"Content-Type": "text/html"}, "https://publisher.example/articles/example")
        if url.startswith("https://publisher.example/"):
            self.calls.append((url, params or {}, headers or {}))
            return RawResponse(self.table, 200, {"Content-Type": "text/html"}, url)
        if self.epmc_failure and url.endswith("/search"):
            self.calls.append((url, params or {}, headers or {}))
            raise ProviderError("provider HTTP 500 temporary outage")
        response = super().get(url, params=params, headers=headers)
        if "europepmc" in url:
            data = response.json()
            for record in data["resultList"]["result"]:
                record.pop("pmcid", None)
            return RawResponse(json.dumps(data).encode(), 200, {}, url)
        return response


class CorpusAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def acquirer(self, transport, *, path="cache.sqlite3", plan=None):
        return CorpusAcquirer(self.root / path, ScholarlyClient(transport=transport, plan=plan),
                              requests_per_second={"*": 100000, "www.ebi.ac.uk": 100000,
                                                   "eutils.ncbi.nlm.nih.gov": 100000, "api.crossref.org": 100000})

    def test_resolve_status_and_acquisition_reuse_identical_provider_responses(self):
        transport = SourceTransport()
        acquirer = self.acquirer(transport)
        result = acquirer.fetch_work({"doi": "10.1234/example"})
        self.assertEqual("abstract-only", result["outcome"])
        self.assertTrue(result["abstractComplete"])
        self.assertEqual([], result["documents"])
        self.assertFalse(any("fullTextXML" in call[0] for call in transport.calls))
        self.assertTrue(result["identityResolution"]["records"])
        search = [call for call in transport.calls if call[0].endswith("/search")]
        crossref = [call for call in transport.calls if "crossref" in call[0]]
        self.assertEqual(1, len(search))
        self.assertEqual(0, len(crossref))
        self.assertFalse(any("eutils.ncbi" in call[0] for call in transport.calls))
        self.assertEqual("provider-index-only", result["publicationStatus"]["coverage"])
        self.assertEqual("unknown", result["publicationStatus"]["state"])
        original_count = len(transport.calls)
        reopened = self.acquirer(transport)
        self.assertEqual("abstract-only", reopened.fetch_work({"doi": "10.1234/example"})["outcome"])
        self.assertEqual(original_count, len(transport.calls))
        for record in result["cacheProvenance"]:
            self.assertTrue(Path(record["artifactPath"]).is_file())
            self.assertEqual(64, len(record["sha256"]))

    def test_full_text_requires_explicit_escalation(self):
        transport = SourceTransport()
        result = self.acquirer(transport).fetch_work({"doi": "10.1234/example"}, include_full_text=True)
        self.assertEqual("available", result["outcome"])
        self.assertTrue(result["documents"])
        self.assertFalse(any("crossref" in call[0] or "eutils.ncbi" in call[0] for call in transport.calls))
        self.assertEqual(1, sum("fullTextXML" in call[0] for call in transport.calls))
        self.assertTrue(any("Supplement links" in text for text in result["limitations"]))

    def test_one_hundred_complete_exact_dois_need_two_batches_and_no_per_work_requests(self):
        class Batched:
            def __init__(self):
                self.calls = []
            def get(self, url, *, params=None, headers=None):
                self.calls.append((url, params, headers))
                if not url.endswith("/search"):
                    raise AssertionError("Unexpected per-work provider request")
                dois = re.findall(r'"(10\.[^"]+)"', params["query"])
                records = [{"id": str(i + 1), "source": "MED", "doi": doi,
                            "title": "Complete study " + doi, "pubYear": "2024",
                            "abstractText": "The complete structured abstract.", "isRetracted": "N"}
                           for i, doi in enumerate(dois)]
                return RawResponse(json.dumps({"hitCount": len(records), "resultList": {"result": records}}).encode(), 200, {})
        transport = Batched()
        acquirer = self.acquirer(transport)
        dois = [f"10.1234/study-{i}" for i in range(100)]
        self.assertEqual(2, acquirer.prefetch_dois(dois)["batches"])
        results = [acquirer.fetch_work({"doi": doi}) for doi in dois]
        self.assertEqual(2, len(transport.calls))
        self.assertTrue(all(r["abstractComplete"] for r in results))
        self.assertTrue(all(r["identityResolution"]["method"] == "exact-index-identifier-and-title" for r in results))
        self.assertTrue(all(r["publicationStatus"]["state"] == "unknown" for r in results))
        self.assertTrue(all(r["publicationStatus"]["providerFlags"][0]["isRetractedKnown"] for r in results))
        self.assertTrue(all(r["cacheProvenance"] for r in results))

    def test_absent_or_conflicting_index_fields_and_explicit_verification_use_full_resolver(self):
        class Conflicting(SourceTransport):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                if url.endswith("/search"):
                    payload = response.json()
                    record = payload["resultList"]["result"][0]
                    payload["resultList"]["result"].append({**record, "title": "Conflicting identity"})
                    return RawResponse(json.dumps(payload).encode(), 200, {})
                return response
        for name, transport, options in (("missing", SourceTransport(abstract=""), {}),
                                         ("conflict", Conflicting(), {}),
                                         ("explicit", SourceTransport(), {"verify_independently": True}),
                                         ("title", SourceTransport(), {})):
            with self.subTest(name=name):
                supplied = {"doi": "10.1234/example"}
                if name == "title":
                    supplied["title"] = "Different expected title"
                result = self.acquirer(transport, path=name + ".sqlite3").fetch_work(supplied, **options)
                self.assertTrue(any("crossref" in call[0] for call in transport.calls))
                self.assertNotEqual(result["identityResolution"].get("method"), "exact-index-identifier-and-title")

    def test_index_retraction_flags_are_positive_evidence_and_missing_flags_are_unknown(self):
        class Retracted(SourceTransport):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                payload = response.json()
                payload["resultList"]["result"][0]["isRetracted"] = "Y"
                return RawResponse(json.dumps(payload).encode(), 200, {})
        result = self.acquirer(Retracted()).fetch_work({"doi": "10.1234/example"})
        self.assertEqual("retracted", result["publicationStatus"]["state"])
        self.assertFalse(result["publicationStatus"]["independentSurveillancePerformed"])

    def test_unverified_extra_pmcid_is_not_used(self):
        transport = PublisherTransport()
        result = self.acquirer(transport).fetch_review({"doi": "10.1234/example", "pmcid": "PMC999999"})
        self.assertEqual("available", result["outcome"])
        self.assertFalse(any("PMC999999" in call[0] for call in transport.calls))

    def test_abstract_stub_with_bibliography_is_not_a_complete_review(self):
        page = HTML.replace(b'<a href="#r1">1</a>', b'one source')
        result = self.acquirer(PublisherTransport(page=page, table=TABLE.replace(b'<a href="/articles/example#r1">1</a>', b'one'))).fetch_review({"doi": "10.1234/example"})
        self.assertIsNone(result["document"])

    def test_batch_primes_lookup_and_keeps_raw_provider_payload(self):
        transport = SourceTransport()
        acquirer = self.acquirer(transport)
        prefetched = acquirer.prefetch_dois(["10.1234/example"])
        self.assertEqual(1, prefetched["primed"])
        result = acquirer.fetch_work({"doi": "10.1234/example"})
        self.assertEqual(1, len([call for call in transport.calls if call[0].endswith("/search")]))
        projected = [row for row in result["cacheProvenance"] if row["derivedFrom"]]
        self.assertEqual(1, len(projected))
        self.assertTrue(any(row["sha256"] == prefetched["cacheProvenance"][0]["sha256"]
                            for row in result["cacheProvenance"]))

    def test_batch_size_is_fifty_and_unrelated_records_do_not_verify_identity(self):
        transport = SourceTransport()
        acquirer = self.acquirer(transport)
        result = acquirer.prefetch_dois([f"10.1234/test-{i}" for i in range(51)])
        self.assertEqual(2, result["batches"])
        self.assertEqual(51, result["primed"])
        calls = [call for call in transport.calls if call[0].endswith("/search")]
        self.assertEqual([50, 1], [call[1]["query"].count('"10.') for call in calls])
        self.assertEqual([], acquirer.client._europe_pmc_lookup('DOI:"10.1234/test-0"',
                                                              "resolve_identifier", {"doi": "10.1234/test-0"}))

    def test_independent_cache_instances_coalesce_the_same_live_request(self):
        class Slow:
            calls = 0
            def get(self, url, **kwargs):
                self.calls += 1
                time.sleep(0.1)
                return RawResponse(b'{"value":1}', 200, {})
        transport = Slow()
        first = self.acquirer(transport)
        second = self.acquirer(transport)
        barrier = threading.Barrier(4)
        def call(index):
            barrier.wait()
            return (first if index % 2 else second).cache.get("https://api.example/resource").json()
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual([{"value": 1}] * 4, list(pool.map(call, range(4))))
        self.assertEqual(1, transport.calls)

    def test_provider_rate_reservations_are_shared_between_instances(self):
        transport = SourceTransport()
        first = CorpusResponseCache(self.root / "rate.sqlite3", transport, {"*": 100.0})
        second = CorpusResponseCache(self.root / "rate.sqlite3", transport, {"*": 100.0})
        first._reserve("https://one.example/a")
        with first._db() as db:
            previous = db.execute("SELECT next_at FROM provider_slots").fetchone()[0]
        second._reserve("https://one.example/b")
        with first._db() as db:
            after = db.execute("SELECT next_at FROM provider_slots").fetchone()[0]
        self.assertGreaterEqual(after, previous + 0.009)

    def test_cache_hit_still_applies_the_current_egress_policy(self):
        transport = SourceTransport()
        original = self.acquirer(transport)
        original.fetch_work({"doi": "10.1234/example"})
        count = len(transport.calls)
        restricted = self.acquirer(transport, plan=EgressPlan(approved_operations=frozenset({"search_crossref"})))
        with self.assertRaises(ConfidentialityError):
            restricted.fetch_work({"doi": "10.1234/example"})
        self.assertEqual(count, len(transport.calls))

    def test_credentials_reach_transport_but_not_cache_or_portable_metadata(self):
        key = "fixture-key-do-not-persist"
        token = "fixture-bearer-do-not-persist=="
        cookie = "fixture-cookie-do-not-persist"
        class CredentialTransport:
            def __init__(self):
                self.calls = []
            def get(self, url, *, params=None, headers=None):
                self.calls.append((url, params, headers))
                return RawResponse(b'{"scientific":"x=1"}', 200,
                                   {"Content-Type": "application/json", "Set-Cookie": cookie},
                                   "https://api.example/record?api_key=" + key)
        transport = CredentialTransport()
        acquirer = self.acquirer(transport)
        headers = {"Authorization": "Bearer " + token, "Cookie": "session=" + cookie, "X-API-Key": key}
        with acquirer.cache.capture() as provenance:
            acquirer.cache.get("https://api.example/record?access_token=" + token,
                               params={"api_key": key, "id": "123"}, headers=headers)
        self.assertEqual(transport.calls[0][1]["api_key"], key)
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer " + token)
        stored = json.dumps(provenance)
        for path in self.root.rglob("*"):
            if path.is_file():
                stored += path.read_bytes().decode("utf-8", errors="ignore")
        for secret in (key, token, cookie):
            self.assertNotIn(secret, stored)
        request = provenance[0]["request"]
        self.assertEqual(request["params"]["api_key"], "[redacted]")
        self.assertNotIn("authorization", request["headers"])
        self.assertEqual(len(request["authorizationScopeSha256"]), 64)
        first = acquirer.cache.request_key("https://api.example/record", params={"api_key": key})[0]
        second = acquirer.cache.request_key("https://api.example/record", params={"api_key": key + "-rotated"})[0]
        self.assertNotEqual(first, second)

    def test_provider_errors_do_not_copy_supplied_credentials_into_receipts(self):
        secret = "fixture-secret-error-sentinel"
        class Failure:
            def get(self, url, **kwargs):
                raise ProviderError("provider HTTP 403 for api_key=" + secret)
        acquirer = self.acquirer(Failure())
        with acquirer.cache.capture():
            for _ in range(2):
                with self.assertRaises(ProviderError) as caught:
                    acquirer.cache.get("https://api.example/record", params={"api_key": secret})
                self.assertNotIn(secret, str(caught.exception))
                self.assertIn("[redacted]", str(caught.exception))

    def test_response_that_echoes_request_credentials_is_not_persisted(self):
        secret = "fixture-secret-reflected-in-body"
        class Echo:
            def get(self, url, **kwargs):
                return RawResponse(json.dumps({"api_key": secret}).encode(), 200, {})
        acquirer = self.acquirer(Echo())
        with self.assertRaisesRegex(ProviderError, "echoes request credentials"):
            acquirer.cache.get("https://api.example/record", params={"api_key": secret})
        with acquirer.cache._db() as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM responses").fetchone()[0])
        self.assertEqual([], list(acquirer.cache.artifacts.iterdir()))

    def test_transient_failure_is_recorded_but_not_cached_as_missing(self):
        class Flaky:
            calls = 0
            def get(self, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("provider HTTP 500 unavailable")
                return RawResponse(b"{}", 200, {})
        transport = Flaky()
        acquirer = self.acquirer(transport)
        with self.assertRaises(ProviderError):
            acquirer.cache.get("https://api.example/retry")
        self.assertEqual({}, acquirer.cache.get("https://api.example/retry").json())
        with acquirer.cache._db() as db:
            row = db.execute("SELECT outcome,retryable FROM failures").fetchone()
            self.assertEqual(("provider-transient", 1), tuple(row))
        self.assertFalse(failure_kind(ProviderError("HTTP 404"))["retryable"])
        self.assertEqual("access-blocked", failure_kind(ProviderError("HTTP 403"))["outcome"])

    def test_publisher_html_and_linked_tables_work_without_pmc(self):
        transport = PublisherTransport()
        acquirer = self.acquirer(transport)
        result = acquirer.fetch_review({"doi": "10.1234/example"})
        self.assertEqual("available", result["outcome"])
        doc = result["document"]
        self.assertEqual("text/html", doc["mediaType"])
        self.assertEqual(HTML, doc["originalBody"])
        self.assertIn(b"The table finding", doc["body"])
        self.assertEqual("linked-tables-inlined-v1", doc["transformation"])
        self.assertEqual(2, len(doc["components"]))
        self.assertTrue(any(row["sourceUrl"].endswith("/tables/1") for row in result["cacheProvenance"]))

    def test_mismatched_publisher_identity_never_becomes_review_evidence(self):
        transport = PublisherTransport(page=HTML.replace(b'content="10.1234/example"', b'content="10.1234/wrong"'))
        result = self.acquirer(transport).fetch_review({"doi": "10.1234/example"})
        self.assertIsNone(result["document"])
        self.assertEqual("inaccessible", result["outcome"])

    def test_missing_or_mismatched_linked_table_blocks_complete_review(self):
        for table in [b"<html><body>Access denied</body></html>", TABLE.replace(b'content="10.1234/example"', b'content="10.1234/wrong"')]:
            acquirer = self.acquirer(PublisherTransport(table=table), path=str(len(table)) + ".sqlite3")
            result = acquirer.fetch_review({"doi": "10.1234/example"})
            self.assertIsNone(result["document"])

    def test_tampered_cached_payload_fails_closed(self):
        acquirer = self.acquirer(SourceTransport())
        result = acquirer.prefetch_dois(["10.1234/example"])
        path = Path(result["cacheProvenance"][0]["artifactPath"])
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(CacheIntegrityError, "hash mismatch"):
            acquirer.fetch_work({"doi": "10.1234/example"})


if __name__ == "__main__":
    unittest.main()
