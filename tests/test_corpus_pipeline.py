# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import re
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import patch

from ocsl_review import corpus_pipeline
from ocsl_review.confidentiality import EgressPlan
from ocsl_review.corpus_acquisition import CorpusAcquirer
from ocsl_review.corpus_assessment import TaskPlanner
from ocsl_review.corpus_library import now
from ocsl_review.corpus_pipeline import _escalated_loci, acquire_corpus, acquire_escalations, prepare_tasks, register_acquisition
from ocsl_review.corpus_store import CorpusRepository, canonical, identity
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


def reference_plan(repo, tier="abstract", context_chars=100000):
    """The planner as it was before keyset streaming, frozen as a test oracle; it never writes."""
    analysis = repo.analysis()["analysis_id"]
    with repo.connect() as connection:
        rows = [dict(r) for r in connection.execute("SELECT l.*,b.resolved_work_id,b.identity_state,b.candidate_identifiers_json,b.review_id,w.metadata_json,c.checkpoint_json FROM claim_loci l JOIN bibliography_entries b USING(entry_id) LEFT JOIN works w ON w.work_id=b.resolved_work_id JOIN corpus_reviews c USING(review_id)")]
        done = {(r[0], r[1]) for r in connection.execute("SELECT locus_id,tier FROM corpus_tasks WHERE analysis_id=? AND state IN ('completed','pending','running')", (analysis,))}
        done.update((r[0], r[1]) for r in connection.execute("SELECT locus_id,evidence_level FROM assessments WHERE analysis_id=? AND validation_state='accepted'", (analysis,)))
        escalated = _escalated_loci(connection, analysis)
    planned = {}
    for row in rows:
        if (row["locus_id"], tier) in done:
            continue
        if tier == "full-text" and row["locus_id"] not in escalated:
            continue
        evidence = repo.evidence(row["resolved_work_id"], tier) if row["resolved_work_id"] else None
        reason = None if row["identity_state"] == "verified" else "cited work identity is " + row["identity_state"]
        if not evidence:
            reason = reason or tier + " evidence unavailable"
            evidence = {"evidenceTier": tier, "evidenceUnits": [], "complete": False}
        units = evidence["evidenceUnits"]
        if sum(len(u["text"]) for u in units) > context_chars:
            terms = set(re.findall(r"\w{4,}", row["claim_text"].lower()))
            ranked = sorted(enumerate(units), key=lambda p: (-len(terms & set(re.findall(r"\w{4,}", p[1]["text"].lower()))), p[0]))
            selected, used = [], 0
            for index, unit in ranked:
                remaining = context_chars - used
                if remaining <= 0:
                    break
                excerpt = unit if len(unit["text"]) <= remaining else {**unit, "text": unit["text"][:remaining], "excerpt": True}
                selected.append((index, excerpt)); used += len(excerpt["text"])
            evidence = {**evidence, "evidenceUnits": [u for _, u in sorted(selected)], "complete": False}
        try:
            locator = json.loads(row["locator"])
        except ValueError:
            locator = {"locator": row["locator"], "bindingStatus": "exact"}
        metadata = json.loads(row["metadata_json"] or "{}")
        cited_work = {key: metadata[key] for key in ("title", "year", "authors", "doi", "pmid", "pmcid", "journal")
                      if metadata.get(key) is not None}
        if not cited_work.get("journal") and metadata.get("container"):
            cited_work["journal"] = metadata["container"]
        spec = {**evidence, "claim": row["claim_text"], "context": json.loads(row["context_text"]),
                "citationLocus": {"id": row["locus_id"], **locator}, "claimType": locator.get("claimType", "background"),
                "citedWork": cited_work,
                "identifiers": json.loads(row["candidate_identifiers_json"]).get("identifiers", {}),
                "identityVerified": row["identity_state"] == "verified", "reviewId": row["review_id"],
                "reviewProof": json.loads(row["checkpoint_json"])}
        planned_task = TaskPlanner().plan({**spec, "id": row["locus_id"], "blockedReason": reason})
        planned[row["locus_id"]] = ("task-" + identity(analysis, row["locus_id"], tier, spec), reason or planned_task["reason"])
    return planned


