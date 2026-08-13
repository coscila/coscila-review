from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from ocsl_review.confidentiality import EgressPlan
from ocsl_review.errors import ConfidentialityError, ProviderError, ReviewError
from ocsl_review.scholarly import (
    MAX_RESPONSE_BYTES,
    HttpTransport,
    RawResponse,
    ScholarlyClient,
    call_tool,
    normalize_doi,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeHttpResponse:
    def __init__(self, body=b"{}", *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._body.close()

    def read(self, size=-1):
        return self._body.read(size)


def http_error(status, *, headers=None, body=b"rate limited"):
    return urllib.error.HTTPError(
        "https://api.crossref.org/works",
        status,
        "provider error",
        headers or {},
        io.BytesIO(body),
    )


class FakeTransport:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params or {}, headers or {}))
        if "crossref.org/works/" in url:
            data = {
                "message": {
                    "DOI": "10.1000/example",
                    "title": ["Example"],
                    "author": [{"given": "A", "family": "Researcher"}],
                    "published": {"date-parts": [[2025]]},
                    "container-title": ["Journal"],
                    "type": "journal-article",
                    "update-to": [],
                    "relation": {},
                }
            }
        elif "crossref.org/works" in url:
            data = {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/example",
                            "title": ["Example"],
                            "author": [],
                            "published": {"date-parts": [[2025]]},
                            "container-title": ["Journal"],
                            "type": "journal-article",
                        }
                    ]
                }
            }
        elif "esearch.fcgi" in url:
            data = {"esearchresult": {"idlist": ["123"]}}
        elif "esummary.fcgi" in url:
            data = {
                "result": {
                    "123": {
                        "title": "PubMed item",
                        "authors": [],
                        "pubdate": "2025",
                        "source": "J",
                        "pubtype": ["Journal Article"],
                        "articleids": [],
                    }
                }
            }
        elif "europepmc" in url:
            data = {"resultList": {"result": []}}
        elif "datacite" in url:
            data = {
                "data": {
                    "id": "10.1000/example",
                    "attributes": {
                        "doi": "10.1000/example",
                        "titles": [],
                        "creators": [],
                        "types": {"resourceTypeGeneral": "Dataset"},
                    },
                }
            }
        else:
            data = []
        return RawResponse(json.dumps(data).encode(), 200, {})


