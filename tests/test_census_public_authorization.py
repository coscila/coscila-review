"""Public worker authorization is distinct from workspace material labels."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from ocsl_review import census, literature, public_literature
from ocsl_review.confidentiality import EgressPlan
from ocsl_review.errors import ConfidentialityError
from ocsl_review.util import atomic_json
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.claim_inventory import build_claim_inventory
import test_census as fixtures


class PublicCensusAuthorizationTests(unittest.TestCase):
    setUp = fixtures.CensusTests.setUp
    tearDown = fixtures.CensusTests.tearDown
    confirm = fixtures.CensusTests.confirm
    prepare = fixtures.CensusTests.prepare

    def public_run(self):
        self.configuration.update(evidencePolicy="public-literature", bulkConcurrencyLimit=60)
        published = self.workspace / "published.xml"
        published.write_bytes((fixtures.HOME / "tests/fixtures/citation_census_canary.xml").read_bytes())
        self.manuscript = ingest_manuscript(published, confidential=False)
        self.graph = build_claim_inventory(self.manuscript)
        atomic_json(self.workspace / ".ocsl-review/manuscript.json", self.manuscript)
        self.state, self.graph = literature.prepare_literature(self.workspace, self.manuscript, self.graph)
        literature._save(self.workspace, self.state, self.graph, event="public-test-prepare")
        self.prepare()
        with census._database(self.workspace) as connection:
            run = census._run(connection, self.args.run)
            tasks = census._task_rows(connection, self.args.run)
        for task in tasks:
            task["source"].update(provider="europe-pmc", sourceUrl="https://www.ebi.ac.uk/source.xml")
        catalog = {"revision": 7, "workers": [copy.deepcopy(self.configuration)],
                   "authorization": {"publicLiteratureAllowed": True, "attemptId": "approved-attempt",
                                     "workspace": str(self.workspace.parent)}}
        self.enterContext(patch.dict(os.environ, {"PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID": "approved-attempt"}))
        return run, tasks, catalog

    def authorize(self, run, tasks, catalog):
        with patch.object(public_literature, "broker_catalog", return_value=catalog):
            return public_literature.authorize_public_census(self.workspace, run, tasks)

    def test_public_corpus_grant_binds_active_worker_revision_and_source_hashes(self):
        run, tasks, catalog = self.public_run()
        with patch.object(public_literature, "broker_catalog", return_value=catalog), \
                patch.object(census, "load_scholarly_context", return_value=(EgressPlan(), self.workspace)):
            census._authorize(self.args, self.workspace, run, tasks)
        events = json.loads((self.workspace / ".ocsl-review/egress-events.json").read_text())
        event = events[-1]
        self.assertEqual(event["decision"], "allowed-configured-public-literature")
        self.assertEqual(event["attemptId"], "approved-attempt")
        self.assertEqual(event["workerConfigSha256"], run["spec"]["workerConfigSha256"])
        self.assertEqual(len(event["sourceManifestSha256"]), 64)
        self.assertNotIn("manuscriptEvidence", event)

    def test_workspace_labels_never_grant_worker_permission(self):
        run, tasks, catalog = self.public_run()
        for mutation in (
            lambda c: c["authorization"].update(publicLiteratureAllowed=False),
            lambda c: c["authorization"].update(attemptId="previous-attempt"),
            lambda c: c["authorization"].update(workspace=str(self.workspace / "different-chat")),
            lambda c: c.update(revision=8),
            lambda c: c["workers"][0].pop("evidencePolicy"),
            lambda c: c["workers"][0]["modelProvider"].update(model="different-model"),
            lambda c: c["workers"][0].update(assignment="different assessment"),
            lambda c: c["workers"][0].update(effort="none"),
        ):
            altered = copy.deepcopy(catalog)
            mutation(altered)
            with self.assertRaises(ConfidentialityError):
                self.authorize(run, tasks, altered)

    def test_live_capacity_change_keeps_public_permission_and_pinned_receipt(self):
        run, tasks, catalog = self.public_run()
        original = copy.deepcopy(run)
        catalog["workers"][0]["bulkConcurrencyLimit"] = 160
        event = self.authorize(run, tasks, catalog)
        self.assertEqual("allowed-configured-public-literature", event["decision"])
        self.assertEqual(original["spec"]["workerConfigSha256"], event["workerConfigSha256"])
        self.assertEqual(original, run)
        # A capacity-only change cannot relax attempt ownership or disclosure.
        catalog["authorization"]["publicLiteratureAllowed"] = False
        with self.assertRaises(ConfidentialityError):
            self.authorize(run, tasks, catalog)

    def test_capacity_normalization_cannot_hide_corrupt_snapshot_or_invalid_capacity(self):
        run, tasks, catalog = self.public_run()
        corrupted = copy.deepcopy(run)
        corrupted["spec"]["workerConfig"]["bulkConcurrencyLimit"] = 160
        with self.assertRaises(ConfidentialityError):
            self.authorize(corrupted, tasks, catalog)
        for limit in (0, True, 1.5, "160", None, 2**53):
            altered = copy.deepcopy(catalog)
            altered["workers"][0]["bulkConcurrencyLimit"] = limit
            with self.subTest(limit=limit), self.assertRaises(ConfidentialityError):
                self.authorize(run, tasks, altered)

    def test_confidential_manuscripts_and_local_attachments_are_excluded(self):
        run, tasks, catalog = self.public_run()
        path = self.workspace / self.state["manuscriptPath"]
        manuscript = json.loads(path.read_text())
        for alteration in ({"confidential": True}, {"structuredSource": {}}):
            atomic_json(path, {**manuscript, **alteration})
            with self.assertRaises(ConfidentialityError):
                self.authorize(run, tasks, catalog)
        atomic_json(path, manuscript)
        for change in ({"provider": "local"}, {"sourceUrl": None}):
            altered = copy.deepcopy(tasks)
            altered[0]["source"].update(change)
            with self.assertRaisesRegex(ConfidentialityError, "provenance"):
                self.authorize(run, altered, catalog)

    def test_post_prepare_relabelling_or_manuscript_edits_do_not_grant_permission(self):
        run, tasks, catalog = self.public_run()
        path = self.workspace / self.state["manuscriptPath"]
        manuscript = json.loads(path.read_text())
        manuscript["paragraphs"][0]["text"] = "Substituted private manuscript text"
        atomic_json(path, manuscript)
        with self.assertRaisesRegex(ConfidentialityError, "changed manuscripts"):
            self.authorize(run, tasks, catalog)

    def test_payload_must_match_verified_acquired_evidence(self):
        run, tasks, catalog = self.public_run()
        payload = json.loads(tasks[0]["input"]["messages"][1]["content"])
        payload["evidenceUnits"][0]["text"] = "Private text substituted for published evidence"
        tasks[0]["input"]["messages"][1]["content"] = json.dumps(payload)
        with self.assertRaisesRegex(ConfidentialityError, "differs"):
            self.authorize(run, tasks, catalog)

    def test_explicit_operator_restriction_is_not_overridden(self):
        run, tasks, catalog = self.public_run()
        atomic_json(self.workspace / ".ocsl-review/egress-plan.json", {"confidential": True})
        with patch.object(public_literature, "broker_catalog") as broker, \
                patch.object(census, "load_scholarly_context", return_value=(EgressPlan(), self.workspace)):
            with self.assertRaises(ConfidentialityError):
                census._authorize(self.args, self.workspace, run, tasks)
            broker.assert_not_called()

    def test_configuration_contract_accepts_policy_without_runtime_authority_in_snapshot(self):
        from test_census import REAL_CONFIGURATION
        run, tasks, catalog = self.public_run()
        with patch.object(census, "_worker", return_value=catalog):
            revision, config = REAL_CONFIGURATION(self.args, self.workspace)
        self.assertEqual(config["evidencePolicy"], "public-literature")
        self.assertNotIn("authorization", config)
