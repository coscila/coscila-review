# SPDX-License-Identifier: Apache-2.0
"""Study-first lookups against a pinned, read-only store (the label-free projection as a byte cache)."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ocsl_review.confidentiality import EgressPlan
from ocsl_review.corpus_acquisition import CorpusAcquirer
from ocsl_review.corpus_library import digest_file, now
from ocsl_review.corpus_pipeline import acquire_corpus
from ocsl_review.corpus_review_reuse import retained_review_sources, reuse_review
from ocsl_review.corpus_seed import import_legacy_evidence
from ocsl_review.corpus_store import CorpusRepository, canonical, identity
from ocsl_review.errors import ValidationError
from ocsl_review.scholarly import ScholarlyClient
from ocsl_review.store_client import Store, attach_store
from ocsl_review.store_projection import build_projection
from test_corpus_pipeline import TitleTransport
from test_structured import article


class StoreClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / "lab"
        self.workspace.mkdir()
        self.mount = self.root / "datasets"
        self.mount.mkdir()
        self.store_path = self.build_store()

    def build_store(self):
        """An earlier corpus (with legacy labels) projected into a label-free store."""
        seed_root = self.root / "operator"
        seed_root.mkdir()
        seed = CorpusRepository.create(seed_root, "corpus", "Earlier question", "earlier", {})
        legacy = seed_root / "legacy"
        (legacy / "work/reviews").mkdir(parents=True)
        path = legacy / "work/reviews/nrco_citation_db.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE works(work_key TEXT,doi TEXT,title TEXT,year INT,abstract TEXT,abstract_source TEXT,"
                             "local_path TEXT,origin_url TEXT,fulltext_format TEXT);"
                             "CREATE TABLE papers(doi TEXT,title TEXT,year INT,path_xml TEXT);"
                             "CREATE TABLE bib_refs(doi TEXT,label TEXT,printed TEXT,work_key TEXT);")
            db.execute("INSERT INTO works VALUES(?,?,?,?,?,?,NULL,NULL,NULL)",
                       ("doi:10.1234/example", "10.1234/example", "Sample study", 2024, "Legacy field only.", "europepmc"))
            db.execute("INSERT INTO papers VALUES(?,?,?,?)", ("10.1234/review", "A review", 2024, "work/corpus/review.xml"))
            db.execute("INSERT INTO bib_refs VALUES(?,?,?,?)", ("10.1234/review", "1", "Printed reference", "doi:10.1234/example"))
        (legacy / "work/corpus").mkdir(parents=True)
        (legacy / "work/corpus/review.xml").write_text(article('<p>A finding <xref ref-type="bibr" rid="R1">1</xref>.</p>'))
        raw = {"id": "123", "source": "MED", "doi": "10.1234/example", "title": "Sample study", "pubYear": "2024",
               "abstractText": "Complete retained provider abstract."}
        (legacy / "work/raw/epmc_abs").mkdir(parents=True)
        (legacy / "work/raw/epmc_abs/batch.json").write_text(json.dumps(
            {"request": {"resultType": "core", "queryString": "DOI:10.1234/example", "pageSize": 1},
             "resultList": {"result": [raw]}}))
        import_legacy_evidence(seed, path, source_root=legacy)
        with seed.connect(write=True) as connection:
            method = connection.execute("SELECT sha256 FROM source_artifacts LIMIT 1").fetchone()[0]
            analysis = connection.execute("SELECT analysis_id FROM analyses WHERE analysis_id LIKE 'legacy-evidence:%'").fetchone()[0]
            connection.execute("INSERT INTO work_observations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                "old-score", analysis, "work-" + identity("doi:10.1234/example"),
                "legacy.citation_score", canonical({"row": {"abs_class": "sure_not_supported"}}), "legacy", method, None,
                "unresolved", now()))
        seed.library.snapshot("corpus-snapshot")
        projection = self.mount / "literature-evidence-2"
        build_projection(seed_root / "corpus-snapshot", projection, workers=1)
        return projection

    def study(self):
        repo = CorpusRepository.create(self.workspace, "work/literature", "Fresh question", "fresh", {})
        attach_store(repo, self.store_path)
        return CorpusRepository(self.workspace, "work/literature")

    def acquirer(self, transport):
        return CorpusAcquirer(self.workspace / "cache.sqlite3", ScholarlyClient(transport=transport, plan=EgressPlan(confidential=False)),
                              requests_per_second={"*": 100000})

    def test_store_is_verified_and_pinned_by_its_merkle_root(self):
        store = Store(self.store_path, level="deep")
        self.assertEqual(store.store_id, "literature-evidence@2")
        with self.assertRaisesRegex(ValidationError, "pinned"):
            Store(self.store_path, expected_root="0" * 64)
        repo = self.study()
        entry = repo.library.bundle["stores"][0]
        self.assertEqual((entry["merkleRoot"], entry["path"]), (store.merkle_root, str(self.store_path)))
        self.assertEqual(repo.store_client.stores[0].merkle_root, store.merkle_root)
        with sqlite3.connect(":memory:") as connection:
            store.attach(connection, "evidence")
            self.assertGreater(connection.execute("SELECT count(*) FROM evidence.works").fetchone()[0], 0)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM evidence.works")
        connection.close()

    def test_fresh_study_reuses_store_evidence_after_rechecking_provider_records(self):
        before = digest_file(self.store_path / "corpus.sqlite3")
        repo = self.study()
        review = repo.review({"doi": "10.1234/review", "title": "A review"})
        with repo.connect() as connection:
            selected = [dict(connection.execute("SELECT r.review_id,w.work_id,w.metadata_json FROM reviews r "
                                                "JOIN works w USING(work_id)").fetchone())]
        sources = retained_review_sources(repo, selected)
        self.assertEqual(len(sources[selected[0]["work_id"]]), 1)
        self.assertTrue(reuse_review(repo, selected[0], sources[selected[0]["work_id"]]))
        with repo.connect() as connection:
            entries = connection.execute("SELECT count(*) FROM bibliography_entries WHERE review_id=?", (review,)).fetchone()[0]
        self.assertGreater(entries, 0)
        with repo.connect(write=True) as db:
            db.execute("INSERT INTO bibliography_entries VALUES(?,?,?,?,?,NULL,'unresolved',NULL)", (
                "entry-new", review, "new", "Author. Sample study. Journal (2024).",
                canonical({"identifiers": {"doi": "10.1234/example"}, "identity": {"title": "Sample study", "year": 2024}})))
        transport = TitleTransport(fail=True)
        acquire_corpus(repo, self.acquirer(transport), workers=1)
        # The review's other references are not in the store and go to providers; the retained work never does.
        self.assertFalse([call for call in transport.calls if "10.1234/example" in json.dumps(call)])
        with repo.connect() as connection:
            row = connection.execute("SELECT identity_state, resolved_work_id FROM bibliography_entries WHERE entry_id='entry-new'").fetchone()
            adopted = connection.execute("SELECT count(*) FROM retrievals WHERE provider='store:literature-evidence@2'").fetchone()[0]
            legacy = connection.execute("SELECT count(*) FROM work_observations").fetchone()[0]
        self.assertEqual(row[0], "verified")
        evidence = repo.evidence(row[1], "abstract")
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["evidenceUnits"][0]["text"], "Complete retained provider abstract.")
        self.assertEqual(adopted, 1)
        self.assertEqual(legacy, 0)
        repo.library.verify()
        repo.library.snapshot("results/frozen")
        frozen = CorpusRepository(self.workspace, "results/frozen")
        self.assertEqual(frozen.evidence(row[1], "abstract")["evidenceUnits"][0]["text"], "Complete retained provider abstract.")
        self.assertEqual(digest_file(self.store_path / "corpus.sqlite3"), before)

    def test_study_rows_come_first_and_adoption_is_idempotent(self):
        repo = self.study()
        client = repo.store_client
        first = client.adopt_for_identifiers({"doi": "10.1234/example"})
        self.assertEqual(first["documents"], 2)
        self.assertIsNone(client.adopt_for_identifiers({"doi": "10.1234/example"}))
        self.assertIsNone(client.adopt_for_identifiers({"doi": "10.1234/absent"}))
        repo.library.verify()


if __name__ == "__main__":
    unittest.main()
