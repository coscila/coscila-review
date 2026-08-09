from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from ocsl_review.audit import build_audit_summary, validate_audit_graph
from ocsl_review.confidentiality import EgressPlan, authorize_egress
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.util import sha256_file

NOW = datetime.now(UTC).isoformat()
SHA = "0" * 64


def scope(value="match"):
    return {
        "species": value,
        "population": value,
        "exposureOrIntervention": value,
        "comparator": "not-applicable",
        "outcome": value,
        "direction": value,
        "magnitude": "unclear",
        "causalStrength": value,
        "setting": "unclear",
        "timeframe": "unclear",
    }


def graph():
    return {
        "schemaVersion": "1.0",
        "auditId": "audit-1",
        "createdAt": NOW,
        "manuscript": {
            "id": "ms-1",
            "sourceSha256": SHA,
            "confidential": True,
            "ingestionComplete": True,
            "ingestionIssues": [],
            "sectionsTotal": 2,
            "sectionsProcessed": 2,
        },
        "auditScope": {
            "mode": "exhaustive",
            "citationBindingComplete": True,
            "searchCurrentThrough": "2026-08-07",
        },
        "claims": [
            {
                "id": "c1",
                "locator": {
                    "section": "Introduction",
                    "paragraph": 1,
                    "sentence": 1,
                    "clause": 1,
                    "page": 1,
                    "anchor": "p1.s1.c1",
                },
                "text": "The intervention improves the outcome.",
                "origin": "external-background",
                "claimType": "causal",
                "citationRequired": True,
                "citationRequirementReason": "external causal claim",
                "citationClusterIds": ["cc1"],
                "citationBindingStatus": "exact",
                "requiredEvidenceLevel": "full-text",
                "processed": True,
            }
        ],
        "citationClusters": [
            {
                "id": "cc1",
                "locator": {
                    "section": "Introduction",
                    "paragraph": 1,
                    "sentence": 1,
                    "clause": 1,
                    "page": 1,
                    "anchor": "p1.s1.c1",
                },
                "raw": "[1]",
                "markers": ["1"],
                "referenceIds": ["r1"],
                "processed": True,
            }
        ],
        "references": [
            {
                "id": "r1",
                "raw": "Example citation",
                "identifiers": {"doi": "10.1/x"},
                "identity": {
                    "title": "A",
                    "authors": ["Example"],
                    "year": 2025,
                    "container": "Journal",
                    "version": "version-of-record",
                },
                "resolutionStatus": "resolved",
                "publicationStatus": {
                    "state": "current",
                    "checkedAt": NOW,
                    "providers": ["crossref", "pubmed"],
                    "details": [],
                },
                "sourceRole": "primary",
                "independenceGroup": "study-1",
                "metadataProblems": [],
            }
        ],
        "verificationLinks": [
            {
                "id": "v1",
                "claimId": "c1",
                "referenceId": "r1",
                "access": {
                    "level": "full-text",
                    "provider": "publisher",
                    "uri": "https://example.invalid",
                    "checkedAt": NOW,
                },
                "evidenceLocations": [{"locator": "Results, paragraph 2", "excerptSha256": SHA}],
                "supportVerdict": "direct-support",
                "supportReason": "aligned result",
                "scopeComparison": scope(),
                "quotationOrNumberCheck": "not-applicable",
                "confidence": 0.91,
                "abstentionReason": None,
                "humanAction": "Retain citation.",
            }
        ],
        "completenessAssessments": [
            {
                "claimId": "c1",
                "verdict": "sufficient",
                "reasons": ["direct and in scope"],
                "searchRunIds": [],
                "requiredHumanAction": "None.",
            }
        ],
        "searchRuns": [],
        "reviewAssertions": [],
        "provenance": {
            "toolVersion": "0.1.0",
            "schemaSha256": sha256_file(
                __import__("pathlib").Path(__file__).resolve().parents[1]
                / "schemas"
                / "claim-citation-audit.schema.json"
            ),
            "createdBy": "test",
            "commands": [],
        },
    }


