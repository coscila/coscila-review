# SPDX-License-Identifier: Apache-2.0
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from ocsl_review.cli import build_parser, main
from ocsl_review.corpus_store import CorpusRepository
from ocsl_review.corpus_enumeration import enumerate_reviews
from ocsl_review.corpus_export import derive_facts, export_corpus, import_snapshot
from ocsl_review.corpus_cli import Progress
from ocsl_review.errors import ValidationError
from ocsl_review.scholarly import RawResponse


class FixtureClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []
        self.authorized = []
        self.transport = self

    def _authorize(self, provider, operation, args):
        self.authorized.append((provider, operation, args))

    def get(self, url, params):
        self.requests.append(params)
        return RawResponse(json.dumps(next(self.pages)).encode(), 200, {}, url)


def page(ids, total, cursor):
    return {"hitCount": total, "nextCursorMark": cursor,
            "resultList": {"result": [{"id": str(i), "source": "MED", "doi": f"10.1234/review{i}",
                                       "title": f"Fixture review {i}", "pubYear": "2025"} for i in ids]}}


class CorpusWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/literature", "Question", "study", {"version": 2})

    def test_pagination_exhausts_population_and_resume_uses_committed_cursor(self):
        client = FixtureClient([page([1, 2], 3, "next"), page([3], 3, "last")])
        result = enumerate_reviews(self.repo, SimpleNamespace(client=client), query="Review", label="primary")
        self.assertTrue(result["complete"])
        self.assertEqual(result["records"], 3)
        self.assertEqual([r["cursorMark"] for r in client.requests], ["*", "next"])
        self.assertEqual(len(client.authorized), 2)
        repeat = enumerate_reviews(self.repo, SimpleNamespace(client=FixtureClient([])), query="Review", label="primary")
        self.assertEqual(result, repeat)
        self.assertEqual(self.repo.status()["counts"]["reviews"], 3)

    def test_missing_page_and_changing_population_do_not_report_completion(self):
        client = FixtureClient([page([1], 3, "next"), page([], 3, "next")])
        with self.assertRaisesRegex(ValidationError, "stopped before"):
            enumerate_reviews(self.repo, SimpleNamespace(client=client), query="Review", label="primary")
        resumed = FixtureClient([page([2, 3], 3, "done")])
        result = enumerate_reviews(self.repo, SimpleNamespace(client=resumed), query="Review", label="primary")
        self.assertEqual(resumed.requests[0]["cursorMark"], "next")
        self.assertEqual(result["records"], 3)

    def test_export_facts_replay_and_portable_branch(self):
        self.repo.review({"doi": "10.1234/review", "title": "Fixture"})
        first = derive_facts(self.repo)
        second = derive_facts(self.repo)
        self.assertEqual(first, second)
        output = export_corpus(self.repo, "results/export")
        self.assertEqual(output["facts"]["reviewPopulation"], [{"state": "included", "count": 1}])
        for item in output["files"]:
            file = self.root / "results/export" / item["path"]
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), item["sha256"])
        self.repo.library.snapshot("results/snapshot")
        other = self.root / "other-chat"
        other.mkdir()
        imported = import_snapshot(other, "work/seed", self.root / "results/snapshot")
        self.assertEqual(imported["counts"]["reviews"], 1)
        parent = CorpusRepository(self.root, "results/snapshot")
        parent.library.branch("work/question2", "Different question", "different", {"kind": "study-design"})
        child = CorpusRepository(self.root, "work/question2")
        self.assertEqual(child.status()["counts"]["reviews"], 1)
        self.assertEqual(child.status()["assessments"], [])

    def test_heartbeat_is_valid_without_inventing_an_empty_denominator(self):
        progress = Progress(self.repo)
        progress(force=True)
        value = json.loads((self.root / "results/.ocsl/progress.json").read_text())
        self.assertNotIn("denominator", value)
        self.assertEqual(value["state"], "running")
        self.assertIn("+", value["updatedAt"])

    def test_terminal_progress_survives_queued_heartbeats_and_stops_thread(self):
        with Progress(self.repo) as progress:
            progress(force=True, phase="validation", summary="Validation running")
            progress(state="completed", summary="Saved all results")
            path = self.root / "results/.ocsl/progress.json"
            terminal = path.read_bytes()
            threads = [threading.Thread(target=lambda: progress(force=True)) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(path.read_bytes(), terminal)
            self.assertTrue(progress.stop.is_set())
            self.assertEqual(json.loads(terminal)["state"], "completed")
            self.assertEqual(json.loads(terminal)["phase"], "validation")
        self.assertFalse(progress.thread.is_alive())

    def test_cli_routes_and_state_independent_init(self):
        parsed = build_parser().parse_args(["corpus", "execute", "--worker-config", "w1", "--low-worker-config", "w2"])
        self.assertFalse(parsed._state_required)
        protocol = self.root / "protocol.json"
        protocol.write_text('{"version":2}')
        code = main(["corpus", "init", "--workspace", str(self.root), "--library", "work/cli", "--question", "Q", "--analysis", "cli", "--protocol", str(protocol)])
        self.assertEqual(code, 0)
        self.assertEqual(CorpusRepository(self.root, "work/cli").analysis()["analysis_id"], "cli")

    def test_cli_migrate_is_explicit_and_idempotent(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["corpus", "migrate", "--workspace", str(self.root), "--library", "work/literature"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["migrations"], [])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(["corpus", "migrate", "--workspace", str(self.root), "--library", "work/literature", "--rebuild-counters"])
        self.assertTrue(json.loads(output.getvalue())["countersRebuilt"])
        self.repo.library.verify()


if __name__ == "__main__":
    unittest.main()