class PlannerEquivalenceTests(unittest.TestCase):
    """The streamed planner must produce the task IDs and blocked reasons of the frozen planner."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/library", "Question", "v2", {"version": 2})
        repo = self.repo
        self.manuscript = repo.json_artifact({"confidential": False}, "review-manuscript")
        reviews = []
        for index in range(3):
            review = repo.review({"doi": f"10.1234/review-{index}", "title": f"Review {index}"})
            repo.review_state(review, "binding", "completed", checkpoint={
                "manuscriptSha256": self.manuscript, "provider": "europe-pmc", "public": True,
                "sourceUrl": f"https://europepmc.org/article/MED/{index}", "publication": {"doi": f"10.1234/review-{index}"}})
            reviews.append(review)
        works = {name: repo.work({"doi": f"10.1234/{name}", "title": f"Cited {name}", "year": 2020,
                                  **({"container": "Container journal"} if name == "w1" else {"journal": "Journal"})})
                 for name in ("w1", "w2", "w3", "w4", "w5")}
        repo.document(works["w1"], {"text": "Sixty patients were randomised to treatment.", "complete": True,
                                    "sourceUrl": "https://example.org/w1"}, "abstract", "europe-pmc")
        repo.document(works["w2"], {"text": "A partial abstract", "complete": False,
                                    "sourceUrl": "https://example.org/w2"}, "abstract", "pubmed")
        repo.document(works["w5"], {"text": "Complete abstract of a cohort of mice.", "complete": True,
                                    "sourceUrl": "https://example.org/w5a"}, "abstract", "europe-pmc")
        repo.document(works["w5"], {"text": "A newer but incomplete abstract.", "complete": False,
                                    "sourceUrl": "https://example.org/w5b"}, "abstract", "pubmed")
        blocks = [{"id": f"s{i}", "text": (f"Section {i} reports randomised patients outcome. " * 12), "kind": "body"}
                  for i in range(6)]
        repo.document(works["w4"], {"text": "", "blocks": blocks, "complete": True,
                                    "sourceUrl": "https://example.org/w4"}, "full-text", "europe-pmc")
        entries = [("e1", reviews[0], works["w1"], "verified"), ("e2", reviews[0], works["w2"], "verified"),
                   ("e3", reviews[1], works["w3"], "verified"), ("e4", reviews[1], works["w4"], "verified"),
                   ("e5", reviews[2], works["w5"], "verified"), ("e6", reviews[2], None, "unresolved"),
                   ("e7", reviews[2], works["w1"], "conflict")]
        with repo.connect(write=True) as connection:
            for entry, review, work, state in entries:
                connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", (
                    entry, review, entry, f"Printed {entry}", canonical({"identifiers": {"doi": f"10.1234/{entry}"}}),
                    work, state, self.manuscript if state == "verified" else None))
            loci = []
            for number, (entry, *_rest) in enumerate(entries * 2):
                locator = canonical({"claimType": "numeric" if number % 2 else "fact", "bindingStatus": "exact",
                                     "position": number}) if number % 5 else f"plain-locator-{number}"
                loci.append((f"locus-{number:02d}", entry, self.manuscript, locator,
                             f"Claim {number}: randomised patients outcome.", canonical([f"Context {number}."]), "fixture"))
            connection.executemany("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", loci)
        self.loci = [locus[0] for locus in loci]
        # Earlier progress: one completed task, one accepted assessment and one stale blocked task.
        repo.add_task("v2", self.loci[0], "abstract", {"claim": "earlier"}, None)
        with repo.connect(write=True) as connection:
            connection.execute("UPDATE corpus_tasks SET state='completed' WHERE locus_id=?", (self.loci[0],))
            connection.execute("INSERT INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                "receipt-1", "v2", "item", self.manuscript, self.manuscript, "fixture", "model", "{}", "{}",
                "completed", now(), now()))
            connection.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", (
                "assessment-1", "v2", self.loci[1], "receipt-1", "fixture", "abstract",
                canonical({"escalate": False}), "accepted", now()))
            connection.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", (
                "assessment-2", "v2", self.loci[3], "receipt-1", "fixture", "abstract",
                canonical({"escalate": True}), "accepted", now()))
        self.stale = repo.add_task("v2", self.loci[7], "abstract", {"claim": "stale"}, "abstract evidence unavailable")

    def assert_equivalent(self, tier, context_chars=100000):
        expected = reference_plan(self.repo, tier, context_chars)
        result = prepare_tasks(self.repo, tier=tier, context_chars=context_chars)
        self.assertEqual((result["tasks"], result["blocked"]),
                         (len(expected), sum(1 for _, reason in expected.values() if reason)))
        with self.repo.connect() as connection:
            rows = {row["task_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM corpus_tasks WHERE analysis_id='v2' AND tier=?", (tier,))}
        for locus, (task_id, reason) in expected.items():
            with self.subTest(locus=locus):
                self.assertEqual((rows[task_id]["locus_id"], rows[task_id]["reason"]), (locus, reason))
                self.assertEqual(rows[task_id]["state"], "blocked" if reason else "pending")
        return expected

    def test_streamed_pages_and_evicting_cache_keep_task_ids_and_reasons(self):
        with patch.object(corpus_pipeline, "PLANNER_PAGE_ROWS", 2), patch.object(corpus_pipeline, "EVIDENCE_CACHE_WORKS", 1):
            expected = self.assert_equivalent("abstract")
        self.assertNotIn(self.loci[0], expected)
        self.assertNotIn(self.loci[1], expected)
        reasons = {reason for _, reason in expected.values()}
        self.assertTrue({None, "complete abstract is unavailable", "abstract evidence unavailable",
                         "cited work identity is unresolved", "cited work identity is conflict"} <= reasons)
        with self.repo.connect() as connection:
            stale = connection.execute("SELECT state,reason FROM corpus_tasks WHERE task_id=?", (self.stale,)).fetchone()
        self.assertEqual(tuple(stale), ("cancelled", "superseded by acquired evidence"))
        self.assertEqual(prepare_tasks(self.repo)["tasks"], sum(1 for _, reason in expected.values() if reason))

    def test_full_text_window_and_escalations_match_and_evidence_loads_once_per_work(self):
        loads = []
        original = CorpusRepository.evidence_many

        def counting(repo, work_ids, tier, *, connection=None):
            work_ids = list(work_ids)
            loads.extend(work_ids)
            return original(repo, work_ids, tier, connection=connection)

        with patch.object(CorpusRepository, "evidence_many", counting):
            expected = self.assert_equivalent("full-text", context_chars=1000)
        self.assertIn(self.loci[3], expected)
        self.assertEqual(len(loads), len(set(loads)))
        task = next(t for t in self.repo.tasks("v2", tier="full-text") if t["task_id"] == expected[self.loci[3]][0])
        self.assertFalse(task["spec"]["complete"])
        self.assertEqual(sum(len(unit["text"]) for unit in task["spec"]["evidenceUnits"]), 1000)


class EscalationFetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = CorpusRepository.create(self.root, "work/library", "Question", "v2", {"version": 2})
        review = self.repo.review({"doi": "10.1234/review", "title": "Review"})
        self.repo.review_state(review, "binding", "completed")
        manuscript = self.repo.json_artifact({"confidential": False}, "review-manuscript")
        self.works = [self.repo.work({"doi": f"10.1234/w{i}", "title": f"Work {i}"}) for i in range(6)]
        with self.repo.connect(write=True) as connection:
            for index, work in enumerate(self.works):
                connection.execute("INSERT INTO bibliography_entries VALUES (?,?,?,?,?,?,?,?)", (
                    f"e{index}", review, str(index), "Printed", "{}", work, "verified", manuscript))
                connection.execute("INSERT INTO claim_loci VALUES (?,?,?,?,?,?,?)", (
                    f"l{index}", f"e{index}", manuscript, f"p{index}", "Claim.", "[]", "fixture"))

    class Acquirer:
        def __init__(self, parties, fail=()):
            self.barrier = threading.Barrier(parties, timeout=10) if parties > 1 else None
            self.fail, self.calls, self.lock = set(fail), [], threading.Lock()
            self.active = self.peak = 0

        def fetch_work(self, identifiers, include_full_text=False):
            with self.lock:
                self.calls.append(identifiers["doi"]); self.active += 1; self.peak = max(self.peak, self.active)
            try:
                if self.barrier:
                    self.barrier.wait()
                if identifiers["doi"] in self.fail:
                    from ocsl_review.errors import ProviderError
                    raise ProviderError("provider HTTP 503 temporary failure")
                return {"outcome": "acquired", "retrievedAt": "2026-01-01T00:00:00+00:00", "documents": [
                    {"text": "Full text of " + identifiers["doi"], "complete": True, "provider": "europe-pmc",
                     "sourceUrl": "https://example.org/" + identifiers["doi"]}]}
            finally:
                with self.lock:
                    self.active -= 1

    def test_escalated_works_are_fetched_concurrently_and_registered(self):
        acquirer = self.Acquirer(parties=3)
        result = acquire_escalations(self.repo, acquirer, workers=3)
        self.assertEqual(result, {"targetedWorks": 6, "acquisitionAttempts": 6})
        self.assertEqual(acquirer.peak, 3)
        self.assertTrue(all(self.repo.evidence(work, "full-text")["complete"] for work in self.works))
        self.assertEqual(acquire_escalations(self.repo, acquirer, workers=3)["acquisitionAttempts"], 0)

    def test_failed_fetch_keeps_finished_results_and_resume_repeats_only_missing_works(self):
        acquirer = self.Acquirer(parties=1, fail={"10.1234/w2"})
        with self.assertRaisesRegex(Exception, "503"):
            acquire_escalations(self.repo, acquirer, workers=2)
        stored = [work for work in self.works if self.repo.evidence(work, "full-text")]
        self.assertEqual(len(stored), 5)
        retry = self.Acquirer(parties=1)
        self.assertEqual(acquire_escalations(self.repo, retry, workers=2)["acquisitionAttempts"], 1)
        self.assertEqual(retry.calls, ["10.1234/w2"])


if __name__ == "__main__":
    unittest.main()
