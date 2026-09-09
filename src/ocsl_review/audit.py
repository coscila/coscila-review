"""Validation and deterministic summaries for claim-to-source audit graphs.

The graph is deliberately evidence-first: bibliographic identity, source access,
claim support, scope alignment, and citation sufficiency are distinct records.
The summary function never upgrades missing evidence into a positive verdict.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import atomic_json

ACCESS_RANK = {"inaccessible": 0, "metadata-only": 1, "abstract-only": 2, "full-text": 3}
CONFIDENCE_RANK = {"not-confident": 0, "confident": 1, "very-confident": 2}
FULL_TEXT_CLAIM_TYPES = {"numeric", "causal", "quotation", "method", "software", "dataset", "novelty"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def reference_digest(reference: dict[str, Any]) -> str:
    return _digest({key: reference.get(key) for key in ("raw", "identifiers", "identity", "resolutionStatus", "identityEvidence")})


def claim_digest(claim: dict[str, Any]) -> str:
    return _digest({key: claim.get(key) for key in ("id", "text", "locator", "citationClusterIds", "requiredEvidenceLevel", "evidenceRequirementReason", "claimType", "citationRequired")})


def citation_cluster_digest(cluster: dict[str, Any]) -> str:
    return _digest({key: cluster.get(key) for key in ("id", "locator", "raw", "markers", "referenceIds", "bindingStatus", "unresolvedMarkers")})


def _fully_read(examination: dict[str, Any]) -> bool:
    expected = set(examination.get("expectedUnitIds", []))
    return bool(examination.get("contentComplete") and expected and expected == set(examination.get("readUnitIds", [])))


def _strong_identity(reference: dict[str, Any]) -> bool:
    evidence = reference.get("identityEvidence", {})
    return bool(reference.get("resolutionStatus") == "resolved"
                and evidence.get("agreementStrength") == "strong"
                and len(set(evidence.get("providersCompared", []))) >= 2
                and not evidence.get("differences") and not evidence.get("missingFields"))


def confidence_ceiling(graph: dict[str, Any], link: dict[str, Any]) -> str:
    """Evidence permits a maximum certainty; availability never grants certainty."""
    if graph.get("schemaVersion") != "1.1" or link.get("supportVerdict") == "abstain":
        return "not-confident"
    reference = next((item for item in graph["references"] if item["id"] == link["referenceId"]), {})
    examination = next((item for item in graph.get("sourceExaminations", []) if item["id"] == link.get("sourceExaminationId")), {})
    locations = link.get("evidenceLocations", [])
    if (not _strong_identity(reference) or not _fully_read(examination) or not locations
            or any(item.get("unitId") not in examination.get("readUnitIds", []) for item in locations)):
        return "not-confident"
    tier = link.get("evidenceTier")
    if tier == "full-text" and examination.get("accessLevel") == "full-text":
        return "very-confident"
    if tier == "abstract-only":
        return "confident"
    return "not-confident"

POSITIVE_SUPPORT = {"direct-support", "scope-caveat"}
HARD_SUPPORT_FAILURES = {"contradicted", "not-addressed"}
INDETERMINATE_SUPPORT = {"partial-indirect", "abstain"}
SCOPE_DIMENSIONS = (
    "species",
    "population",
    "exposureOrIntervention",
    "comparator",
    "outcome",
    "direction",
    "magnitude",
    "causalStrength",
    "setting",
    "timeframe",
)


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / "claim-citation-audit.schema.json"


def _jsonschema_errors(value: dict[str, Any]) -> list[str]:
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - declared core dependency
        raise ValidationError("jsonschema is required to validate review artifacts") from exc
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{('/'.join(str(part) for part in error.absolute_path) or '<root>')}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    ]


def validate_audit_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Validate structure plus evidence-safety invariants.

    Returns a compact validation report and raises ``ValidationError`` on any
    defect. These semantic checks are intentionally stricter than JSON Schema.
    """

    problems = _jsonschema_errors(graph)
    if problems:
        raise ValidationError("audit graph validation failed:\n- " + "\n- ".join(problems))
    claims = {item.get("id"): item for item in graph.get("claims", [])}
    references = {item.get("id"): item for item in graph.get("references", [])}
    additional_references = {item["id"]: item for item in graph.get("additionalReferences", [])}
    all_references = {**references, **additional_references}
    cluster_ids = {item.get("id") for item in graph.get("citationClusters", [])}
    clusters = {item["id"]: item for item in graph["citationClusters"]}
    modern = graph["schemaVersion"] == "1.1"
    if modern and "sourceExaminations" not in graph:
        problems.append("sourceExaminations: v1.1 requires explicit examination records")
    examinations = {item["id"]: item for item in graph.get("sourceExaminations", [])}
    if len(examinations) != len(graph.get("sourceExaminations", [])):
        problems.append("sourceExaminations: identifiers must be unique")
    for examination in examinations.values():
        if set(examination["readUnitIds"]) - set(examination["expectedUnitIds"]):
            problems.append(f"examination {examination['id']}: read units are not in source document")
        if set(examination["referenceDigests"]) != set(examination["referenceIds"]):
            problems.append(f"examination {examination['id']}: reference digest set differs")
        for ref_id in examination["referenceIds"]:
            if ref_id not in all_references or examination["referenceDigests"].get(ref_id) != reference_digest(all_references[ref_id]):
                problems.append(f"examination {examination['id']}: stale or unknown reference {ref_id}")
        artifact = Path(examination["artifactPath"])
        if artifact.is_absolute() or ".." in artifact.parts:
            problems.append(f"examination {examination['id']}: artifact path must remain workspace relative")
    for cluster in clusters.values():
        if set(cluster["referenceIds"]) - references.keys():
            problems.append(f"citation cluster {cluster['id']}: unknown reference")
        if cluster.get("bindingStatus") == "exact" and (cluster.get("unresolvedMarkers") or not cluster["referenceIds"]):
            problems.append(f"citation cluster {cluster['id']}: exact binding has unresolved markers")

    if len(claims) != len(graph.get("claims", [])):
        problems.append("claims: identifiers must be unique")
    if len(references) != len(graph.get("references", [])):
        problems.append("references: identifiers must be unique")
    if len(additional_references) != len(graph.get("additionalReferences", [])) or references.keys() & additional_references.keys():
        problems.append("additionalReferences: identifiers must be unique across bibliography and additional sources")
    if len(cluster_ids) != len(graph.get("citationClusters", [])):
        problems.append("citationClusters: identifiers must be unique")

    for claim in graph.get("claims", []):
        if modern and claim["citationRequired"]:
            if claim["requiredEvidenceLevel"] not in {"full-text", "abstract-sufficient"}:
                problems.append(f"claim {claim['id']}: substantive claims require examined content")
            if claim["requiredEvidenceLevel"] == "abstract-sufficient":
                if not claim.get("evidenceRequirementReason", "").strip():
                    problems.append(f"claim {claim['id']}: abstract sufficiency needs an explicit reason")
                if claim["claimType"] in FULL_TEXT_CLAIM_TYPES:
                    problems.append(f"claim {claim['id']}: this claim type requires full-text examination")
        unknown = set(claim.get("citationClusterIds", [])) - cluster_ids
        if unknown:
            problems.append(f"claim {claim.get('id')}: unknown citation clusters {sorted(unknown)}")
        if claim.get("citationBindingStatus") == "exact" and any(
            clusters[item].get("bindingStatus", "exact") != "exact" or clusters[item].get("unresolvedMarkers")
            for item in claim.get("citationClusterIds", []) if item in clusters
        ):
            problems.append(f"claim {claim['id']}: exact binding conflicts with unresolved cluster")
        if claim.get("citationRequired") and claim.get("citationBindingStatus") == "not-applicable":
            problems.append(f"claim {claim.get('id')}: required citation cannot be not-applicable")
        if (
            not claim.get("citationRequired")
            and not claim.get("citationRequirementReason", "").strip()
        ):
            problems.append(f"claim {claim.get('id')}: an exemption requires a reason")

    seen_links: set[str] = set()
    seen_pairs: set[tuple[str, str, str]] = set()
    links_by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in graph.get("verificationLinks", []):
        link_id = link.get("id")
        if link_id in seen_links:
            problems.append(f"verificationLinks: duplicate id {link_id}")
        seen_links.add(link_id)
        claim = claims.get(link.get("claimId"))
        if claim is None:
            problems.append(f"verification link {link_id}: unknown claim {link.get('claimId')}")
            continue
        if link.get("referenceId") not in references:
            problems.append(
                f"verification link {link_id}: unknown reference {link.get('referenceId')}"
            )
        links_by_claim[claim["id"]].append(link)
        access = link.get("access", {}).get("level")
        verdict = link.get("supportVerdict")
        if modern:
            required_fields = {"citationClusterId", "sourceExaminationId", "evidenceTier", "claimSha256", "citationClusterSha256"}
            if required_fields - link.keys():
                problems.append(f"verification link {link_id}: missing v1.1 evidence fields {sorted(required_fields - link.keys())}")
            cluster = clusters.get(link.get("citationClusterId"), {})
            pair = (claim["id"], link["referenceId"], link.get("citationClusterId", ""))
            if pair in seen_pairs:
                problems.append(f"verification link {link_id}: duplicate claim-reference-locus assessment")
            seen_pairs.add(pair)
            if cluster.get("id") not in claim["citationClusterIds"] or link["referenceId"] not in cluster.get("referenceIds", []):
                problems.append(f"verification link {link_id}: reference is not bound at this claim locus")
            if link.get("claimSha256") != claim_digest(claim) or link.get("citationClusterSha256") != citation_cluster_digest(cluster):
                problems.append(f"verification link {link_id}: stale claim or citation locus")
            examination = examinations.get(link.get("sourceExaminationId"), {})
            if link.get("sourceExaminationId") is not None and not examination:
                problems.append(f"verification link {link_id}: unknown source examination")
            tier = link.get("evidenceTier", "inaccessible")
            if ACCESS_RANK[tier] > ACCESS_RANK[access]:
                problems.append(f"verification link {link_id}: evidence tier exceeds obtained access")
            if examination and (link["referenceId"] not in examination["referenceIds"] or ACCESS_RANK[tier] > ACCESS_RANK[examination["accessLevel"]]):
                problems.append(f"verification link {link_id}: evidence examination does not match reference or tier")
            if claim["requiredEvidenceLevel"] == "full-text" and tier != "full-text" and verdict != "abstain":
                problems.append(f"verification link {link_id}: full-text question requires full-text evidence")
            if claim["claimType"] in {"numeric", "quotation"} and verdict != "abstain":
                if link["quotationOrNumberCheck"] == "not-checked":
                    problems.append(f"verification link {link_id}: exact quotation or numeric detail was not checked")
                if verdict == "direct-support" and link["quotationOrNumberCheck"] != "faithful":
                    problems.append(f"verification link {link_id}: direct support requires a faithful quotation or numeric detail")
            for location in link["evidenceLocations"]:
                if location.get("unitId") not in examination.get("readUnitIds", []):
                    problems.append(f"verification link {link_id}: evidence location was not read")
                if "excerpt" in location and hashlib.sha256(location["excerpt"].encode()).hexdigest() != location["excerptSha256"]:
                    problems.append(f"verification link {link_id}: excerpt hash differs")
            ceiling = confidence_ceiling(graph, link)
            if verdict != "abstain" and ceiling == "not-confident":
                problems.append(f"verification link {link_id}: incomplete, unread, or weak identity evidence requires abstention")
            if CONFIDENCE_RANK[link.get("confidenceLabel", ceiling)] > CONFIDENCE_RANK[ceiling]:
                problems.append(f"verification link {link_id}: confidence exceeds examined evidence")
        if access in {"metadata-only", "inaccessible"} and verdict != "abstain":
            problems.append(f"verification link {link_id}: {access} access requires abstention")
        if (
            claim.get("requiredEvidenceLevel") == "full-text"
            and access == "abstract-only"
            and verdict != "abstain"
        ):
            problems.append(
                f"verification link {link_id}: abstract-only access cannot decide a full-text claim"
            )
        if verdict == "abstain":
            if not link.get("abstentionReason"):
                problems.append(f"verification link {link_id}: abstention needs a reason")
            if float(link.get("confidence", 1)) > 0.5:
                problems.append(
                    f"verification link {link_id}: abstention confidence must be <= 0.5"
                )
        elif link.get("abstentionReason"):
            problems.append(f"verification link {link_id}: non-abstention has abstentionReason")
        if verdict == "direct-support":
            mismatches = [
                dimension
                for dimension in SCOPE_DIMENSIONS
                if link.get("scopeComparison", {}).get(dimension) == "mismatch"
            ]
            if mismatches:
                problems.append(
                    f"verification link {link_id}: direct-support conflicts with scope mismatches "
                    + ", ".join(mismatches)
                )
        if verdict != "abstain" and not link.get("evidenceLocations"):
            problems.append(
                f"verification link {link_id}: a content verdict needs an evidence locator"
            )

    assessments: dict[str, dict[str, Any]] = {}
    run_ids = [run["id"] for run in graph["searchRuns"]]
    if len(set(run_ids)) != len(run_ids):
        problems.append("searchRuns: identifiers must be unique")
    for item in graph.get("completenessAssessments", []):
        claim_id = item.get("claimId")
        if claim_id in assessments:
            problems.append(f"completenessAssessments: duplicate claim {claim_id}")
        assessments[claim_id] = item
        if claim_id not in claims:
            problems.append(f"completeness assessment: unknown claim {claim_id}")
        if set(item["searchRunIds"]) - set(run_ids):
            problems.append(f"completeness assessment {claim_id}: unknown search run")
        for examination_id in item.get("sourceExaminationIds", []):
            examination = examinations.get(examination_id)
            if examination is None:
                problems.append(f"completeness assessment {claim_id}: unknown source examination")
            elif item["verdict"] != "not-assessable" and (
                not _fully_read(examination)
                or examination["accessLevel"] == "metadata-only"
                or any(not _strong_identity(all_references.get(ref_id, {})) for ref_id in examination["referenceIds"])
                or (claims.get(claim_id, {}).get("requiredEvidenceLevel") == "full-text" and examination["accessLevel"] != "full-text")
            ):
                problems.append(f"completeness assessment {claim_id}: supplementary source is unread, incomplete, or weak identity")

    for claim_id, claim in claims.items():
        if not modern and claim.get("citationRequired") and claim.get("processed") and claim_id not in assessments:
            problems.append(
                f"claim {claim_id}: processed required claim lacks completeness assessment"
            )
        if claim.get("claimType") == "novelty" and claim_id in assessments:
            run_ids = assessments[claim_id].get("searchRunIds", [])
            runs = {run.get("id"): run for run in graph.get("searchRuns", [])}
            providers = {runs[item]["provider"] for item in run_ids if item in runs}
            if assessments[claim_id].get("verdict") == "sufficient" and len(providers) < 2:
                problems.append(
                    f"claim {claim_id}: a novelty claim cannot be sufficient after fewer than two providers"
                )

    assertion_ids: set[str] = set()
    links_by_id = {link["id"]: link for link in graph["verificationLinks"]}
    examined_references = {
        ref_id for examination in examinations.values()
        if _fully_read(examination) and examination["accessLevel"] != "metadata-only"
        for ref_id in examination["referenceIds"]
        if _strong_identity(all_references.get(ref_id, {}))
    }
    for assertion in graph.get("reviewAssertions", []):
        if assertion["id"] in assertion_ids:
            problems.append(f"review assertion {assertion['id']}: duplicate identifier")
        assertion_ids.add(assertion["id"])
        citation_ids = set(assertion["citationIds"])
        if modern and citation_ids - (all_references.keys() | links_by_id.keys() | claims.keys()):
            problems.append(f"review assertion {assertion['id']}: unknown evidence identifier")
        if modern and assertion["type"] == "factual":
            if assertion["supportStatus"] == "manuscript-grounded" and (not citation_ids or citation_ids - claims.keys()):
                problems.append(f"review assertion {assertion['id']}: manuscript-grounded facts require known claim identifiers")
            if assertion["supportStatus"] == "supported":
                examined_ids = examined_references | {
                    link_id for link_id, link in links_by_id.items()
                    if link["supportVerdict"] != "abstain" and link["referenceId"] in examined_references
                }
                if not citation_ids or citation_ids - examined_ids:
                    problems.append(f"review assertion {assertion['id']}: factual support requires actually examined cited evidence")
        if assertion.get("type") == "factual" and assertion.get("supportStatus") in {
            "opinion",
            "unsupported",
        }:
            problems.append(
                f"review assertion {assertion.get('id')}: factual review text is not evidence-backed"
            )

    if problems:
        raise ValidationError("audit graph validation failed:\n- " + "\n- ".join(problems))
    return {
        "ok": True,
        "claims": len(claims),
        "references": len(references),
        "verificationLinks": len(seen_links),
        "requiredClaims": sum(bool(item.get("citationRequired")) for item in claims.values()),
    }


