from __future__ import annotations

import copy
import unittest

from ocsl_review.audit import (
    build_audit_summary, validate_audit_graph, confidence_ceiling,
    claim_digest, citation_cluster_digest, reference_digest,
)
from ocsl_review.errors import ValidationError
from test_audit_confidentiality import graph


def abstain(value):
    link = value["verificationLinks"][0]
    link.update(supportVerdict="abstain", abstentionReason="Necessary evidence was not examined.", confidence=0.2)
    link["evidenceLocations"] = []
    return link


class EvidenceCoverageTests(unittest.TestCase):
    def test_completeness_does_not_require_finished_citation_correctness(self):
        value = graph()
        value["verificationLinks"] = []
        value["claims"][0]["processed"] = False
        value["citationClusters"][0]["processed"] = False
        value["completenessAssessments"][0]["sourceExaminationIds"] = ["exam-1"]
        summary = build_audit_summary(value)
        self.assertEqual("yes", summary["citedEnough"]["status"])
        self.assertEqual("not-fully-verifiable", summary["citedCorrectly"]["status"])

    def test_empty_and_missing_assessments_never_pass(self):
        value = graph()
        value["verificationLinks"] = []
        summary = build_audit_summary(value)
        self.assertEqual("not-fully-verifiable", summary["citedCorrectly"]["status"])
        self.assertNotEqual("yes", summary["citedEnough"]["status"])
        for key in ("claims", "references", "citationClusters", "sourceExaminations", "completenessAssessments"):
            value[key] = []
        self.assertNotEqual("yes", build_audit_summary(value)["citedCorrectly"]["status"])

    def test_each_reference_in_each_occurrence_requires_assessment(self):
        value = graph()
        cluster = copy.deepcopy(value["citationClusters"][0])
        cluster["id"] = "cc2"
        value["citationClusters"].append(cluster)
        value["claims"][0]["citationClusterIds"].append("cc2")
        value["verificationLinks"][0]["claimSha256"] = claim_digest(value["claims"][0])
        self.assertIn("missing-claim-reference-locus-assessment", build_audit_summary(value)["citedCorrectly"]["verificationLimits"])

    def test_unknown_reference_and_duplicate_locus_are_rejected(self):
        value = graph()
        value["citationClusters"][0]["referenceIds"] = ["missing"]
        with self.assertRaises(ValidationError):
            validate_audit_graph(value)
        value = graph()
        duplicate = copy.deepcopy(value["verificationLinks"][0])
        duplicate["id"] = "v2"
        value["verificationLinks"].append(duplicate)
        with self.assertRaisesRegex(ValidationError, "duplicate claim-reference-locus"):
            validate_audit_graph(value)

    def test_unread_and_partial_sources_require_abstention_in_both_directions(self):
        for verdict in ("direct-support", "contradicted", "not-addressed", "partial-indirect"):
            for incomplete in ("content", "reading"):
                value = graph()
                value["verificationLinks"][0]["supportVerdict"] = verdict
                if incomplete == "content":
                    value["sourceExaminations"][0]["contentComplete"] = False
                else:
                    value["sourceExaminations"][0]["readUnitIds"] = ["results"]
                with self.subTest(verdict=verdict, incomplete=incomplete), self.assertRaises(ValidationError):
                    validate_audit_graph(value)

    def test_downloaded_and_abstained_does_not_mean_checked(self):
        value = graph()
        abstain(value)
        value["sourceExaminations"][0]["readUnitIds"] = []
        summary = build_audit_summary(value)
        self.assertEqual(1, summary["auditCoverage"]["sourcesFullTextObtained"])
        self.assertEqual(0, summary["auditCoverage"]["sourcesFullTextChecked"])
        self.assertEqual(1, summary["auditCoverage"]["verificationLinksAbstained"])
        self.assertNotEqual("yes", summary["citedCorrectly"]["status"])
        self.assertNotEqual("yes", summary["citedEnough"]["status"])

    def test_complete_abstract_confidence_has_a_ceiling(self):
        value = graph()
        claim = value["claims"][0]
        claim.update(claimType="fact", requiredEvidenceLevel="abstract-sufficient",
                     evidenceRequirementReason="The complete abstract explicitly states this fact.")
        link = value["verificationLinks"][0]
        link.update(evidenceTier="abstract-only", claimSha256=claim_digest(claim))
        link["access"]["level"] = "abstract-only"
        exam = value["sourceExaminations"][0]
        exam.update(accessLevel="abstract-only", expectedUnitIds=["abstract"], readUnitIds=["abstract"])
        link["evidenceLocations"][0]["unitId"] = "abstract"
        self.assertEqual("confident", confidence_ceiling(value, link))
        validate_audit_graph(value)
        link["confidenceLabel"] = "very-confident"
        with self.assertRaisesRegex(ValidationError, "confidence exceeds"):
            validate_audit_graph(value)

    def test_full_text_requirement_applies_to_evidence_used(self):
        value = graph()
        value["verificationLinks"][0]["evidenceTier"] = "abstract-only"
        with self.assertRaisesRegex(ValidationError, "full-text question"):
            validate_audit_graph(value)

    def test_full_text_reading_does_not_substitute_for_checking_exact_details(self):
        value = graph()
        value["claims"][0]["claimType"] = "numeric"
        link = value["verificationLinks"][0]
        link["claimSha256"] = claim_digest(value["claims"][0])
        link["quotationOrNumberCheck"] = "not-checked"
        with self.assertRaisesRegex(ValidationError, "numeric detail was not checked"):
            validate_audit_graph(value)
        link["quotationOrNumberCheck"] = "faithful"
        validate_audit_graph(value)

    def test_abstract_policy_requires_reason_and_respects_claim_type(self):
        value = graph()
        claim = value["claims"][0]
        claim["requiredEvidenceLevel"] = "abstract-sufficient"
        value["verificationLinks"][0]["claimSha256"] = claim_digest(claim)
        with self.assertRaisesRegex(ValidationError, "abstract sufficiency needs"):
            validate_audit_graph(value)
        claim["evidenceRequirementReason"] = "The abstract contains this statement."
        claim["claimType"] = "numeric"
        value["verificationLinks"][0]["claimSha256"] = claim_digest(claim)
        with self.assertRaisesRegex(ValidationError, "claim type requires full-text"):
            validate_audit_graph(value)

    def test_uncited_required_claim_does_not_hide_behind_other_support(self):
        value = graph()
        claim = copy.deepcopy(value["claims"][0])
        claim.update(id="c2", citationClusterIds=[], citationBindingStatus="missing")
        value["claims"].append(claim)
        assessment = copy.deepcopy(value["completenessAssessments"][0])
        assessment.update(claimId="c2", verdict="missing-citation")
        value["completenessAssessments"].append(assessment)
        self.assertIn("missing-required-citation", build_audit_summary(value)["citedCorrectly"]["verificationLimits"])

    def test_additional_literature_requires_reading_without_changing_bibliography(self):
        value = graph()
        reference = copy.deepcopy(value["references"][0])
        reference["id"] = "additional1"
        value["additionalReferences"] = [reference]
        examination = copy.deepcopy(value["sourceExaminations"][0])
        examination.update(id="additional-exam", referenceIds=["additional1"],
                           referenceDigests={"additional1": reference_digest(reference)})
        value["sourceExaminations"].append(examination)
        value["completenessAssessments"][0]["sourceExaminationIds"] = ["additional-exam"]
        value["verificationLinks"][0] = abstain(value)
        summary = build_audit_summary(value)
        self.assertEqual("yes", summary["citedEnough"]["status"])
        self.assertEqual("not-fully-verifiable", summary["citedCorrectly"]["status"])
        self.assertEqual(1, summary["auditCoverage"]["bibliographyEntriesTotal"])
        examination["readUnitIds"] = []
        for verdict in ("sufficient", "insufficient-balance"):
            value["completenessAssessments"][0]["verdict"] = verdict
            with self.subTest(verdict=verdict), self.assertRaisesRegex(ValidationError, "supplementary source is unread"):
                validate_audit_graph(value)

    def test_scope_or_identity_edits_invalidate_recorded_evidence(self):
        for mutation in ("claim", "reference", "cluster"):
            value = graph()
            if mutation == "claim":
                value["claims"][0]["text"] = "An altered claim."
            elif mutation == "reference":
                value["references"][0]["identity"]["title"] = "A different article"
            else:
                value["citationClusters"][0]["raw"] = "[2]"
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValidationError, "stale"):
                validate_audit_graph(value)

    def test_weak_identity_needs_abstention(self):
        value = graph()
        reference = value["references"][0]
        reference["identityEvidence"]["providersCompared"] = ["same", "same"]
        value["sourceExaminations"][0]["referenceDigests"]["r1"] = reference_digest(reference)
        with self.assertRaisesRegex(ValidationError, "weak identity"):
            validate_audit_graph(value)

    def test_correctness_and_completeness_are_independent(self):
        value = graph()
        value["completenessAssessments"][0]["searchRunIds"] = []
        summary = build_audit_summary(value)
        self.assertEqual("yes", summary["citedCorrectly"]["status"])
        self.assertEqual("not-fully-verifiable", summary["citedEnough"]["status"])

    def test_adverse_literature_completeness_requires_examined_evidence(self):
        value = graph()
        abstain(value)
        value["completenessAssessments"][0]["verdict"] = "insufficient-balance"
        summary = build_audit_summary(value)
        self.assertEqual("not-fully-verifiable", summary["citedEnough"]["status"])
        self.assertIn("insufficient-evidence-for-completeness", summary["citedEnough"]["verificationLimits"])

    def test_publication_integrity_does_not_automatically_contradict_support(self):
        value = graph()
        value["references"][0]["publicationStatus"]["state"] = "retracted"
        summary = build_audit_summary(value)
        self.assertEqual("yes", summary["citedCorrectly"]["status"])
        self.assertEqual(1, summary["citedCorrectly"]["statusProblems"])

    def test_review_factual_assertions_need_known_examined_evidence(self):
        value = graph()
        assertion = {"id": "assert1", "text": "The source reports this finding.",
                     "type": "factual", "citationIds": ["r1"], "supportStatus": "supported"}
        value["reviewAssertions"] = [assertion]
        validate_audit_graph(value)
        for ids in ([], ["unknown"], ["c1"]):
            assertion["citationIds"] = ids
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                validate_audit_graph(value)
        assertion.update(citationIds=["c1"], supportStatus="manuscript-grounded")
        validate_audit_graph(value)
        assertion.update(citationIds=["r1"], supportStatus="supported")
        abstain(value)
        value["sourceExaminations"][0]["readUnitIds"] = []
        with self.assertRaisesRegex(ValidationError, "actually examined"):
            validate_audit_graph(value)

    def test_legacy_records_load_without_inventing_provenance(self):
        value = graph()
        value["schemaVersion"] = "1.0"
        value.pop("sourceExaminations")
        link = value["verificationLinks"][0]
        for key in ("citationClusterId", "sourceExaminationId", "evidenceTier", "claimSha256", "citationClusterSha256"):
            link.pop(key)
        link["evidenceLocations"][0].pop("unitId")
        summary = build_audit_summary(value)
        self.assertIn("legacy-evidence-provenance-unavailable", summary["verificationLimits"])
        self.assertEqual(0, summary["auditCoverage"]["sourcesFullTextChecked"])
        self.assertEqual("not-confident", confidence_ceiling(value, link))


if __name__ == "__main__":
    unittest.main()
