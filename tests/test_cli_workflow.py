from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ocsl_review.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "manuscript_with_citations.md"


class CliWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        shutil.copy2(FIXTURE, self.workspace / "manuscript.md")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(list(argv))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_catalog_validation_accepts_global_json_after_subcommand(self) -> None:
        result, stdout, stderr = self.invoke("catalog", "validate", "--json")
        self.assertEqual(result, 0, stderr)
        self.assertTrue(json.loads(stdout)["ok"])

    def test_prepare_summarize_and_render_keep_conservative_denominators(self) -> None:
        result, stdout, stderr = self.invoke(
            "review",
            "prepare",
            "manuscript.md",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(result, 0, stderr)
        prepared = json.loads(stdout)
        self.assertIn("auditSummary", prepared)
        summary = json.loads(Path(prepared["auditSummary"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["citedCorrectly"]["status"], "not-fully-verifiable")
        self.assertEqual(summary["citedEnough"]["status"], "not-fully-verifiable")
        self.assertGreater(summary["auditCoverage"]["citationRequiredClaimsTotal"], 0)

        result, stdout, stderr = self.invoke(
            "review", "render", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(result, 0, stderr)
        rendered = json.loads(stdout)
        self.assertEqual(set(rendered), {"docx", "json", "md"})
        self.assertTrue(all(Path(value).is_file() for value in rendered.values()))
        self.assertTrue(
            all(Path(value).stat().st_mode & 0o777 == 0o600 for value in rendered.values())
        )

    def test_review_input_cannot_escape_workspace(self) -> None:
        result, _, stderr = self.invoke(
            "review",
            "ingest",
            str(FIXTURE),
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(result, 2)
        self.assertIn("escapes the selected workspace", stderr)

    def test_operator_command_is_rejected_in_chat_context_before_mutation(self) -> None:
        state = Path(self.temporary.name) / "state"
        with patch.dict(os.environ, {"LAB_WORKSPACE_ROOT": str(self.workspace)}, clear=False):
            result, _, stderr = self.invoke(
                "--state",
                str(state),
                "env",
                "build",
                "--all",
            )
        self.assertEqual(result, 3)
        self.assertIn("trust boundary", stderr)
        self.assertFalse(state.exists())

    def test_env_fork_all_rejects_overlay_instead_of_ignoring_it(self) -> None:
        state = Path(self.temporary.name) / "state"
        target = Path(self.temporary.name) / "forked-workspace"
        target.mkdir()
        requirement = "addon==1.0 --hash=sha256:" + "a" * 64

        result, _, stderr = self.invoke(
            "--state",
            str(state),
            "env",
            "fork",
            "--source-workspace",
            str(self.workspace),
            "--target-workspace",
            str(target),
            "--all",
            "--add-pip",
            requirement,
        )

        self.assertEqual(result, 2)
        self.assertIn("--all cannot be combined with --add-conda or --add-pip", stderr)
        self.assertFalse((target / "envs").exists())

    def test_failed_integration_verification_has_nonzero_exit_status(self) -> None:
        state = Path(self.temporary.name) / "empty-state"
        state.mkdir()
        staged = Path(self.temporary.name) / "staged.json"
        staged.write_text('{"mcpServers": {}}', encoding="utf-8")

        result, stdout, stderr = self.invoke(
            "--state",
            str(state),
            "integrate",
            "verify",
            "--staged-config",
            str(staged),
            "--json",
        )

        self.assertEqual(result, 5)
        self.assertEqual("", stdout)
        self.assertIn("validation: integration verification failed", stderr)


if __name__ == "__main__":
    unittest.main()
