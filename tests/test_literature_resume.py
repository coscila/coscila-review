from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ocsl_review import literature
from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.errors import ValidationError
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.util import atomic_json


class ManagedLiteratureResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        source = self.workspace / "paper.md"
        source.write_text(
            "# Introduction\n\nPublished observations agree [1].\n\n"
            "# References\n\n1. Author. A source. 2024. doi:10.1234/source\n"
        )
        self.manuscript = ingest_manuscript(source)
        self.seed = build_claim_inventory(self.manuscript)
        self.projection = self.workspace / ".ocsl-review/audit-graph.json"
        atomic_json(self.workspace / ".ocsl-review/manuscript.json", self.manuscript)
        atomic_json(self.projection, self.seed)
        self.state, self.graph = literature.prepare_literature(
            self.workspace, self.manuscript, self.seed
        )
        literature._save(self.workspace, self.state, self.graph, event="prepared")

    def tearDown(self):
        self.temporary.cleanup()

    def _interrupt_projection(self, state, graph):
        def write(path, value, **kwargs):
            if path == self.projection:
                raise OSError("interrupted projection")
            atomic_json(path, value, **kwargs)

        with patch.object(literature, "atomic_json", side_effect=write):
            with self.assertRaisesRegex(OSError, "interrupted projection"):
                literature._save(self.workspace, state, graph, event="checkpoint")

    def _mark_acquisition_failed(self):
        self.state["entries"][0]["state"] = "failed"
        self.graph["references"][0]["acquisition"] = {
            "state": "failed", "attempted": True, "limitations": ["synthetic outage"]
        }

    def test_external_projection_edit_is_not_silently_overwritten_on_resume(self):
        edited = copy.deepcopy(self.graph)
        edited["references"][0]["raw"] += " Manual correction."
        atomic_json(self.projection, edited)
        ledger = self.workspace / literature.LEDGER_PATH
        before = ledger.read_bytes()
        with self.assertRaisesRegex(ValidationError, "external edits"):
            literature._load(None, self.workspace)
        with self.assertRaisesRegex(ValidationError, "external edits"):
            literature._save(self.workspace, self.state, self.graph, event="resume")
        with self.assertRaisesRegex(ValidationError, "external edits"):
            literature.prepare_literature(self.workspace, self.manuscript, self.graph)
        self.assertEqual(before, ledger.read_bytes())
        self.assertEqual(edited, json.loads(self.projection.read_text()))

    def test_interrupted_projection_recovers_committed_progress(self):
        self._mark_acquisition_failed()
        self._interrupt_projection(self.state, self.graph)
        state, graph = literature._load(None, self.workspace)
        self.assertEqual("failed", state["entries"][0]["state"])
        self.assertEqual("failed", graph["references"][0]["acquisition"]["state"])
        literature._save(self.workspace, state, graph, event="resumed")
        self.assertEqual(graph, json.loads(self.projection.read_text()))

    def test_prepare_uses_canonical_progress_when_projection_is_stale(self):
        stale = json.loads(self.projection.read_text())
        self._mark_acquisition_failed()
        self._interrupt_projection(self.state, self.graph)
        state, graph = literature.prepare_literature(self.workspace, self.manuscript, stale)
        self.assertEqual("failed", state["entries"][0]["state"])
        self.assertEqual("failed", graph["references"][0]["acquisition"]["state"])
        self.assertEqual(self.state["graphSha256"], literature.digest(graph))

    def test_first_commit_can_recover_the_preparation_input_projection(self):
        (self.workspace / literature.LEDGER_PATH).unlink()
        atomic_json(self.projection, self.seed)
        state, graph = literature.prepare_literature(self.workspace, self.manuscript, self.seed)
        self.assertNotEqual(literature.digest(self.seed), literature.digest(graph))
        self._interrupt_projection(state, graph)
        resumed_state, resumed_graph = literature._load(None, self.workspace)
        self.assertEqual(literature.digest(self.seed), resumed_state["projectionRecoverySha256"])
        self.assertEqual(graph, resumed_graph)

    def test_explicit_prepare_accepts_manual_corrections_and_reassesses(self):
        self._mark_acquisition_failed()
        literature._save(self.workspace, self.state, self.graph, event="checkpoint")
        edited = copy.deepcopy(self.graph)
        edited["references"][0]["raw"] += " Corrected journal."
        atomic_json(self.projection, edited)
        state, graph = literature.prepare_literature(self.workspace, self.manuscript, edited)
        literature._save(self.workspace, state, graph, event="prepared")
        self.assertIn("Corrected journal", graph["references"][0]["raw"])
        self.assertEqual("pending", state["entries"][0]["state"])
        self.assertIsNone(state["confirmation"])

    def test_additional_source_purpose_survives_reprepare(self):
        claim_id = self.graph["claims"][0]["id"]
        self.state["confirmation"] = {
            "decision": "accepted", "at": "2026-09-09T00:00:00Z",
            "scopeSha256": self.state["scopeSha256"], "basis": "User requested extensive review."
        }
        literature.add_gap_sources(self.state, self.graph, ["10.1234/additional"], [claim_id])
        literature._save(self.workspace, self.state, self.graph, event="additional-source")
        edited = copy.deepcopy(self.graph)
        edited["references"][0]["raw"] += " Corrected details."
        state, _ = literature.prepare_literature(self.workspace, self.manuscript, edited)
        self.assertEqual([claim_id], state["entries"][-1]["purposeClaimIds"])
        edited["claims"][0]["text"] = "A revised claim needs different corroboration"
        state, _ = literature.prepare_literature(self.workspace, self.manuscript, edited)
        self.assertEqual([claim_id], state["entries"][-1]["purposeClaimIds"])
        self.assertIn("require-reassessment", state["entries"][-1]["limitations"][0])
        edited["claims"] = []
        state, _ = literature.prepare_literature(self.workspace, self.manuscript, edited)
        self.assertEqual([], state["entries"][-1]["purposeClaimIds"])
        self.assertIn(claim_id, state["entries"][-1]["limitations"][0])


if __name__ == "__main__":
    unittest.main()
