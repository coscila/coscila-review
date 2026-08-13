from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "ocsl-review"


class LauncherTests(unittest.TestCase):
    def test_wheel_does_not_advertise_a_data_incomplete_console_script(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertNotIn("scripts", project)

    def test_symlinked_launcher_resolves_the_companion_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "ocsl-review"
            link.symlink_to(LAUNCHER)
            environment = dict(os.environ)
            environment.pop("OCSL_REVIEW_HOME", None)
            environment.pop("OCSL_REVIEW_STATE", None)
            environment["OCSL_REVIEW_PLATFORM"] = "linux-64"
            result = subprocess.run(
                [str(link), "--version"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ocsl-review 0.1.0")

    def test_promoted_review_core_python_is_selected_from_explicit_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            build_id = "a" * 64
            promoted = (
                state
                / "environment-templates"
                / "linux-64"
                / "review-core"
                / build_id
            )
            (promoted / "bin").mkdir(parents=True)
            (promoted / ".ocsl-review-template.json").write_text("{}\n", encoding="utf-8")
            fake_python = promoted / "bin" / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'selected:%s\\n' \"$1 $2 $3\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            (promoted.parent / "current").symlink_to(build_id, target_is_directory=True)
            environment = dict(os.environ)
            environment.pop("OCSL_REVIEW_HOME", None)
            environment.pop("OCSL_REVIEW_STATE", None)
            environment["OCSL_REVIEW_PLATFORM"] = "linux-64"
            result = subprocess.run(
                [str(LAUNCHER), "--state", str(state), "--version"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "selected:-m ocsl_review.cli --state")

    def test_unsafe_current_pointer_falls_back_to_host_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            template_root = (
                state / "environment-templates" / "linux-64" / "review-core"
            )
            template_root.mkdir(parents=True)
            outside = Path(temporary) / ("b" * 64)
            (outside / "bin").mkdir(parents=True)
            (outside / ".ocsl-review-template.json").write_text("{}\n", encoding="utf-8")
            fake_python = outside / "bin" / "python"
            fake_python.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
            fake_python.chmod(0o755)
            (template_root / "current").symlink_to(outside, target_is_directory=True)
            environment = dict(os.environ)
            environment.pop("OCSL_REVIEW_HOME", None)
            environment.pop("OCSL_REVIEW_STATE", None)
            environment["OCSL_REVIEW_PLATFORM"] = "linux-64"
            result = subprocess.run(
                [str(LAUNCHER), "--state", str(state), "--version"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ocsl-review 0.1.0")


if __name__ == "__main__":
    unittest.main()