class AuditTests(unittest.TestCase):
    def test_complete_supported_graph_can_answer_yes(self):
        summary = build_audit_summary(graph())
        self.assertEqual(summary["citedCorrectly"]["status"], "yes")
        self.assertEqual(summary["citedEnough"]["status"], "yes")
        self.assertEqual(summary["auditCoverage"]["citationRequiredClaimsTotal"], 1)

    def test_sampling_forbids_yes(self):
        value = graph()
        value["auditScope"]["mode"] = "sample"
        summary = build_audit_summary(value)
        self.assertEqual(summary["citedCorrectly"]["status"], "not-fully-verifiable")
        self.assertIn("sampled-audit", summary["verificationLimits"])

    def test_abstract_cannot_support_full_text_question(self):
        value = graph()
        value["verificationLinks"][0]["access"]["level"] = "abstract-only"
        with self.assertRaises(ValidationError):
            validate_audit_graph(value)

    def test_direct_support_rejects_scope_mismatch(self):
        value = graph()
        value["verificationLinks"][0]["scopeComparison"]["species"] = "mismatch"
        with self.assertRaises(ValidationError):
            validate_audit_graph(value)

    def test_inaccessible_source_requires_abstention_and_blocks_yes(self):
        value = graph()
        link = value["verificationLinks"][0]
        link["access"]["level"] = "inaccessible"
        link["supportVerdict"] = "abstain"
        link["supportReason"] = "source unavailable"
        link["evidenceLocations"] = []
        link["confidence"] = 0.2
        link["abstentionReason"] = "No lawful full-text access."
        value["completenessAssessments"][0]["verdict"] = "not-assessable"
        summary = build_audit_summary(value)
        self.assertNotEqual(summary["citedCorrectly"]["status"], "yes")
        self.assertEqual(summary["auditCoverage"]["sourcesInaccessible"], 1)


class EgressTests(unittest.TestCase):
    def test_identifier_and_short_concept_query_are_allowed(self):
        plan = EgressPlan()
        event = authorize_egress(
            provider="crossref",
            operation="resolve_identifier",
            arguments={"doi": "10.1000/example"},
            plan=plan,
        )
        self.assertEqual(event["decision"], "allowed-minimized")
        self.assertNotIn("10.1000/example", str(event))
        authorize_egress(
            provider="pubmed",
            operation="search_pubmed",
            arguments={"query": "microglia aging spatial transcriptomics"},
            plan=plan,
        )

    def test_manuscript_text_and_title_are_blocked(self):
        plan = EgressPlan()
        with self.assertRaises(ConfidentialityError):
            authorize_egress(
                provider="crossref",
                operation="search_crossref",
                arguments={"manuscriptParagraph": "Our unpublished result shows ..."},
                plan=plan,
            )
        with self.assertRaises(ConfidentialityError):
            authorize_egress(
                provider="crossref",
                operation="resolve_reference",
                arguments={"title": "A hidden title", "author": "Hidden"},
                plan=plan,
            )

    def test_sentence_like_query_is_blocked(self):
        with self.assertRaises(ConfidentialityError):
            authorize_egress(
                provider="europe-pmc",
                operation="search_europe_pmc",
                arguments={"query": "We found a new effect. This has never been reported."},
                plan=EgressPlan(),
            )

    def test_explicit_plan_can_approve_a_sensitive_field(self):
        plan = EgressPlan.from_mapping(
            {
                "confidential": True,
                "approvedOperations": ["resolve_reference"],
                "approvedFields": ["title"],
            }
        )
        event = authorize_egress(
            provider="crossref",
            operation="resolve_reference",
            arguments={"title": "Approved unpublished title"},
            plan=plan,
        )
        self.assertEqual(event["decision"], "allowed-minimized")
        self.assertNotIn("Approved unpublished title", json.dumps(event))

    def test_plan_rejects_string_booleans_and_duplicate_fields(self):
        with self.assertRaises(ValidationError):
            EgressPlan.from_mapping({"confidential": "false"})
        with self.assertRaises(ValidationError):
            EgressPlan.from_mapping({"approvedFields": ["title", "title"]})


if __name__ == "__main__":
    unittest.main()
