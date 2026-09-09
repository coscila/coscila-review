"""Create reproducible review routes and location-anchored work records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import ValidationError

PLAN_SCHEMA_VERSION = "1.1.0"
PACKAGE_SCHEMA_VERSION = "1.1.0"

CONCERN_PRIORITIES = frozenset({"blocking", "substantial", "editorial"})
ACTION_STATES = frozenset(
    {
        "not-started",
        "author-decision-needed",
        "new-work-needed",
        "edit-prepared",
        "verification-pending",
        "resolved",
        "accepted-exception",
    }
)

# The vocabulary and selection model are original to this companion.  Lenses
# describe questions to ask rather than pretending that one synthetic reviewer
# can certify every kind of validity.
_LENS_CATALOG: dict[str, dict[str, Any]] = {
    "evidence-lineage": {
        "label": "Evidence lineage",
        "mission": "Trace consequential statements to the exact material offered in support.",
        "targets": ["claims", "citation binding", "source role", "scope"],
    },
    "design-counterfactuals": {
        "label": "Design counterfactuals",
        "mission": "Ask which rival explanations remain possible under the stated design.",
        "targets": ["allocation", "controls", "confounding", "causal language"],
    },
    "measurement-chain": {
        "label": "Measurement chain",
        "mission": "Follow constructs from collection through transformation to the reported endpoint.",
        "targets": ["instrument", "operational definition", "blinding", "measurement error"],
    },
    "uncertainty-budget": {
        "label": "Uncertainty budget",
        "mission": "Account for sampling, modeling, multiplicity, missingness, and sensitivity choices.",
        "targets": ["estimands", "intervals", "model checks", "missing data"],
    },
    "boundary-transfer": {
        "label": "Boundary transfer",
        "mission": "Define where the result can and cannot travel across populations and settings.",
        "targets": ["population", "species", "setting", "external validity"],
    },
    "computational-replay": {
        "label": "Computational replay",
        "mission": "Determine whether data transformations and software-dependent results can be replayed.",
        "targets": ["code", "data lineage", "versions", "randomness"],
    },
    "participant-stewardship": {
        "label": "Participant stewardship",
        "mission": "Check whether participant rights, approvals, and sensitive data handling are explicit.",
        "targets": ["consent", "oversight", "privacy", "risk"],
    },
    "synthesis-boundaries": {
        "label": "Synthesis boundaries",
        "mission": "Test search reach, selection decisions, dependence, and treatment of contrary evidence.",
        "targets": ["search", "eligibility", "study dependence", "evidence balance"],
    },
    "display-reconciliation": {
        "label": "Display reconciliation",
        "mission": "Reconcile narrative statements with numbers, tables, figures, and captions.",
        "targets": ["results text", "tables", "figures", "numerical consistency"],
    },
}

_PROFILE_SIGNALS: dict[str, tuple[re.Pattern[str], ...]] = {
    "human-participants": (
        re.compile(r"\bpatients?\b", re.IGNORECASE),
        re.compile(r"\bparticipants?\b", re.IGNORECASE),
        re.compile(r"\bcohort\b", re.IGNORECASE),
        re.compile(r"\binformed consent\b", re.IGNORECASE),
    ),
    "intervention": (
        re.compile(r"\brandomi[sz]ed\b", re.IGNORECASE),
        re.compile(r"\bintervention\b", re.IGNORECASE),
        re.compile(r"\bplacebo\b", re.IGNORECASE),
        re.compile(r"\btrial\b", re.IGNORECASE),
    ),
    "observational": (
        re.compile(r"\bobservational\b", re.IGNORECASE),
        re.compile(r"\bretrospective\b", re.IGNORECASE),
        re.compile(r"\bprospective cohort\b", re.IGNORECASE),
        re.compile(r"\bcross-sectional\b", re.IGNORECASE),
    ),
    "nonhuman-model": (
        re.compile(r"\bmice\b|\bmouse\b|\brats?\b", re.IGNORECASE),
        re.compile(r"\banimal model\b", re.IGNORECASE),
        re.compile(r"\bin vivo\b", re.IGNORECASE),
    ),
    "computational": (
        re.compile(r"\bsoftware\b|\bsource code\b|\brepository\b", re.IGNORECASE),
        re.compile(r"\balgorithm\b|\bmodel training\b|\bsimulation\b", re.IGNORECASE),
        re.compile(r"\bpython\b|\bR package\b|\bcontainer\b", re.IGNORECASE),
    ),
    "evidence-synthesis": (
        re.compile(r"\bsystematic review\b|\bmeta-analysis\b", re.IGNORECASE),
        re.compile(r"\bnarrative review\b|\bliterature review\b|\breview article\b", re.IGNORECASE),
        re.compile(r"\bsearch strategy\b|\bdatabases? searched\b", re.IGNORECASE),
        re.compile(r"\bPRISMA\b", re.IGNORECASE),
    ),
    "qualitative": (
        re.compile(r"\binterviews?\b|\bfocus groups?\b", re.IGNORECASE),
        re.compile(r"\bthematic analysis\b|\bgrounded theory\b", re.IGNORECASE),
    ),
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _stable_id(prefix: str, value: Any, length: int = 16) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_json(value)).hexdigest()[:length]}"


def _manuscript_text(manuscript: Mapping[str, Any], *, limit: int = 2_000_000) -> str:
    parts: list[str] = []
    size = 0
    for paragraph in manuscript.get("paragraphs", []):
        if paragraph.get("kind") == "bibliography-entry":
            continue
        value = str(paragraph.get("text", ""))
        if not value:
            continue
        remaining = limit - size
        if remaining <= 0:
            break
        parts.append(value[:remaining])
        size += min(len(value), remaining)
    return "\n".join(parts)


def classify_study(manuscript: Mapping[str, Any]) -> dict[str, Any]:
    """Return transparent study signals used solely for routing review work."""

    text = _manuscript_text(manuscript)
    matches: dict[str, int] = {}
    for name, patterns in _PROFILE_SIGNALS.items():
        count = sum(1 for pattern in patterns if pattern.search(text))
        if count:
            matches[name] = count
    if not matches:
        matches["unspecified"] = 1
    ranked = sorted(matches, key=lambda item: (-matches[item], item))
    return {
        "signals": ranked,
        "signalStrength": matches,
        "method": "declared-keyword-routing",
        "requiresHumanConfirmation": ranked == ["unspecified"],
    }


def route_review_lenses(manuscript: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select complementary lenses without assigning fictional identities."""

    profile = classify_study(manuscript)
    signals = set(profile["signals"])
    chosen = ["evidence-lineage", "design-counterfactuals", "uncertainty-budget"]
    if {"human-participants", "intervention", "observational"} & signals:
        chosen.extend(["participant-stewardship", "boundary-transfer"])
    elif "nonhuman-model" in signals:
        chosen.extend(["measurement-chain", "boundary-transfer"])
    else:
        chosen.append("measurement-chain")
    if "computational" in signals:
        chosen.append("computational-replay")
    if "evidence-synthesis" in signals:
        chosen.append("synthesis-boundaries")
    if manuscript.get("tables") or manuscript.get("figures"):
        chosen.append("display-reconciliation")
    # Preserve meaningful order and cap routine routes.  Additional lenses can
    # still be requested explicitly by a caller.
    unique = list(dict.fromkeys(chosen))[:7]
    return [{"id": lens_id, **_LENS_CATALOG[lens_id]} for lens_id in unique]


