from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ocsl_review import integration  # noqa: E402
from ocsl_review.catalogs import validate_path_boundaries  # noqa: E402
from ocsl_review.config import Paths, protected_source_roots, science_lab_repo  # noqa: E402
from ocsl_review.errors import ReviewError, ValidationError  # noqa: E402


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Review Test"], check=True)
    (path / "tracked.txt").write_text("unchanged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)


class SiblingCheckoutTests(unittest.TestCase):
    """Old (oc-science-lab*) and new (coscila*) sibling names: alone or side by side."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "checkouts"
        self.state = self.root / "operator" / "review-state"
        self.state.mkdir(parents=True)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("OCSL_REPO", "OCSL_REVIEW_STAGE_COMMAND", "OCSL_REVIEW_STAGE_CWD"):
            os.environ.pop(name, None)

    def layout(self, home: str, *siblings: str, git: tuple[str, ...] = ()) -> Path:
        companion = self.source / home
        companion.mkdir(parents=True)
        for name in siblings:
            (self.source / name).mkdir()
            if name in git:
                git_init(self.source / name)
        return companion

    def assert_every_sibling_rejected_as_state(self, paths: Paths, siblings: list[Path]) -> None:
        for sibling in siblings:
            with self.subTest(sibling=sibling.name):
                inside = Paths(home=paths.home, state=sibling / "review-state", lab_repo=paths.lab_repo)
                with self.assertRaisesRegex(ValidationError, "overlaps protected source"):
                    validate_path_boundaries(inside)

    def assert_every_sibling_rejected_as_mcp_config(self, paths: Paths, siblings: list[Path]) -> None:
        for sibling in siblings:
            with self.subTest(sibling=sibling.name):
                with self.assertRaisesRegex(ValidationError, "must not be inside tracked source"):
                    integration.install_integration(
                        paths,
                        skill_roots={"native": self.root / "native", "codex": self.root / "codex"},
                        mcp_entries={},
                        mcp_config=sibling / "mcp.json",
                        stage_argv=[sys.executable, "-c", "raise SystemExit(0)"],
                        stage_cwd=self.root,
                    )
                self.assertFalse((sibling / "mcp.json").exists())

    @unittest.skipUnless(shutil.which("git"), "git is required for sibling snapshots")
    def test_old_and_new_names_side_by_side_are_all_protected(self) -> None:
        names = ("oc-science-lab", "coscila", "oc-science-lab-bio", "coscila-bio")
        home = self.layout("oc-science-lab-review", *names, git=names)
        siblings = [self.source / name for name in names]

        lab = science_lab_repo(None, home)
        self.assertEqual(self.source / "oc-science-lab", lab)
        paths = Paths(home=home, state=self.state, lab_repo=lab)

        roots = protected_source_roots(paths)
        self.assertEqual({home.resolve(), *(s.resolve() for s in siblings)}, {path for _, path in roots})
        self.assertEqual(len(roots), len({label for label, _ in roots}), "labels must stay distinct")
        self.assert_every_sibling_rejected_as_state(paths, siblings)
        self.assert_every_sibling_rejected_as_mcp_config(paths, siblings)
        self.assertEqual({str(s.resolve()) for s in siblings}, set(integration.snapshot_siblings(paths)))

    @unittest.skipUnless(shutil.which("git"), "git is required for sibling snapshots")
    def test_new_names_only_are_protected_but_never_chosen_for_staging(self) -> None:
        names = ("coscila", "coscila-bio")
        home = self.layout("coscila-review", *names, git=names)
        siblings = [self.source / name for name in names]

        # The automatic choice keeps the old name only, so deployment staging behaves as before in development clones.
        self.assertIsNone(science_lab_repo(None, home))
        paths = Paths(home=home, state=self.state, lab_repo=science_lab_repo(None, home))
        with self.assertRaisesRegex(ReviewError, "staging command is required"):
            integration.resolve_staging(paths)

        self.assertEqual({home.resolve(), *(s.resolve() for s in siblings)},
                         {path for _, path in protected_source_roots(paths)})
        self.assert_every_sibling_rejected_as_state(paths, siblings)
        self.assert_every_sibling_rejected_as_mcp_config(paths, siblings)
        self.assertEqual({str(s.resolve()) for s in siblings}, set(integration.snapshot_siblings(paths)))

    def test_explicit_lab_checkout_wins(self) -> None:
        home = self.layout("coscila-review", "coscila", "coscila-bio")
        chosen = self.source / "coscila"
        self.assertEqual(chosen.resolve(), science_lab_repo(str(chosen), home))
        os.environ["OCSL_REPO"] = str(chosen)
        self.assertEqual(chosen.resolve(), science_lab_repo(None, home))

    @unittest.skipUnless(shutil.which("git"), "git is required for sibling snapshots")
    def test_a_further_sibling_that_is_not_a_git_worktree_is_skipped(self) -> None:
        home = self.layout(
            "oc-science-lab-review", "oc-science-lab", "coscila", "oc-science-lab-bio",
            git=("oc-science-lab", "oc-science-lab-bio"),
        )
        paths = Paths(home=home, state=self.state, lab_repo=science_lab_repo(None, home))
        snapshots = integration.snapshot_siblings(paths)
        self.assertEqual(
            {str((self.source / name).resolve()) for name in ("oc-science-lab", "oc-science-lab-bio")},
            set(snapshots),
        )
        # It is still a protected source root.
        self.assertIn((self.source / "coscila").resolve(), {path for _, path in protected_source_roots(paths)})


if __name__ == "__main__":
    unittest.main()
