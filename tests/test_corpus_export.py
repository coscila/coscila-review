# SPDX-License-Identifier: Apache-2.0
"""The facts input digest streams its lists (keyset pages, incremental sha256) and keeps its value."""
import contextlib
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest import mock

from ocsl_review import corpus_export, corpus_library as library
from ocsl_review.corpus_export import Scan, derive_facts, keyset_pages, streamed_identity
from ocsl_review.corpus_library import Library
from ocsl_review.corpus_store import CorpusRepository, identity


def in_memory_digest(repo, analysis_id=None):
    """The input digest as derive_facts computed it before streaming, with both lists held in memory."""
    analysis = repo.analysis(analysis_id)
    with repo.connect() as connection:
        facts = {key: [dict(r) for r in connection.execute(sql, (analysis["analysis_id"],) if "?" in sql else ())]
                 for key, sql in corpus_export.FACT_QUERIES.items()}
        inputs = {"analysis": analysis["analysis_id"], "protocolSha256": analysis["protocol_sha256"],
                  "artifacts": [r[0] for r in connection.execute(
                      "SELECT sha256 FROM source_artifacts WHERE artifact_kind!='fact-derivation' ORDER BY sha256")],
                  "assessments": [r[0] for r in connection.execute(
                      "SELECT assessment_id FROM assessments WHERE analysis_id=? ORDER BY assessment_id",
                      (analysis["analysis_id"],))],
                  "factsSha256": identity(facts)}
    return identity(inputs)


def populate(repo, analysis_id, prefix, count):
    """Works with abstracts, cited by one review, with judged loci of ``analysis_id``."""
    works = []
    for number in range(count):
        work = repo.work({"doi": f"10.1234/{prefix}{number}", "title": f"Fixture work {prefix}{number}"})
        repo.document(work, {"text": f"Evidence {prefix}{number}.", "complete": True,
                             "blocks": [{"id": "b1", "text": f"Evidence {prefix}{number}.", "kind": "abstract"}]},
                      "abstract", "fixture")
        works.append(work)
    manuscript = repo.json_artifact({"review": prefix}, "review-manuscript")
    review = repo.review({"doi": f"10.1234/review-{prefix}", "title": f"Review {prefix}"})
    with repo.connect(write=True) as connection:
        connection.executemany("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", [
            (f"{prefix}-e{n}", review, str(n), f"Printed {n}", "{}", works[n], "verified", manuscript) for n in range(count)])
        connection.executemany("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", [
            (f"{prefix}-l{n}", f"{prefix}-e{n}", manuscript, f"p{n}", f"Claim {n}.", "[]", "fixture") for n in range(count)])
        connection.execute("INSERT INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            f"{prefix}-receipt", analysis_id, "item", manuscript, manuscript, "fixture", "model", "{}", "{}", "completed",
            library.now(), library.now()))
        # Non-ASCII and quoted ids exercise the canonical JSON escapes of the digest.
        connection.executemany("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", [
            (f"{prefix}-a{n}{suffix}", analysis_id, f"{prefix}-l{n}", f"{prefix}-receipt", "fixture", "abstract",
             '{"supportVerdict":"supported"}', "accepted", library.now())
            for n, suffix in zip(range(count), ("", "-é", '-"q"', "-\\", "- ", "-z"))])
    return works


