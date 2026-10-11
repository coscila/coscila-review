"""Synthetic broker results validate mechanics, never live-model quality."""
from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ocsl_review import census, literature
from ocsl_review.config import Paths
from ocsl_review.confidentiality import EgressPlan
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.util import atomic_json
import test_literature as fixtures

HOME = Path(__file__).resolve().parents[1]


class CensusTests(unittest.TestCase):
    setUp = fixtures.LiteratureTests.setUp
    tearDown = fixtures.LiteratureTests.tearDown
    confirm = fixtures.LiteratureTests.confirm

    def prepare(self, mode="full-text", limit=None):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.args = SimpleNamespace(workspace=str(self.workspace), paths=Paths(HOME, None, None),
                                    mode=mode, worker_config="reference-check", reference_id=[],
                                    limit=limit, retry=False, output="results/census")
        result = census.prepare(self.args, self.workspace, self.state, self.graph)
        self.args.run = result["runId"]
        return result

    def task(self):
        with census._database(self.workspace) as connection:
            return census._task_rows(connection, self.args.run)[0]

    def result(self, task=None, verdict="direct-support"):
        task = task or self.task()
        document = literature._verified_source(self.workspace, task["source"])
        unit = document["blocks"][0]
        return {"id": task["id"], "state": "completed", "providerId": "synthetic",
                "model": "synthetic-test-model", "promptSha256": task["inputSha256"], "usage": {"inputTokens": 100, "outputTokens": 20},
                "output": {"supportVerdict": verdict, "supportConfidence": 88,
                           "rationale": "Synthetic direct evidence.", "readUnitIds": [b["id"] for b in document["blocks"]],
                           "evidence": [{"unitId": unit["id"], "quote": unit["text"]}],
                           "abstractSufficientReason": "General qualitative association stated directly."}}

    def ingest_result(self, records):
        with census._database(self.workspace) as connection:
            return census.import_results(self.args, self.workspace, connection, records)

    def test_full_text_not_truncated_and_every_locus_survives(self):
        result = self.prepare()
        self.assertEqual(2, result["tasks"])
        self.assertEqual(2, result["dispatchable"])
        task = self.task()
        payload = json.loads(task["input"]["messages"][1]["content"])
        self.assertIn("No causal inference was made.", payload["evidenceUnits"][-1]["text"])
        self.assertEqual("full-text", payload["evidenceTier"])

    def test_long_evidence_tail_and_quotes_survive_verbatim(self):
        self.prepare()
        entry = self.state["entries"][0]
        text = "Filler sentence. " * 10000 + 'Final result: "quoted Unicode Ω".'
        document = {"body": text.encode(), "text": text, "complete": True,
                    "blocks": [{"id": "long-block", "kind": "paragraph", "locator": "Entire source", "text": text}]}
        source_id = literature._store_source(self.workspace, self.state, document, access_level="full-text", provider="synthetic")
        entry["sourceId"] = source_id
        result = census.prepare(self.args, self.workspace, self.state, self.graph)
        self.args.run = result["runId"]
        with census._database(self.workspace) as connection:
            task = next(task for task in census._task_rows(connection, self.args.run) if task["referenceId"] == entry["referenceId"])
        evidence = json.loads(task["input"]["messages"][1]["content"])["evidenceUnits"][0]["text"]
        self.assertEqual(text, evidence)
        record = self.result(task)
        record["output"]["evidence"][0]["quote"] = '"quoted Unicode Ω"'
        self.assertEqual(1, self.ingest_result([record])["accepted"])

    def test_history_is_append_only_and_duplicate_import_idempotent(self):
        self.prepare()
        record = self.result()
        self.assertEqual(1, self.ingest_result([record])["accepted"])
        self.assertEqual(1, self.ingest_result([record])["duplicates"])
        revised = self.result(verdict="scope-caveat")
        self.assertEqual(1, self.ingest_result([revised])["accepted"])
        with census._database(self.workspace) as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM assessments")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE tasks SET spec='{}'")

    def test_missing_units_invented_quotes_and_unrecorded_models_rejected(self):
        self.prepare()
        for mutate in (
            lambda r: r["output"].update(readUnitIds=[]),
            lambda r: r["output"]["evidence"][0].update(quote="Invented quote"),
            lambda r: r.update(model=None),
        ):
            record = self.result()
            mutate(record)
            self.assertEqual(1, self.ingest_result([record])["rejected"])
        self.assertEqual(1, self.ingest_result([self.result()])["accepted"])

    def test_unknown_and_duplicate_tasks_rejected_before_recording(self):
        self.prepare()
        record = self.result()
        with self.assertRaises(ValidationError):
            self.ingest_result([record, record])
        record["id"] = "invented"
        with self.assertRaises(ValidationError):
            self.ingest_result([record])

    def test_abstract_and_fulltext_runs_retain_independent_assessments(self):
        self.prepare(mode="abstract")
        abstract_run = self.args.run
        self.assertEqual("abstract-only", self.task()["source"]["accessLevel"])
        self.assertEqual(1, self.ingest_result([self.result()])["accepted"])
        self.args.mode = "full-text"
        result = census.prepare(self.args, self.workspace, self.state, self.graph)
        self.args.run = result["runId"]
        self.assertNotEqual(abstract_run, self.args.run)
        self.assertEqual(1, self.ingest_result([self.result()])["accepted"])
        with census._database(self.workspace) as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(DISTINCT run_id) FROM assessments").fetchone()[0])

    def test_specific_abstract_claim_cannot_receive_support(self):
        self.prepare(mode="abstract")
        task = self.task()
        task["claimType"] = "numeric"
        schema = json.loads((HOME / "schemas/census-assessment.schema.json").read_text())
        with self.assertRaisesRegex(ValidationError, "specific"):
            census.validate_assessment(task, self.result()["output"], schema, self.workspace)
        self.assertEqual("abstain", census.validate_assessment(task, self.result(verdict="abstain")["output"], schema, self.workspace)["supportVerdict"])

    def test_export_facts_recompute_and_unfinished_items_stay_uncertain(self):
        self.prepare()
        self.ingest_result([self.result()])
        result = census.cmd_export(self.args)
        bundle = json.loads(Path(result["facts"]).read_text())
        self.assertEqual(2, bundle["facts"]["citation_loci"]["value"])
        self.assertEqual(1, bundle["facts"]["uncertain"]["value"])
        self.assertEqual(100, bundle["facts"]["input_tokens_recorded"]["value"])
        self.assertIsNone(bundle["facts"]["total_tokens_recorded"]["value"])
        db = Path(result["facts"]).parent / "facts.sqlite3"
        with sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
            for fact in bundle["facts"].values():
                self.assertEqual(fact["value"], connection.execute(fact["sql"]).fetchone()[0])
                self.assertEqual(literature.sha256_file(db), fact["source"]["sha256"])

    def test_dispatch_denies_confidential_egress_before_broker_and_submits_once(self):
        self.prepare()
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(), self.workspace)), \
                patch.object(census, "_worker") as worker:
            with self.assertRaises(ConfidentialityError):
                census.cmd_run(self.args)
            worker.assert_not_called()
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", return_value={"jobId": "synthetic-job", "state": "queued"}) as worker:
            self.assertEqual("synthetic-job", census.cmd_run(self.args)["jobId"])
            self.assertEqual("already-submitted", census.cmd_run(self.args)["state"])
            worker.assert_called_once()

    def test_source_and_pinned_schema_tampering_fail_closed(self):
        self.prepare()
        task = self.task()
        (self.workspace / task["source"]["artifactPath"]).write_text("replaced")
        self.assertEqual(1, self.ingest_result([self.result_from_saved(task)])["rejected"])

    def result_from_saved(self, task):
        # Used only to test validation of a provider response after source tampering.
        return {"id": task["id"], "state": "completed", "providerId": "synthetic", "model": "test",
                "promptSha256": task["inputSha256"], "output": {"supportVerdict": "abstain", "supportConfidence": 0,
                  "rationale": "Insufficient evidence.", "readUnitIds": [], "evidence": [], "abstractSufficientReason": ""}}

    def test_ordinary_literature_records_have_immutable_history(self):
        self.prepare()
        entry = self.state["entries"][0]
        source = self.state["sources"][entry["sourceId"]]
        record = {"referenceId": entry["referenceId"], "readUnitIds": source["expectedUnitIds"]}
        literature.record_assessments(self.workspace, self.state, self.graph, record)
        literature.record_assessments(self.workspace, self.state, self.graph, record)
        histories = list((self.workspace / ".ocsl-review/literature/history").glob("*.json"))
        self.assertEqual(1, len(histories))
        self.assertEqual(record, json.loads(histories[0].read_text())["record"])


if __name__ == "__main__":
    unittest.main()
