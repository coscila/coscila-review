from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ocsl_review.calibration import cmd_calibrate
from ocsl_review.config import Paths, protected_workspace_roots
from ocsl_review.environments import _assert_runtime_workspace, _chat_root
from ocsl_review.errors import ReviewError
from ocsl_review.workflow import _workspace, cmd_ingest


class WorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source_parent = self.root / "source-checkouts"
        self.home = source_parent / "oc-science-lab-review"
        self.bio = source_parent / "oc-science-lab-bio"
        self.lab = self.root / "laboratory" / "oc-science-lab"
        self.state = self.root / "operator" / "review-state"
        for directory in (self.home, self.bio, self.lab, self.state):
            directory.mkdir(parents=True)
        self.paths = Paths(home=self.home, state=self.state, lab_repo=self.lab)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def protected(self) -> dict[str, Path]:
        return dict(protected_workspace_roots(self.paths))

    def test_protected_root_catalog_includes_source_state_lab_and_bio(self) -> None:
        self.assertEqual(
            self.protected(),
            {
                "companion source": self.home.resolve(),
                "OC Science Lab source": self.lab.resolve(),
                "OC Science Lab Bio source": self.bio.resolve(),
                "operator state": self.state.resolve(),
            },
        )

    def test_review_and_environment_workspaces_reject_both_overlap_directions(self) -> None:
        for label, protected in self.protected().items():
            descendant = protected / "untrusted-workspace"
            descendant.mkdir()
            for relationship, candidate in (
                ("same", protected),
                ("descendant", descendant),
                ("ancestor", protected.parent),
            ):
                with self.subTest(label=label, relationship=relationship):
                    workflow_args = SimpleNamespace(workspace=str(candidate), paths=self.paths)
                    with self.assertRaisesRegex(ReviewError, "overlaps protected"):
                        _workspace(workflow_args)
                    with self.assertRaisesRegex(ReviewError, "overlaps protected"):
                        _assert_runtime_workspace(self.paths, candidate)

    def test_review_command_rejects_before_mutating_any_protected_root(self) -> None:
        for label, protected in self.protected().items():
            sentinel = protected / "sentinel.txt"
            sentinel.write_bytes(b"protected-before\n")
            workspace = protected.parent
            manuscript = workspace / f"{label.replace(' ', '-')}.txt"
            manuscript.write_text("A manuscript sentence.\n", encoding="utf-8")
            args = SimpleNamespace(
                paths=self.paths,
                workspace=str(workspace),
                input=str(manuscript),
                output=str(sentinel),
            )
            with self.subTest(label=label), self.assertRaisesRegex(
                ReviewError,
                "overlaps protected",
            ):
                cmd_ingest(args)
            self.assertEqual(sentinel.read_bytes(), b"protected-before\n")

    def test_chat_conda_root_rejects_all_protected_roots_and_their_ancestors(self) -> None:
        for label, protected in self.protected().items():
            for relationship, candidate in (
                ("same", protected),
                ("ancestor", protected.parent),
            ):
                sentinel = protected / f"chat-{relationship}.txt"
                sentinel.write_bytes(b"unchanged\n")
                with self.subTest(label=label, relationship=relationship), self.assertRaisesRegex(
                    ReviewError,
                    "must not overlap",
                ):
                    _chat_root(candidate, paths=self.paths)
                self.assertEqual(sentinel.read_bytes(), b"unchanged\n")

    def test_calibration_cannot_write_through_an_ancestor_workspace(self) -> None:
        sentinel = self.lab / "calibration-report.json"
        sentinel.write_bytes(b"protected-before\n")
        predictions = self.lab.parent / "predictions.jsonl"
        predictions.write_text("{}\n", encoding="utf-8")
        args = SimpleNamespace(
            paths=self.paths,
            workspace=str(self.lab.parent),
            predictions=str(predictions),
            gold=None,
            output=str(sentinel),
        )
        with self.assertRaisesRegex(ReviewError, "overlaps protected"):
            cmd_calibrate(args)
        self.assertEqual(sentinel.read_bytes(), b"protected-before\n")


if __name__ == "__main__":
    unittest.main()
