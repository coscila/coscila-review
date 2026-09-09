"""Render review packages to a transactional JSON/Markdown/DOCX bundle."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .errors import ReviewError, ValidationError

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_DCTERMS_NS = "http://purl.org/dc/terms/"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("w", _W_NS)
ET.register_namespace("r", _R_NS)
ET.register_namespace("cp", _CP_NS)
ET.register_namespace("dc", _DC_NS)
ET.register_namespace("dcterms", _DCTERMS_NS)
ET.register_namespace("xsi", _XSI_NS)
ET.register_namespace("ep", _EP_NS)
ET.register_namespace("vt", _VT_NS)

_VERDICTS = {"yes", "no", "partly", "not-fully-verifiable"}


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be an object")
    return value


def validate_review_package(package: Mapping[str, Any]) -> None:
    """Validate structural invariants and conservative affirmative verdicts."""

    for key in (
        "schemaVersion",
        "packageId",
        "title",
        "confidential",
        "manuscript",
        "reviewPlan",
        "auditGraph",
        "concerns",
        "revisionLedger",
        "auditSummary",
    ):
        if key not in package:
            raise ValidationError(f"review package is missing {key}")
    if not isinstance(package["confidential"], bool):
        raise ValidationError("review package confidential flag must be boolean")
    manuscript = _require_mapping(package["manuscript"], "manuscript")
    plan = _require_mapping(package["reviewPlan"], "reviewPlan")
    if manuscript.get("manuscriptId") != plan.get("manuscriptId"):
        raise ValidationError("review package manuscript and plan identifiers differ")
    if package["confidential"] != plan.get("confidential"):
        raise ValidationError("review package and plan confidentiality flags differ")
    if manuscript.get("ingestionComplete") != plan.get("ingestionComplete"):
        raise ValidationError("review package and plan ingestion status differ")
    audit_graph = package["auditGraph"]
    graph_claims: dict[str, Mapping[str, Any]] = {}
    graph_links: dict[str, Mapping[str, Any]] = {}
    expected_summary: Mapping[str, Any] | None = None
    if audit_graph is not None and not isinstance(audit_graph, Mapping):
        raise ValidationError("auditGraph must be a canonical audit graph or null")
    if isinstance(audit_graph, Mapping):
        from .audit import build_audit_summary, validate_audit_graph

        validate_audit_graph(dict(audit_graph))
        expected_summary = build_audit_summary(dict(audit_graph))
        graph_manuscript = audit_graph.get("manuscript", {})
        graph_id = graph_manuscript.get("id") if isinstance(graph_manuscript, Mapping) else None
        if graph_id != manuscript.get("manuscriptId"):
            raise ValidationError("auditGraph belongs to a different manuscript")
        if graph_manuscript.get("confidential") != package["confidential"]:
            raise ValidationError("auditGraph and review package confidentiality flags differ")
        if graph_manuscript.get("sourceSha256") != manuscript.get("sourceSha256"):
            raise ValidationError("auditGraph and review package source hashes differ")
        if graph_manuscript.get("ingestionComplete") != manuscript.get("ingestionComplete"):
            raise ValidationError("auditGraph and review package ingestion status differ")
        graph_claims = {str(item["id"]): item for item in audit_graph.get("claims", [])}
        graph_links = {str(item["id"]): item for item in audit_graph.get("verificationLinks", [])}
    concerns = package["concerns"]
    ledger = package["revisionLedger"]
    if not isinstance(concerns, list) or not isinstance(ledger, list):
        raise ValidationError("concerns and revisionLedger must be arrays")
    concern_ids: set[str] = set()
    for index, concern_value in enumerate(concerns):
        concern = _require_mapping(concern_value, f"concerns[{index}]")
        concern_id = concern.get("concernId")
        anchor = _require_mapping(concern.get("anchor"), f"concerns[{index}].anchor")
        if not isinstance(concern_id, str) or not concern_id:
            raise ValidationError(f"concerns[{index}].concernId is required")
        if concern_id in concern_ids:
            raise ValidationError(f"duplicate concernId: {concern_id}")
        concern_ids.add(concern_id)
        if not anchor.get("locatorId") or not isinstance(anchor.get("locator"), Mapping):
            raise ValidationError(f"concerns[{index}] has no manuscript location anchor")
        claim_id = concern.get("claimId")
        verification_ids = concern.get("verificationLinkIds", [])
        if claim_id is not None and claim_id not in graph_claims:
            raise ValidationError(f"concerns[{index}] refers to an unknown audit claim")
        if not isinstance(verification_ids, list):
            raise ValidationError(f"concerns[{index}].verificationLinkIds must be an array")
        for link_id in verification_ids:
            link = graph_links.get(link_id)
            if link is None:
                raise ValidationError(f"concerns[{index}] refers to an unknown verification link")
            if claim_id is not None and link.get("claimId") != claim_id:
                raise ValidationError(
                    f"concerns[{index}] links a verification record for a different claim"
                )
    action_ids: set[str] = set()
    for index, action_value in enumerate(ledger):
        action = _require_mapping(action_value, f"revisionLedger[{index}]")
        if action.get("concernId") not in concern_ids:
            raise ValidationError(f"revisionLedger[{index}] refers to an unknown concern")
        action_id = action.get("actionId")
        if not isinstance(action_id, str) or not action_id or action_id in action_ids:
            raise ValidationError(f"revisionLedger[{index}] has a missing or duplicate actionId")
        action_ids.add(action_id)
    summary_value = package["auditSummary"]
    if summary_value is None:
        return
    summary = _require_mapping(summary_value, "auditSummary")
    expected_summary_fields = {
        "schemaVersion",
        "auditId",
        "auditCoverage",
        "citedCorrectly",
        "citedEnough",
        "verificationLimits",
    }
    if set(summary) != expected_summary_fields:
        raise ValidationError("auditSummary must use the canonical six-field shape")
    if summary.get("schemaVersion") not in {"1.0", "1.1"} or not summary.get("auditId"):
        raise ValidationError("auditSummary schemaVersion or auditId is invalid")
    if isinstance(audit_graph, Mapping) and summary.get("auditId") != audit_graph.get("auditId"):
        raise ValidationError("auditSummary and auditGraph identifiers differ")
    legacy = isinstance(audit_graph, Mapping) and audit_graph.get("schemaVersion") == "1.0" and summary.get("schemaVersion") == "1.0"
    if expected_summary is not None and dict(summary) != dict(expected_summary) and not legacy:
        raise ValidationError("auditSummary does not match the deterministic auditGraph summary")
    coverage = _require_mapping(summary.get("auditCoverage"), "auditSummary.auditCoverage")
    correctness = _require_mapping(summary.get("citedCorrectly"), "auditSummary.citedCorrectly")
    sufficiency = _require_mapping(summary.get("citedEnough"), "auditSummary.citedEnough")
    verification_limits = summary.get("verificationLimits", [])
    if not isinstance(verification_limits, list) or not all(
        isinstance(item, str) for item in verification_limits
    ):
        raise ValidationError("auditSummary.verificationLimits must be an array of strings")
    for name, block in (("citedCorrectly", correctness), ("citedEnough", sufficiency)):
        if block.get("status") not in _VERDICTS:
            raise ValidationError(f"auditSummary.{name}.status is invalid")
    numeric_fields = {
        "inTextCitationClustersTotal",
        "inTextCitationClustersProcessed",
        "bibliographyEntriesTotal",
        "bibliographyEntriesResolved",
        "citationRequiredClaimsTotal",
        "citationRequiredClaimsProcessed",
        "sourcesFullTextChecked",
        "sourcesAbstractOnly",
        "sourcesInaccessible",
    }
    for key in numeric_fields:
        value = coverage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(
                f"auditSummary.auditCoverage.{key} must be a non-negative integer"
            )
    for processed, total in (
        ("inTextCitationClustersProcessed", "inTextCitationClustersTotal"),
        ("bibliographyEntriesResolved", "bibliographyEntriesTotal"),
        ("citationRequiredClaimsProcessed", "citationRequiredClaimsTotal"),
    ):
        if coverage[processed] > coverage[total]:
            raise ValidationError(f"auditSummary {processed} cannot exceed {total}")
    for block_name, block in (("citedCorrectly", correctness), ("citedEnough", sufficiency)):
        for key, value in block.items():
            if key in {"status", "verificationLimits"}:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValidationError(
                    f"auditSummary.{block_name}.{key} must be a non-negative integer"
                )
    affirmative = correctness.get("status") == "yes" or sufficiency.get("status") == "yes"
    if affirmative:
        if expected_summary is None:
            raise ValidationError("affirmative citation verdict requires its canonical audit graph")
        # Canonical graph validation applies each axis's own blockers. Legacy
        # summaries are retained as input only; every rendered view recomputes them.
        return


def _md_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _claim_audit_rows(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    graph = package.get("auditGraph")
    if not isinstance(graph, Mapping):
        return []
    completeness = {item.get("claimId"): item for item in graph.get("completenessAssessments", [])}
    clusters = {item["id"]: item for item in graph.get("citationClusters", [])}
    references = {item["id"]: item for item in graph.get("references", [])}
    links: dict[str, list[Mapping[str, Any]]] = {}
    for item in graph.get("verificationLinks", []):
        links.setdefault(str(item.get("claimId", "")), []).append(item)
    rows: list[dict[str, Any]] = []
    for claim in graph.get("claims", []):
        claim_id = str(claim.get("id", ""))
        claim_links = links.get(claim_id, [])
        assessment = completeness.get(claim_id, {})
        locator = claim.get("locator", {})
        if isinstance(locator, Mapping):
            location = "/".join(
                str(locator.get(key))
                for key in ("section", "paragraph", "sentence", "clause")
                if locator.get(key) is not None
            )
        else:
            location = str(locator)
        entries = [(link, link["referenceId"], link.get("citationClusterId"), None)
                   for link in claim_links]
        assessed = {(link["referenceId"], link.get("citationClusterId")) for link in claim_links}
        for cluster_id in claim.get("citationClusterIds", []):
            cluster = clusters.get(cluster_id, {})
            entries.extend((None, reference_id, cluster_id, None)
                           for reference_id in cluster.get("referenceIds", [])
                           if (reference_id, cluster_id) not in assessed)
            entries.extend((None, None, cluster_id, marker)
                           for marker in cluster.get("unresolvedMarkers", []))
            if not cluster.get("referenceIds") and not cluster.get("unresolvedMarkers"):
                entries.append((None, None, cluster_id, None))
        for link, reference_id, cluster_id, marker in entries or [(None, None, None, None)]:
            support = "not assessed; pending; not confident"
            linked_location = location
            actions = []
            if reference_id is not None:
                linked_location += f"; reference {reference_id}"
            if cluster_id is not None:
                linked_location += f"; locus {cluster_id}"
            if marker is not None:
                linked_location += f"; unresolved marker {marker}"
            if link is not None:
                label = link.get("confidenceLabel", "not-confident").replace("-", " ")
                support = f"{link['supportVerdict']}; {link.get('evidenceTier', 'legacy-tier-unknown')}; {label}"
                if cluster_id is None:
                    linked_location += "; locus legacy-locus-unknown"
                source_locations = "; ".join(str(item['locator']) for item in link["evidenceLocations"])
                if source_locations:
                    linked_location += f"; source: {source_locations}"
                actions.append(str(link["humanAction"]))
            elif reference_id is not None:
                state = references.get(reference_id, {}).get("acquisition", {}).get("state", "pending")
                support = f"not assessed; acquisition {state}; not confident"
                actions.append("Obtain, read, and assess this source at this citation locus.")
            elif claim.get("citationRequired"):
                actions.append("Resolve this citation binding and record its assessment.")
            if assessment.get("requiredHumanAction"):
                actions.append(str(assessment["requiredHumanAction"]))
            rows.append({
                "atomicClaim": f"{claim_id}: {claim.get('text', '')}",
                "manuscriptLocation": linked_location,
                "supportVerdict": support,
                "citationCompletenessVerdict": assessment.get("verdict", "not assessed"),
                "requiredHumanAction": "; ".join(dict.fromkeys(actions)) or "none recorded",
            })
    return rows


def _summary_view(package: Mapping[str, Any]) -> Mapping[str, Any]:
    graph = package.get("auditGraph")
    if isinstance(graph, Mapping):
        from .audit import build_audit_summary
        return build_audit_summary(dict(graph))
    summary = package.get("auditSummary")
    if isinstance(summary, Mapping):
        return summary
    return {
        "auditCoverage": {
            "inTextCitationClustersTotal": 0,
            "inTextCitationClustersProcessed": 0,
            "bibliographyEntriesTotal": 0,
            "bibliographyEntriesResolved": 0,
            "citationRequiredClaimsTotal": 0,
            "citationRequiredClaimsProcessed": 0,
            "sourcesFullTextChecked": 0,
            "sourcesAbstractOnly": 0,
            "sourcesInaccessible": 0,
        },
        "citedCorrectly": {"status": "not-fully-verifiable"},
        "citedEnough": {"status": "not-fully-verifiable"},
        "verificationLimits": ["audit-summary-unavailable"],
    }


def _concern_graph_context(
    package: Mapping[str, Any], concern: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    graph = package.get("auditGraph")
    if not isinstance(graph, Mapping):
        return None, []
    claims = {str(item.get("id")): item for item in graph.get("claims", [])}
    links = {str(item.get("id")): item for item in graph.get("verificationLinks", [])}
    claim = claims.get(str(concern.get("claimId"))) if concern.get("claimId") else None
    selected = [
        links[str(link_id)]
        for link_id in concern.get("verificationLinkIds", [])
        if str(link_id) in links
    ]
    return claim, selected


def render_markdown(package: Mapping[str, Any]) -> str:
    validate_review_package(package)
    summary = _summary_view(package)
    coverage = summary["auditCoverage"]
    lines: list[str] = [f"# {_md_escape(package['title'])}", ""]
    lines.extend(
        [
            f"- Package: `{_md_escape(package['packageId'])}`",
            f"- Manuscript: `{_md_escape(package['manuscript']['manuscriptId'])}`",
            f"- Handling: {'confidential' if package['confidential'] else 'non-confidential'}",
            f"- Ingestion complete: {'yes' if package['manuscript']['ingestionComplete'] else 'no'}",
            "",
            "## Audit determination",
            "",
            f"- Citation support: **{_md_escape(summary['citedCorrectly']['status'])}**",
            f"- Citation sufficiency: **{_md_escape(summary['citedEnough']['status'])}**",
            "",
            "| Coverage item | Processed | Total |",
            "| --- | ---: | ---: |",
            f"| In-text citation clusters | {coverage['inTextCitationClustersProcessed']} | {coverage['inTextCitationClustersTotal']} |",
            f"| Bibliography identities | {coverage['bibliographyEntriesResolved']} | {coverage['bibliographyEntriesTotal']} |",
            f"| Claims requiring citations | {coverage['citationRequiredClaimsProcessed']} | {coverage['citationRequiredClaimsTotal']} |",
            "",
            f"Full texts checked: {coverage['sourcesFullTextChecked']}; abstract-only sources: {coverage['sourcesAbstractOnly']}; inaccessible sources: {coverage['sourcesInaccessible']}.",
            f"Bibliography entries attempted: {coverage.get('referencesAttempted', 0)} / {coverage['bibliographyEntriesTotal']}; assessed: {coverage.get('referencesAssessed', 0)}; pending: {coverage.get('referencesPending', 0)}; blocked or unavailable: {coverage.get('referencesBlocked', 0)}.",
            f"Additional sources for targeted literature gaps: {coverage.get('additionalReferencesTotal', 0)}. Source access and reading counts include these sources.",
            f"Full-text entries obtained: {coverage.get('sourcesFullTextObtained', 0)}; fully read: {coverage['sourcesFullTextChecked']}. Evidence used by assessments: full text {coverage.get('evidenceFullTextUsed', 0)}, abstract {coverage.get('evidenceAbstractUsed', 0)}; abstentions {coverage.get('verificationLinksAbstained', 0)} / {coverage.get('verificationLinksTotal', 0)}.",
            f"Correctness limits: {_md_escape('; '.join(summary['citedCorrectly'].get('verificationLimits', [])) or 'none recorded')}",
            f"Completeness limits: {_md_escape('; '.join(summary['citedEnough'].get('verificationLimits', [])) or 'none recorded')}",
            "",
            "## Review route",
            "",
        ]
    )
    for lens in package["reviewPlan"].get("lenses", []):
        lines.append(f"- **{_md_escape(lens.get('label'))}:** {_md_escape(lens.get('mission'))}")
    if not package["reviewPlan"].get("lenses"):
        lines.append("No review lenses were recorded.")
    lines.extend(["", "## Claim–citation audit", ""])
    audits = _claim_audit_rows(package)
    if not audits:
        lines.append("No claim-level audit records have been added.")
    else:
        lines.extend(
            [
                "| Claim | Location | Support | Completeness | Human action |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in audits:
            location = item.get("manuscriptLocation") or item.get("locatorId") or ""
            lines.append(
                "| "
                + " | ".join(
                    _md_escape(value)
                    for value in (
                        item.get("atomicClaim"),
                        location,
                        item.get("supportVerdict"),
                        item.get("citationCompletenessVerdict"),
                        item.get("requiredHumanAction"),
                    )
                )
                + " |"
            )
    lines.extend(["", "## Anchored concerns", ""])
    if not package["concerns"]:
        lines.append("No concerns have been recorded.")
    for index, concern in enumerate(package["concerns"], start=1):
        canonical_claim, canonical_links = _concern_graph_context(package, concern)
        lines.extend(
            [
                f"### {index}. {_md_escape(concern['finding'])}",
                "",
                f"Location: `{_md_escape(concern['anchor']['locatorId'])}`  ",
                f"Lens: `{_md_escape(concern.get('lensId'))}`  ",
                f"Priority: `{_md_escape(concern.get('priority'))}`  ",
                f"Confidence: `{_md_escape(concern.get('confidence'))}`",
                "",
                _md_escape(concern.get("basis")),
                "",
                f"Requested resolution: {_md_escape(concern.get('requestedResolution'))}",
                "",
            ]
        )
        if canonical_claim is not None:
            lines.extend(
                [
                    (
                        f"Audit claim `{_md_escape(canonical_claim.get('id'))}`: "
                        f"{_md_escape(canonical_claim.get('text'))}"
                    ),
                    "",
                ]
            )
        for link in canonical_links:
            lines.extend(
                [
                    (
                        f"Verification `{_md_escape(link.get('id'))}` against "
                        f"`{_md_escape(link.get('referenceId'))}`: "
                        f"{_md_escape(link.get('supportVerdict'))} — "
                        f"{_md_escape(link.get('supportReason'))}"
                    ),
                    "",
                ]
            )
    lines.extend(["## Revision ledger", ""])
    if not package["revisionLedger"]:
        lines.append("No revision actions have been recorded.")
    else:
        lines.extend(
            [
                "| Action | Concern | State | Owner | Verification criterion |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for action in package["revisionLedger"]:
            lines.append(
                "| "
                + " | ".join(
                    _md_escape(value)
                    for value in (
                        action.get("action"),
                        action.get("concernId"),
                        action.get("state"),
                        action.get("owner"),
                        action.get("verificationCriterion"),
                    )
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def _w(local: str) -> str:
    return f"{{{_W_NS}}}{local}"


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _add_run(paragraph: ET.Element, text: Any, *, bold: bool = False) -> None:
    run = ET.SubElement(paragraph, _w("r"))
    if bold:
        properties = ET.SubElement(run, _w("rPr"))
        ET.SubElement(properties, _w("b"))
    value = str(text if text is not None else "")
    chunks = value.split("\n")
    for index, chunk in enumerate(chunks):
        if index:
            ET.SubElement(run, _w("br"))
        text_node = ET.SubElement(run, _w("t"))
        if chunk.startswith(" ") or chunk.endswith(" "):
            text_node.set(f"{{{_XML_NS}}}space", "preserve")
        text_node.text = chunk


def _add_paragraph(
    body: ET.Element, text: Any = "", *, style: str | None = None, bold: bool = False
) -> ET.Element:
    paragraph = ET.SubElement(body, _w("p"))
    if style:
        properties = ET.SubElement(paragraph, _w("pPr"))
        style_node = ET.SubElement(properties, _w("pStyle"))
        style_node.set(_w("val"), style)
    _add_run(paragraph, text, bold=bold)
    return paragraph


def _add_table(body: ET.Element, headers: Sequence[Any], rows: Iterable[Sequence[Any]]) -> None:
    table = ET.SubElement(body, _w("tbl"))
    properties = ET.SubElement(table, _w("tblPr"))
    style = ET.SubElement(properties, _w("tblStyle"))
    style.set(_w("val"), "TableGrid")
    width = ET.SubElement(properties, _w("tblW"))
    width.set(_w("w"), "0")
    width.set(_w("type"), "auto")
    all_rows = [list(headers), *[list(row) for row in rows]]
    for row_index, values in enumerate(all_rows):
        row = ET.SubElement(table, _w("tr"))
        for value in values:
            cell = ET.SubElement(row, _w("tc"))
            cell_properties = ET.SubElement(cell, _w("tcPr"))
            cell_width = ET.SubElement(cell_properties, _w("tcW"))
            cell_width.set(_w("w"), "0")
            cell_width.set(_w("type"), "auto")
            _add_paragraph(cell, value, bold=row_index == 0)


def _document_xml(package: Mapping[str, Any]) -> bytes:
    document = ET.Element(_w("document"))
    body = ET.SubElement(document, _w("body"))
    _add_paragraph(body, package["title"], style="Title")
    classification = (
        "CONFIDENTIAL MANUSCRIPT" if package["confidential"] else "NON-CONFIDENTIAL MATERIAL"
    )
    _add_paragraph(body, classification, bold=True)
    _add_paragraph(body, f"Package {package['packageId']}")
    _add_paragraph(body, "Audit determination", style="Heading1")
    summary = _summary_view(package)
    _add_paragraph(body, f"Citation support: {summary['citedCorrectly']['status']}")
    _add_paragraph(body, f"Citation sufficiency: {summary['citedEnough']['status']}")
    coverage = summary["auditCoverage"]
    _add_paragraph(body, f"Bibliography entries: attempted {coverage.get('referencesAttempted', 0)} / {coverage['bibliographyEntriesTotal']}; assessed {coverage.get('referencesAssessed', 0)}; pending {coverage.get('referencesPending', 0)}; blocked or unavailable {coverage.get('referencesBlocked', 0)}.")
    _add_paragraph(body, f"Additional sources for targeted literature gaps: {coverage.get('additionalReferencesTotal', 0)}. Source access and reading counts include these sources.")
    _add_paragraph(body, f"Full-text entries: obtained {coverage.get('sourcesFullTextObtained', 0)}, fully read {coverage['sourcesFullTextChecked']}. Assessments using full text {coverage.get('evidenceFullTextUsed', 0)}, abstract {coverage.get('evidenceAbstractUsed', 0)}; abstentions {coverage.get('verificationLinksAbstained', 0)} / {coverage.get('verificationLinksTotal', 0)}.")
    for axis in ("citedCorrectly", "citedEnough"):
        _add_paragraph(body, f"{axis} limits: " + ("; ".join(summary[axis].get("verificationLimits", [])) or "none recorded"))
    _add_table(
        body,
        ["Coverage item", "Processed", "Total"],
        [
            [
                "In-text citation clusters",
                coverage["inTextCitationClustersProcessed"],
                coverage["inTextCitationClustersTotal"],
            ],
            [
                "Bibliography identities",
                coverage["bibliographyEntriesResolved"],
                coverage["bibliographyEntriesTotal"],
            ],
            [
                "Claims requiring citations",
                coverage["citationRequiredClaimsProcessed"],
                coverage["citationRequiredClaimsTotal"],
            ],
        ],
    )
    _add_paragraph(body, "Review route", style="Heading1")
    lenses = package["reviewPlan"].get("lenses", [])
    if lenses:
        _add_table(
            body,
            ["Lens", "Purpose"],
            [[lens.get("label", ""), lens.get("mission", "")] for lens in lenses],
        )
    else:
        _add_paragraph(body, "No review lenses were recorded.")
    _add_paragraph(body, "Claim–citation audit", style="Heading1")
    audits = _claim_audit_rows(package)
    if audits:
        _add_table(
            body,
            ["Claim", "Location", "Support", "Completeness", "Human action"],
            [
                [
                    item.get("atomicClaim", ""),
                    item.get("manuscriptLocation") or item.get("locatorId", ""),
                    item.get("supportVerdict", ""),
                    item.get("citationCompletenessVerdict", ""),
                    item.get("requiredHumanAction", ""),
                ]
                for item in audits
            ],
        )
    else:
        _add_paragraph(body, "No claim-level audit records have been added.")
    _add_paragraph(body, "Anchored concerns", style="Heading1")
    if not package["concerns"]:
        _add_paragraph(body, "No concerns have been recorded.")
    for index, concern in enumerate(package["concerns"], start=1):
        canonical_claim, canonical_links = _concern_graph_context(package, concern)
        _add_paragraph(body, f"{index}. {concern['finding']}", style="Heading2")
        _add_paragraph(body, f"Location: {concern['anchor']['locatorId']}")
        _add_paragraph(
            body,
            f"Lens: {concern.get('lensId', '')}; priority: {concern.get('priority', '')}; confidence: {concern.get('confidence', '')}",
        )
        _add_paragraph(body, concern.get("basis", ""))
        _add_paragraph(body, f"Requested resolution: {concern.get('requestedResolution', '')}")
        if canonical_claim is not None:
            _add_paragraph(
                body,
                f"Audit claim {canonical_claim.get('id', '')}: {canonical_claim.get('text', '')}",
            )
        for link in canonical_links:
            _add_paragraph(
                body,
                f"Verification {link.get('id', '')} against {link.get('referenceId', '')}: "
                f"{link.get('supportVerdict', '')} — {link.get('supportReason', '')}",
            )
    _add_paragraph(body, "Revision ledger", style="Heading1")
    ledger = package["revisionLedger"]
    if ledger:
        _add_table(
            body,
            ["Action", "Concern", "State", "Owner", "Verification criterion"],
            [
                [
                    item.get("action", ""),
                    item.get("concernId", ""),
                    item.get("state", ""),
                    item.get("owner", ""),
                    item.get("verificationCriterion", ""),
                ]
                for item in ledger
            ],
        )
    else:
        _add_paragraph(body, "No revision actions have been recorded.")
    section = ET.SubElement(body, _w("sectPr"))
    page_size = ET.SubElement(section, _w("pgSz"))
    page_size.set(_w("w"), "11906")
    page_size.set(_w("h"), "16838")
    margins = ET.SubElement(section, _w("pgMar"))
    for key, value in {
        "top": "1417",
        "right": "1417",
        "bottom": "1417",
        "left": "1417",
        "header": "708",
        "footer": "708",
        "gutter": "0",
    }.items():
        margins.set(_w(key), value)
    return _xml_bytes(document)


def _styles_xml() -> bytes:
    styles = ET.Element(_w("styles"))
    defaults = ET.SubElement(styles, _w("docDefaults"))
    run_defaults = ET.SubElement(defaults, _w("rPrDefault"))
    run_properties = ET.SubElement(run_defaults, _w("rPr"))
    size = ET.SubElement(run_properties, _w("sz"))
    size.set(_w("val"), "21")
    paragraph_defaults = ET.SubElement(defaults, _w("pPrDefault"))
    paragraph_properties = ET.SubElement(paragraph_defaults, _w("pPr"))
    spacing = ET.SubElement(paragraph_properties, _w("spacing"))
    spacing.set(_w("after"), "120")
    spacing.set(_w("line"), "276")
    spacing.set(_w("lineRule"), "auto")
    for style_id, name, based_on, size_value in (
        ("Normal", "Normal", None, "21"),
        ("Title", "Title", "Normal", "32"),
        ("Heading1", "Heading 1", "Normal", "27"),
        ("Heading2", "Heading 2", "Normal", "23"),
    ):
        style = ET.SubElement(styles, _w("style"))
        style.set(_w("type"), "paragraph")
        style.set(_w("styleId"), style_id)
        if style_id == "Normal":
            style.set(_w("default"), "1")
        name_node = ET.SubElement(style, _w("name"))
        name_node.set(_w("val"), name)
        if based_on:
            based = ET.SubElement(style, _w("basedOn"))
            based.set(_w("val"), based_on)
        run_properties = ET.SubElement(style, _w("rPr"))
        font_size = ET.SubElement(run_properties, _w("sz"))
        font_size.set(_w("val"), size_value)
        if style_id != "Normal":
            ET.SubElement(run_properties, _w("b"))
    table_style = ET.SubElement(styles, _w("style"))
    table_style.set(_w("type"), "table")
    table_style.set(_w("styleId"), "TableGrid")
    table_name = ET.SubElement(table_style, _w("name"))
    table_name.set(_w("val"), "Table Grid")
    return _xml_bytes(styles)


def _content_types_xml() -> bytes:
    root = ET.Element(f"{{{_CONTENT_TYPES_NS}}}Types")
    for extension, content_type in (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
    ):
        node = ET.SubElement(root, f"{{{_CONTENT_TYPES_NS}}}Default")
        node.set("Extension", extension)
        node.set("ContentType", content_type)
    for part_name, content_type in (
        (
            "/word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        ),
        (
            "/word/styles.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
        ),
        ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
        (
            "/docProps/app.xml",
            "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        ),
    ):
        node = ET.SubElement(root, f"{{{_CONTENT_TYPES_NS}}}Override")
        node.set("PartName", part_name)
        node.set("ContentType", content_type)
    return _xml_bytes(root)


def _root_relationships_xml() -> bytes:
    root = ET.Element(f"{{{_PKG_REL_NS}}}Relationships")
    for relationship_id, relationship_type, target in (
        (
            "rId1",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
            "word/document.xml",
        ),
        (
            "rId2",
            "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
            "docProps/core.xml",
        ),
        (
            "rId3",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
            "docProps/app.xml",
        ),
    ):
        node = ET.SubElement(root, f"{{{_PKG_REL_NS}}}Relationship")
        node.set("Id", relationship_id)
        node.set("Type", relationship_type)
        node.set("Target", target)
    return _xml_bytes(root)


def _document_relationships_xml() -> bytes:
    root = ET.Element(f"{{{_PKG_REL_NS}}}Relationships")
    node = ET.SubElement(root, f"{{{_PKG_REL_NS}}}Relationship")
    node.set("Id", "rId1")
    node.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles")
    node.set("Target", "styles.xml")
    return _xml_bytes(root)


def _core_properties_xml(package: Mapping[str, Any]) -> bytes:
    root = ET.Element(f"{{{_CP_NS}}}coreProperties")
    title = ET.SubElement(root, f"{{{_DC_NS}}}title")
    title.text = str(package["title"])
    creator = ET.SubElement(root, f"{{{_DC_NS}}}creator")
    creator.text = "OC Science Lab Review"
    identifier = ET.SubElement(root, f"{{{_DC_NS}}}identifier")
    identifier.text = str(package["packageId"])
    return _xml_bytes(root)


def _app_properties_xml() -> bytes:
    root = ET.Element(f"{{{_EP_NS}}}Properties")
    application = ET.SubElement(root, f"{{{_EP_NS}}}Application")
    application.text = "OC Science Lab Review"
    app_version = ET.SubElement(root, f"{{{_EP_NS}}}AppVersion")
    app_version.text = "1.0"
    return _xml_bytes(root)


def _zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build_docx_bytes(package: Mapping[str, Any]) -> bytes:
    validate_review_package(package)
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        _zip_entry(archive, "[Content_Types].xml", _content_types_xml())
        _zip_entry(archive, "_rels/.rels", _root_relationships_xml())
        _zip_entry(archive, "word/document.xml", _document_xml(package))
        _zip_entry(archive, "word/styles.xml", _styles_xml())
        _zip_entry(archive, "word/_rels/document.xml.rels", _document_relationships_xml())
        _zip_entry(archive, "docProps/core.xml", _core_properties_xml(package))
        _zip_entry(archive, "docProps/app.xml", _app_properties_xml())
    value = output.getvalue()
    with zipfile.ZipFile(BytesIO(value)) as archive:
        required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
        if not required.issubset(archive.namelist()) or archive.testzip() is not None:
            raise ReviewError("generated DOCX package did not pass its integrity check")
    return value


def _bundle_base(output_stem: str | os.PathLike[str]) -> Path:
    candidate = Path(output_stem).expanduser()
    if candidate.suffix.casefold() in {".json", ".md", ".docx"}:
        candidate = candidate.with_suffix("")
    if not candidate.name or candidate.name in {".", ".."}:
        raise ReviewError("output stem must include a file name")
    parent = candidate.parent.resolve()
    return parent / candidate.name


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_bundle(
    base: Path, contents: Mapping[str, bytes], *, mode: int = 0o644
) -> dict[str, Path]:
    base.parent.mkdir(parents=True, exist_ok=True)
    targets = {suffix: base.parent / f"{base.name}{suffix}" for suffix in contents}
    for target in targets.values():
        if target.exists() and not target.is_file():
            raise ReviewError(f"output target is not a regular file: {target}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{base.name}.bundle-", dir=base.parent))
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    installed: set[str] = set()
    retain_temporary = False
    try:
        for suffix, data in contents.items():
            path = temporary / f"staged{suffix}"
            with path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, mode)
            staged[suffix] = path
        # Everything is constructed and durable before the first public path is
        # touched.  Existing bundles are recoverable if a rename fails midway.
        backup_dir = temporary / "previous"
        backup_dir.mkdir()
        try:
            for suffix, target in targets.items():
                if target.exists():
                    backup = backup_dir / f"previous{suffix}"
                    os.replace(target, backup)
                    backups[suffix] = backup
            for suffix, target in targets.items():
                os.replace(staged[suffix], target)
                installed.add(suffix)
            _fsync_directory(base.parent)
        except BaseException as commit_error:
            try:
                for suffix in installed:
                    target = targets[suffix]
                    if target.is_file():
                        target.unlink()
                for suffix, backup in backups.items():
                    if backup.exists():
                        os.replace(backup, targets[suffix])
                _fsync_directory(base.parent)
            except OSError as recovery_error:
                # Preserve the precisely scoped staging directory, including
                # backups, rather than deleting the only recoverable copies.
                retain_temporary = True
                raise ReviewError(
                    f"bundle commit failed and automatic recovery was incomplete; "
                    f"recover from {temporary}: {recovery_error}"
                ) from commit_error
            raise
        return targets
    except (OSError, ValueError) as exc:
        raise ReviewError(f"could not publish review bundle atomically: {exc}") from exc
    finally:
        if not retain_temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def render_review_package(
    package: Mapping[str, Any], output_stem: str | os.PathLike[str]
) -> dict[str, Path]:
    """Generate and publish same-stem JSON, Markdown, and DOCX outputs.

    All three representations are created and checked before any destination
    file is replaced.  A failed commit restores the prior complete bundle.
    """

    validate_review_package(package)
    if isinstance(package.get("auditGraph"), Mapping) and package["auditGraph"].get("schemaVersion") == "1.0":
        package = dict(package)
        package["auditSummary"] = dict(_summary_view(package))
    json_bytes = (json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    markdown_bytes = render_markdown(package).encode("utf-8")
    docx_bytes = build_docx_bytes(package)
    base = _bundle_base(output_stem)
    contents = {".json": json_bytes, ".md": markdown_bytes, ".docx": docx_bytes}
    graph = package.get("auditGraph") or {}
    manifest = {
        "schemaVersion": "1.0",
        "packageId": package["packageId"],
        "generator": "ocsl_review.rendering.render_review_package",
        "generatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inputCodeSha256": {
            "audit.py": hashlib.sha256(Path(__file__).with_name("audit.py").read_bytes()).hexdigest(),
            "claim-citation-audit.schema.json": hashlib.sha256((Path(__file__).resolve().parents[2] / "schemas" / "claim-citation-audit.schema.json").read_bytes()).hexdigest(),
        },
        "manuscriptSha256": package["manuscript"].get("sourceSha256"),
        "auditGraphSha256": hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "sourceDependencies": [
            {key: item[key] for key in ("id", "artifactPath", "artifactSha256", "textSha256", "referenceDigests")}
            for item in graph.get("sourceExaminations", [])
        ],
        "artifacts": [
            {"name": f"{base.name}{suffix}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for suffix, data in contents.items()
        ],
        "verificationLinks": len(graph.get("verificationLinks", [])),
    }
    contents[".manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    return _publish_bundle(
        base,
        contents,
        mode=0o600 if package["confidential"] else 0o644,
    )


def render_package(
    package: Mapping[str, Any], output_stem: str | os.PathLike[str]
) -> dict[str, Path]:
    return render_review_package(package, output_stem)


__all__ = [
    "build_docx_bytes",
    "render_markdown",
    "render_package",
    "render_review_package",
    "validate_review_package",
]
