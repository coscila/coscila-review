from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from ocsl_review.cli import RESULT_FORMAT, main


class ResultFileTests(unittest.TestCase):
    """--result-file: the machine-readable result a Study Run's executor reads after a census step ended."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_records_the_exit_code_and_the_json_result_the_command_printed(self) -> None:
        target = self.root / "results" / "g0-s1-1.json"
        code, stdout, stderr = self.invoke("--result-file", str(target), "instruments", "list")
        self.assertEqual(code, 0, stderr)
        record = self.read(target)
        self.assertEqual(record["format"], RESULT_FORMAT)
        self.assertEqual(record["exitCode"], 0)
        self.assertEqual(record["command"], ["instruments", "list"])
        self.assertIsNone(record["error"])
        self.assertEqual(record["result"], json.loads(stdout))
        self.assertTrue(record["finishedAt"].endswith("Z"))
        # Written atomically: nothing but the result is left beside it.
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["g0-s1-1.json"])

    def test_accepts_the_option_after_the_command_like_the_other_global_options(self) -> None:
        target = self.root / "after.json"
        code, _, stderr = self.invoke("instruments", "list", "--result-file", str(target))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.read(target)["exitCode"], 0)

    def test_records_the_class_of_an_error_with_its_exit_code(self) -> None:
        target = self.root / "refused.json"
        code, _, stderr = self.invoke(
            "--result-file", str(target), "study", "gates", "--database", str(self.root / "missing.sqlite3")
        )
        self.assertNotEqual(code, 0)
        record = self.read(target)
        self.assertEqual(record["exitCode"], code)
        self.assertIsNone(record["result"])
        self.assertIn(record["error"]["kind"], {"validation", "runtime", "usage"})
        self.assertTrue(record["error"]["message"])
        self.assertTrue(stderr)

    def test_a_refused_command_line_still_leaves_a_result(self) -> None:
        target = self.root / "usage.json"
        code, _, _ = self.invoke("--result-file", str(target), "study", "no-such-command")
        self.assertEqual(code, 2)
        record = self.read(target)
        self.assertEqual(record["exitCode"], 2)
        self.assertEqual(record["error"]["kind"], "usage")

    def test_without_the_option_a_refused_command_line_exits_as_before(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["study", "no-such-command"])
        self.assertEqual(raised.exception.code, 2)

    def test_a_pass_through_argument_is_never_taken_for_the_option(self) -> None:
        from ocsl_review.cli import _result_file_argument

        self.assertIsNone(_result_file_argument(["env", "run", "--", "tool", "--result-file", "x"]))
        self.assertEqual(_result_file_argument(["--result-file=y", "instruments", "list"]), "y")


if __name__ == "__main__":
    unittest.main()
