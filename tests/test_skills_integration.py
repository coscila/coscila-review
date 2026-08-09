from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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


class ReviewPathsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state = base / "state"
        self.state.mkdir()
        self.paths = Paths(home=ROOT, state=self.state, lab_repo=None)
        self.native = base / "native-skills"
        self.codex = base / "codex-skills"
        self.roots = {"native": self.native, "codex": self.codex}


class SkillCatalogTests(ReviewPathsTestCase):
    def test_catalog_and_all_runtime_contracts_validate(self) -> None:
        result = skills.validate_catalog(self.paths)
        self.assertEqual(20, result["skills"])
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
        self.assertEqual(40, removed["removed"])
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


if __name__ == "__main__":
    unittest.main()