def build_review_plan(manuscript: Mapping[str, Any]) -> dict[str, Any]:
    if not manuscript.get("manuscriptId"):
        raise ValidationError("review planning requires manuscriptId")
    if "confidential" not in manuscript:
        raise ValidationError("review planning requires an explicit confidential flag")
    profile = classify_study(manuscript)
    lenses = route_review_lenses(manuscript)
    plan_identity = {
        "manuscriptId": manuscript["manuscriptId"],
        "lenses": [lens["id"] for lens in lenses],
        "ingestionComplete": bool(manuscript.get("ingestion", {}).get("complete")),
    }
    passes = [
        {
            "sequence": index,
            "lensId": lens["id"],
            "targets": list(lens["targets"]),
        }
        for index, lens in enumerate(lenses, start=1)
    ]
    return {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "planId": _stable_id("plan", plan_identity),
        "manuscriptId": manuscript["manuscriptId"],
        "confidential": bool(manuscript["confidential"]),
        "ingestionComplete": bool(manuscript.get("ingestion", {}).get("complete")),
        "studyProfile": profile,
        "lenses": lenses,
        "passes": passes,
        "concerns": [],
        "revisionLedger": [],
        "guardrails": {
            "locationAnchorRequired": True,
            "metadataIsEvidence": False,
            "abstractCanEstablishFullTextSupport": False,
            "inaccessibleEvidenceRequiresAbstention": True,
        },
        "literatureReview": {
            "mode": "extensive",
            "confirmationRequired": True,
            "correctness": True,
            "completeness": True,
            "scope": "bibliography-and-targeted-gaps",
            "bibliographyEntries": len(manuscript.get("bibliography", [])),
            "citationOccurrences": len(manuscript.get("citationClusters", [])),
            "stateArtifact": ".ocsl-review/literature-audit.json",
            "unavailablePolicy": "continue-with-gaps",
        },
    }