class StreamedIdentityTests(unittest.TestCase):
    def test_streamed_identity_is_identity_over_the_same_values(self):
        values = [
            {}, [], (), "", None, True, 0, -3, 1.5, 1e-7,
            {"b": [1, {"y": None, "x": "é"}], "a": "\"quoted\" \\ \n\t\u0000   \U0001f600", "c": []},
            [{"k": ("t", 1)}, [[], {}], "z"], {1: "integer key", 2: [3]},
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(streamed_identity(value), identity(value))
                self.assertEqual(streamed_identity(value, "second"), identity(value, "second"))
        lists = {"artifacts": ["a" * 64, "b" * 64, "c" * 64], "assessments": ["x-é", 'y"'], "analysis": "first", "empty": []}
        # Pages of any length, empty ones included, hash as the one array they make up.
        scanned = {key: Scan(iter([value[:1], [], value[1:]])) if isinstance(value, list) else value
                   for key, value in lists.items()}
        self.assertEqual(streamed_identity(scanned), identity(lists))
        self.assertEqual(streamed_identity(Scan(iter([]))), identity([]))

    def test_keyset_pages_yield_every_row_once_in_order_for_any_page_size(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("CREATE TABLE t(id TEXT PRIMARY KEY, grp TEXT NOT NULL)")
            connection.execute("CREATE INDEX t_by_grp ON t(grp)")
            ids = [None, None] + [f"{n:03d}-{chr(0x61 + n % 26)}é" for n in range(23)]
            connection.executemany("INSERT INTO t VALUES (?,?)", [(value, "odd" if n % 2 else "even")
                                                                   for n, value in enumerate(ids)])
            for page in (1, 2, 3, 5, 23, 25, 100):
                for condition, params, where in (("", (), ""), ("+grp=?", ("odd",), " WHERE grp='odd'")):
                    with self.subTest(page=page, condition=condition):
                        pages = list(keyset_pages(connection, "t", "id", condition, params, page=page))
                        self.assertTrue(all(0 < len(values) <= page for values in pages))
                        self.assertEqual([value for values in pages for value in values],
                                         [r[0] for r in connection.execute(f"SELECT id FROM t{where} ORDER BY id")])
            plan = " ".join(row[3] for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM t WHERE id > ? AND (+grp=?) ORDER BY id LIMIT ?", ("", "odd", 2)))
            self.assertNotIn("TEMP B-TREE", plan, "a page walks the key's index instead of sorting the table")


class FactsDigestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/literature", "Fixture question", "first", {"v": 1})
        populate(self.repo, "first", "p", 5)

    def test_digest_equals_the_in_memory_digest_across_pages_in_a_library_and_a_branch(self):
        expected = in_memory_digest(self.repo)
        with mock.patch.object(corpus_export, "DIGEST_PAGE_ROWS", 2):
            first = derive_facts(self.repo)
            self.assertEqual(first["inputManifestSha256"], expected)
            # The fact-derivation artifact derive_facts writes is outside the digest, so a rerun keeps it.
            self.assertEqual(derive_facts(self.repo), first)
        self.assertEqual(derive_facts(self.repo)["inputManifestSha256"], expected)
        self.repo.library.snapshot("results/snapshot")
        Library(self.repo.workspace, "results/snapshot").branch("work/branch", "Branch question", "second", {"v": 2})
        branch = CorpusRepository(self.root, "work/branch")
        populate(branch, "second", "b", 6)
        branch.work({"doi": "10.1234/p0", "title": "Corrected in the branch"})  # a parent row the branch overrides
        for analysis_id in ("first", "second"):
            with self.subTest(analysis=analysis_id), mock.patch.object(corpus_export, "DIGEST_PAGE_ROWS", 3):
                self.assertEqual(derive_facts(branch, analysis_id)["inputManifestSha256"],
                                 in_memory_digest(branch, analysis_id))

    def test_a_commit_between_pages_does_not_reach_the_digest(self):
        expected = in_memory_digest(self.repo)
        pages, written = corpus_export.keyset_pages, []

        def interleaved(connection, *args, **kwargs):
            for values in pages(connection, *args, **kwargs):
                yield values
                if not written:
                    # Another connection commits artifacts while the digest is half read.
                    written.extend(self.repo.artifact(f"written mid-scan {n}".encode(), "fixture", "text/plain")
                                   for n in range(8))
        with mock.patch.object(corpus_export, "DIGEST_PAGE_ROWS", 2), \
                mock.patch.object(corpus_export, "keyset_pages", interleaved):
            during = derive_facts(self.repo)
        self.assertTrue(written)
        self.assertEqual(during["inputManifestSha256"], expected)
        self.assertNotEqual(derive_facts(self.repo)["inputManifestSha256"], expected)

    def test_digest_memory_stays_at_one_page(self):
        rows = 60_000
        with self.repo.connect(write=True) as connection:
            connection.executemany("INSERT INTO source_artifacts VALUES (?,?,?,?,?,?)", (
                (digest, "sources/" + digest, 1, "text/plain", "synthetic", library.now())
                for digest in (hashlib.sha256(str(n).encode()).hexdigest() for n in range(rows))))
        peaks = {}
        for name, function in (("in-memory", in_memory_digest), ("streamed", lambda repo: derive_facts(repo)["inputManifestSha256"])):
            tracemalloc.start()
            try:
                peaks[name] = (function(self.repo), tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
        self.assertEqual(peaks["streamed"][0], peaks["in-memory"][0])
        self.assertLess(peaks["streamed"][1], 4 * 2**20)
        self.assertLess(peaks["streamed"][1] * 4, peaks["in-memory"][1])


if __name__ == "__main__":
    unittest.main()
