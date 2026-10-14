# SPDX-License-Identifier: Apache-2.0
"""Synthetic broker results validate mechanics, never live-model quality."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from ocsl_review import census, literature
from ocsl_review.config import Paths
from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.confidentiality import EgressPlan
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.util import atomic_json
import test_literature as fixtures
from test_fulltext import SourceTransport
from ocsl_review.scholarly import ScholarlyClient

HOME = Path(__file__).resolve().parents[1]
REAL_CONFIGURATION = census._configuration


class CensusTests(unittest.TestCase):
    def setUp(self):
        fixtures.LiteratureTests.setUp(self)
        self.configuration = {"id": "reference-check", "assignment": "Synthetic reviewer",
                              "agent": "codex", "modelProvider": {"id": "synthetic", "model": "synthetic-test-model"},
                              "providerId": "synthetic", "model": "synthetic-test-model", "bulkAvailable": True}
        self.config_patch = patch.object(census, "_configuration", return_value=(7, self.configuration))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
    tearDown = fixtures.LiteratureTests.tearDown
    confirm = fixtures.LiteratureTests.confirm
    invoke = fixtures.LiteratureTests.invoke

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

    def test_paginated_wait_and_full_status_records_are_the_same_assessment(self):
        self.prepare()
        record = self.result()
        job = {"id": "synthetic-job", "kind": "bulk", "task_ids": [record["id"]]}
        with census._database(self.workspace) as connection:
            first = census._job_results(self.args, self.workspace, connection, job, [{**record, "revision": 8}])
            second = census._job_results(self.args, self.workspace, connection, job, [record])
            self.assertEqual(first["accepted"], 1)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 1)

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

    def test_real_acquisition_contract_dispatches_complete_abstract_beside_full_text(self):
        manuscript_path = self.workspace / "manuscript.md"
        manuscript_path.write_text(fixtures.TEXT.replace("Source observations", "Sample study"))
        self.manuscript = ingest_manuscript(manuscript_path)
        self.graph = build_claim_inventory(self.manuscript)
        atomic_json(self.workspace / ".ocsl-review/manuscript.json", self.manuscript)
        self.state, self.graph = literature.prepare_literature(self.workspace, self.manuscript, self.graph)
        literature._save(self.workspace, self.state, self.graph, event="new-test-manuscript")
        self.client = ScholarlyClient(transport=SourceTransport())
        result = self.prepare(mode="abstract")
        self.assertEqual(result["dispatchable"], 2)
        self.assertEqual({self.state["sources"][entry["sourceId"]]["accessLevel"] for entry in self.state["entries"]}, {"full-text"})
        task = self.task()
        self.assertEqual(task["source"]["accessLevel"], "abstract-only")
        self.assertTrue(task["source"]["sourceUrl"].startswith("https://"))
        self.assertEqual(task["source"]["identifiers"]["doi"], "10.1234/example")
        self.assertEqual(self.ingest_result([self.result()])["accepted"], 1)

    def test_legacy_abstract_completeness_requires_reacquisition_not_flag_migration(self):
        self.client.abstract_only = True
        self.prepare(mode="abstract")
        for entry in self.state["entries"]:
            entry["abstractCandidates"] = [{"provider": "pubmed", "text": "Old complete-looking sentence.", "complete": True}]
        result = census.prepare(self.args, self.workspace, self.state, self.graph)
        self.assertEqual(result["dispatchable"], 0)
        self.args.run = result["runId"]
        self.assertIn("retry acquisition", self.task()["blockedReason"])

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
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
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

    def test_pinned_schema_and_task_set_tampering_fail_closed(self):
        self.prepare()
        record = self.result()
        schema_path = self.workspace / census.ROOT / self.args.run / "output-schema.json"
        schema = json.loads(schema_path.read_text())
        schema["additionalProperties"] = True
        atomic_json(schema_path, schema)
        with self.assertRaisesRegex(ValidationError, "schema changed"):
            self.ingest_result([record])
        with census._database(self.workspace) as connection:
            connection.execute("DROP TRIGGER task_no_update")
            connection.execute("UPDATE tasks SET spec='{}'")
        with self.assertRaisesRegex(ValidationError, "task inputs changed"):
            census.cmd_status(self.args)

    def test_worker_settings_are_pinned_and_changed_revision_rejected(self):
        self.prepare()
        with census._database(self.workspace) as connection:
            run = census._run(connection, self.args.run)
        self.assertEqual(7, run["spec"]["workerConfigRevision"])
        self.assertEqual(self.configuration, run["spec"]["workerConfig"])
        with patch.object(census, "_configuration", return_value=(8, self.configuration)), \
                patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker") as worker:
            with self.assertRaisesRegex(ValidationError, "settings changed"):
                census.cmd_run(self.args)
            worker.assert_not_called()

    def test_public_configuration_rejects_credentials_and_unknown_worker(self):
        self.prepare()
        response = {"version": 1, "revision": 7, "workers": [self.configuration]}
        with patch.object(census, "_worker", return_value=response):
            self.assertEqual((7, self.configuration), REAL_CONFIGURATION(self.args, self.workspace))
            self.args.worker_config = "unconfigured"
            with self.assertRaisesRegex(ValidationError, "configured"):
                REAL_CONFIGURATION(self.args, self.workspace)
            self.args.worker_config = "reference-check"
            self.configuration["modelProvider"]["authTokenEnv"] = "NEVER_WORKSPACE_KEY"
            with self.assertRaisesRegex(ValidationError, "public configuration"):
                REAL_CONFIGURATION(self.args, self.workspace)

    @unittest.skipUnless(shutil.which("node"), "helper execution regression requires Node")
    def test_worker_helper_uses_selected_workspace_with_lab_repo_set(self):
        self.prepare()
        helper = self.workspace / "tools/ocsl-workers.mjs"
        helper.parent.mkdir()
        helper.write_text('console.log(JSON.stringify({source: "selected-workspace", command: process.argv[2]}));\n')
        self.args.paths = Paths(HOME, None, self.workspace.parent / "lab-source-without-helper")
        with patch.dict(os.environ, {"OCSL_WORKERS_CLI": "", "OCSL_WORKERS_NODE": shutil.which("node")}):
            response = census._worker(self.args, self.workspace, "configs")
        self.assertEqual({"source": "selected-workspace", "command": "configs"}, response)

    @unittest.skipUnless(shutil.which("node"), "helper execution regression requires Node")
    def test_worker_helper_honors_explicit_absolute_override(self):
        self.prepare()
        helper = self.workspace.parent / "explicit-worker.mjs"
        helper.write_text('console.log(JSON.stringify({source: "explicit-override"}));\n')
        with patch.dict(os.environ, {"OCSL_WORKERS_CLI": str(helper), "OCSL_WORKERS_NODE": shutil.which("node")}):
            self.assertEqual({"source": "explicit-override"}, census._worker(self.args, self.workspace, "configs"))
        with patch.dict(os.environ, {"OCSL_WORKERS_CLI": "relative-helper.mjs"}):
            with self.assertRaisesRegex(ValidationError, "absolute helper path"):
                census._worker(self.args, self.workspace, "configs")

    def test_operator_reading_progress_does_not_change_evidence_run(self):
        initial = self.prepare()
        for source in self.state["sources"].values():
            source["readUnitIds"] = source["expectedUnitIds"]
        repeat = census.prepare(self.args, self.workspace, self.state, self.graph)
        self.assertEqual(initial["runId"], repeat["runId"])

    def test_zero_limit_and_unknown_reference_filter_rejected(self):
        with self.assertRaisesRegex(ValidationError, "positive"):
            self.prepare(limit=0)
        self.args.limit = None
        self.args.reference_id = ["unbound"]
        with self.assertRaisesRegex(ValidationError, "unknown manuscript reference"):
            census.prepare(self.args, self.workspace, self.state, self.graph)

    def test_native_agent_jobs_keep_each_locus_and_resume_survives_job_rotation(self):
        self.configuration["bulkAvailable"] = False
        self.prepare()
        tasks = None
        with census._database(self.workspace) as connection:
            tasks = census._task_rows(connection, self.args.run)
        calls = []
        def submit(args, workspace, *command):
            self.assertEqual("agent", command[0])
            path = Path(command[command.index("--task-file") + 1])
            payload = json.loads(path.read_text().split("\n", 1)[1])
            self.assertIn("outputSchema", payload)
            self.assertEqual(2, len(payload["messages"]))
            calls.append(command)
            return {"jobId": "native-" + str(len(calls)), "state": "queued", "configRevision": 7}
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", side_effect=submit):
            submitted = census.cmd_run(self.args)
            self.assertEqual(2, len(submitted["jobs"]))
            self.assertEqual("already-submitted", census.cmd_run(self.args)["state"])
        with census._database(self.workspace) as connection:
            jobs = census._jobs(connection, self.args.run)
        task = next(task for task in tasks if task["id"] == jobs[0]["task_ids"][0])
        record = self.result(task)
        record["id"] = jobs[0]["id"]
        record["output"] = json.dumps(record["output"])
        self.args.job = jobs[0]["id"]
        with patch.object(census, "_worker", return_value={"jobId": self.args.job, "state": "completed", "results": [record]}):
            response = census.cmd_status(self.args)
            self.assertEqual(1, response["jobs"][0]["import"]["accepted"])
        self.args.job = jobs[1]["id"]
        paused = {"jobId": self.args.job, "state": "paused", "results": []}
        resumed = {"jobId": "native-reconnected", "state": "queued", "configRevision": 7, "results": []}
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", side_effect=[paused, resumed]):
            self.assertEqual("native-reconnected", census.cmd_resume(self.args)["jobs"][0]["jobId"])
        listed = census.cmd_list(self.args)
        self.assertIn("native-reconnected", listed["runs"][0]["jobIds"])
        with census._database(self.workspace) as connection:
            self.assertEqual(jobs[1]["id"], census._jobs(connection, self.args.run)[-1]["parent_job"])

    def test_bounded_wait_uses_file_and_imports_before_compact_return(self):
        self.prepare()
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", return_value={"jobId": "bulk-job", "state": "queued"}):
            census.cmd_run(self.args)
        record = self.result()
        self.args.timeout_ms = 1000
        def wait(args, workspace, *command):
            self.assertEqual("wait", command[0])
            path = Path(command[command.index("--out") + 1])
            path.write_text(json.dumps(record) + "\n")
            return {"jobId": "bulk-job", "state": "completed", "resultsFile": str(path), "itemCount": 1}
        with patch.object(census, "_worker", side_effect=wait):
            response = census.cmd_wait(self.args)
        self.assertEqual(1, response["jobs"][0]["import"]["accepted"])
        self.assertNotIn("results", response["jobs"][0])

    def test_retry_excludes_accepted_loci_and_resume_keeps_old_completed_results(self):
        self.prepare()
        record = self.result()
        context = patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace))
        with context, patch.object(census, "_worker", return_value={"jobId": "old-job", "state": "queued"}):
            census.cmd_run(self.args)
        old = {"jobId": "old-job", "state": "failed", "results": [record]}
        new = {"jobId": "new-job", "state": "queued", "results": []}
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", side_effect=[old, new]):
            census.cmd_resume(self.args)
        self.args.retry = True
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", return_value={"jobId": "retry-job", "state": "queued"}) as worker:
            census.cmd_run(self.args)
        command = worker.call_args.args
        items = Path(command[command.index("--items") + 1])
        dispatched = [json.loads(line) for line in items.read_text().splitlines()]
        self.assertEqual(1, len(dispatched))
        self.assertNotEqual(record["id"], dispatched[0]["id"])

    def test_resume_revalidates_source_and_egress(self):
        self.prepare()
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(confidential=False), self.workspace)), \
                patch.object(census, "_worker", return_value={"jobId": "old-job", "state": "queued"}):
            census.cmd_run(self.args)
        with patch.object(census, "load_scholarly_context", return_value=(EgressPlan(), self.workspace)), \
                patch.object(census, "_worker") as worker:
            with self.assertRaises(ConfidentialityError):
                census.cmd_resume(self.args)
            worker.assert_not_called()
        task = self.task()
        (self.workspace / task["source"]["artifactPath"]).write_text("changed")
        with patch.object(census, "_worker") as worker:
            with self.assertRaisesRegex(ValidationError, "artifact changed"):
                census.cmd_resume(self.args)
            worker.assert_not_called()

    def test_native_usage_keys_and_cache_subsets_preserved_without_invented_arithmetic(self):
        self.prepare()
        record = self.result()
        record["usage"] = {"input": 100, "output": 20, "cacheRead": 80, "cacheWrite": 0, "reasoning": 5, "total": None, "quality": "partial"}
        self.ingest_result([record])
        result = census.cmd_export(self.args)
        bundle = json.loads(Path(result["facts"]).read_text())
        self.assertEqual(100, bundle["facts"]["input_tokens_recorded"]["value"])
        self.assertEqual(80, bundle["facts"]["cache_read_tokens_recorded"]["value"])
        self.assertIsNone(bundle["facts"]["total_tokens_recorded"]["value"])
        self.assertEqual(1, bundle["facts"]["calls_missing_total_tokens"]["value"])

    def test_cli_routes_census_commands_and_reconnection_listing(self):
        self.prepare()
        code, stdout, stderr = self.invoke("review", "census", "list")
        self.assertEqual(0, code, stderr)
        self.assertEqual(self.args.run, json.loads(stdout)["runs"][0]["runId"])
        code, stdout, stderr = self.invoke("review", "census", "status", "--run", self.args.run)
        self.assertEqual(0, code, stderr)
        self.assertEqual("prepared", json.loads(stdout)["state"])
        code, stdout, stderr = self.invoke("review", "census", "export", "--run", self.args.run)
        self.assertEqual(0, code, stderr)
        self.assertEqual(2, json.loads(stdout)["measurements"])

    def test_structured_census_facts_chain_preserves_every_display_and_range_member(self):
        target = self.workspace / "canary.xml"
        target.write_bytes((HOME / "tests/fixtures/citation_census_canary.xml").read_bytes())
        self.manuscript = ingest_manuscript(target)
        self.assertTrue(self.manuscript["ingestion"]["complete"])
        self.graph = build_claim_inventory(self.manuscript)
        atomic_json(self.workspace / ".ocsl-review/manuscript.json", self.manuscript, mode=0o600)
        self.state, self.graph = literature.prepare_literature(self.workspace, self.manuscript, self.graph)
        self.prepare()
        with census._database(self.workspace) as connection:
            tasks = census._task_rows(connection, self.args.run)
        self.assertGreater(len(tasks), len(self.manuscript["citationClusters"]))
        self.assertEqual(3, len(self.state["entries"]))
        self.assertEqual(1, len({task["source"]["artifactSha256"] for task in tasks}))
        self.assertEqual(len(tasks), self.ingest_result([self.result(task) for task in tasks])["accepted"])
        exported = census.cmd_export(self.args)
        bundle = json.loads(Path(exported["facts"]).read_text())
        self.assertEqual(len(tasks), bundle["facts"]["citation_loci"]["value"])
        self.assertEqual(0, bundle["facts"]["uncertain"]["value"])
        db = Path(exported["facts"]).parent / "facts.sqlite3"
        self.assertEqual(literature.sha256_file(db), bundle["facts"]["supported"]["source"]["sha256"])

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