def plan_review(manuscript: Mapping[str, Any]) -> dict[str, Any]:
    return build_review_plan(manuscript)


def _locator_index(manuscript: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for collection in ("paragraphs", "sentences", "tables", "figures"):
        for item in manuscript.get(collection, []):
            if item.get("id"):
                result[str(item["id"])] = item
    return result


def make_concern(
    manuscript: Mapping[str, Any],
    *,
    lens_id: str,
    locator_id: str,
    finding: str,
    basis: str,
    requested_resolution: str,
    priority: str = "substantial",
    confidence: float = 0.5,
    human_decision_required: bool = True,
    claim_id: str | None = None,
    verification_link_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Create a deterministic concern that cannot float free of the manuscript."""

    if lens_id not in _LENS_CATALOG:
        raise ValidationError(f"unknown review lens: {lens_id}")
    if priority not in CONCERN_PRIORITIES:
        raise ValidationError(f"unknown concern priority: {priority}")
    if not (0.0 <= confidence <= 1.0):
        raise ValidationError("concern confidence must be between zero and one")
    if not all(value.strip() for value in (finding, basis, requested_resolution)):
        raise ValidationError("concern finding, basis, and requested resolution are required")
    item = _locator_index(manuscript).get(locator_id)
    if item is None:
        raise ValidationError(f"concern locator is not present in the manuscript: {locator_id}")
    linked_verifications = list(dict.fromkeys(str(item) for item in verification_link_ids))
    if claim_id is not None and not claim_id.strip():
        raise ValidationError("claim_id cannot be blank")
    if any(not item.strip() for item in linked_verifications):
        raise ValidationError("verification link identifiers cannot be blank")
    identity = {
        "manuscriptId": manuscript.get("manuscriptId"),
        "lensId": lens_id,
        "locatorId": locator_id,
        "finding": finding.strip(),
        "claimId": claim_id,
        "verificationLinkIds": linked_verifications,
    }
    concern = {
        "concernId": _stable_id("concern", identity),
        "lensId": lens_id,
        "priority": priority,
        "anchor": {"locatorId": locator_id, "locator": item.get("locator", {})},
        "finding": finding.strip(),
        "basis": basis.strip(),
        "requestedResolution": requested_resolution.strip(),
        "confidence": round(float(confidence), 4),
        "humanDecisionRequired": bool(human_decision_required),
        "verificationState": "unassessed",
    }
    if claim_id is not None:
        concern["claimId"] = claim_id.strip()
    if linked_verifications:
        concern["verificationLinkIds"] = linked_verifications
    return concern


def make_revision_action(
    concern: Mapping[str, Any],
    *,
    action: str,
    state: str = "not-started",
    owner: str = "author",
    verification: str,
) -> dict[str, Any]:
    if not concern.get("concernId"):
        raise ValidationError("revision action requires a concernId")
    if state not in ACTION_STATES:
        raise ValidationError(f"unknown revision action state: {state}")
    if not action.strip() or not verification.strip() or not owner.strip():
        raise ValidationError("revision action, owner, and verification criterion are required")
    identity = {
        "concernId": concern["concernId"],
        "action": action.strip(),
        "verification": verification.strip(),
    }
    return {
        "actionId": _stable_id("action", identity),
        "concernId": concern["concernId"],
        "state": state,
        "owner": owner.strip(),
        "action": action.strip(),
        "verificationCriterion": verification.strip(),
    }


def attach_concern(
    plan: dict[str, Any], concern: Mapping[str, Any], action: Mapping[str, Any] | None = None
) -> None:
    lens_ids = {lens["id"] for lens in plan.get("lenses", [])}
    if concern.get("lensId") not in lens_ids:
        raise ValidationError("concern lens is not part of this review plan")
    if any(item.get("concernId") == concern.get("concernId") for item in plan.get("concerns", [])):
        raise ValidationError("duplicate concernId in review plan")
    plan.setdefault("concerns", []).append(dict(concern))
    if action is not None:
        if action.get("concernId") != concern.get("concernId"):
            raise ValidationError("revision action is bound to a different concern")
        plan.setdefault("revisionLedger", []).append(dict(action))


def empty_audit_summary(manuscript: Mapping[str, Any]) -> dict[str, Any]:
    """Return conservative zero-state denominators; never an implicit success."""

    coverage = manuscript.get("ingestion", {}).get("coverage", {})
    verification_limits = ["audit-not-yet-performed"]
    if not manuscript.get("ingestion", {}).get("complete"):
        verification_limits.append("manuscript-ingestion-incomplete")
    return {
        "schemaVersion": "1.0",
        "auditId": f"audit-{manuscript.get('manuscriptId', 'unknown')}",
        "auditCoverage": {
            "inTextCitationClustersTotal": int(coverage.get("citationClusters", 0)),
            "inTextCitationClustersProcessed": 0,
            "bibliographyEntriesTotal": int(coverage.get("bibliographyEntries", 0)),
            "bibliographyEntriesResolved": 0,
            "citationRequiredClaimsTotal": 0,
            "citationRequiredClaimsProcessed": 0,
            "sourcesFullTextChecked": 0,
            "sourcesAbstractOnly": 0,
            "sourcesInaccessible": 0,
        },
        "citedCorrectly": {
            "status": "not-fully-verifiable",
            "directlySupported": 0,
            "scopeCaveats": 0,
            "partialOrIndirect": 0,
            "contradicted": 0,
            "notAddressed": 0,
            "statusProblems": 0,
            "metadataProblems": 0,
        },
        "citedEnough": {
            "status": "not-fully-verifiable",
            "sufficientClaims": 0,
            "missingCitations": 0,
            "insufficientDirectness": 0,
            "insufficientScope": 0,
            "insufficientBreadth": 0,
            "insufficientCurrency": 0,
            "insufficientBalance": 0,
        },
        "verificationLimits": verification_limits,
    }


def new_review_package(
    manuscript: Mapping[str, Any],
    plan: Mapping[str, Any] | None = None,
    *,
    title: str = "Scientific review evidence audit",
    audit_graph: Mapping[str, Any] | None = None,
    audit_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_plan = dict(plan or build_review_plan(manuscript))
    if selected_plan.get("manuscriptId") != manuscript.get("manuscriptId"):
        raise ValidationError("review plan belongs to a different manuscript")
    identity = {
        "manuscriptId": manuscript.get("manuscriptId"),
        "planId": selected_plan.get("planId"),
    }
    selected_audit = dict(audit_graph) if audit_graph is not None else None
    if selected_audit is not None:
        graph_manuscript = selected_audit.get("manuscript", {})
        graph_id = graph_manuscript.get("id") if isinstance(graph_manuscript, Mapping) else None
        if graph_id != manuscript.get("manuscriptId"):
            raise ValidationError("claim-citation audit belongs to a different manuscript")
        from .audit import build_audit_summary, validate_audit_graph

        validate_audit_graph(selected_audit)
        if audit_summary is None:
            audit_summary = build_audit_summary(selected_audit)
    selected_summary = dict(audit_summary) if audit_summary is not None else None
    return {
        "schemaVersion": PACKAGE_SCHEMA_VERSION,
        "packageId": _stable_id("review", identity),
        "title": title.strip() or "Scientific review evidence audit",
        "confidential": bool(manuscript.get("confidential", True)),
        "manuscript": {
            "manuscriptId": manuscript.get("manuscriptId"),
            "sourceFileName": manuscript.get("source", {}).get("fileName"),
            "sourceSha256": manuscript.get("source", {}).get("sha256"),
            "ingestionComplete": bool(manuscript.get("ingestion", {}).get("complete")),
        },
        "reviewPlan": selected_plan,
        "auditGraph": selected_audit,
        "concerns": [dict(item) for item in selected_plan.get("concerns", [])],
        "revisionLedger": [dict(item) for item in selected_plan.get("revisionLedger", [])],
        "auditSummary": selected_summary,
    }


__all__ = [
    "ACTION_STATES",
    "CONCERN_PRIORITIES",
    "attach_concern",
    "build_review_plan",
    "classify_study",
    "empty_audit_summary",
    "make_concern",
    "make_revision_action",
    "new_review_package",
    "plan_review",
    "route_review_lenses",
]
