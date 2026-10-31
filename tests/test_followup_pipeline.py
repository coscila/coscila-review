# SPDX-License-Identifier: Apache-2.0
"""The follow-up audit end to end: every command of the evidence and analysis sets, in pipeline order, on one branch
over the synthetic census, through the command line with result files.

The tests of each command set insert the rows the other set writes; here the commands write them for each other:
status rows feed the strata, the frozen strata feed support, function and numeric judging, full-text verdicts feed
the dependency, the dependency picks the reviews whose citing works are traversed, the cited-by edges feed the
second-generation sample, and every table feeds the lag model, the estimands, the gates, the facts and the exports.
No test reaches the network: provider answers come from the fixtures of the command sets' tests, the HTTPS opener of
the scholarly transport is replaced by a failing stub, and one scripted on-premises judge answers every instrument.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_estimands, followup_net, followup_strata
from ocsl_review.claim_check import ConfidentialPolicy
from ocsl_review.cli import main
from ocsl_review.followup_branch import FollowupBranch

from test_followup_citedby import routes as citedby_routes
from test_followup_estimands import declared
from test_followup_numeric import PARSES, TABLES, TABLES_SHA
from test_followup_secondgen import ROUTES as SECONDGEN_ROUTES, VERDICTS
from test_followup_status import RW_CSV, clean_environment, no_network, status_routes
from test_followup_strata import A1, A2, B1, lid
from test_followup_support import FULL_TEXT, jats, routes as support_routes

JA, JB = F.JOURNAL_A, F.JOURNAL_B
REAL_CLIENT = followup_net.client
JUDGE = ("--model", F.MODEL, "--base-url", "http://judge.example/v1")
# Review a1's shared sentence cites the retracted work and an ordinary work; the ordinary work's full text does not
# support that claim, so the retracted work is its essential support and review a1 is traversed for citing works.
COCITED_A1 = lid(JA, A1, "2")
PLAIN_FULL_TEXT = jats("10.5555/fu.work.plain1", "PMC900004", "Marker zeta predicts outcome in adults.")


def pipeline_protocol() -> dict:
    protocol = F.protocol()
    protocol["strata"]["S3"] = {**protocol["strata"]["S3"], "licensingVerified": True,
                                "tableSource": {"declaredVersion": "synthetic-tables-1", "sha256": TABLES_SHA}}
    protocol["secondGeneration"] = {**protocol["secondGeneration"], "edges": 4}
    protocol["estimands"] = [declared(name, labelSource="adjudication") if name == "distortion-type" else
                             declared(name) for name in followup_estimands.ESTIMANDS]
    return protocol


def words(sentence: str) -> str:
    return sentence.split(" ⟨")[0]


def answer(messages, settings):
    """One scripted judge for every instrument, told apart by the fields of its items."""
    payload = json.loads(messages[1]["content"])
    outputs = []
    for item in payload["items"]:
        if "evidenceUnits" in item:  # citation-support@2
            unit = item["evidenceUnits"][0]
            adverse = payload.get("evidenceTier") == "full-text" and item["id"].startswith(COCITED_A1 + "|")
            outputs.append({"id": item["id"], "supportVerdict": "contradicted" if adverse else "direct-support",
                            "rationale": "The evidence was compared with the claim.", "escalate": False,
                            "escalationReason": "", "evidence": [{"unitId": unit["id"], "quote": unit["text"][:20]}]})
        elif "candidateSentence" in item:  # claim-match@1
            verdict = VERDICTS.get(item["candidateSentence"]) or {
                "match": "different-claim", "reliance": "cites-review-otherwise",
                "quote": words(item["candidateSentence"])}
            outputs.append({"id": item["id"], **verdict, "rationale": "Compared.", "abstainReason": ""})
        elif "contextBefore" in item:  # citation-function@1
            sentence = item["citingSentence"]
            outputs.append({"id": item["id"], "function": "notes-retraction" if "retracted" in sentence else "relies",
                            "quote": words(sentence), "quoteField": "citingSentence",
                            "rationale": "The review rests a statement on it.", "abstainReason": ""})
        else:  # numeric-check@1
            outputs.append({**PARSES[item["marker"]], "id": item["id"]})
    return json.dumps({"items": outputs})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.census = F.make_parent(self.root)
        self.database = self.root / "followup.sqlite3"
        self.protocol = self.root / "protocol.json"
        self.protocol.write_text(json.dumps(pipeline_protocol()), encoding="utf-8")
        for patcher in (mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network),
                        mock.patch.dict(os.environ, clean_environment(), clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.judge = F.ScriptedJudge(answer)
        self.transports: list[F.FixtureTransport] = []
        self.calls = 0

    def invoke(self, verb: str, *argv: str, routes=None) -> dict:
        self.calls += 1
        target = self.root / f"result-{self.calls}-{verb}.json"
        policy = ConfidentialPolicy("public-literature")
        with ExitStack() as stack:
            for name in ("followup_judge", "followup_numeric", "followup_secondgen"):
                stack.enter_context(mock.patch(f"ocsl_review.{name}.judge_from_args",
                                               return_value=(self.judge, policy)))
            if routes is not None:
                transport = F.FixtureTransport(routes)
                self.transports.append(transport)
                stack.enter_context(mock.patch("ocsl_review.followup_net.client", lambda **kwargs: REAL_CLIENT(
                    transport=transport, sleep=lambda seconds: None)))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(redirect_stderr(StringIO()))
            code = main(["--result-file", str(target), "followup", verb, *argv])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, f"followup {verb}: {record.get('error')}")
        self.assertEqual(["followup", verb], record["command"])
        return record["result"]

    def rows(self, sql: str, *params) -> list[dict]:
        with closing(FollowupBranch(self.database).connect()) as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def test_every_command_runs_in_pipeline_order_on_one_branch(self):
        database = ("--database", str(self.database))
        self.invoke("init", *database, "--parent", str(self.census["parent"]), "--parent-text",
                    str(self.census["text"]), "--protocol", str(self.protocol))
        self.invoke("verify", *database)
        self.assertEqual({"parent", "parent-text", "protocol"},
                         {dataset["kind"] for dataset in self.invoke("describe", *database)["datasets"]})
        # status: every cited work of the parent gets one row
        self.invoke("status-import", *database, "--csv", str(RW_CSV), "--declared-version", "synthetic-snapshot")
        self.invoke("status-fetch", *database, routes=status_routes())
        resolved = self.invoke("status-resolve", *database)
        self.assertEqual(0, resolved["citedWorksWithoutStatusRow"])
        statuses = {row["work_key"]: row["status"] for row in self.rows("SELECT work_key, status FROM fu_work_status")}
        self.assertEqual({"w-retracted": "retracted", "w-concern": "expression-of-concern",
                          "w-corrected": "corrected"},
                         {work: status for work, status in statuses.items() if status not in ("none", "unknown")})
        # strata from the status rows, frozen before any judging
        self.invoke("hotspots", *database)
        frozen = self.invoke("strata", *database, "--freeze")
        self.assertTrue(frozen["freeze"]["freezeId"])
        s1 = {row["locus_id"] for row in self.rows("SELECT locus_id FROM fu_strata WHERE stratum='S1'")}
        self.assertEqual({lid(JA, A1, "1"), lid(JA, A2, "1"), lid(JA, A2, "2"), lid(JB, B1, "1"), lid(JB, B1, "2")},
                         s1)
        # judging on the frozen strata; the full-text verdicts decide the dependency
        self.invoke("support", *database, "--tier", "abstract", *JUDGE, routes=support_routes())
        full_text = {**support_routes(), FULL_TEXT.format("PMC900004"): (200, PLAIN_FULL_TEXT)}
        self.invoke("support", *database, "--tier", "full-text", "--europe-pmc", *JUDGE, routes=full_text)
        self.invoke("function", *database, *JUDGE)
        # every judging command draws its replicate units with the one rule of the strata module
        functions = [row["locus_id"] for row in self.rows("SELECT DISTINCT locus_id FROM fu_strata WHERE stratum IN "
                                                          "('S0','S1','S2','S3')")]
        replicate = self.invoke("function", *database, *JUDGE, "--variant", "replicate")
        self.assertEqual((followup_strata.REPLICATE_RULE, len(followup_strata.replicate_keys(
            pipeline_protocol(), functions, scope_parts=("citation-function@1",)))),
            (replicate["selection"]["replicateRule"], replicate["selection"]["replicateUnits"]))
        self.invoke("dependency", *database)
        dependency = {row["locus_id"]: row["class"] for row in self.rows("SELECT locus_id, class FROM fu_dependency")}
        self.assertEqual({lid(JA, A1, "1"): "essential", lid(JA, A2, "1"): "sole", lid(JA, A2, "2"): "sole",
                          lid(JB, B1, "1"): "sole", lid(JB, B1, "2"): "redundant"}, dependency)
        numeric = self.invoke("numeric", *database, "--tables", str(TABLES), "--allow-synthetic-tables", *JUDGE)
        self.assertEqual({"consistent"}, {row["class"] for row in self.rows(
            "SELECT class FROM fu_numeric WHERE locus_id=?", lid(JA, A1, "3"))}, numeric)
        # propagation: the affected reviews' citing works, then the later articles that restate the claim
        self.invoke("citedby", *database, routes=citedby_routes())
        self.invoke("second-gen", *database, *JUDGE, routes=SECONDGEN_ROUTES)
        confirmed = self.rows("SELECT anchor_locus_id, citing_key FROM fu_secondgen_matches WHERE assessable=1 AND "
                              "match='same-claim' AND reliance='cites-review-for-claim'")
        self.assertEqual([{"anchor_locus_id": lid(JA, A1, "1"), "citing_key": "doi:10.5555/fu.later.1"}], confirmed)
        # analysis, gates, facts and exports
        self.invoke("lag", *database)
        self.invoke("estimands", *database)
        gates = self.invoke("gates", *database)
        self.assertTrue(gates["ok"], gates)
        facts = self.invoke("facts", *database, "--output", str(self.root / "facts"))
        verified = self.invoke("verify-facts", "--facts", str(self.root / "facts"))
        self.assertEqual(facts["count"], verified["facts"])
        keys = set(json.loads((self.root / "facts" / "facts.json").read_text(encoding="utf-8"))["facts"])
        self.assertIn("followup.estimates.compromised-share.retracted.all.all.share", keys)
        self.assertFalse([key for key in keys if key.startswith("followup.estimates.followup.")])
        exported = self.invoke("export", *database, "--output", str(self.root / "exports"))
        self.assertEqual(1, exported["counts"]["articles"])
        graph = json.loads((self.root / "exports" / "propagation-graph.json").read_text(encoding="utf-8"))
        restated = [edge for edge in graph["edges"] if edge["kind"] == "restated-by"]
        self.assertEqual(["article:doi:10.5555/fu.later.1"], [edge["to"] for edge in restated])
        self.invoke("verify", *database)
        described = self.invoke("describe", *database)
        self.assertEqual({"parent", "parent-text", "protocol", "retraction-watch-csv", "status-snapshot",
                          "table-source", "cited-by-graph"}, {dataset["kind"] for dataset in described["datasets"]})
        self.assertEqual(1, len(described["protocolFreezes"]))
        # no request carried a contact identity
        for transport in self.transports:
            for request in transport.requests:
                self.assertNotIn("@", json.dumps(request["params"]))
                self.assertNotIn("mailto", request["target"])


if __name__ == "__main__":
    unittest.main()
