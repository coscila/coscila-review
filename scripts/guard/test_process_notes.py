"""Tests for the process-note rule of guard.py (stdlib only): python3 -m unittest discover -s <this directory>.

Repository text describes the system in technical terms. The rule rejects added lines and commit messages that cite the
development process instead, leaves vendored, data, migration and generated paths alone, checks only the lines of a
merge that none of the merged revisions contains, and is not applied by pre-push. The example lines below are synthetic
and carry a waiver, so this file passes the rule itself.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")
_spec = importlib.util.spec_from_file_location("guard_under_test", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PROCESS_NOTES = [
    "// Parses the header (plan H7): one field per line",  # guard: allow process-note
    "// Plan E9: the cache keeps one entry per key",  # guard: allow process-note
    "// The page budgets (plan items UI-Y, UI-Z).",  # guard: allow process-note
    "// The client tests (plans P9 and E9) against a stub",  # guard: allow process-note
    "// Token checks (plan AUTH-9): the token stays in memory",  # guard: allow process-note
    "/** The checks, in the order a request passes them (plan Phase 9). */",  # guard: allow process-note
    'describe("example sandboxes (Phase 9 review)", () => {',  # guard: allow process-note
    'it("refuses early (Phase 9 readiness, round 9b)", () => {',  # guard: allow process-note
    "# (round 9's rule keeps the entry open.)",  # guard: allow process-note
    "// The round-9 fix keeps the cursor.",  # guard: allow process-note
    "// round Z finds it slow",  # guard: allow process-note
    "The readiness round passed.",  # guard: allow process-note
    "See kb/example.md for the numbers.",  # guard: allow process-note
    "See kb/2001-01-01-notes.md.",  # guard: allow process-note
    "The numbers are in PROGRESS.md.",  # guard: allow process-note
    "// Cache layout (DESIGN_example section 9).",  # guard: allow process-note
    "// Stream limits (ISSUES ABC-12)",  # guard: allow process-note
    "// The drill (runbook Z9.9)",  # guard: allow process-note
    "found by wf_0123abcd in its second pass",  # guard: allow process-note
    "// (operator decision 2001-01-01): one queue for all jobs",  # guard: allow process-note
    "// Colour scheme (user decision: blue)",  # guard: allow process-note
    "// Replays the 2001-01-01 incident shape",  # guard: allow process-note
]

ORDINARY_TEXT = [
    "Stop after 3 review rounds per deliverable in any case.",
    'message: "Peer review round completed and the dossier synthesized."',
    "const hostCpuPct = round2(value);",
    "for (let round = 0; round < 8; round += 1) {",
    "### Phase 1: Paper discovery and search",
    '"(95% CI 1.2-2.0)", "(Phase 3, 2021)", "(Table 1)"',
    "Draft, restructure, or plan Nature-style manuscript sections",
    "For a requested PDF document, plan LaTeX source plus a build script.",
    "We plan T1-weighted and T2-weighted scans for every participant.",
    'raise ValidationError("egress plan SHA-256 does not match the configuration")',
    "EXPLAIN QUERY PLAN SELECT id FROM t WHERE id > ?",
    "Changing the bind address should require an explicit operator decision.",
    "The approval waits for a decision (user decision required).",
    "The round 2 review compares the revisions; reviewers in round 2 found three issues.",
    "const MAX_PROGRESS = 50;",
    "Concepts are versioned and disposable BY DESIGN.",
    "The relay holds 64 kb/s per stream.",
    "The link holds 64 kb/s.",
    "Load 1 kb/100 bp of ladder per lane.",
    "Reads per kb/Mb window",
    "mb = kb/1024",
    "workspace/kb/notes.md holds the chat's knowledge base",
    "After the first review, round 2 starts with the revised draft.",
    "Incident cases from 2015 to 2019 were counted per region.",
]

EXEMPT_PATHS = [
    "upstream/skills/x/SKILL.md",
    "sources/kdense/skills/y.md",
    "translations/en/skills/z.md",
    "licenses/Apache-2.0.txt",
    "LICENSE",
    "tests/fixtures/census.xml",
    "test/fixtures/contract/plan.status.json",
    "packages/lab/src/__snapshots__/start-prompt.test.ts.snap",
    "test/fixtures/legacy/store-0123abc.ts",
    "packages/db/prisma/migrations/20260101000000_x/migration.sql",
    "packages/db/src/generated/prisma/internal/class.ts",
]
CHECKED_PATHS = ["src/store.ts", "test/fixtures/fake-proc.ts", "docs/licenses.md", "README.md"]
NOTE = "// Moves the queue (plan H9)."  # guard: allow process-note
NOTE_MESSAGE = "Add the store (plan S9)"  # guard: allow process-note
HINT = "describe the system in technical terms; plan ids, rounds, kb references are not allowed in repository text"


class PatternTest(unittest.TestCase):
    def test_process_notes_are_rejected(self):
        for line in PROCESS_NOTES:
            with self.subTest(line=line):
                self.assertIn("process-note", guard.line_problems("src/example.ts", line))
                self.assertIn("msg-process-note", guard.message_problems(line))

    def test_ordinary_text_passes(self):
        for line in ORDINARY_TEXT:
            with self.subTest(line=line):
                self.assertNotIn("process-note", guard.line_problems("src/example.ts", line))
                self.assertNotIn("msg-process-note", guard.message_problems(line))

    def test_vendored_data_migration_and_generated_paths_are_exempt(self):
        for path in EXEMPT_PATHS:
            with self.subTest(path=path):
                self.assertNotIn("process-note", guard.line_problems(path, NOTE))
        for path in CHECKED_PATHS:
            with self.subTest(path=path):
                self.assertIn("process-note", guard.line_problems(path, NOTE))

    def test_a_waiver_names_the_rule(self):
        self.assertEqual([], guard.line_problems("tests/test_rule.py", NOTE + "  # guard: allow process-note"))
        self.assertIn("process-note", guard.line_problems("tests/test_rule.py", NOTE + "  # guard: allow jwt"))

    def test_the_guard_runs_without_local_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = os.path.join(directory, "guard.py")
            shutil.copyfile(GUARD, copy)
            spec = importlib.util.spec_from_file_location("guard_mirror_under_test", copy)
            mirror = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mirror)
        self.assertIsNone(mirror.LOCAL)
        self.assertIn("process-note", mirror.line_problems("src/store.ts", NOTE))
        self.assertEqual([], mirror.local_message_problems(NOTE_MESSAGE, ["src/store.ts"]))

    def test_the_guard_and_this_file_pass_the_rule(self):
        for name in ("guard.py", "README.md", os.path.basename(__file__)):
            path = os.path.join(HERE, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    self.assertNotIn("process-note", guard.line_problems(name, line), f"{name}:{number}")


class HookTest(unittest.TestCase):
    """guard.py ci, pre-commit and pre-push in a throwaway repository (no hooks installed)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        name, email = guard.IDENTITY[:-1].split(" <")
        self.env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
                    "GIT_COMMITTER_EMAIL": email}
        self.git("init", "-q", "-b", "dev")
        self.commit("a.txt", "x\n", "Add a")
        self.base = self.head()

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args, check=True):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, check=check)

    def guard(self, *args, stdin=None):
        return subprocess.run([sys.executable, GUARD, *args], cwd=self.repo, env=self.env, capture_output=True,
                              text=True, input=stdin)

    def write(self, path, content):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)

    def commit(self, path, content, message):
        self.write(path, content)
        self.git("add", "--", path)
        self.git("commit", "-q", "-m", message)
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_ci_reports_lines_and_messages_with_what_to_do(self):
        sha = self.commit("src/store.ts", NOTE + "\n", NOTE_MESSAGE)
        result = self.guard("ci", "--range", f"{self.base}..HEAD")
        self.assertNotEqual(0, result.returncode)
        self.assertIn(f"process-note\t{sha[:12]} src/store.ts:1", result.stderr)
        self.assertIn(f"msg-process-note\t{sha[:12]}", result.stderr)
        self.assertIn(HINT, result.stderr)
        self.assertNotIn("(plan", result.stderr)  # locations only, never the matched text

    def test_pre_commit_rejects_a_staged_note(self):
        self.write("src/store.ts", NOTE + "\n")
        self.git("add", "src/store.ts")
        result = self.guard("pre-commit")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("process-note\tsrc/store.ts:1", result.stderr)

    def test_a_merge_is_not_checked_for_the_lines_it_merges(self):
        self.commit("notes.ts", "// one\n", "Add notes")
        self.git("checkout", "-q", "-b", "side")
        side = self.commit("notes.ts", NOTE + "\n", "Describe the queue")
        self.git("checkout", "-q", "dev")
        self.commit("notes.ts", "// Moves the queue in bounded batches.\n", "Describe the queue")
        self.assertNotEqual(0, self.git("merge", "-q", "side", check=False).returncode)
        self.git("checkout", "--theirs", "notes.ts")
        self.git("add", "notes.ts")
        self.assertEqual(0, self.guard("pre-commit").returncode)  # MERGE_HEAD: the other branch's lines stay unchecked
        self.git("commit", "-q", "-m", "Merge side")
        merge = self.head()
        result = self.guard("ci", "--range", f"{self.base}..HEAD")
        self.assertIn(f"process-note\t{side[:12]} notes.ts:1", result.stderr)  # the merged commit on its own
        self.assertNotIn(merge[:12], result.stderr)

    def test_a_merge_is_checked_for_the_lines_it_writes(self):
        self.commit("notes.ts", "// one\n", "Add notes")
        self.git("checkout", "-q", "-b", "side")
        self.commit("notes.ts", "// Moves the queue in batches.\n", "Describe the queue")
        self.git("checkout", "-q", "dev")
        self.commit("notes.ts", "// Moves the queue in bounded batches.\n", "Describe the queue")
        self.assertNotEqual(0, self.git("merge", "-q", "side", check=False).returncode)
        self.write("notes.ts", NOTE + "\n")  # a resolution that neither side contains
        self.git("add", "notes.ts")
        result = self.guard("pre-commit")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("process-note\tnotes.ts:1", result.stderr)
        self.git("commit", "-q", "-m", "Merge side")
        merge = self.head()
        result = self.guard("ci", "--range", f"{self.base}..HEAD")
        self.assertIn(f"process-note\t{merge[:12]} notes.ts:1", result.stderr)

    def test_pre_push_does_not_apply_the_rule(self):
        sha = self.commit("src/store.ts", NOTE + "\n", NOTE_MESSAGE)
        result = self.guard("pre-push", "origin", "url", stdin=f"refs/heads/dev {sha} refs/heads/dev {'0' * 40}\n")
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
