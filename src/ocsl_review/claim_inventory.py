"""Build an auditable atomic-claim graph seed from an ingested manuscript."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .audit import validate_audit_graph
from .errors import ValidationError
from .util import atomic_json, sha256_file

_CITATION_TEXT = re.compile(
    r"\[(?:[^\]]*\d[^\]]*|(?:@[A-Za-z0-9_.:+/-]+(?:\s*;\s*)?)+)\]|"
    r"\((?=[^()]{1,240}\b(?:19|20)\d{2}[a-z]?\b)[^()]{1,240}\)"
)
_CLAUSE_BOUNDARY = re.compile(
    r"\s*;\s*|\s+[—–]\s+|,\s+(?=(?:but|whereas|while|although|yet)\b)", re.IGNORECASE
)
_METHOD_RESOURCE_AND_INTERNAL_PROCEDURE = re.compile(
    r"^(?P<prefix>.*?\b(?:used|uses|employed|employs|applied|applies)\s+)"
    r"(?P<resource>.+?\b(?:software(?:\s+environment)?|package|algorithm|tool)\b.*?)"
    r"\s+and\s+"
    r"(?P<procedure>(?:(?:an?|the)\s+)?(?:(?:independently\s+held\s+)?"
    r"(?:test|validation)|held[- ]out)\s+(?:set|sample|cohort)\b.*)$",
    re.IGNORECASE,
)
_INTERNAL_METHOD_PROCEDURE = re.compile(
    r"\b(?:(?:independently\s+held\s+)?(?:test|training|development|validation)|"
    r"held[- ]out)\s+"
    r"(?:set|sample|cohort|split)\b",
    re.IGNORECASE,
)
_EXTERNAL_PROVENANCE_LANGUAGE = re.compile(
    r"\b(?:previously described|established|standard|published|version|accession)\b",
    re.IGNORECASE,
)
_RESULT_SECTIONS = {"results", "findings"}
_METHOD_SECTIONS = {"methods", "materials and methods", "methodology", "supplementary methods"}
_INTERPRETATION_SECTIONS = {"discussion", "conclusion", "conclusions", "limitations"}
_EXTERNAL_SECTIONS = {"introduction", "background"}


def _clean_claim(text: str) -> str:
    value = _CITATION_TEXT.sub("", text)
    return re.sub(r"\s+", " ", value).strip(" ,;:.\t")


def _split_method_coordination(text: str) -> list[str]:
    match = _METHOD_RESOURCE_AND_INTERNAL_PROCEDURE.match(text)
    if match is None:
        return [_clean_claim(text)]
    prefix = match.group("prefix")
    resource = match.group("resource")
    procedure = match.group("procedure")
    return [
        _clean_claim(f"{prefix}{resource}"),
        _clean_claim(f"{prefix}{procedure}"),
    ]


def _atomic_clauses(text: str, *, split_method_coordination: bool = False) -> list[tuple[int, str]]:
    clauses: list[tuple[int, str]] = []
    for piece in _CLAUSE_BOUNDARY.split(text):
        if len(_clean_claim(piece)) < 3:
            continue
        variants = (
            _split_method_coordination(piece.strip())
            if split_method_coordination
            else [_clean_claim(piece)]
        )
        for variant in variants:
            if len(variant) >= 3:
                clauses.append((len(clauses) + 1, variant))
    return clauses


def _claim_type(text: str) -> str:
    lowered = text.casefold()
    if re.search(r"\b(first|earliest|novel|unprecedented|no previous|never before)\b", lowered):
        return "novelty"
    if re.search(r"[\"“”].+[\"“”]", text):
        return "quotation"
    if re.search(
        r"\b\d+(?:\.\d+)?\s*(?:%|percent|fold|mg|kg|ml|mm|cm|hz|p\s*[<=>])?\b",
        lowered,
    ):
        return "numeric"
    if re.search(
        r"\b(causes?|caused|drives?|led to|results? in|mediates?|prevents?|induces?)\b",
        lowered,
    ):
        return "causal"
    if re.search(r"\b(software|package|version|github|repository|algorithm|tool)\b", lowered):
        return "software"
    if re.search(r"\b(dataset|database|registry|cohort|accession)\b", lowered):
        return "dataset"
    if re.search(r"\b(method|assay|protocol|model|estimator|test|sequencing)\b", lowered):
        return "method"
    if re.search(r"\b(all|always|generally|widely|universal|across populations)\b", lowered):
        return "generalization"
    if re.search(r"\b(should|recommend|ought|must)\b", lowered):
        return "recommendation"
    if re.search(r"\b(suggests?|indicates?|implies?|consistent with|may reflect)\b", lowered):
        return "interpretation"
    return "fact"


def _origin(section: str, text: str) -> str:
    normalized = section.casefold().strip()
    if normalized in _RESULT_SECTIONS:
        return "manuscript-result"
    if normalized in _METHOD_SECTIONS:
        return "manuscript-method"
    if normalized in _INTERPRETATION_SECTIONS:
        return "manuscript-interpretation"
    if normalized in _EXTERNAL_SECTIONS:
        return "external-background"
    if re.search(
        r"\b(we|our|this study|these results|we observed|we found)\b", text, re.IGNORECASE
    ):
        return "manuscript-result"
    return "other"


def citation_requirement(origin: str, claim_type: str, text: str) -> tuple[bool, str]:
    """Decide evidence need without treating the manuscript's own results as uncited lore."""

    lowered = text.casefold()
    if claim_type == "novelty":
        return True, "Priority language requires a reproducible, multi-source literature search."
    if origin == "manuscript-result":
        return False, (
            "This is presented as a result generated by the manuscript and needs an internal "
            "result anchor, not an external citation."
        )
    if origin == "manuscript-method":
        if claim_type == "software" or _EXTERNAL_PROVENANCE_LANGUAGE.search(text):
            return True, (
                "A named or externally derived method, software release, or data resource "
                "needs provenance."
            )
        if _INTERNAL_METHOD_PROCEDURE.search(text) and not _EXTERNAL_PROVENANCE_LANGUAGE.search(
            text
        ):
            return False, (
                "This is an internally defined held-out or validation procedure and needs an "
                "internal methods anchor, not an external citation."
            )
        if claim_type in {"method", "dataset"}:
            return True, (
                "A named or externally derived method, software release, or data resource "
                "needs provenance."
            )
        return False, (
            "This clause describes the authors' own procedural action without invoking an "
            "external method identity."
        )
    if (
        origin == "manuscript-interpretation"
        and re.search(r"\b(our|this study|these results|we observed|we found)\b", lowered)
        and claim_type not in {"generalization", "causal", "fact"}
    ):
        return False, (
            "This is explicitly framed as interpretation of the manuscript's own findings."
        )
    if claim_type == "recommendation" and origin not in {"external-background", "other"}:
        return False, (
            "This is an authorial recommendation; its rationale should be assessed separately."
        )
    return True, (
        "The clause asserts externally checkable knowledge beyond the manuscript's newly "
        "reported observations."
    )


