# SPDX-License-Identifier: Apache-2.0
"""The follow-up's network base: polite spacing, no contact identity unless the operator configured one, filtered
receipts. No test reaches the network: the HTTPS opener of the scholarly transport is replaced by a failing stub."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_net
from ocsl_review.followup_branch import FollowupBranch


def no_network(*args, **kwargs):
    raise AssertionError("a follow-up test tried to open a network connection")


class NetworkBaseTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_contact_identity_is_sent_or_inferred(self):
        environment = {key: value for key, value in os.environ.items() if key != "OCSL_REVIEW_CONTACT_EMAIL"}
        environment.update(EMAIL="someone@example.org", GIT_AUTHOR_EMAIL="someone@example.org")
        transport = F.FixtureTransport({"https://api.crossref.org/works/": (200, "status/crossref-work-retracted.json")})
        with mock.patch.dict(os.environ, environment, clear=True):
            scholarly = followup_net.client(transport=transport, spacing={"api.crossref.org": 0.0})
            self.assertIsNone(scholarly.contact_email)
            record = scholarly._crossref_work("10.5555/fu.work.retracted", "get_publication_status")
            default = followup_net.client()
        self.assertEqual("10.5555/fu.work.retracted", record["doi"])
        self.assertTrue(transport.requests)
        for request in transport.requests:
            self.assertNotIn("@", json.dumps(request))
            self.assertNotIn("mailto", json.dumps(request).lower())
        self.assertNotIn("@", default.transport.user_agent)
        self.assertNotIn("mailto", default.transport.user_agent)

    def test_the_operators_configured_contact_is_the_only_one(self):
        with mock.patch.dict(os.environ, {"OCSL_REVIEW_CONTACT_EMAIL": "operator@example.org"}):
            scholarly = followup_net.client()
        self.assertEqual("operator@example.org", scholarly.contact_email)
        self.assertIn("mailto:operator@example.org", scholarly.transport.user_agent)

    def test_requests_to_one_host_keep_their_interval(self):
        clock = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(round(seconds, 3))
            clock[0] += seconds
        inner = F.FixtureTransport({"https://www.ebi.ac.uk/": (200, "status/europe-pmc-plain.json"),
                                    "https://api.crossref.org/": (200, "status/crossref-work-plain.json")})
        transport = followup_net.SpacedTransport(inner, {"www.ebi.ac.uk": 0.4}, clock=lambda: clock[0], sleep=sleep)
        for _ in range(3):
            transport.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={"query": "x"})
        transport.get("https://api.crossref.org/works/10.5555/x")
        self.assertEqual([0.4, 0.4], slept)
        self.assertEqual(4, len(inner.requests))

    def test_a_throttling_host_pushes_its_next_slot_by_retry_after_and_bulk_loops_can_stop(self):
        """A sustained HTTP 429 with Retry-After: 60: after the scholarly transport's own bounded retries, the host's
        next request waits for the server's Retry-After, and after a run of throttles the host counts as throttled."""
        import email.message
        import urllib.error
        from ocsl_review import scholarly
        from ocsl_review.errors import ProviderError
        clock, sleeps, sent = [1000.0], [], []

        def sleep(seconds):
            sleeps.append(round(seconds, 2))
            clock[0] += seconds

        def throttle(request, *, timeout, allow_cross_origin=False, authorize_url=None):
            sent.append(round(clock[0], 2))
            headers = email.message.Message()
            headers["Retry-After"] = "60"
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", headers, None)
        inner = scholarly.HttpTransport(user_agent="ocsl-review/0.1 (test)", sleep=sleep, monotonic=lambda: clock[0])
        spaced = followup_net.SpacedTransport(inner, clock=lambda: clock[0], sleep=sleep)
        client = type("Client", (), {"transport": spaced})()
        with mock.patch("ocsl_review.scholarly.open_public_https", side_effect=throttle):
            for index in range(followup_net.MAX_CONSECUTIVE_THROTTLES):
                self.assertFalse(followup_net.throttled(client, "crossref"))
                with self.assertRaises(ProviderError):
                    spaced.get(f"https://api.crossref.org/works/10.5555/w{index}")
        self.assertTrue(followup_net.throttled(client, "crossref"))
        self.assertTrue(followup_net.throttled(client, "api.crossref.org"))
        self.assertFalse(followup_net.throttled(client, "europe-pmc"))
        self.assertEqual(followup_net.MAX_CONSECUTIVE_THROTTLES, spaced.consecutive_throttles("api.crossref.org"))
        # every later work's first request waited the server's 60 seconds after the previous throttle
        attempts = len(sent) // followup_net.MAX_CONSECUTIVE_THROTTLES
        firsts = sent[::attempts]
        self.assertTrue(all(later - earlier >= 60 for earlier, later in zip(firsts, firsts[1:])), (firsts, sleeps))
        # an answer resets the host's run of throttles; without a Retry-After the backoff doubles
        ok = followup_net.SpacedTransport(F.FixtureTransport({"https://api.crossref.org/": (200, "status/crossref-work-plain.json")}),
                                          clock=lambda: clock[0], sleep=sleep)
        ok._throttles["api.crossref.org"] = 3
        ok.get("https://api.crossref.org/works/10.5555/x")
        self.assertEqual(0, ok.consecutive_throttles("api.crossref.org"))
        failing = followup_net.SpacedTransport(F.FixtureTransport({"https://api.crossref.org/": (429, b"{}")}),
                                               clock=lambda: clock[0], sleep=sleep)
        for expected in (5.0, 10.0):
            with self.assertRaises(ProviderError):
                failing.get("https://api.crossref.org/works/10.5555/y")
            self.assertAlmostEqual(clock[0] + expected, failing._next["api.crossref.org"])

    def test_receipts_drop_contact_and_key_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            census = F.make_parent(Path(directory))
            FollowupBranch.create(Path(directory) / "branch.sqlite3", census["parent"])
            branch = FollowupBranch(Path(directory) / "branch.sqlite3")
            with branch.writing() as connection:
                request_id = followup_net.record_request(
                    connection, provider="crossref", operation="status", outcome="ok", http_status=200,
                    url="https://api.crossref.org/works?filter=updates:10.5555/x&mailto=operator@example.org",
                    params={"rows": 20, "email": "operator@example.org", "api_key": "secret-value"}, body=b"{}")
                row = connection.execute("SELECT * FROM fu_requests WHERE request_id=?", (request_id,)).fetchone()
                self.assertEqual("https://api.crossref.org/works", row["url"])
                recorded = json.loads(row["params_json"])
                self.assertEqual({"rows": 20, "filter": "updates:10.5555/x"}, recorded)
                self.assertNotIn("@", json.dumps(dict(row)))
                self.assertNotIn("secret-value", json.dumps(dict(row)))


if __name__ == "__main__":
    unittest.main()
