# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review followup``: registration, ocsl-review-result-v1 results and the branch commands."""
from __future__ import annotations

import argparse
import importlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import followup_fixtures as F
from ocsl_review import followup_cli
from ocsl_review.cli import RESULT_FORMAT, build_parser, main


class FollowupCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        cls.protocol = cls.root / "protocol.json"
        cls.protocol.write_text(json.dumps(F.protocol()), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def invoke(self, *argv: str) -> tuple[int, dict]:
        target = self.root / f"result-{len(list(self.root.glob('result-*')))}.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), *argv])
        return code, json.loads(target.read_text(encoding="utf-8"))

    def test_init_verify_describe_and_gates_write_result_files(self):
        database = self.root / "cli-branch.sqlite3"
        code, record = self.invoke("followup", "init", "--database", str(database), "--parent", str(self.census["parent"]),
                                   "--parent-text", str(self.census["text"]), "--protocol", str(self.protocol))
        self.assertEqual(0, code, record)
        self.assertEqual((RESULT_FORMAT, ["followup", "init"]), (record["format"], record["command"]))
        from ocsl_review.scores_db import file_sha256
        self.assertEqual(file_sha256(self.census["parent"]), record["result"]["parent"]["sha256"])
        self.assertEqual(file_sha256(self.census["text"]), record["result"]["parentText"]["sha256"])
        code, record = self.invoke("followup", "verify", "--database", str(database))
        self.assertEqual((0, True), (code, record["result"]["ok"]))
        code, record = self.invoke("followup", "describe", "--database", str(database))
        self.assertEqual(0, code)
        self.assertEqual(0, record["result"]["rows"]["fu_frame"])
        code, record = self.invoke("followup", "gates", "--database", str(database))
        self.assertEqual(0, code)
        checks = record["result"]["checks"]
        self.assertTrue(checks["parentDigestUnchanged"])
        self.assertTrue(checks["referencesResolve"])
        self.assertFalse(checks["everyCitedWorkHasStatusRow"])
        self.assertFalse(checks["strataFrozenBeforeJudging"])
        self.assertFalse(record["result"]["ok"])

    def test_a_failed_verification_exits_non_zero_with_its_problems(self):
        code, record = self.invoke("followup", "verify", "--database", str(self.root / "missing.sqlite3"))
        self.assertNotEqual(0, code)
        self.assertEqual("validation", record["error"]["kind"])

    def test_every_command_is_registered_with_help_and_a_handler(self):
        parser = build_parser()
        family = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        followup = family.choices["followup"]
        commands = next(action for action in followup._actions if isinstance(action, argparse._SubParsersAction))
        expected = {"init", "verify", "describe", "gates", "pin-bundle"} | set(followup_cli.VERBS)
        self.assertEqual(expected, set(commands.choices))
        helps = {action.dest: action.help for action in commands._choices_actions}
        for name, sub in commands.choices.items():
            with self.subTest(command=name):
                self.assertTrue(helps.get(name))
                self.assertIn("_handler", sub._defaults)
                if name not in ("verify-facts",):
                    self.assertIn("--database", sub._option_string_actions)

    def test_every_command_names_a_function_of_its_module(self):
        for name, (module, function) in followup_cli.VERBS.items():
            with self.subTest(command=name):
                self.assertTrue(callable(getattr(importlib.import_module(f"ocsl_review.{module}"), function, None)))

    def test_judging_commands_share_the_study_judge_options(self):
        parser = build_parser()
        for argv in (["followup", "support", "--tier", "abstract"], ["followup", "function"],
                     ["followup", "numeric", "--tables", "t.json"], ["followup", "second-gen"]):
            with self.subTest(command=argv[1]):
                args = parser.parse_args([*argv, "--database", "b.sqlite3", "--model", "m", "--on-premises",
                                          "--policy", "p.json", "--concurrency", "4", "--transport-retries", "2",
                                          "--broker-worker", "w1", "--scope", "pilot", "--variant", "replicate"])
                self.assertEqual(("m", True, 4, "pilot", "replicate"),
                                 (args.model, args.on_premises, args.concurrency, args.scope, args.variant))


if __name__ == "__main__":
    unittest.main()
