"""Validation and deterministic summaries for claim-to-source audit graphs.

The graph is deliberately evidence-first: bibliographic identity, source access,
claim support, scope alignment, and citation sufficiency are distinct records.
The summary function never upgrades missing evidence into a positive verdict.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import atomic_json

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
    claims = {item.get("id"): item for item in graph.get("claims", [])}
    references = {item.get("id"): item for item in graph.get("references", [])}
    cluster_ids = {item.get("id") for item in graph.get("citationClusters", [])}

    if len(claims) != len(graph.get("claims", [])):
        problems.append("claims: identifiers must be unique")
    if len(references) != len(graph.get("references", [])):
        problems.append("references: identifiers must be unique")
    if len(cluster_ids) != len(graph.get("citationClusters", [])):
        problems.append("citationClusters: identifiers must be unique")

    for claim in graph.get("claims", []):
        unknown = set(claim.get("citationClusterIds", [])) - cluster_ids
        if unknown:
            problems.append(f"claim {claim.get('id')}: unknown citation clusters {sorted(unknown)}")
        if claim.get("citationRequired") and claim.get("citationBindingStatus") == "not-applicable":
            problems.append(f"claim {claim.get('id')}: required citation cannot be not-applicable")
        if (
            not claim.get("citationRequired")
            and not claim.get("citationRequirementReason", "").strip()
        ):
            problems.append(f"claim {claim.get('id')}: an exemption requires a reason")

    seen_links: set[str] = set()
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
        if verdict in POSITIVE_SUPPORT and not link.get("evidenceLocations"):
            problems.append(
                f"verification link {link_id}: positive support needs an evidence locator"
            )

    assessments: dict[str, dict[str, Any]] = {}
    for item in graph.get("completenessAssessments", []):
        claim_id = item.get("claimId")
        if claim_id in assessments:
            problems.append(f"completenessAssessments: duplicate claim {claim_id}")
        assessments[claim_id] = item
        if claim_id not in claims:
            problems.append(f"completeness assessment: unknown claim {claim_id}")

    for claim_id, claim in claims.items():
        if claim.get("citationRequired") and claim.get("processed") and claim_id not in assessments:
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

    for assertion in graph.get("reviewAssertions", []):
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


def _unique_source_access(links: Iterable[dict[str, Any]]) -> Counter[str]:
    priority = {"inaccessible": 0, "metadata-only": 1, "abstract-only": 2, "full-text": 3}
    best: dict[str, str] = {}
    for link in links:
        reference = link["referenceId"]
        level = link["access"]["level"]
        if reference not in best or priority[level] > priority[best[reference]]:
            best[reference] = level
    return Counter(best.values())


def _verification_blockers(graph: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    manuscript = graph["manuscript"]
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
    if any(reference["resolutionStatus"] != "resolved" for reference in graph["references"]):
        blockers.append("unresolved-bibliography-entries")
    required = {claim["id"]: claim for claim in graph["claims"] if claim["citationRequired"]}
    if any(not claim["processed"] for claim in required.values()):
        blockers.append("unprocessed-required-claims")
    for link in graph["verificationLinks"]:
        claim = required.get(link["claimId"])
        if claim is None:
            continue
        access = link["access"]["level"]
        if access == "inaccessible":
            blockers.append("necessary-source-inaccessible")
        if access == "metadata-only":
            blockers.append("metadata-only-source-check")
        if claim["requiredEvidenceLevel"] == "full-text" and access == "abstract-only":
            blockers.append("full-text-question-checked-from-abstract")
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
    return "yes"


def build_audit_summary(graph: dict[str, Any]) -> dict[str, Any]:
    """Create the mandatory denominator-rich answer from a valid audit graph."""

    validate_audit_graph(graph)
    support = Counter(link["supportVerdict"] for link in graph["verificationLinks"])
    access = _unique_source_access(graph["verificationLinks"])
    completeness = Counter(item["verdict"] for item in graph["completenessAssessments"])
    required_claims = [claim for claim in graph["claims"] if claim["citationRequired"]]
    metadata_problems = sum(len(reference["metadataProblems"]) for reference in graph["references"])
    status_problems = sum(
        reference["publicationStatus"]["state"]
        in {"retracted", "expression-of-concern", "superseded"}
        for reference in graph["references"]
    )
    blockers = _verification_blockers(graph)
    correctly_positive = support["direct-support"] + support["scope-caveat"]
    correctly_hard = support["contradicted"] + support["not-addressed"] + status_problems
    correctly_soft = support["partial-indirect"] + metadata_problems
    enough_hard = completeness["missing-citation"]
    enough_soft = sum(
        completeness[name]
        for name in (
            "insufficient-directness",
            "insufficient-scope",
            "insufficient-breadth",
            "insufficient-currency",
            "insufficient-balance",
            "not-assessable",
        )
    )
    summary = {
        "schemaVersion": "1.0",
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
            "citationRequiredClaimsTotal": len(required_claims),
            "citationRequiredClaimsProcessed": sum(item["processed"] for item in required_claims),
            "sourcesFullTextChecked": access["full-text"],
            "sourcesAbstractOnly": access["abstract-only"],
            "sourcesInaccessible": access["inaccessible"],
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
        },
        "citedEnough": {
            "status": _status(
                positive=completeness["sufficient"],
                hard_negative=enough_hard,
                soft_negative=enough_soft,
                blockers=blockers,
            ),
            "sufficientClaims": completeness["sufficient"],
            "missingCitations": completeness["missing-citation"],
            "insufficientDirectness": completeness["insufficient-directness"],
            "insufficientScope": completeness["insufficient-scope"],
            "insufficientBreadth": completeness["insufficient-breadth"],
            "insufficientCurrency": completeness["insufficient-currency"],
            "insufficientBalance": completeness["insufficient-balance"],
        },
        "verificationLimits": blockers,
    }
    if blockers and (
        summary["citedCorrectly"]["status"] == "yes" or summary["citedEnough"]["status"] == "yes"
    ):
        raise AssertionError("a positive status escaped a mandatory abstention condition")
    return summary


def write_audit_summary(graph: dict[str, Any], output: Path) -> dict[str, Any]:
    summary = build_audit_summary(graph)
    atomic_json(output, summary)
    return summary
