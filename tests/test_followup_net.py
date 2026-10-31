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
