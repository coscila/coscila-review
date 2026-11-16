from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ocsl_review import config as review_config
from ocsl_review import doctor as review_doctor
from ocsl_review import integration, lifecycle, skills
from ocsl_review.config import Paths
from ocsl_review.errors import ReviewError, ValidationError

MCP_ENTRIES = {
    "ocsl_review_catalog": {
        "command": "bash",
        "args": ["/opt/ocsl-review/catalog.sh"],
        "env": {"MODE": "catalog"},
    },
    "ocsl_scholar": {
        "command": "bash",
        "args": ["/opt/ocsl-review/scholar.sh"],
        "env": {"MODE": "scholar"},
    },
}
FOLLOWUP_AUDIT = "ocsl-review-followup-audit"


@contextmanager
def skill_set_without(skill_id: str) -> Iterator[None]:
    """The companion as a release whose skill set lacks skill_id: its required skills and the catalog check it passed."""
    smaller = tuple(identifier for identifier in skills.REQUIRED_SKILLS if identifier != skill_id)
    with (
        patch.object(skills, "REQUIRED_SKILLS", smaller),
        patch.object(skills, "validate_catalog", return_value={"ok": True, "skills": len(smaller)}),
    ):
        yield


class ReviewPathsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state = base / "state"
        self.state.mkdir()
        # Sibling checkouts come from this temporary tree, never from the directories next to this repository, so a test
        # can neither write into nor depend on a real lab or bio checkout.
        self.siblings = base / "siblings"
        self.siblings.mkdir()
        patcher = patch.object(
            review_config,
            "sibling_checkouts",
            lambda home, names: tuple(
                self.siblings / name for name in names if (self.siblings / name).is_dir()
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = Paths(home=ROOT, state=self.state, lab_repo=None)
        self.native = base / "native-skills"
        self.codex = base / "codex-skills"
        self.roots = {"native": self.native, "codex": self.codex}

    def targets(self, skill_id: str) -> set[str]:
        """The installed places of one skill, in both skill roots."""
        return {str(root.resolve() / skill_id) for root in (self.native, self.codex)}

    def skill_roots_snapshot(self) -> dict[Path, dict[str, dict[str, Any]]]:
        """Every node in both skill roots with its digest, mode or link target, to show that nothing changed."""
        return {root: skills._node_manifest(root) for root in (self.native, self.codex)}


class SkillCatalogTests(ReviewPathsTestCase):
    def test_catalog_and_all_runtime_contracts_validate(self) -> None:
        result = skills.validate_catalog(self.paths)
        self.assertEqual(23, result["skills"])
        self.assertEqual(
            {
                "review-core",
                "review-documents",
                "review-literature",
                "review-nlp",
                "review-statistics",
                "mcp-review",
            },
            set(result["environments"]),
        )
        schema = json.loads((ROOT / "schemas" / "skill-catalog.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["skill"]["items"]["additionalProperties"])

    def test_explicit_skill_roots_are_independent(self) -> None:
        args = SimpleNamespace(
            native_skills_root=str(self.native), codex_skills_root=str(self.codex)
        )
        resolved = skills.resolve_skill_roots(args)
        self.assertEqual(self.native.resolve(), resolved["native"])
        self.assertEqual(self.codex.resolve(), resolved["codex"])

    def test_install_is_idempotent_and_restores_distinct_preexisting_skills(self) -> None:
        for root, marker in ((self.native, "native-original"), (self.codex, "codex-original")):
            target = root / "ocsl-review-router"
            target.mkdir(parents=True)
            (target / "local.txt").write_text(marker, encoding="utf-8")

        first = skills.install_skill_set(self.paths, self.roots)
        self.assertFalse(first["reused"])
        second = skills.install_skill_set(self.paths, self.roots)
        self.assertTrue(second["reused"])

        manifest = json.loads(skills.manifest_path(self.paths).read_text(encoding="utf-8"))
        router_backups = [
            entry["backup"]["path"]
            for root_record in manifest["roots"]
            for entry in root_record["skills"]
            if entry["id"] == "ocsl-review-router"
        ]
        self.assertEqual(2, len(set(router_backups)))

        removed = skills.uninstall_skill_set(self.paths)
        self.assertEqual(2 * len(skills.REQUIRED_SKILLS), removed["removed"])
        self.assertEqual(
            "native-original",
            (self.native / "ocsl-review-router" / "local.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "codex-original",
            (self.codex / "ocsl-review-router" / "local.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue(skills.uninstall_skill_set(self.paths)["reused"])

    def test_uninstall_refuses_post_install_skill_edit(self) -> None:
        skills.install_skill_set(self.paths, self.roots)
        changed = self.native / "ocsl-review-router" / "SKILL.md"
        changed.write_text(changed.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "edited after installation"):
            skills.uninstall_skill_set(self.paths)
        self.assertTrue(changed.read_text(encoding="utf-8").endswith("local edit\n"))
        self.assertTrue(skills.manifest_path(self.paths).is_file())

    def copied_home(self) -> Path:
        """A companion source tree of its own (skills and the catalogs they name), so a test can change the source."""
        home = Path(self.temporary.name) / "home"
        for name in ("skills", "environments", "mcp"):
            shutil.copytree(ROOT / name, home / name)
        return home

    def test_stale_installation_fails_verification_and_a_repeated_install_refreshes_it(self) -> None:
        home = self.copied_home()
        paths = Paths(home=home, state=self.state, lab_repo=None)
        source = home / "skills" / "ocsl-review-router"
        withdrawn = source / "references" / "withdrawn.md"
        withdrawn.parent.mkdir()
        withdrawn.write_text("text a later scrub removes from the source\n", encoding="utf-8")
        target = self.native / "ocsl-review-router"
        target.mkdir(parents=True)
        (target / "local.txt").write_text("native-original", encoding="utf-8")
        skills.install_skill_set(paths, self.roots)
        self.assertTrue(skills.verify_skill_installation(paths)["ok"])
        self.assertTrue((self.codex / "ocsl-review-router" / "references" / "withdrawn.md").is_file())

        # The scrub: the source changes and drops a reference file; the installed copies still hold the old text.
        (source / "SKILL.md").write_text(
            (source / "SKILL.md").read_text(encoding="utf-8") + "\nchanged in the source\n", encoding="utf-8"
        )
        withdrawn.unlink()
        withdrawn.parent.rmdir()
        report = skills.verify_skill_installation(paths)
        self.assertFalse(report["ok"])
        self.assertEqual(
            {str(self.native.resolve() / "ocsl-review-router"), str(self.codex.resolve() / "ocsl-review-router")},
            {entry["target"] for entry in report["stale"]},
        )
        self.assertIn("integrate refresh", report["reason"])
        # Integrity alone (what uninstall needs) still holds for a stale copy.
        self.assertTrue(skills.verify_skill_installation(paths, check_source=False)["ok"])

        again = skills.install_skill_set(paths, self.roots)
        self.assertTrue(again["reused"])
        self.assertEqual(2, len(again["refreshed"]))
        for root in (self.native, self.codex):
            installed = root / "ocsl-review-router"
            self.assertTrue((installed / "SKILL.md").read_text(encoding="utf-8").endswith("changed in the source\n"))
            self.assertFalse((installed / "references").exists())
            owner = json.loads((installed / skills.OWNER_FILE).read_text(encoding="utf-8"))
            self.assertEqual(skills.tree_digest(source), owner["sourceDigest"])
        self.assertTrue(skills.verify_skill_installation(paths)["ok"])
        self.assertEqual([], skills.refresh_skill_set(paths)["refreshed"])

        # The pre-install node is still what uninstall restores.
        skills.uninstall_skill_set(paths)
        self.assertEqual("native-original", (target / "local.txt").read_text(encoding="utf-8"))

    def test_refresh_refuses_an_installed_copy_edited_after_installation(self) -> None:
        home = self.copied_home()
        paths = Paths(home=home, state=self.state, lab_repo=None)
        skills.install_skill_set(paths, self.roots)
        source = home / "skills" / "ocsl-review-router" / "SKILL.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nnew source\n", encoding="utf-8")
        edited = self.codex / "ocsl-review-router" / "SKILL.md"
        edited.write_text(edited.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "edited after installation"):
            skills.refresh_skill_set(paths)
        self.assertTrue(edited.read_text(encoding="utf-8").endswith("local edit\n"))

    def test_refresh_installs_a_skill_the_installation_lacks_into_every_root(self) -> None:
        with skill_set_without(FOLLOWUP_AUDIT):
            skills.install_skill_set(self.paths, self.roots)
        targets = self.targets(FOLLOWUP_AUDIT)

        report = skills.verify_skill_installation(self.paths)
        self.assertFalse(report["ok"])
        self.assertEqual(targets, {item["target"] for item in report["missing"]})
        self.assertIn(f"the installation lacks required skills: {FOLLOWUP_AUDIT};", report["reason"])
        self.assertIn("`ocsl-review integrate refresh`", report["reason"])
        # Integrity alone (what uninstall and the refresh itself start from) holds.
        self.assertTrue(skills.verify_skill_installation(self.paths, check_source=False)["ok"])

        result = skills.refresh_skill_set(self.paths)
        self.assertEqual((targets, [], []), (set(result["added"]), result["refreshed"], result["removed"]))
        source = ROOT / "skills" / FOLLOWUP_AUDIT
        for target in map(Path, targets):
            self.assertEqual((source / "SKILL.md").read_bytes(), (target / "SKILL.md").read_bytes())
            owner = json.loads((target / skills.OWNER_FILE).read_text(encoding="utf-8"))
            self.assertEqual(skills.tree_digest(source), owner["sourceDigest"])
        self.assertTrue(skills.verify_skill_installation(self.paths)["ok"])

        # The added copies belong to the installation: uninstall removes them with the others.
        self.assertEqual(2 * len(skills.REQUIRED_SKILLS), skills.uninstall_skill_set(self.paths)["removed"])
        self.assertFalse(any(Path(target).exists() for target in targets))

    def test_a_repeated_install_adds_a_skill_the_installation_lacks(self) -> None:
        with skill_set_without(FOLLOWUP_AUDIT):
            skills.install_skill_set(self.paths, self.roots)
        again = skills.install_skill_set(self.paths, self.roots)
        self.assertTrue(again["reused"])
        self.assertEqual(self.targets(FOLLOWUP_AUDIT), set(again["added"]))
        self.assertTrue(skills.verify_skill_installation(self.paths)["ok"])

    def test_refresh_with_nothing_missing_or_stale_changes_nothing(self) -> None:
        skills.install_skill_set(self.paths, self.roots)
        manifest = skills.manifest_path(self.paths)
        before = (manifest.read_bytes(), self.skill_roots_snapshot())
        result = skills.refresh_skill_set(self.paths)
        self.assertEqual(([], [], []), (result["refreshed"], result["added"], result["removed"]))
        self.assertEqual(before, (manifest.read_bytes(), self.skill_roots_snapshot()))

    def test_refresh_refuses_a_node_it_does_not_own_at_a_missing_skill_and_changes_nothing(self) -> None:
        home = self.copied_home()
        paths = Paths(home=home, state=self.state, lab_repo=None)
        with skill_set_without(FOLLOWUP_AUDIT):
            skills.install_skill_set(paths, self.roots)
        # A stale copy as well, so that the refusal is seen to leave every kind of change undone.
        source = home / "skills" / "ocsl-review-router" / "SKILL.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nchanged in the source\n", encoding="utf-8")
        foreign = self.native / FOLLOWUP_AUDIT  # in the root refreshed last
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("a skill a person keeps here\n", encoding="utf-8")
        manifest = skills.manifest_path(paths)
        before = (manifest.read_bytes(), self.skill_roots_snapshot())

        with self.assertRaisesRegex(
            ValidationError, f"refusing to install {FOLLOWUP_AUDIT}: .* does not belong to this installation"
        ):
            skills.refresh_skill_set(paths)
        self.assertEqual(before, (manifest.read_bytes(), self.skill_roots_snapshot()))

        # Once that folder is moved out of the skill root, the refresh completes.
        shutil.move(str(foreign), str(Path(self.temporary.name) / "kept-skill"))
        result = skills.refresh_skill_set(paths)
        self.assertEqual(self.targets(FOLLOWUP_AUDIT), set(result["added"]))
        self.assertEqual(self.targets("ocsl-review-router"), set(result["refreshed"]))
        self.assertTrue(skills.verify_skill_installation(paths)["ok"])

    def test_a_refresh_that_fails_midway_restores_the_installation(self) -> None:
        home = self.copied_home()
        paths = Paths(home=home, state=self.state, lab_repo=None)
        with skill_set_without(FOLLOWUP_AUDIT):
            skills.install_skill_set(paths, self.roots)
        source = home / "skills" / "ocsl-review-router" / "SKILL.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nchanged in the source\n", encoding="utf-8")
        manifest = skills.manifest_path(paths)
        before = (manifest.read_bytes(), self.skill_roots_snapshot())

        copytree = shutil.copytree
        staged: list[Path] = []

        def copy_failing_for_the_last_added_skill(source: Path, destination: Path, **kwargs: Any) -> Any:
            # The codex root comes first: when the native copy of the missing skill is staged, the codex root holds a
            # re-copied and an added skill and the native router has been re-copied. The failing copy leaves a part.
            destination = Path(destination)
            staged.append(destination)
            if destination.parent == self.native.resolve() and destination.name.endswith(FOLLOWUP_AUDIT):
                destination.mkdir()
                (destination / "SKILL.md").write_text("partial", encoding="utf-8")
                raise OSError("no space left on device")
            return copytree(source, destination, **kwargs)

        with (
            patch.object(skills.shutil, "copytree", side_effect=copy_failing_for_the_last_added_skill),
            self.assertRaisesRegex(OSError, "no space left on device"),
        ):
            skills.refresh_skill_set(paths)
        self.assertEqual(4, len(staged))
        self.assertEqual(before, (manifest.read_bytes(), self.skill_roots_snapshot()))

    def test_refresh_removes_an_installed_skill_outside_the_skill_set_and_restores_its_place(self) -> None:
        home = self.copied_home()
        paths = Paths(home=home, state=self.state, lab_repo=None)
        kept = self.native / "ocsl-review-router"
        kept.mkdir(parents=True)
        (kept / "local.txt").write_text("native-original", encoding="utf-8")
        skills.install_skill_set(paths, self.roots)
        targets = self.targets("ocsl-review-router")

        # A companion release whose skill set lacks the router skill, and its source with it.
        shutil.rmtree(home / "skills" / "ocsl-review-router")
        without = tuple(identifier for identifier in skills.REQUIRED_SKILLS if identifier != "ocsl-review-router")
        with patch.object(skills, "REQUIRED_SKILLS", without):
            report = skills.verify_skill_installation(paths)
            self.assertFalse(report["ok"])
            self.assertEqual(targets, {item["target"] for item in report["extra"]})
            self.assertNotIn("stale", report)
            self.assertIn("`ocsl-review integrate refresh`", report["reason"])

            result = skills.refresh_skill_set(paths)
            self.assertEqual((targets, [], []), (set(result["removed"]), result["refreshed"], result["added"]))
            self.assertEqual(["local.txt"], [path.name for path in kept.iterdir()])
            self.assertEqual("native-original", (kept / "local.txt").read_text(encoding="utf-8"))
            self.assertFalse((self.codex / "ocsl-review-router").exists())
            self.assertTrue(skills.verify_skill_installation(paths)["ok"])
            skills.uninstall_skill_set(paths)
        self.assertEqual("native-original", (kept / "local.txt").read_text(encoding="utf-8"))

    def test_skill_roots_cannot_overlap_protected_source_or_state(self) -> None:
        # Bio and lab siblings keep their old names on the lab host and use the coscila-* names in development clones;
        # both may exist at once and every one is protected.
        siblings = [self.siblings / name for name in ("oc-science-lab-bio", "coscila-bio", "coscila")]
        for sibling in siblings:
            sibling.mkdir()
        companion_root = ROOT / "untracked-skill-root"
        self.assertFalse(companion_root.exists())
        self.addCleanup(shutil.rmtree, companion_root, True)  # never leave anything behind, even on a regression
        unsafe_roots = (
            companion_root,
            *(sibling / "untracked-skill-root" for sibling in siblings),
            self.state / "skills",
            self.state.parent,
        )
        for unsafe in unsafe_roots:
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(
                    ValidationError, "overlaps protected"
                ):
                    skills.install_skill_set(
                        self.paths,
                        {"native": unsafe, "codex": self.codex},
                    )
                self.assertFalse((unsafe / "ocsl-review-router").exists())


@unittest.skipUnless(shutil.which("git"), "git is required for sibling-tree verification")
class IntegrationTests(ReviewPathsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.lab = Path(self.temporary.name) / "oc-science-lab"
        self.lab.mkdir()
        subprocess.run(["git", "init", "-q", str(self.lab)], check=True)
        subprocess.run(
            ["git", "-C", str(self.lab), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.lab), "config", "user.name", "Review Test"], check=True
        )
        (self.lab / "tracked.txt").write_text("unchanged\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.lab), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.lab), "commit", "-qm", "fixture"], check=True)
        self.paths = Paths(home=ROOT, state=self.state, lab_repo=self.lab)
        self.mcp_config = Path(self.temporary.name) / "deployment.json"
        self.staged_config = Path(self.temporary.name) / "staged.json"
        self.original_config = {
            "theme": "unchanged",
            "mcpServers": {
                "paper-search-mcp": {"command": "paper"},
                "ocsl_review_catalog": {"command": "prior-catalog"},
                "ocsl_scholar": {"command": "prior-scholar"},
            },
        }
        self.original_bytes = json.dumps(self.original_config, indent=4).encode("utf-8")
        self.mcp_config.write_bytes(self.original_bytes)
        self.staged_config.write_text(
            json.dumps({"mcpServers": MCP_ENTRIES}), encoding="utf-8"
        )
        self.stage_ok = [sys.executable, "-c", "raise SystemExit(0)"]

    def install(self, *, staged_config: Path | None = None) -> dict:
        return integration.install_integration(
            self.paths,
            skill_roots=self.roots,
            mcp_entries=MCP_ENTRIES,
            mcp_config=self.mcp_config,
            stage_argv=self.stage_ok,
            stage_cwd=self.lab,
            stage_timeout=30,
            staged_mcp_config=staged_config,
        )

    def verify_args(
        self,
        *,
        staged_config: Path | None = None,
        live: bool = False,
        strict: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            paths=self.paths,
            staged_config=str(staged_config) if staged_config else None,
            live=live,
            strict=strict,
        )

    def test_transaction_preserves_and_restores_mcp_values(self) -> None:
        first = self.install()
        self.assertFalse(first["reused"])
        active = json.loads(self.mcp_config.read_text(encoding="utf-8"))
        self.assertEqual({"command": "paper"}, active["mcpServers"]["paper-search-mcp"])
        self.assertEqual(MCP_ENTRIES["ocsl_scholar"], active["mcpServers"]["ocsl_scholar"])
        self.assertTrue(self.install()["reused"])

        result = integration.uninstall_integration(self.paths, stage_timeout=30)
        self.assertFalse(result["reused"])
        self.assertEqual(self.original_bytes, self.mcp_config.read_bytes())
        self.assertFalse(integration.install_state_path(self.paths).exists())
        self.assertTrue(integration.uninstall_integration(self.paths)["reused"])
        self.assertEqual("unchanged\n", (self.lab / "tracked.txt").read_text(encoding="utf-8"))

    def test_mcp_config_mode_survives_install_and_uninstall(self) -> None:
        self.mcp_config.chmod(0o640)
        self.install()
        self.assertEqual(0o640, stat.S_IMODE(self.mcp_config.stat().st_mode))
        state = json.loads(integration.install_state_path(self.paths).read_text(encoding="utf-8"))
        self.assertEqual(0o640, state["mcp"]["configMode"])

        integration.uninstall_integration(self.paths, stage_timeout=30)
        self.assertEqual(self.original_bytes, self.mcp_config.read_bytes())
        self.assertEqual(0o640, stat.S_IMODE(self.mcp_config.stat().st_mode))

    def test_symlink_mcp_config_is_rejected_without_mutating_its_target(self) -> None:
        target = Path(self.temporary.name) / "real-deployment.json"
        target.write_bytes(self.original_bytes)
        self.mcp_config.unlink()
        self.mcp_config.symlink_to(target)

        resolved = integration.resolve_mcp_config(
            SimpleNamespace(mcp_config=str(self.mcp_config))
        )
        self.assertEqual(self.mcp_config.absolute(), resolved)
        self.assertTrue(resolved.is_symlink())

        with self.assertRaisesRegex(ReviewError, "symbolic link"):
            self.install()

        self.assertTrue(self.mcp_config.is_symlink())
        self.assertEqual(self.original_bytes, target.read_bytes())
        self.assertFalse(skills.manifest_path(self.paths).exists())
        self.assertFalse(integration.install_state_path(self.paths).exists())

    def test_skill_root_in_untracked_lab_directory_is_rejected_before_creation(self) -> None:
        unsafe = self.lab / "untracked-skills"
        with self.assertRaisesRegex(ReviewError, "overlaps protected"):
            integration.install_integration(
                self.paths,
                skill_roots={"native": unsafe, "codex": self.codex},
                mcp_entries=MCP_ENTRIES,
                mcp_config=self.mcp_config,
                stage_argv=self.stage_ok,
                stage_cwd=self.lab,
                stage_timeout=30,
            )
        self.assertFalse(unsafe.exists())
        self.assertEqual(self.original_bytes, self.mcp_config.read_bytes())

    def test_verify_probes_recorded_staged_config_not_source_config(self) -> None:
        self.install(staged_config=self.staged_config)
        state = json.loads(
            integration.install_state_path(self.paths).read_text(encoding="utf-8")
        )
        self.assertEqual(str(self.staged_config.resolve()), state["mcp"]["stagedConfigPath"])

        report = {"ok": True, "acceptanceComplete": False}
        with patch("ocsl_review.lifecycle.mcps.doctor", return_value=report) as doctor:
            result = lifecycle.cmd_verify(self.verify_args())

        self.assertEqual(report, result["mcpAcceptance"])
        self.assertEqual(self.staged_config.resolve(), doctor.call_args.kwargs["staged_config"])
        self.assertNotEqual(self.mcp_config.resolve(), doctor.call_args.kwargs["staged_config"])

    def test_legacy_state_requires_or_accepts_explicit_staged_config(self) -> None:
        self.install()
        state_path = integration.install_state_path(self.paths)
        original_state = state_path.read_bytes()

        with self.assertRaisesRegex(ValidationError, "generated deployment MCP config is unknown"):
            lifecycle.cmd_verify(self.verify_args())

        report = {"ok": True, "acceptanceComplete": False}
        with patch("ocsl_review.lifecycle.mcps.doctor", return_value=report) as doctor:
            lifecycle.cmd_verify(self.verify_args(staged_config=self.staged_config))
        self.assertEqual(self.staged_config.resolve(), doctor.call_args.kwargs["staged_config"])
        self.assertEqual(original_state, state_path.read_bytes())

    def test_verify_rejects_source_config_and_failed_or_incomplete_acceptance(self) -> None:
        self.install(staged_config=self.staged_config)
        with self.assertRaisesRegex(ValidationError, "must differ"):
            lifecycle.cmd_verify(self.verify_args(staged_config=self.mcp_config))

        failed = {"ok": False, "acceptanceComplete": False}
        with (
            patch("ocsl_review.lifecycle.mcps.doctor", return_value=failed),
            self.assertRaisesRegex(ValidationError, "MCP verification failed"),
        ):
            lifecycle.cmd_verify(self.verify_args())

        incomplete = {"ok": True, "acceptanceComplete": False}
        with (
            patch("ocsl_review.lifecycle.mcps.doctor", return_value=incomplete),
            self.assertRaisesRegex(ValidationError, "requires live MCP acceptance"),
        ):
            lifecycle.cmd_verify(self.verify_args(strict=True))

    def test_staging_failure_rolls_back_skills_and_mcp_config(self) -> None:
        self.mcp_config.chmod(0o640)
        failing = [sys.executable, "-c", "raise SystemExit(17)"]
        with self.assertRaisesRegex(ReviewError, "integration install failed"):
            integration.install_integration(
                self.paths,
                skill_roots=self.roots,
                mcp_entries=MCP_ENTRIES,
                mcp_config=self.mcp_config,
                stage_argv=failing,
                stage_cwd=self.lab,
                stage_timeout=30,
            )
        self.assertEqual(self.original_bytes, self.mcp_config.read_bytes())
        self.assertEqual(0o640, stat.S_IMODE(self.mcp_config.stat().st_mode))
        self.assertFalse(skills.manifest_path(self.paths).exists())
        self.assertFalse(integration.install_state_path(self.paths).exists())
        self.assertFalse((self.native / "ocsl-review-router").exists())
        self.assertFalse((self.codex / "ocsl-review-router").exists())

    def test_uninstall_preserves_unrelated_later_mcp_changes(self) -> None:
        self.install()
        active = json.loads(self.mcp_config.read_text(encoding="utf-8"))
        active["theme"] = "operator-changed"
        active["mcpServers"]["new-unrelated-server"] = {"command": "new"}
        self.mcp_config.write_text(json.dumps(active), encoding="utf-8")

        integration.uninstall_integration(self.paths, stage_timeout=30)
        restored = json.loads(self.mcp_config.read_text(encoding="utf-8"))
        self.assertEqual("operator-changed", restored["theme"])
        self.assertEqual({"command": "new"}, restored["mcpServers"]["new-unrelated-server"])
        self.assertEqual(
            {"command": "prior-catalog"}, restored["mcpServers"]["ocsl_review_catalog"]
        )
        self.assertEqual({"command": "prior-scholar"}, restored["mcpServers"]["ocsl_scholar"])

    def test_absent_mcp_config_is_absent_again_after_uninstall(self) -> None:
        self.mcp_config.unlink()
        self.install()
        self.assertTrue(self.mcp_config.is_file())
        integration.uninstall_integration(self.paths, stage_timeout=30)
        self.assertFalse(self.mcp_config.exists())

    def test_integration_uninstall_detects_skill_and_mcp_edits(self) -> None:
        self.install()
        changed = self.native / "ocsl-review-router" / "SKILL.md"
        changed.write_text(changed.read_text(encoding="utf-8") + "\noperator edit\n")
        with self.assertRaisesRegex(ValidationError, "edited after installation"):
            integration.uninstall_integration(self.paths, stage_timeout=30)
        active = json.loads(self.mcp_config.read_text(encoding="utf-8"))
        self.assertEqual(
            MCP_ENTRIES["ocsl_review_catalog"],
            active["mcpServers"]["ocsl_review_catalog"],
        )

        # A separate installation verifies MCP edit detection before any removal.
        changed.write_text(changed.read_text(encoding="utf-8").removesuffix("\noperator edit\n"))
        active["mcpServers"]["ocsl_scholar"] = {"command": "operator-edit"}
        self.mcp_config.write_text(json.dumps(active), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "installed MCP entry changed"):
            integration.uninstall_integration(self.paths, stage_timeout=30)

    def test_stale_integration_fails_verify_and_integrate_refresh_brings_it_up_to_date(self) -> None:
        home = Path(self.temporary.name) / "home"
        for name in ("skills", "environments", "mcp"):
            shutil.copytree(ROOT / name, home / name)
        self.paths = Paths(home=home, state=self.state, lab_repo=self.lab)
        self.install()
        self.assertTrue(integration.verify_integration(self.paths)["ok"])
        source = home / "skills" / "ocsl-review-reference-analysis" / "SKILL.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nscrubbed source\n", encoding="utf-8")

        stale = integration.verify_integration(self.paths)
        self.assertFalse(stale["ok"])
        self.assertIn("stale", stale["reason"])
        with self.assertRaisesRegex(ValidationError, "integrate refresh"):
            skills.refresh_skill_set(self.paths)

        refreshed = integration.refresh_integration(self.paths)
        self.assertEqual(2, len(refreshed["refreshed"]))
        self.assertTrue(integration.verify_integration(self.paths)["ok"])
        for root in (self.native, self.codex):
            self.assertTrue(
                (root / "ocsl-review-reference-analysis" / "SKILL.md")
                .read_text(encoding="utf-8")
                .endswith("scrubbed source\n")
            )
        state = json.loads(integration.install_state_path(self.paths).read_text(encoding="utf-8"))
        self.assertEqual(
            integration.sha256_file(skills.manifest_path(self.paths)), state["skillManifest"]["sha256"]
        )
        # A repeated install refreshes as well, and a stale installation can always be uninstalled.
        source.write_text(source.read_text(encoding="utf-8") + "\nonce more\n", encoding="utf-8")
        again = self.install()
        self.assertTrue(again["reused"])
        self.assertEqual(2, len(again["refreshed"]))
        source.write_text(source.read_text(encoding="utf-8") + "\nstale again\n", encoding="utf-8")
        self.assertFalse(integration.verify_integration(self.paths)["ok"])
        result = integration.uninstall_integration(self.paths, stage_timeout=30)
        self.assertFalse(result["reused"])
        self.assertEqual(self.original_bytes, self.mcp_config.read_bytes())

    def test_integrate_refresh_installs_a_missing_skill_that_verify_and_doctor_report(self) -> None:
        with skill_set_without(FOLLOWUP_AUDIT):
            self.install()
        targets = self.targets(FOLLOWUP_AUDIT)
        state_path = integration.install_state_path(self.paths)
        manifest = skills.manifest_path(self.paths)
        acceptance = {"ok": True, "acceptanceComplete": True}

        verified = integration.verify_integration(self.paths)
        self.assertFalse(verified["ok"])
        self.assertIn(f"the installation lacks required skills: {FOLLOWUP_AUDIT};", verified["reason"])
        self.assertIn("`ocsl-review integrate refresh`", verified["reason"])
        with self.assertRaisesRegex(ValidationError, f"lacks required skills: {FOLLOWUP_AUDIT};.*integrate refresh"):
            lifecycle.cmd_verify(self.verify_args())
        with patch("ocsl_review.doctor.mcps.doctor", return_value=acceptance):
            report = review_doctor.doctor(self.paths)
        self.assertFalse(report["checks"]["integration"])
        self.assertEqual(verified["reason"], report["integration"]["reason"])

        # A folder the installation does not own at the missing skill's place stops the refresh before any change.
        foreign = self.native / FOLLOWUP_AUDIT
        foreign.mkdir()
        (foreign / "notes.md").write_text("kept by a person\n", encoding="utf-8")
        before = (state_path.read_bytes(), manifest.read_bytes(), self.skill_roots_snapshot())
        with self.assertRaisesRegex(ValidationError, "does not belong to this installation"):
            integration.refresh_integration(self.paths)
        self.assertEqual(before, (state_path.read_bytes(), manifest.read_bytes(), self.skill_roots_snapshot()))
        shutil.move(str(foreign), str(Path(self.temporary.name) / "kept-notes"))

        refreshed = integration.refresh_integration(self.paths)
        self.assertEqual((targets, [], []), (set(refreshed["added"]), refreshed["refreshed"], refreshed["removed"]))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(integration.sha256_file(manifest), state["skillManifest"]["sha256"])
        self.assertTrue(integration.verify_integration(self.paths)["ok"])
        with patch("ocsl_review.doctor.mcps.doctor", return_value=acceptance):
            self.assertTrue(review_doctor.doctor(self.paths)["checks"]["integration"])

        # Nothing left to do: a further refresh or a repeated install writes neither the manifest nor the state.
        before = (state_path.read_bytes(), manifest.read_bytes(), self.skill_roots_snapshot())
        again = integration.refresh_integration(self.paths)
        self.assertEqual(([], [], []), (again["refreshed"], again["added"], again["removed"]))
        self.assertEqual([], self.install()["added"])
        self.assertEqual(before, (state_path.read_bytes(), manifest.read_bytes(), self.skill_roots_snapshot()))

        integration.uninstall_integration(self.paths, stage_timeout=30)
        self.assertFalse(any(Path(target).exists() for target in targets))
        self.assertEqual(self.original_bytes, self.mcp_config.read_bytes())

    def test_stage_timeout_environment_is_honored_when_cli_value_is_absent(self) -> None:
        args = SimpleNamespace(
            stage_command=self.stage_ok,
            stage_cwd=str(self.lab),
            stage_timeout=None,
        )
        with patch.dict(os.environ, {"OCSL_REVIEW_STAGE_TIMEOUT": "41.5"}, clear=False):
            _, _, timeout = integration.resolve_staging(self.paths, args)
            self.assertEqual(41.5, timeout)
            with patch.object(integration, "uninstall_integration", return_value={}) as uninstall:
                integration.cmd_uninstall(SimpleNamespace(paths=self.paths, stage_timeout=None))
        uninstall.assert_called_once_with(self.paths, stage_timeout=41.5)

        with self.assertRaisesRegex(ReviewError, "must be positive"):
            integration.resolve_staging(
                self.paths,
                SimpleNamespace(
                    stage_command=self.stage_ok,
                    stage_cwd=str(self.lab),
                    stage_timeout=0,
                ),
            )
        with self.assertRaisesRegex(ReviewError, "must be positive"):
            integration.cmd_uninstall(SimpleNamespace(paths=self.paths, stage_timeout=0))


if __name__ == "__main__":
    unittest.main()