def _evidence_level(claim_type: str) -> str:
    if claim_type in {"dataset", "software"}:
        return "abstract-sufficient"
    return "full-text"


def _table_cells(table: dict[str, Any]) -> Iterator[dict[str, Any]]:
    rows = table.get("rows", [])
    if not isinstance(rows, list) or len(rows) < 2:
        return
    headers = [str(value).strip() for value in rows[0]]
    uses_row_labels = len(headers) > 1 and bool(headers[0])
    for row_number, row in enumerate(rows[1:], start=2):
        values = [str(value).strip() for value in row]
        row_label = values[0] if uses_row_labels and values else ""
        first_data_column = 1 if uses_row_labels else 0
        for zero_based_column in range(first_data_column, len(values)):
            value = values[zero_based_column]
            if not value:
                continue
            header = headers[zero_based_column] if zero_based_column < len(headers) else ""
            context = []
            if row_label:
                context.append(f"{headers[0]}: {row_label}")
            context.append(f"{header}: {value}" if header else value)
            yield {
                "row": row_number,
                "column": zero_based_column + 1,
                "text": "; ".join(context),
            }


def _section_maps(
    manuscript: dict[str, Any],
) -> tuple[dict[str, str], dict[str, int], dict[str, int]]:
    section_titles = {item["id"]: item["title"] for item in manuscript["sections"]}
    paragraph_ordinals = {item["id"]: item["ordinal"] for item in manuscript["paragraphs"]}
    sentence_numbers = {
        item["id"]: int(item["locator"].get("sentenceNumber", 1))
        for item in manuscript["sentences"]
    }
    return section_titles, paragraph_ordinals, sentence_numbers


