from __future__ import annotations

import hashlib
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
    read_egress_events,
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

    def test_cwd_derives_exact_workspace_and_keeps_two_ledgers_separate(self) -> None:
        workspace_b = self.workspace_root / "chat-b"
        workspace_b.mkdir()
        environment = {"LAB_WORKSPACE_ROOT": str(self.workspace_root)}

        plan_a, selected_a = load_scholarly_context(
            self.paths,
            environment,
            cwd=self.workspace,
        )
        plan_b, selected_b = load_scholarly_context(
            self.paths,
            environment,
            cwd=workspace_b,
        )

        self.assertEqual(plan_a, EgressPlan())
        self.assertEqual(plan_b, EgressPlan())
        self.assertEqual(selected_a, self.workspace.resolve())
        self.assertEqual(selected_b, workspace_b.resolve())
        append_egress_event(selected_a, {"operation": "workspace-a"})
        self.assertFalse((workspace_b / ".ocsl-review").exists())
        append_egress_event(selected_b, {"operation": "workspace-b"})
        self.assertEqual(read_egress_events(self.workspace), [{"operation": "workspace-a"}])
        self.assertEqual(read_egress_events(workspace_b), [{"operation": "workspace-b"}])

    def test_cwd_uses_only_its_fixed_optional_plan_without_parent_search(self) -> None:
        parent_plan = self.workspace_root / ".ocsl-review" / "egress-plan.json"
        parent_plan.parent.mkdir()
        parent_plan.write_text(
            json.dumps({"approvedOperations": ["search_crossref"]}) + "\n",
            encoding="utf-8",
        )
        plan, selected = load_scholarly_context(
            self.paths,
            {"LAB_WORKSPACE_ROOT": str(self.workspace_root)},
            cwd=self.workspace,
        )
        self.assertEqual(plan, EgressPlan())
        self.assertEqual(selected, self.workspace.resolve())

        fixed_plan = self.workspace / ".ocsl-review" / "egress-plan.json"
        fixed_plan.parent.mkdir()
        fixed_plan.write_text(
            json.dumps(
                {
                    "allowSanitizedQueries": False,
                    "maxQueryTerms": 8,
                    "approvedOperations": ["resolve_identifier"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        selected_plan, selected_again = load_scholarly_context(
            self.paths,
            {"LAB_WORKSPACE_ROOT": str(self.workspace_root)},
            cwd=self.workspace,
        )
        self.assertEqual(
            selected_plan.approved_operations,
            frozenset({"resolve_identifier"}),
        )
        self.assertFalse(selected_plan.allow_sanitized_queries)
        self.assertEqual(selected_plan.max_query_terms, 8)
        self.assertEqual(selected_again, self.workspace.resolve())

    def test_writable_auto_plan_cannot_broaden_confidential_defaults(self) -> None:
        plan_path = self.workspace / ".ocsl-review" / "egress-plan.json"
        plan_path.parent.mkdir()
        unsafe_plans = (
            {"confidential": False},
            {"confidential": False, "sharedCache": True},
            {"approvedFields": ["keywords"]},
            {"maxQueryTerms": 13},
        )
        for value in unsafe_plans:
            with self.subTest(value=value):
                plan_path.write_text(json.dumps(value) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "may only narrow"):
                    load_scholarly_context(
                        self.paths,
                        {"LAB_WORKSPACE_ROOT": str(self.workspace_root)},
                        cwd=self.workspace,
                    )

    def test_broader_explicit_plan_requires_current_operator_digest(self) -> None:
        plan_path = self.workspace / ".ocsl-review" / "egress-plan.json"
        plan_path.parent.mkdir()
        original = b'{"confidential":false}\n'
        replacement = b'{ "confidential": false }\n'
        plan_path.write_bytes(original)
        original_digest = hashlib.sha256(original).hexdigest()
        base = {
            "LAB_WORKSPACE_ROOT": str(self.workspace_root),
            "OCSL_REVIEW_WORKSPACE": str(self.workspace),
            "OCSL_REVIEW_EGRESS_PLAN": str(plan_path),
        }
        with self.assertRaisesRegex(ValidationError, "requires OCSL_REVIEW_EGRESS_PLAN_SHA256"):
            load_scholarly_context(self.paths, base)
        with self.assertRaisesRegex(ValidationError, "does not match"):
            load_scholarly_context(
                self.paths,
                {**base, "OCSL_REVIEW_EGRESS_PLAN_SHA256": "0" * 64},
            )

        plan_path.write_bytes(replacement)
        with self.assertRaisesRegex(ValidationError, "does not match"):
            load_scholarly_context(
                self.paths,
                {**base, "OCSL_REVIEW_EGRESS_PLAN_SHA256": original_digest},
            )

    def test_digest_mismatch_stops_dispatch_before_client_or_network(self) -> None:
        plan_path = self.workspace / ".ocsl-review" / "egress-plan.json"
        plan_path.parent.mkdir()
        plan_path.write_text('{"confidential":false}\n', encoding="utf-8")
        environment = {
            "OCSL_REVIEW_HOME": str(Path(__file__).resolve().parents[1]),
            "OCSL_REVIEW_STATE": str(self.state),
            "LAB_WORKSPACE_ROOT": str(self.workspace_root),
            "OCSL_REVIEW_WORKSPACE": str(self.workspace),
            "OCSL_REVIEW_EGRESS_PLAN": str(plan_path),
            "OCSL_REVIEW_EGRESS_PLAN_SHA256": "0" * 64,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("ocsl_review.scholar_server.ScholarlyClient") as client,
            patch("ocsl_review.scholarly.urllib.request.urlopen") as opener,
            self.assertRaisesRegex(ValidationError, "does not match"),
        ):
            make_dispatch()
        client.assert_not_called()
        opener.assert_not_called()

    def test_valid_hash_bound_broad_plan_loads_exact_authorized_bytes(self) -> None:
        plan_path = self.workspace / ".ocsl-review" / "egress-plan.json"
        plan_path.parent.mkdir()
        body = b'{"confidential":false,"approvedFields":["keywords"]}\n'
        plan_path.write_bytes(body)
        plan, workspace = load_scholarly_context(
            self.paths,
            {
                "LAB_WORKSPACE_ROOT": str(self.workspace_root),
                "OCSL_REVIEW_WORKSPACE": str(self.workspace),
                "OCSL_REVIEW_EGRESS_PLAN": str(plan_path),
                "OCSL_REVIEW_EGRESS_PLAN_SHA256": hashlib.sha256(body).hexdigest(),
            },
        )
        self.assertFalse(plan.confidential)
        self.assertEqual(plan.approved_fields, frozenset({"keywords"}))
        self.assertEqual(workspace, self.workspace.resolve())

    def test_workspace_root_cwd_is_rejected_without_creating_a_shared_ledger(self) -> None:
        with self.assertRaisesRegex(ValidationError, "itself is not a scholarly chat"):
            load_scholarly_context(
                self.paths,
                {"LAB_WORKSPACE_ROOT": str(self.workspace_root)},
                cwd=self.workspace_root,
            )
        self.assertFalse((self.workspace_root / ".ocsl-review").exists())

    def test_cwd_outside_workspace_root_keeps_source_doctor_no_context_mode(self) -> None:
        outside = self.root / "source-doctor"
        outside.mkdir()
        plan, workspace = load_scholarly_context(
            self.paths,
            {"LAB_WORKSPACE_ROOT": str(self.workspace_root)},
            cwd=outside,
        )
        self.assertEqual(plan, EgressPlan())
        self.assertIsNone(workspace)
        self.assertFalse((outside / ".ocsl-review").exists())

    def test_cwd_symlink_is_rejected_without_plan_or_ledger_cross_talk(self) -> None:
        linked = self.workspace_root / "linked-chat"
        linked.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "symlink component"):
            load_scholarly_context(
                self.paths,
                {"LAB_WORKSPACE_ROOT": str(self.workspace_root)},
                cwd=linked,
            )
        self.assertFalse((self.workspace / ".ocsl-review").exists())

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
        sentinel = external / "egress-events.jsonl"
        sentinel.write_bytes(b"external-before\n")
        (self.workspace / ".ocsl-review").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "symlink component"):
            append_egress_event(self.workspace, {"operation": "test"})
        self.assertEqual(sentinel.read_bytes(), b"external-before\n")

    def test_ledger_is_private_and_malformed_existing_content_is_not_overwritten(self) -> None:
        append_egress_event(self.workspace, {"operation": "first"})
        audit_root = self.workspace / ".ocsl-review"
        ledger = audit_root / "egress-events.jsonl"
        self.assertEqual(audit_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_egress_events(self.workspace), [{"operation": "first"}])
        ledger.write_bytes(b'{"not":"a chained event"}\n')
        before = ledger.read_bytes()
        with self.assertRaisesRegex(ValidationError, "hash chain"):
            append_egress_event(self.workspace, {"operation": "second"})
        self.assertEqual(ledger.read_bytes(), before)
        legacy_workspace = self.workspace_root / "chat-legacy"
        (legacy_workspace / ".ocsl-review").mkdir(parents=True)
        legacy = legacy_workspace / ".ocsl-review" / "egress-events.json"
        legacy.write_bytes(b'{"not":"an event array"}\n')
        with self.assertRaisesRegex(ValidationError, "event array"):
            append_egress_event(legacy_workspace, {"operation": "second"})
        self.assertEqual(legacy.read_bytes(), b'{"not":"an event array"}\n')
        self.assertFalse((legacy_workspace / ".ocsl-review" / "egress-events.jsonl").read_bytes())

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
