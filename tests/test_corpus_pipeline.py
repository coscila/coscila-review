# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import tempfile
import sqlite3
import unittest

from ocsl_review.confidentiality import EgressPlan
from ocsl_review.corpus_acquisition import CorpusAcquirer
from ocsl_review.corpus_pipeline import acquire_corpus, acquire_escalations, prepare_tasks, register_acquisition
from ocsl_review.corpus_store import CorpusRepository, canonical
from ocsl_review.corpus_seed import import_legacy_evidence
from ocsl_review.errors import ProviderError, ValidationError
from ocsl_review.scholarly import RawResponse, ScholarlyClient
from test_corpus_acquisition import PublisherTransport
from test_fulltext import SourceTransport


class TitleTransport(SourceTransport):
    def __init__(self, *, ambiguous=False, fail=False):
        super().__init__()
        self.ambiguous, self.fail = ambiguous, fail

    def get(self, url, *, params=None, headers=None):
        if self.fail:
            self.calls.append((url, params or {}, headers or {}))
            raise ProviderError("provider HTTP 503 temporary failure")
        if url.endswith("/works"):
            self.calls.append((url, params or {}, headers or {}))
            item = {"DOI": "10.1234/example", "title": ["Sample study"], "published": {"date-parts": [[2024]]}}
            return RawResponse(json.dumps({"message": {"items": [item, {**item, "DOI": "10.1234/other"}] if self.ambiguous else [item]}}).encode(), 200, {})
        return super().get(url, params=params, headers=headers)


class CorpusPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/library", "Question", "v2", {"version": 2})
        self.review = self.repo.review({"doi": "10.1234/review", "title": "A review"})
        self.repo.review_state(self.review, "binding", "completed")

    def acquirer(self, transport):
        return CorpusAcquirer(self.root / "cache.sqlite3", ScholarlyClient(transport=transport, plan=EgressPlan(confidential=False)), requests_per_second={"*": 100000, "api.crossref.org": 100000, "www.ebi.ac.uk": 100000, "eutils.ncbi.nlm.nih.gov": 100000})

    def entry(self, name, *, identifiers=None, title="Sample study", year=2024, raw=None, locus=False):
        identifiers = {"doi": "10.1234/example"} if identifiers is None else identifiers
        value = {"identifiers": identifiers, "identity": {"title": title, "year": year, "container": "Wrong journal abbreviation", "volume": "999"}}
        with self.repo.connect(write=True) as db:
            db.execute("INSERT INTO bibliography_entries VALUES(?,?,?,?,?,NULL,'unresolved',NULL)", (name, self.review, name, raw or f"Author. {title}. Journal (2024).", canonical(value)))
        if locus:
            sha = self.repo.artifact(b"Original review with exact claim", "review")
            with self.repo.connect(write=True) as db:
                db.execute("INSERT INTO claim_loci VALUES(?,?,?,?,?,?,?)", ("locus-" + name, name, sha, canonical({"bindingStatus": "exact", "claimType": "numeric"}), "An observed result.", "[]", "test"))
        return name

    def rows(self):
        with self.repo.connect() as db:
            return {r["entry_id"]: dict(r) for r in db.execute("SELECT * FROM bibliography_entries")}

    def legacy_abstract(self, *, structured=True, conflicting=False):
        root = self.root / "legacy"
        (root / "work/reviews").mkdir(parents=True)
        path = root / "work/reviews/nrco_citation_db.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE works(work_key TEXT,doi TEXT,title TEXT,year INT,abstract TEXT,abstract_source TEXT,local_path TEXT,origin_url TEXT,fulltext_format TEXT);"
                             "CREATE TABLE papers(doi TEXT,title TEXT,year INT,path_xml TEXT);"
                             "CREATE TABLE bib_refs(doi TEXT,label TEXT,printed TEXT,work_key TEXT);")
            db.execute("INSERT INTO works VALUES(?,?,?,?,?,?,NULL,NULL,NULL)", ("doi:10.1234/example", "10.1234/example", "Sample study", 2024, "Legacy field only.", "europepmc"))
        if structured:
            directory = root / "work/raw/epmc_abs"
            directory.mkdir(parents=True)
            raw = {"id": "123", "source": "MED", "doi": "10.1234/example",
                   "title": "Conflicting work" if conflicting else "Sample study", "pubYear": "2024",
                   "abstractText": "Complete retained provider abstract."}
            payload = {"request": {"resultType": "core", "queryString": "DOI:10.1234/example", "pageSize": 1},
                       "resultList": {"result": [raw]}}
            (directory / "batch.json").write_text(json.dumps(payload))
        return import_legacy_evidence(self.repo, path, source_root=root)

    def test_imported_complete_provider_evidence_is_reused_before_doi_prefetch(self):
        self.legacy_abstract()
        self.entry("one", locus=True)
        self.entry("conflict", identifiers={"doi": "10.1234/example", "pmid": "999"})
        transport = TitleTransport(fail=True)
        acquire_corpus(self.repo, self.acquirer(transport), workers=2)
        self.assertEqual([], transport.calls)
        self.assertEqual("verified", self.rows()["one"]["identity_state"])
        self.assertEqual("conflict", self.rows()["conflict"]["identity_state"])
        evidence = self.repo.evidence(self.rows()["one"]["resolved_work_id"], "abstract")
        self.assertTrue(evidence["complete"])
        self.assertEqual("Complete retained provider abstract.", evidence["evidenceUnits"][0]["text"])
        with self.repo.connect() as db:
            receipts = [json.loads(self.repo.read_artifact(r[0])) for r in db.execute("SELECT artifact_sha256 FROM retrievals")]
        reused = next(r for r in receipts if r.get("operation") == "reuse_retained_abstract")
        self.assertEqual("retained-provider-record-only", reused["publicationStatus"]["coverage"])
        self.repo.verify_artifact(reused["retainedEvidence"]["sourceSha256"])

    def test_unverified_legacy_abstract_field_and_conflicting_provider_identity_require_fetch(self):
        for structured in (False, True):
            with self.subTest(structured=structured):
                # Each subtest gets its own store and imported seed.
                with tempfile.TemporaryDirectory() as directory:
                    old_root, old_repo, old_review = self.root, self.repo, self.review
                    try:
                        self.root = Path(directory)
                        self.repo = CorpusRepository.create(self.root, "work/library", "Q", "v2", {})
                        self.review = self.repo.review({"doi": "10.1234/review", "title": "Review"})
                        self.repo.review_state(self.review, "binding", "completed")
                        self.legacy_abstract(structured=structured, conflicting=True)
                        self.entry("one")
                        transport = TitleTransport()
                        acquire_corpus(self.repo, self.acquirer(transport), workers=1)
                        self.assertTrue(transport.calls)
                        self.assertEqual("verified", self.rows()["one"]["identity_state"])
                    finally:
                        self.root, self.repo, self.review = old_root, old_repo, old_review

    def test_portable_v2_provider_receipt_reuse_keeps_original_artifacts(self):
        self.entry("one", identifiers={"doi": "10.1234/example", "pmid": "123"})
        acquire_corpus(self.repo, self.acquirer(TitleTransport()), workers=1)
        self.repo.library.snapshot("results/snapshot")
        CorpusRepository(self.root, "results/snapshot").library.branch("work/fork", "New question", "fork", {})
        self.repo = CorpusRepository(self.root, "work/fork")
        self.entry("new-occurrence", identifiers={"pmid": "123"})
        transport = TitleTransport(fail=True)
        acquire_corpus(self.repo, self.acquirer(transport), workers=1)
        self.assertEqual([], transport.calls)
        self.assertEqual("verified", self.rows()["new-occurrence"]["identity_state"])
        self.repo.library.snapshot("results/fork")
        CorpusRepository(self.root, "results/fork").library.verify(require_snapshot=True)

    def test_abstract_acquisition_reuses_work_and_survives_minor_bibliographic_disagreement(self):
        self.entry("one", locus=True)
        self.entry("two", locus=True)
        transport = TitleTransport()
        acquirer = self.acquirer(transport)
        acquire_corpus(self.repo, acquirer, workers=2)
        rows = self.rows()
        self.assertEqual({"verified"}, {r["identity_state"] for r in rows.values()})
        self.assertEqual(1, len({r["resolved_work_id"] for r in rows.values()}))
        self.assertFalse(any("fullTextXML" in call[0] for call in transport.calls))
        evidence = self.repo.evidence(rows["one"]["resolved_work_id"], "abstract")
        self.assertTrue(evidence["complete"])
        self.assertIn("europepmc", evidence["sourceUrl"])
        initial_calls = len(transport.calls)
        acquire_corpus(self.repo, acquirer, workers=2)
        self.assertEqual(initial_calls, len(transport.calls))
        self.assertEqual(2, prepare_tasks(self.repo)["tasks"])
        self.assertEqual(0, prepare_tasks(self.repo)["tasks"])
        self.assertEqual(2, len(self.repo.tasks("v2")))
        self.repo.library.verify()

    def test_per_entry_conflicting_secondary_id_is_not_overwritten_by_shared_doi(self):
        self.entry("right")
        self.entry("conflict", identifiers={"doi": "10.1234/example", "pmid": "999"})
        acquire_corpus(self.repo, self.acquirer(TitleTransport()), workers=2)
        rows = self.rows()
        self.assertEqual("verified", rows["right"]["identity_state"])
        self.assertEqual("conflict", rows["conflict"]["identity_state"])
        with self.repo.connect() as db:
            self.assertIsNone(db.execute("SELECT * FROM work_identifiers WHERE scheme='pmid' AND value='999'").fetchone())

    def test_worker_cited_work_has_only_bibliographic_metadata(self):
        self.entry("one", locus=True)
        acquire_corpus(self.repo, self.acquirer(TitleTransport()), workers=1)
        work = self.rows()["one"]["resolved_work_id"]
        with self.repo.connect(write=True) as db:
            metadata = json.loads(db.execute("SELECT metadata_json FROM works WHERE work_id=?", (work,)).fetchone()[0])
            metadata.update(raw={"abstractText": "duplicated raw evidence"}, abstract="duplicated abstract", container="Journal")
            db.execute("UPDATE works SET metadata_json=? WHERE work_id=?", (canonical(metadata), work))
        prepare_tasks(self.repo)
        spec = self.repo.tasks("v2")[0]["spec"]
        self.assertLessEqual(set(spec["citedWork"]), {"title", "year", "authors", "doi", "pmid", "pmcid", "journal"})
        self.assertEqual(spec["citedWork"]["journal"], "Journal")
        self.assertTrue(spec["evidenceUnits"])
        self.assertNotIn("duplicated", canonical(spec["citedWork"]))

    def test_provider_title_conflict_is_not_a_minor_metadata_discrepancy(self):
        class ConflictingTitle(TitleTransport):
            def get(self, url, *, params=None, headers=None):
                response = super().get(url, params=params, headers=headers)
                if "europepmc" in url and url.endswith("/search"):
                    value = response.json()
                    value["resultList"]["result"][0]["title"] = "A different study"
                    return RawResponse(json.dumps(value).encode(), 200, {})
                return response
        self.entry("one")
        acquire_corpus(self.repo, self.acquirer(ConflictingTitle()), workers=1)
        self.assertEqual("conflict", self.rows()["one"]["identity_state"])

    def test_missing_identifiers_use_exact_title_year_and_shared_normalized_lookup(self):
        self.entry("one", identifiers={}, title="Sample study")
        self.entry("two", identifiers={}, title="SAMPLE—STUDY")
        transport = TitleTransport()
        acquire_corpus(self.repo, self.acquirer(transport), workers=2)
        self.assertEqual({"verified"}, {r["identity_state"] for r in self.rows().values()})
        queries = [call for call in transport.calls if call[0].endswith("/works")]
        self.assertEqual(1, len(queries))
        self.assertEqual(5, queries[0][1]["rows"])
        self.assertIn("sample study", queries[0][1]["query.bibliographic"])

    def test_raw_reference_fallback_requires_title_and_year_and_preserves_ambiguity(self):
        self.entry("one", identifiers={}, title=None, year=None, raw="Smith A. Sample study. Example Journal 2024;3:15-20.")
        result = self.acquirer(TitleTransport(ambiguous=True))
        acquire_corpus(self.repo, result, workers=1)
        self.assertEqual("conflict", self.rows()["one"]["identity_state"])
        with self.repo.connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM work_identifiers").fetchone()[0])

    def test_transient_lookup_is_retried_on_resume_not_cached_as_unavailable(self):
        self.entry("one")
        transport = TitleTransport(fail=True)
        acquirer = self.acquirer(transport)
        acquire_corpus(self.repo, acquirer, workers=1)
        self.assertEqual("unresolved", self.rows()["one"]["identity_state"])
        transport.fail = False
        acquire_corpus(self.repo, acquirer, workers=1)
        self.assertEqual("verified", self.rows()["one"]["identity_state"])

    def test_publisher_original_bytes_and_transformed_fulltext_are_retained(self):
        acquirer = self.acquirer(PublisherTransport())
        result = acquirer.fetch_review({"doi": "10.1234/example"})
        work = self.repo.work({"doi": "10.1234/example"})
        register_acquisition(self.repo, work, result)
        source = result["document"]
        self.assertEqual(source["originalBody"], self.repo.read_artifact(hashlib.sha256(source["originalBody"]).hexdigest()))
        self.assertEqual(source["body"], self.repo.read_artifact(hashlib.sha256(source["body"]).hexdigest()))
        evidence = self.repo.evidence(work, "full-text")
        self.assertTrue(any("The table finding" in item["text"] for item in evidence["evidenceUnits"]))
        self.repo.library.verify()

    def test_hash_mismatch_stops_before_registering_invalid_response(self):
        path = self.root / "response.json"
        path.write_bytes(b"changed")
        work = self.repo.work({"doi": "10.1234/example"})
        with self.assertRaisesRegex(ValidationError, "hash mismatch"):
            register_acquisition(self.repo, work, {"cacheProvenance": [{"artifactPath": str(path), "sha256": "a" * 64}]})
        with self.repo.connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM retrievals").fetchone()[0])

    def test_absent_abstract_escalates_only_known_work_and_preserves_long_text_window(self):
        self.entry("one", locus=True)
        transport = SourceTransport(abstract="")
        acquirer = self.acquirer(transport)
        acquire_corpus(self.repo, acquirer, workers=1)
        self.assertEqual(1, prepare_tasks(self.repo)["blocked"])
        self.assertEqual(1, acquire_escalations(self.repo, acquirer)["acquisitionAttempts"])
        self.assertEqual(0, acquire_escalations(self.repo, acquirer)["acquisitionAttempts"])
        planned = prepare_tasks(self.repo, tier="full-text", context_chars=1000)
        self.assertEqual(1, planned["tasks"])
        self.assertEqual(0, planned["blocked"])
        task = self.repo.tasks("v2", tier="full-text")[0]["spec"]
        self.assertFalse(task["complete"])
        self.assertEqual(1000, sum(len(unit["text"]) for unit in task["evidenceUnits"]))


if __name__ == "__main__":
    unittest.main()