def _reference_seed(entry: dict[str, Any]) -> dict[str, Any]:
    identifiers = {
        scheme: values[0]
        for scheme, values in entry.get("identifiers", {}).items()
        if isinstance(values, list) and values
    }
    return {
        "id": entry["id"],
        "raw": entry["raw"],
        "identifiers": identifiers,
        "identity": {
            "title": None,
            "authors": [],
            "year": None,
            "container": None,
            "version": None,
        },
        "resolutionStatus": "unresolved",
        "publicationStatus": {
            "state": "unknown",
            "checkedAt": None,
            "providers": [],
            "details": [],
        },
        "sourceRole": "other",
        "independenceGroup": None,
        "metadataProblems": [],
    }


def _claim_record(
    *,
    claim_id: str,
    locator: dict[str, Any],
    text: str,
    origin: str,
    citation_cluster_ids: list[str],
    citation_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    claim_type = _claim_type(text)
    required, reason = citation_requirement(origin, claim_type, text)
    if not required:
        binding = "not-applicable"
    elif not citation_cluster_ids:
        binding = "missing"
    elif all(citation_by_id[item]["referenceIds"] for item in citation_cluster_ids):
        binding = "exact"
    else:
        binding = "ambiguous"
    return {
        "id": claim_id,
        "locator": locator,
        "text": text,
        "origin": origin,
        "claimType": claim_type,
        "citationRequired": required,
        "citationRequirementReason": reason,
        "citationClusterIds": citation_cluster_ids,
        "citationBindingStatus": binding,
        "requiredEvidenceLevel": _evidence_level(claim_type) if required else "not-applicable",
        "processed": False,
    }


def build_claim_inventory(manuscript: dict[str, Any]) -> dict[str, Any]:
    """Return a valid, deliberately unverified claim-citation graph.

    The graph seed marks verification work as incomplete. A downstream verifier
    must resolve references, inspect sources, add links and completeness records,
    and only then mark claim and citation-cluster records processed.
    """

    if manuscript.get("schemaVersion") != "1.0.0":
        raise ValidationError("unsupported manuscript schema version")
    section_titles, paragraph_ordinals, _ = _section_maps(manuscript)
    bibliography = manuscript.get("bibliographyEntries", [])
    by_label = {str(item["label"]): item["id"] for item in bibliography if item.get("label")}
    references = [_reference_seed(item) for item in bibliography]
    citations: list[dict[str, Any]] = []
    citation_by_id: dict[str, dict[str, Any]] = {}
    for item in manuscript.get("citationClusters", []):
        reference_ids = []
        if item["style"] == "numeric":
            reference_ids = [
                by_label[token] for token in item.get("tokens", []) if token in by_label
            ]
        locator = item["locator"]
        record = {
            "id": item["id"],
            "locator": {
                "section": section_titles.get(locator["sectionId"], locator["sectionId"]),
                "paragraph": paragraph_ordinals.get(locator.get("paragraphId", ""), 0),
                "sentence": int(locator.get("sentenceNumber", 0)),
                "clause": 0,
                "page": locator.get("page"),
                "anchor": item["id"],
            },
            "raw": item["raw"],
            "markers": list(item.get("tokens", [])) or [item["raw"]],
            "referenceIds": sorted(set(reference_ids)),
            "processed": False,
        }
        citations.append(record)
        citation_by_id[record["id"]] = record

    bibliography_paragraph_ids = {
        item["id"]
        for item in manuscript.get("paragraphs", [])
        if item.get("kind") == "bibliography-entry"
    }
    sentences_by_section: dict[str, list[dict[str, Any]]] = {}
    for sentence in manuscript.get("sentences", []):
        if sentence.get("paragraphId") in bibliography_paragraph_ids:
            continue
        sentences_by_section.setdefault(sentence["sectionId"], []).append(sentence)

    claims: list[dict[str, Any]] = []
    for section_record in manuscript.get("sections", []):
        section_id = section_record["id"]
        section = section_titles.get(section_id, section_id)
        normalized_section = section.casefold().strip()

        for sentence in sentences_by_section.get(section_id, []):
            cluster_ids = list(sentence.get("citationClusterIds", []))
            locator = sentence["locator"]
            split_coordination = normalized_section in _METHOD_SECTIONS
            for clause_number, text in _atomic_clauses(
                sentence["text"], split_method_coordination=split_coordination
            ):
                claims.append(
                    _claim_record(
                        claim_id=f"claim-{len(claims) + 1:07d}",
                        locator={
                            "section": section,
                            "paragraph": paragraph_ordinals.get(sentence["paragraphId"], 0),
                            "sentence": int(locator.get("sentenceNumber", 0)),
                            "clause": clause_number,
                            "page": locator.get("page"),
                            "anchor": f"{sentence['id']}:c{clause_number}",
                        },
                        text=text,
                        origin=_origin(section, text),
                        citation_cluster_ids=cluster_ids,
                        citation_by_id=citation_by_id,
                    )
                )

    for table in manuscript.get("tables", []):
        section = section_titles.get(table["sectionId"], table["sectionId"])
        table_locator = table["locator"]
        for cell in _table_cells(table):
            anchor = f"{table['id']}:r{cell['row']}:c{cell['column']}"
            text = cell["text"]
            claims.append(
                _claim_record(
                    claim_id=f"claim-{len(claims) + 1:07d}",
                    locator={
                        "section": section,
                        "paragraph": 0,
                        "sentence": 0,
                        "clause": 0,
                        "page": table_locator.get("page"),
                        "anchor": anchor,
                    },
                    text=text,
                    origin=_origin(section, text),
                    citation_cluster_ids=[],
                    citation_by_id=citation_by_id,
                )
            )

    schema = Path(__file__).resolve().parents[2] / "schemas" / "claim-citation-audit.schema.json"
    digest_material = json.dumps(
        {"manuscript": manuscript["manuscriptId"], "claims": claims},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    graph = {
        "schemaVersion": "1.0",
        "auditId": "audit-" + hashlib.sha256(digest_material).hexdigest()[:20],
        "createdAt": datetime.now(UTC).isoformat(),
        "manuscript": {
            "id": manuscript["manuscriptId"],
            "sourceSha256": manuscript["source"]["sha256"],
            "confidential": bool(manuscript["confidential"]),
            "ingestionComplete": bool(manuscript["ingestion"]["complete"]),
            "ingestionIssues": [item["code"] for item in manuscript["ingestion"]["warnings"]],
            "sectionsTotal": len(manuscript["sections"]),
            "sectionsProcessed": len(manuscript["sections"]),
        },
        "auditScope": {
            "mode": "exhaustive",
            "citationBindingComplete": all(
                claim["citationBindingStatus"] != "ambiguous"
                for claim in claims
                if claim["citationRequired"]
            ),
            "searchCurrentThrough": None,
        },
        "claims": claims,
        "citationClusters": citations,
        "references": references,
        "verificationLinks": [],
        "completenessAssessments": [],
        "searchRuns": [],
        "reviewAssertions": [],
        "provenance": {
            "toolVersion": __version__,
            "schemaSha256": sha256_file(schema),
            "createdBy": "ocsl-review claim inventory",
            "commands": [],
        },
    }
    validate_audit_graph(graph)
    return graph


def write_claim_inventory(manuscript: dict[str, Any], output: Path) -> dict[str, Any]:
    graph = build_claim_inventory(manuscript)
    atomic_json(output, graph, mode=0o600 if manuscript.get("confidential", True) else 0o644)
    return graph