class ScholarlyTests(unittest.TestCase):
    def test_normalize_doi(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1000/ABC."), "10.1000/abc")
        with self.assertRaises(ReviewError):
            normalize_doi("not-a-doi")

    def test_searches_are_bounded_and_source_explicit(self):
        transport = FakeTransport()
        client = ScholarlyClient(plan=EgressPlan(), transport=transport)
        result = client.search_crossref("replication null evidence", limit=1000)
        self.assertEqual(result["provider"], "crossref")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(transport.calls[0][1]["rows"], 25)

    def test_identifier_resolution_reconciles_multiple_public_sources(self):
        client = ScholarlyClient(plan=EgressPlan(), transport=FakeTransport())
        result = client.resolve_identifier(doi="10.1000/example")
        self.assertEqual(result["operation"], "resolve_identifier")
        self.assertTrue(any(item["provider"] == "crossref" for item in result["records"]))

    def test_raw_reference_title_is_blocked_for_confidential_review(self):
        client = ScholarlyClient(plan=EgressPlan(), transport=FakeTransport())
        with self.assertRaises(ConfidentialityError):
            client.resolve_reference(
                {"title": "Unpublished hidden manuscript title", "author": "Hidden"}
            )

    def test_status_does_not_use_a_stale_side_database(self):
        transport = FakeTransport()
        client = ScholarlyClient(plan=EgressPlan(), transport=transport)
        result = client.get_publication_status(doi="10.1000/example")
        self.assertEqual(result["state"], "current")
        called_hosts = " ".join(call[0] for call in transport.calls)
        self.assertIn("api.crossref.org", called_hosts)
        self.assertNotIn("labs.crossref", called_hosts)

    def test_public_tool_allowlist_cannot_be_bypassed_through_helper_operations(self):
        cases = (
            (
                "resolve_reference",
                {"reference": {"doi": "10.1000/example"}},
                "resolve_identifier",
            ),
            (
                "get_corrections_retractions",
                {"doi": "10.1000/example"},
                "get_publication_status",
            ),
            (
                "get_abstract_or_open_sections",
                {"doi": "10.1000/example"},
                "resolve_identifier",
            ),
        )
        for requested, arguments, helper in cases:
            with self.subTest(requested=requested, helper=helper):
                transport = FakeTransport()
                plan = EgressPlan.from_mapping({"approvedOperations": [helper]})
                client = ScholarlyClient(plan=plan, transport=transport)
                with self.assertRaisesRegex(
                    ConfidentialityError,
                    f"outside the approved egress plan: {requested}",
                ):
                    call_tool(client, requested, arguments)
                self.assertEqual(transport.calls, [])

    def test_wrapper_tool_allowlist_identity_reaches_every_provider_and_ledger_event(self):
        cases = (
            (
                "resolve_reference",
                {"reference": {"doi": "10.1000/example"}},
            ),
            (
                "get_corrections_retractions",
                {"doi": "10.1000/example"},
            ),
            (
                "get_abstract_or_open_sections",
                {"doi": "10.1000/example"},
            ),
        )
        for requested, arguments in cases:
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                transport = FakeTransport()
                plan = EgressPlan.from_mapping({"approvedOperations": [requested]})
                client = ScholarlyClient(
                    plan=plan,
                    workspace=workspace,
                    transport=transport,
                )
                call_tool(client, requested, arguments)
                self.assertGreater(len(transport.calls), 0)
                events = json.loads(
                    (workspace / ".ocsl-review" / "egress-events.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(events)
                self.assertEqual({event["operation"] for event in events}, {requested})

    def test_direct_wrapper_call_uses_and_then_releases_its_public_operation_scope(self):
        transport = FakeTransport()
        plan = EgressPlan.from_mapping({"approvedOperations": ["resolve_reference"]})
        client = ScholarlyClient(plan=plan, transport=transport)

        client.resolve_reference({"doi": "10.1000/example"})
        completed_calls = len(transport.calls)
        with self.assertRaisesRegex(
            ConfidentialityError,
            "outside the approved egress plan: resolve_identifier",
        ):
            client.resolve_identifier(doi="10.1000/example")
        self.assertEqual(len(transport.calls), completed_calls)

    def test_http_transport_retries_429_and_respects_retry_after(self):
        clock = FakeClock()
        transport = HttpTransport(
            user_agent="test",
            timeout=5,
            max_attempts=3,
            max_retry_delay=2,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        responses = [
            http_error(429, headers={"Retry-After": "1"}),
            FakeHttpResponse(b'{"message":"ok"}'),
        ]
        with patch("ocsl_review.scholarly.urllib.request.urlopen", side_effect=responses) as opener:
            response = transport.get("https://api.crossref.org/works")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"message": "ok"})
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(clock.sleeps, [1.0])
        first_timeout = opener.call_args_list[0].kwargs["timeout"]
        second_timeout = opener.call_args_list[1].kwargs["timeout"]
        self.assertEqual(first_timeout, 5.0)
        self.assertEqual(second_timeout, 4.0)

    def test_http_transport_caps_retry_delay_and_attempt_count(self):
        clock = FakeClock()
        transport = HttpTransport(
            user_agent="test",
            timeout=10,
            max_attempts=99,
            max_retry_delay=0.5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        with (
            patch(
                "ocsl_review.scholarly.urllib.request.urlopen",
                side_effect=[http_error(429, headers={"Retry-After": "999"}) for _ in range(4)],
            ) as opener,
            self.assertRaisesRegex(ProviderError, "provider HTTP 429"),
        ):
            transport.get("https://api.crossref.org/works")

        self.assertEqual(opener.call_count, 4)
        self.assertEqual(clock.sleeps, [0.5, 0.5, 0.5])

    def test_http_transport_does_not_retry_non_transient_status(self):
        clock = FakeClock()
        transport = HttpTransport(user_agent="test", sleep=clock.sleep, monotonic=clock.monotonic)
        with (
            patch(
                "ocsl_review.scholarly.urllib.request.urlopen", side_effect=http_error(400)
            ) as opener,
            self.assertRaisesRegex(ProviderError, "provider HTTP 400"),
        ):
            transport.get("https://api.crossref.org/works")

        self.assertEqual(opener.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_http_transport_does_not_extend_absolute_timeout_for_retry(self):
        clock = FakeClock()
        transport = HttpTransport(
            user_agent="test",
            timeout=1,
            max_retry_delay=5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        with (
            patch(
                "ocsl_review.scholarly.urllib.request.urlopen",
                side_effect=http_error(429, headers={"Retry-After": "2"}),
            ) as opener,
            self.assertRaisesRegex(ProviderError, "retry delay exceeds"),
        ):
            transport.get("https://api.crossref.org/works")

        self.assertEqual(opener.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_http_transport_keeps_response_size_bound_after_retry(self):
        clock = FakeClock()
        transport = HttpTransport(
            user_agent="test",
            timeout=5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        responses = [
            http_error(503, headers={"Retry-After": "0"}),
            FakeHttpResponse(headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
        ]
        with (
            patch("ocsl_review.scholarly.urllib.request.urlopen", side_effect=responses) as opener,
            self.assertRaisesRegex(ProviderError, "response exceeds"),
        ):
            transport.get("https://api.crossref.org/works")

        self.assertEqual(opener.call_count, 2)


if __name__ == "__main__":
    unittest.main()
