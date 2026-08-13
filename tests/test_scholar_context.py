from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ocsl_review.config import Paths
from ocsl_review.confidentiality import (
    EgressPlan,
    append_egress_event,
    load_scholarly_context,
)
from ocsl_review.errors import ValidationError
from ocsl_review.scholar_server import make_dispatch


class ScholarContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "source" / "oc-science-lab-review"
        self.state = self.root / "state"
        self.lab = self.root / "source" / "oc-science-lab"
        self.workspace_root = self.root / "workspaces"
        self.workspace = self.workspace_root / "chat-a"
        for directory in (
            self.home,
            self.state,
            self.lab,
            self.workspace,
        ):
            directory.mkdir(parents=True)
        self.paths = Paths(home=self.home, state=self.state, lab_repo=self.lab)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(self, **extra: str):
        environment = {
            "LAB_WORKSPACE_ROOT": str(self.workspace_root),
            "OCSL_REVIEW_WORKSPACE": str(self.workspace),
            **extra,
        }
        return load_scholarly_context(self.paths, environment)

    def test_absent_context_preserves_confidential_default_without_a_ledger_target(self) -> None:
        plan, workspace = load_scholarly_context(self.paths, {})
        self.assertEqual(plan, EgressPlan())
        self.assertIsNone(workspace)

    def test_partial_or_external_context_is_rejected(self) -> None:
        plan = self.workspace / "egress-plan.json"
        plan.write_text("{}\n", encoding="utf-8")
        external = self.root / "outside"
        external.mkdir()
        cases = (
            {"OCSL_REVIEW_EGRESS_PLAN": str(plan)},
            {"OCSL_REVIEW_WORKSPACE": str(self.workspace)},
            {
                "LAB_WORKSPACE_ROOT": str(self.workspace_root),
                "OCSL_REVIEW_WORKSPACE": str(external),
            },
        )
        for environment in cases:
            with self.subTest(environment=environment), self.assertRaises(ValidationError):
                load_scholarly_context(self.paths, environment)

    def test_plan_must_be_a_real_file_inside_the_exact_workspace(self) -> None:
        external_plan = self.root / "external-plan.json"
        external_plan.write_text("{}\n", encoding="utf-8")
        linked_plan = self.workspace / "linked-plan.json"
        linked_plan.symlink_to(external_plan)
        linked_parent = self.workspace / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        for plan in (external_plan, linked_plan, linked_parent / "external-plan.json"):
            with self.subTest(plan=plan), self.assertRaises(ValidationError):
                self.context(OCSL_REVIEW_EGRESS_PLAN=str(plan))

    def test_valid_context_loads_the_bound_plan(self) -> None:
        plan_path = self.workspace / ".ocsl-review" / "egress-plan.json"
        plan_path.parent.mkdir()
        plan_path.write_text(
            json.dumps({"approvedOperations": ["resolve_identifier"]}) + "\n",
            encoding="utf-8",
        )
        plan, workspace = self.context(OCSL_REVIEW_EGRESS_PLAN=str(plan_path))
        self.assertEqual(plan.approved_operations, frozenset({"resolve_identifier"}))
        self.assertEqual(workspace, self.workspace.resolve())

    def test_scholarly_workspace_cannot_overlap_any_protected_domain(self) -> None:
        for protected in (self.home, self.state, self.lab):
            environment = {
                "LAB_WORKSPACE_ROOT": str(protected.parent),
                "OCSL_REVIEW_WORKSPACE": str(protected),
            }
            with self.subTest(protected=protected), self.assertRaisesRegex(
                ValidationError,
                "overlaps protected",
            ):
                load_scholarly_context(self.paths, environment)

    def test_ledger_rejects_symlink_escape_without_touching_external_data(self) -> None:
        external = self.root / "external-ledger"
        external.mkdir()
        sentinel = external / "egress-events.json"
        sentinel.write_bytes(b"external-before\n")
        (self.workspace / ".ocsl-review").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "symlink component"):
            append_egress_event(self.workspace, {"operation": "test"})
        self.assertEqual(sentinel.read_bytes(), b"external-before\n")

    def test_ledger_is_private_and_malformed_existing_content_is_not_overwritten(self) -> None:
        append_egress_event(self.workspace, {"operation": "first"})
        audit_root = self.workspace / ".ocsl-review"
        ledger = audit_root / "egress-events.json"
        self.assertEqual(audit_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")), [{"operation": "first"}])
        ledger.write_bytes(b'{"not":"an event array"}\n')
        before = ledger.read_bytes()
        with self.assertRaisesRegex(ValidationError, "event array"):
            append_egress_event(self.workspace, {"operation": "second"})
        self.assertEqual(ledger.read_bytes(), before)

    def test_invalid_context_stops_dispatch_construction_before_network(self) -> None:
        environment = {
            "OCSL_REVIEW_HOME": str(Path(__file__).resolve().parents[1]),
            "OCSL_REVIEW_STATE": str(self.state),
            "LAB_WORKSPACE_ROOT": str(self.workspace_root),
            "OCSL_REVIEW_WORKSPACE": str(self.root / "outside"),
        }
        (self.root / "outside").mkdir()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("ocsl_review.scholarly.urllib.request.urlopen") as opener,
            self.assertRaises(ValidationError),
        ):
            make_dispatch()
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
