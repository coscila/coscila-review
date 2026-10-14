from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ocsl_review import literature
from ocsl_review.audit import SCOPE_DIMENSIONS, build_audit_summary
from ocsl_review.claim_inventory import build_claim_inventory
from ocsl_review.cli import main
from ocsl_review.errors import ProviderError, ValidationError
from ocsl_review.evidence import abstract_candidate
from ocsl_review.fulltext import parse_source_document
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.scholarly import ScholarlyClient
from ocsl_review.util import atomic_json


TEXT = """# Literature review

## Background
Published observations show an association [1-2].

## References
1. Smith A. Source observations. Example Journal. 2024. doi:10.1234/example
2. Smith A. Source observations. Example Journal. 2024. doi:10.1234/example
"""


class FakeScholar:
    reconcile_reference_metadata = staticmethod(ScholarlyClient.reconcile_reference_metadata)
    reconcile_identifier_metadata = staticmethod(ScholarlyClient.reconcile_identifier_metadata)

    def __init__(self):
        self.acquisitions = 0
        self.resolutions = 0
        self.fail = False
        self.abstract_only = False

    def resolve_identifier(self, **identifiers):
        self.resolutions += 1
        return {"records": [
            {"provider": provider, "doi": "10.1234/example", "title": "Source observations",
             "pmid": "12345", "pmcid": "PMC12345",
             "authors": ["Smith A"], "year": 2024, "container": "Example Journal"}
            for provider in ("crossref", "europe-pmc")
        ], "limitations": []}

    def get_publication_status(self, **identifiers):
        return {"state": "unknown", "checkedAt": "2026-09-09T00:00:00Z",
                "providersChecked": ["crossref"], "signals": [],
                "limitations": ["positive-control-unavailable"]}

    def acquire_full_text(self, **identifiers):
        if len(identifiers) != 1:
            raise ProviderError("acquire_full_text requires exactly one identifier")
        self.acquisitions += 1
        if self.fail:
            raise ProviderError("synthetic outage")
        text = "Observed exposure was associated with the outcome.\n\nNo causal inference was made."
        abstract = " ".join(text.split())
        return {"provider": "synthetic", "documents": [] if self.abstract_only else [
            parse_source_document(text.encode(), "text/plain", "https://example.org/source")],
            "abstract": abstract, "abstractComplete": True, "abstractProvider": "europe-pmc",
            "abstractCandidates": [abstract_candidate({"provider": "europe-pmc", "doi": "10.1234/example", "abstract": abstract,
                                                        "rawMetadata": {"abstractText": abstract}},
                                                       source_url="https://example.org/structured-abstract", retrieved_at="2026-09-09T00:00:00Z")],
            "attempts": [], "limitations": []}


class LiteratureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "chat"
        self.workspace.mkdir()
        (self.workspace / "manuscript.md").write_text(TEXT)
        self.manuscript = ingest_manuscript(self.workspace / "manuscript.md")
        self.graph = build_claim_inventory(self.manuscript)
        atomic_json(self.workspace / ".ocsl-review/manuscript.json", self.manuscript, mode=0o600)
        self.state, self.graph = literature.prepare_literature(
            self.workspace, self.manuscript, self.graph)
        literature._save(self.workspace, self.state, self.graph, event="test-prepare")
        self.client = FakeScholar()

    def tearDown(self):
        self.temp.cleanup()

    def confirm(self):
        self.state["confirmation"] = {
            "decision": "accepted", "at": "2026-09-09T00:00:00Z",
            "scopeSha256": self.state["scopeSha256"], "basis": "User requested an extensive review."
        }

    def invoke(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([*args, "--workspace", str(self.workspace), "--json"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_confirmation_precedes_any_provider_call(self):
        with self.assertRaisesRegex(ValidationError, "confirmation"):
            literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual(self.client.resolutions, 0)
        self.confirm()
        self.state["confirmation"]["decision"] = "declined"
        with self.assertRaisesRegex(ValidationError, "confirmation"):
            literature.fetch_literature(self.workspace, self.state, self.graph, self.client)

    def test_every_reference_survives_shared_identifier_and_resume(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual(len(self.state["entries"]), 2)
        self.assertEqual(self.client.resolutions, 2)
        self.assertEqual(self.client.acquisitions, 1)
        self.assertEqual(self.state["entries"][0]["sourceId"], self.state["entries"][1]["sourceId"])
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual(self.client.resolutions, 2)
        status = literature.literature_status(self.state, self.graph)
        self.assertTrue(status["acquisitionAccountedFor"])
        self.assertEqual(status["uniqueDocumentsRead"], 0)
        self.assertEqual(status["summary"]["citedCorrectly"]["status"], "not-fully-verifiable")

    def test_outage_is_checkpointed_for_every_entry_and_can_retry(self):
        self.confirm()
        self.client.fail = True
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual([e["state"] for e in self.state["entries"]], ["failed", "failed"])
        self.client.fail = False
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client, retry=True)
        self.assertEqual([e["state"] for e in self.state["entries"]], ["available", "available"])
        self.assertTrue(all(len(e["attempts"]) == 2 for e in self.state["entries"]))

    def test_complete_abstract_is_preserved_at_lower_access(self):
        self.confirm()
        self.client.abstract_only = True
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        source = next(iter(self.state["sources"].values()))
        self.assertEqual(source["accessLevel"], "abstract-only")
        self.assertEqual(source["provider"], "europe-pmc")
        self.assertEqual(self.state["entries"][0]["abstractCandidates"][0]["provider"], "europe-pmc")
        self.assertTrue(source["complete"])
        self.assertEqual(source["readUnitIds"], [])
        self.assertEqual(source["sourceUrl"], "https://example.org/structured-abstract")
        self.assertEqual(source["identifiers"], {"doi": "10.1234/example"})

    def test_candidate_identifier_conflicts_do_not_fetch_wrong_publication_status(self):
        self.confirm()
        first = self.graph["references"][0]
        for name, wrong in (("pmid", "999"), ("pmcid", "PMC999")):
            with self.subTest(name=name):
                first["identifiers"] = {"doi": "10.1234/example", name: wrong}
                with patch.object(self.client, "get_publication_status") as status:
                    literature.fetch_literature(self.workspace, self.state, self.graph, self.client,
                                               reference_ids=[first["id"]], retry=True)
                status.assert_not_called()
                self.assertEqual(first["resolutionStatus"], "ambiguous")
                self.assertEqual(first["publicationStatus"]["state"], "unknown")
                self.assertEqual(self.state["entries"][0]["verifiedIdentifiers"], {})
                self.assertEqual(self.state["entries"][0]["candidateIdentifiers"][name], wrong)
                self.assertEqual(first["identifiers"][name], wrong)

    def test_verified_status_doi_and_abstract_reuse_do_not_overwrite_printed_identifiers(self):
        self.confirm()
        with patch.object(self.client, "get_publication_status", wraps=self.client.get_publication_status) as status:
            literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual([call.kwargs for call in status.call_args_list], [{"doi": "10.1234/example"}] * 2)
        self.assertEqual(self.graph["references"][0]["identifiers"], {"doi": "10.1234/example"})
        self.assertEqual(self.state["entries"][0]["verifiedIdentifiers"]["pmid"], "12345")
        self.assertEqual(self.state["entries"][0]["abstractCandidates"], self.state["entries"][1]["abstractCandidates"])

    def test_missing_candidate_identifier_cannot_become_verified_from_other_fields(self):
        records = self.client.resolve_identifier()["records"]
        for record in records:
            record.pop("pmid")
        self.graph["references"][0]["identifiers"]["pmid"] = "999"
        self.confirm()
        with patch.object(self.client, "resolve_identifier", return_value={"records": records, "limitations": []}), patch.object(self.client, "get_publication_status") as status:
            literature.fetch_literature(self.workspace, self.state, self.graph, self.client,
                                       reference_ids=[self.graph["references"][0]["id"]])
        status.assert_not_called()
        self.assertEqual(self.graph["references"][0]["identityEvidence"]["agreementStrength"], "weak")

    def test_retry_refreshes_old_abstract_and_deduplicates_only_new_download(self):
        self.confirm()
        self.client.abstract_only = True
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual(self.client.acquisitions, 1)
        self.client.abstract_only = False
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client, retry=True)
        self.assertEqual(self.client.acquisitions, 2)
        self.assertEqual({self.state["sources"][e["sourceId"]]["accessLevel"]
                          for e in self.state["entries"]}, {"full-text"})
        self.assertEqual(len({e["sourceId"] for e in self.state["entries"]}), 1)

    def test_interrupted_retry_checkpoints_pending_and_resumes_without_retry_flag(self):
        self.confirm()
        reference_id = self.state["entries"][0]["referenceId"]
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client,
                                     reference_ids=[reference_id])
        with patch.object(self.client, "acquire_full_text", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            literature.fetch_literature(self.workspace, self.state, self.graph, self.client,
                                         reference_ids=[reference_id], retry=True)
        self.state, self.graph = literature._load(SimpleNamespace(), self.workspace)
        entry = self.state["entries"][0]
        self.assertEqual(entry["state"], "pending")
        self.assertIsNone(entry["sourceId"])
        self.assertEqual(self.graph["references"][0]["acquisition"]["state"], "pending")
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual(self.client.acquisitions, 2)
        self.assertTrue(all(e["state"] == "available" and e["sourceId"]
                            for e in self.state["entries"]))

    def test_source_validation_precedes_artifact_writes(self):
        doc = parse_source_document(b"Example text.", "text/plain")
        with patch.object(literature, "MAX_STATE_BYTES", 8):
            with self.assertRaisesRegex(ValidationError, "size bound"):
                literature._store_source(self.workspace, self.state, doc,
                                         access_level="full-text", provider="local")
        doc["complete"] = "false"
        with self.assertRaisesRegex(ValidationError, "boolean"):
            literature._store_source(self.workspace, self.state, doc,
                                     access_level="full-text", provider="local")
        self.assertFalse((self.workspace / ".ocsl-review/literature/sources").exists())

    def test_changed_manuscript_invalidates_confirmation(self):
        self.confirm()
        self.manuscript["source"]["sha256"] = "b" * 64
        graph = copy.deepcopy(self.graph)
        graph["manuscript"]["sourceSha256"] = "b" * 64
        state, _ = literature.prepare_literature(self.workspace, self.manuscript, graph)
        self.assertIsNone(state["confirmation"])
        self.assertNotEqual(state["scopeSha256"], self.state["scopeSha256"])

    def test_record_reading_rejects_invented_units_and_does_not_judge(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        entry = self.state["entries"][0]
        with self.assertRaisesRegex(ValidationError, "unit absent"):
            literature.record_assessments(self.workspace, self.state, self.graph,
                {"referenceId": entry["referenceId"], "readUnitIds": ["invented"]})
        source = self.state["sources"][entry["sourceId"]]
        literature.record_assessments(self.workspace, self.state, self.graph,
            {"referenceId": entry["referenceId"], "readUnitIds": source["expectedUnitIds"]})
        self.assertEqual(self.graph["verificationLinks"], [])
        summary = build_audit_summary(self.graph)
        self.assertEqual(summary["citedCorrectly"]["status"], "not-fully-verifiable")
        self.assertGreater(summary["auditCoverage"]["sourcesFullTextChecked"], 0)

    def test_source_tampering_and_stale_graph_prevent_publication(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        literature.guard_graph(self.workspace, self.graph)
        stale = copy.deepcopy(self.graph)
        stale["references"][0]["raw"] += " edited"
        with self.assertRaisesRegex(ValidationError, "stale"):
            literature.guard_graph(self.workspace, stale)
        source = next(iter(self.state["sources"].values()))
        (self.workspace / source["artifactPath"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValidationError, "changed"):
            literature.guard_graph(self.workspace, self.graph)

    def test_source_paths_and_shared_inodes_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, "escapes"):
            literature._path(self.workspace, "../outside")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "symlink"):
            literature._path(self.workspace, "escape/file")
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        source = next(iter(self.state["sources"].values()))
        os.link(self.workspace / source["artifactPath"], outside / "shared")
        with self.assertRaisesRegex(ValidationError, "inodes"):
            literature._verified_source(self.workspace, source)

    def test_cli_confirm_checks_scope_and_persists_decision(self):
        code, _, error = self.invoke("review", "literature", "confirm", "--accept",
            "--scope-sha256", "a" * 64, "--basis", "User agreed")
        self.assertEqual(code, 5, error)
        code, out, error = self.invoke("review", "literature", "confirm", "--accept",
            "--scope-sha256", self.state["scopeSha256"], "--basis", "User agreed")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(out)["confirmation"]["decision"], "accepted")

    def test_cli_fetch_requires_exact_configured_workspace(self):
        self.confirm()
        literature._save(self.workspace, self.state, self.graph, event="test-confirm")
        with patch.dict(os.environ, {}, clear=True):
            code, _, error = self.invoke("review", "literature", "fetch")
        self.assertEqual(code, 5, error)
        self.assertIn("bound to the selected workspace", error)

    def link_record(self, reference_id, *, verdict="direct-support", label="very-confident"):
        entry = literature._entry(self.state, reference_id)
        source = self.state["sources"][entry["sourceId"]]
        document = literature._verified_source(self.workspace, source)
        return {"referenceId": reference_id, "readUnitIds": source["expectedUnitIds"],
                "verificationLinks": [{
                    "claimId": self.graph["claims"][0]["id"],
                    "citationClusterId": self.graph["citationClusters"][0]["id"],
                    "supportVerdict": verdict,
                    "supportReason": "The examined passage explicitly reports the association.",
                    "scopeComparison": {key: "not-applicable" for key in SCOPE_DIMENSIONS},
                    "quotationOrNumberCheck": "not-applicable", "confidence": 0.95,
                    "confidenceLabel": label, "abstentionReason": None,
                    "humanAction": "Retain the bounded association claim.",
                    "evidenceLocations": [{"unitId": document["blocks"][0]["id"],
                                           "excerpt": "Observed exposure was associated with the outcome."}],
                }]}

    def test_correctness_is_independent_of_missing_completeness_assessment(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        for entry in self.state["entries"]:
            record = self.link_record(entry["referenceId"])
            literature.record_assessments(self.workspace, self.state, self.graph, record)
        summary = build_audit_summary(self.graph)
        self.assertEqual(summary["citedCorrectly"]["status"], "yes")
        self.assertEqual(summary["citedEnough"]["status"], "not-fully-verifiable")

    def test_abstract_support_requires_explicit_appropriate_claim_classification(self):
        self.confirm()
        self.client.abstract_only = True
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        reference_id = self.state["entries"][0]["referenceId"]
        record = self.link_record(reference_id, label="confident")
        with self.assertRaises(ValidationError):
            literature.record_assessments(self.workspace, copy.deepcopy(self.state), copy.deepcopy(self.graph), record)
        record["claimRequirements"] = [{"claimId": self.graph["claims"][0]["id"],
            "requiredEvidenceLevel": "abstract-sufficient",
            "reason": "This is a qualitative association explicitly stated in the complete abstract."}]
        literature.record_assessments(self.workspace, self.state, self.graph, record)
        self.assertEqual(self.graph["verificationLinks"][0]["confidenceLabel"], "confident")
        inflated = copy.deepcopy(record)
        inflated["verificationLinks"][0]["confidenceLabel"] = "very-confident"
        with self.assertRaisesRegex(ValidationError, "confidence exceeds"):
            literature.record_assessments(self.workspace, copy.deepcopy(self.state), copy.deepcopy(self.graph), inflated)

    def test_invented_excerpt_is_rejected_against_source_bytes(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        record = self.link_record(self.state["entries"][0]["referenceId"])
        record["verificationLinks"][0]["evidenceLocations"][0]["excerpt"] = "An invented causal result."
        with self.assertRaisesRegex(ValidationError, "not present"):
            literature.record_assessments(self.workspace, self.state, self.graph, record)

    def test_evidence_requirement_change_invalidates_dependent_review_assertion(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        reference_id = self.state["entries"][0]["referenceId"]
        literature.record_assessments(self.workspace, self.state, self.graph,
                                       self.link_record(reference_id))
        link_id = self.graph["verificationLinks"][0]["id"]
        assertions = [
            {"id": "association-support", "text": "The source supports the association.",
             "type": "factual", "citationIds": [link_id], "supportStatus": "supported"},
            {"id": "editorial-view", "text": "The explanation should be clearer.",
             "type": "editorial-opinion", "citationIds": [], "supportStatus": "opinion"},
        ]
        literature.record_assessments(self.workspace, self.state, self.graph,
                                       {"reviewAssertions": assertions})
        literature.record_assessments(self.workspace, self.state, self.graph, {
            "claimRequirements": [{"claimId": self.graph["claims"][0]["id"],
                "requiredEvidenceLevel": "abstract-sufficient",
                "reason": "The qualitative association is stated in the abstract."}],
        })
        self.assertEqual(self.graph["verificationLinks"], [])
        self.assertEqual(self.graph["reviewAssertions"], [assertions[1]])
        self.assertFalse(self.graph["claims"][0]["processed"])

    def test_gap_sources_keep_bibliography_denominators_and_cannot_be_cited_support(self):
        self.confirm()
        literature.add_gap_sources(self.state, self.graph, ["10.1234/example"], [self.graph["claims"][0]["id"]])
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        status = literature.literature_status(self.state, self.graph)
        self.assertEqual(status["bibliographyEntries"], 2)
        self.assertEqual(status["additionalReferences"], 1)
        self.assertEqual(status["summary"]["auditCoverage"]["bibliographyEntriesTotal"], 2)
        additional = self.graph["additionalReferences"][0]["id"]
        record = self.link_record(additional)
        with self.assertRaisesRegex(ValidationError, "binding"):
            literature.record_assessments(self.workspace, self.state, self.graph, record)

    def test_local_gap_metadata_reconciles_the_candidate_doi_without_printed_title(self):
        self.confirm()
        literature.add_gap_sources(self.state, self.graph, ["10.1234/example"],
                                    [self.graph["claims"][0]["id"]])
        reference = self.graph["additionalReferences"][0]
        records = self.client.resolve_identifier(doi="10.1234/example")["records"]
        literature.record_assessments(self.workspace, self.state, self.graph,
            {"referenceId": reference["id"], "referenceRecords": records})
        self.assertEqual(reference["resolutionStatus"], "resolved")
        self.assertEqual(reference["identityEvidence"]["agreementStrength"], "strong")
        conflicting = [{**item, "doi": "10.1234/different"} for item in records]
        literature.record_assessments(self.workspace, self.state, self.graph,
            {"referenceId": reference["id"], "referenceRecords": conflicting})
        self.assertEqual(reference["resolutionStatus"], "ambiguous")
        self.assertEqual(reference["identityEvidence"]["agreementStrength"], "conflict")

    def test_local_attachment_copies_bytes_instead_of_sharing_inodes(self):
        self.confirm()
        literature._save(self.workspace, self.state, self.graph, event="test-confirm")
        original = self.workspace / "paper.txt"
        original.write_text("The complete supplied paper text.")
        code, out, error = self.invoke("review", "literature", "attach", "paper.txt",
                                      "--reference-id", self.state["entries"][0]["referenceId"])
        self.assertEqual(code, 0, error)
        source = json.loads(out)["sources"][0]["source"]
        attached = self.workspace / source["artifactPath"]
        self.assertEqual(attached.read_bytes(), original.read_bytes())
        self.assertNotEqual(attached.stat().st_ino, original.stat().st_ino)
        original.write_text("changed later")
        self.assertNotEqual(attached.read_bytes(), original.read_bytes())

    def test_local_attachment_rejects_conflicting_identifiers_before_source_writes(self):
        self.confirm()
        literature._save(self.workspace, self.state, self.graph, event="test-confirm")
        reference_id = self.state["entries"][0]["referenceId"]
        for kind, expected, conflicting in (("doi", "10.1234/example", "10.1234/other"),
                                            ("pmid", "12345", "99999"),
                                            ("pmcid", "PMC12345", "PMC99999")):
            with self.subTest(kind=kind):
                self.graph["references"][0]["identifiers"][kind] = expected
                literature._save(self.workspace, self.state, self.graph, event="test-identifiers")
                self.workspace.joinpath("paper.xml").write_text(
                    f'<article><front><article-meta><article-id pub-id-type="{kind}">{conflicting}'
                    '</article-id></article-meta></front><body><p>Complete study text.</p></body></article>')
                code, _, error = self.invoke("review", "literature", "attach", "paper.xml",
                                             "--reference-id", reference_id)
                self.assertEqual(code, 5, error)
                self.assertIn("different source", error)
                self.assertFalse((self.workspace / ".ocsl-review/literature/sources").exists())

    def test_local_attachment_clears_download_identity_and_cannot_seed_fetch_reuse(self):
        self.confirm()
        first_id = self.state["entries"][0]["referenceId"]
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client,
                                     reference_ids=[first_id])
        self.workspace.joinpath("local.txt").write_text("The complete locally supplied source.")
        code, _, error = self.invoke("review", "literature", "attach", "local.txt",
                                     "--reference-id", first_id)
        self.assertEqual(code, 0, error)
        self.state, self.graph = literature._load(SimpleNamespace(), self.workspace)
        entry = self.state["entries"][0]
        self.assertNotIn("acquiredIdentifiers", entry)
        self.assertTrue(any("supplied attachment" in message for message in entry["limitations"]))
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        self.assertEqual(self.client.acquisitions, 2)
        self.assertNotEqual(self.state["entries"][0]["sourceId"], self.state["entries"][1]["sourceId"])

    def test_local_attachment_accepts_equivalent_doi_and_pmc_identifiers(self):
        self.confirm()
        first_id = self.state["entries"][0]["referenceId"]
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client,
                                     reference_ids=[first_id])
        self.workspace.joinpath("paper.xml").write_text(
            '<article><front><article-meta><article-id pub-id-type="doi">https://doi.org/10.1234/EXAMPLE</article-id>'
            '<article-id pub-id-type="pmc">12345</article-id></article-meta></front>'
            '<body><p>Complete study text.</p></body></article>')
        code, _, error = self.invoke("review", "literature", "attach", "paper.xml",
                                     "--reference-id", first_id)
        self.assertEqual(code, 0, error)
        self.state, self.graph = literature._load(SimpleNamespace(), self.workspace)
        self.assertEqual(self.state["entries"][0]["limitations"], [])


if __name__ == "__main__":
    unittest.main()