def _unique_source_access(graph: dict[str, Any]) -> Counter[str]:
    best: dict[str, str] = {}
    for link in graph["verificationLinks"]:
        reference = link["referenceId"]
        level = link["access"]["level"]
        if reference not in best or ACCESS_RANK[level] > ACCESS_RANK[best[reference]]:
            best[reference] = level
    for examination in graph.get("sourceExaminations", []):
        level = examination["accessLevel"]
        for reference in examination["referenceIds"]:
            if reference not in best or ACCESS_RANK[level] > ACCESS_RANK[best[reference]]:
                best[reference] = level
    for reference in graph["references"] + graph.get("additionalReferences", []):
        if reference["id"] not in best and reference.get("acquisition", {}).get("state") in {"blocked", "inaccessible", "failed"}:
            best[reference["id"]] = "inaccessible"
    return Counter(best.values())


def _verification_blockers(graph: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    manuscript = graph["manuscript"]
    if graph["schemaVersion"] != "1.1":
        blockers.append("legacy-evidence-provenance-unavailable")
    if not graph["claims"] or not graph["references"] or not graph["citationClusters"]:
        blockers.append("empty-audit-universe")
    if not manuscript["ingestionComplete"]:
        blockers.append("manuscript-ingestion-incomplete")
    if graph["auditScope"]["mode"] != "exhaustive":
        blockers.append("sampled-audit")
    if not graph["auditScope"]["citationBindingComplete"]:
        blockers.append("citation-binding-incomplete")
    if any(
        claim["citationRequired"] and claim["citationBindingStatus"] == "ambiguous"
        for claim in graph["claims"]
    ):
        blockers.append("ambiguous-citation-binding")
    if any(not cluster["processed"] for cluster in graph["citationClusters"]):
        blockers.append("unprocessed-citation-clusters")
    if any(cluster.get("bindingStatus", "exact") != "exact" or cluster.get("unresolvedMarkers") for cluster in graph["citationClusters"]):
        blockers.append("unresolved-citation-markers")
    if any(reference["resolutionStatus"] != "resolved" for reference in graph["references"]):
        blockers.append("unresolved-bibliography-entries")
    required = {claim["id"]: claim for claim in graph["claims"] if claim["citationRequired"]}
    if not required:
        blockers.append("no-citation-required-claims")
    if any(not claim["processed"] for claim in required.values()):
        blockers.append("unprocessed-required-claims")
    clusters = {item["id"]: item for item in graph["citationClusters"]}
    actual = {(item["claimId"], item["referenceId"], item.get("citationClusterId")) for item in graph["verificationLinks"]}
    for claim in required.values():
        if claim["citationBindingStatus"] == "missing" or not claim["citationClusterIds"]:
            blockers.append("missing-required-citation")
        for cluster_id in claim["citationClusterIds"]:
            for reference_id in clusters[cluster_id]["referenceIds"]:
                if (claim["id"], reference_id, cluster_id) not in actual:
                    blockers.append("missing-claim-reference-locus-assessment")
    covered = {item["referenceId"] for item in graph["verificationLinks"]}
    if any(item["id"] not in covered and not item.get("acquisition", {}).get("attempted") for item in graph["references"]):
        blockers.append("unexamined-bibliography-entries")
    for link in graph["verificationLinks"]:
        claim = required.get(link["claimId"])
        if claim is None:
            continue
        if link["supportVerdict"] == "abstain":
            blockers.append("unverifiable-citation-support")
        access = link["access"]["level"]
        if access == "inaccessible":
            blockers.append("necessary-source-inaccessible")
        if access == "metadata-only":
            blockers.append("metadata-only-source-check")
        if claim["requiredEvidenceLevel"] == "full-text" and access == "abstract-only":
            blockers.append("full-text-question-checked-from-abstract")
    return sorted(set(blockers))


def _completeness_blockers(graph: dict[str, Any]) -> list[str]:
    """Missing-literature search is an independent axis from cited-source support."""
    blockers = []
    if graph["schemaVersion"] != "1.1":
        blockers.append("legacy-evidence-provenance-unavailable")
    if not graph["manuscript"]["ingestionComplete"]:
        blockers.append("manuscript-ingestion-incomplete")
    if graph["auditScope"]["mode"] != "exhaustive":
        blockers.append("sampled-audit")
    required = [claim for claim in graph["claims"] if claim["citationRequired"]]
    if not required:
        blockers.append("no-citation-required-claims")
    assessments = {item["claimId"]: item for item in graph["completenessAssessments"]}
    for claim in required:
        assessment = assessments.get(claim["id"])
        if assessment is None:
            blockers.append("unprocessed-completeness-assessments")
        elif assessment["verdict"] == "not-assessable":
            blockers.append("literature-completeness-not-assessable")
        elif assessment["verdict"] != "missing-citation":
            if assessment["verdict"] in {"sufficient", "insufficient-breadth", "insufficient-currency", "insufficient-balance"} and not assessment["searchRunIds"]:
                blockers.append("targeted-literature-search-unrecorded")
            if not assessment.get("sourceExaminationIds") and not any(link["claimId"] == claim["id"] and link["supportVerdict"] != "abstain" for link in graph["verificationLinks"]):
                blockers.append("insufficient-evidence-for-completeness")
    return sorted(set(blockers))


def _status(
    *,
    positive: int,
    hard_negative: int,
    soft_negative: int,
    blockers: list[str],
) -> str:
    if blockers and hard_negative == 0:
        return "not-fully-verifiable"
    if hard_negative and positive == 0:
        return "no"
    if hard_negative or soft_negative:
        return "partly" if positive else "no"
    if blockers:
        return "not-fully-verifiable"
    return "yes" if positive else "not-fully-verifiable"


def build_audit_summary(graph: dict[str, Any]) -> dict[str, Any]:
    """Create the mandatory denominator-rich answer from a valid audit graph."""

    validate_audit_graph(graph)
    support = Counter(link["supportVerdict"] for link in graph["verificationLinks"])
    access = _unique_source_access(graph)
    completeness = Counter(item["verdict"] for item in graph["completenessAssessments"])
    required_claims = [claim for claim in graph["claims"] if claim["citationRequired"]]
    metadata_problems = sum(len(reference["metadataProblems"]) for reference in graph["references"])
    status_problems = sum(
        reference["publicationStatus"]["state"]
        in {"retracted", "expression-of-concern", "superseded"}
        for reference in graph["references"]
    )
    blockers = _verification_blockers(graph)
    completeness_blockers = _completeness_blockers(graph)
    correctly_positive = support["direct-support"] + support["scope-caveat"]
    correctly_hard = support["contradicted"] + support["not-addressed"]
    correctly_soft = support["partial-indirect"]
    enough_hard = completeness["missing-citation"]
    enough_soft = sum(
        completeness[name]
        for name in (
            "insufficient-directness",
            "insufficient-scope",
            "insufficient-breadth",
            "insufficient-currency",
            "insufficient-balance",
        )
    )
    summary = {
        "schemaVersion": "1.1",
        "auditId": graph["auditId"],
        "auditCoverage": {
            "inTextCitationClustersTotal": len(graph["citationClusters"]),
            "inTextCitationClustersProcessed": sum(
                bool(item["processed"]) for item in graph["citationClusters"]
            ),
            "bibliographyEntriesTotal": len(graph["references"]),
            "bibliographyEntriesResolved": sum(
                item["resolutionStatus"] == "resolved" for item in graph["references"]
            ),
            "additionalReferencesTotal": len(graph.get("additionalReferences", [])),
            "citationRequiredClaimsTotal": len(required_claims),
            "citationRequiredClaimsProcessed": sum(item["processed"] for item in required_claims),
            "sourcesFullTextChecked": len({reference_id for exam in graph.get("sourceExaminations", []) if exam["accessLevel"] == "full-text" and _fully_read(exam) for reference_id in exam["referenceIds"]}),
            "sourcesAbstractOnly": access["abstract-only"],
            "sourcesInaccessible": access["inaccessible"],
            "sourcesMetadataOnly": access["metadata-only"],
            "sourcesFullTextObtained": len({reference_id for exam in graph.get("sourceExaminations", []) if exam["accessLevel"] == "full-text" for reference_id in exam["referenceIds"]}),
            "referencesAttempted": sum(bool(item.get("acquisition", {}).get("attempted")) for item in graph["references"]),
            "referencesPending": sum(item.get("acquisition", {}).get("state", "pending") == "pending" for item in graph["references"]),
            "referencesBlocked": sum(item.get("acquisition", {}).get("state") in {"blocked", "failed", "inaccessible"} for item in graph["references"]),
            "referencesAssessed": len({item["referenceId"] for item in graph["verificationLinks"]}),
            "verificationLinksTotal": len(graph["verificationLinks"]),
            "verificationLinksAbstained": support["abstain"],
            "evidenceFullTextUsed": sum(item.get("evidenceTier") == "full-text" and item["supportVerdict"] != "abstain" for item in graph["verificationLinks"]),
            "evidenceAbstractUsed": sum(item.get("evidenceTier") == "abstract-only" and item["supportVerdict"] != "abstain" for item in graph["verificationLinks"]),
        },
        "citedCorrectly": {
            "status": _status(
                positive=correctly_positive,
                hard_negative=correctly_hard,
                soft_negative=correctly_soft,
                blockers=blockers,
            ),
            "directlySupported": support["direct-support"],
            "scopeCaveats": support["scope-caveat"],
            "partialOrIndirect": support["partial-indirect"],
            "contradicted": support["contradicted"],
            "notAddressed": support["not-addressed"],
            "statusProblems": status_problems,
            "metadataProblems": metadata_problems,
            "verificationLimits": blockers,
        },
        "citedEnough": {
            "status": _status(
                positive=completeness["sufficient"],
                hard_negative=enough_hard,
                soft_negative=enough_soft,
                blockers=completeness_blockers,
            ),
            "sufficientClaims": completeness["sufficient"],
            "missingCitations": completeness["missing-citation"],
            "insufficientDirectness": completeness["insufficient-directness"],
            "insufficientScope": completeness["insufficient-scope"],
            "insufficientBreadth": completeness["insufficient-breadth"],
            "insufficientCurrency": completeness["insufficient-currency"],
            "insufficientBalance": completeness["insufficient-balance"],
            "verificationLimits": completeness_blockers,
        },
        "verificationLimits": sorted(set(blockers + completeness_blockers)),
    }
    if ((blockers and summary["citedCorrectly"]["status"] == "yes")
            or (completeness_blockers and summary["citedEnough"]["status"] == "yes")):
        raise AssertionError("a positive status escaped a mandatory abstention condition")
    return summary


def write_audit_summary(graph: dict[str, Any], output: Path) -> dict[str, Any]:
    summary = build_audit_summary(graph)
    atomic_json(output, summary)
    return summary
