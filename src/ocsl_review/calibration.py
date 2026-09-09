"""Score review-system predictions on synthetic adversarial evidence cases."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import protected_workspace_roots
from .errors import ReviewError, ValidationError
from .util import atomic_json, is_within

SUPPORTING = {"direct-support", "scope-caveat"}
ADVERSE = {"contradicted", "not-addressed"}
VERDICTS = SUPPORTING | ADVERSE | {"partial-indirect", "abstain"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"JSONL record at {path}:{number} is not an object")
        records.append(value)
    return records


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def score_predictions(
    gold_records: Iterable[dict[str, Any]], predictions: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    gold_list = list(gold_records)
    prediction_list = list(predictions)
    if not gold_list:
        raise ValidationError("calibration is blind: expected a nonempty case set")
    for record in gold_list + prediction_list:
        if not isinstance(record.get("caseId"), str) or not record["caseId"]:
            raise ValidationError("calibration records need nonempty caseId values")
    gold = {record["caseId"]: record for record in gold_list}
    predicted = {record["caseId"]: record for record in prediction_list}
    if len(gold) != len(gold_list):
        raise ValidationError("gold case identifiers must be unique")
    if len(predicted) != len(prediction_list):
        raise ValidationError("prediction case identifiers must be unique")
    if set(gold) != set(predicted):
        raise ValidationError(
            f"prediction case set differs: missing={sorted(set(gold) - set(predicted))}, "
            f"extra={sorted(set(predicted) - set(gold))}"
        )
    predicted_supports = 0
    false_supports = 0
    correct_abstentions = 0
    predicted_abstentions = 0
    required_abstentions = 0
    scope_total = 0
    scope_detected = 0
    citation_correct = 0
    completeness_correct = 0
    status_total = 0
    status_correct = 0
    factual_total = 0
    factual_supported = 0
    confidentiality_total = 0
    confidentiality_correct = 0
    per_case = []
    predicted_adverse = false_adverse = support_correct = 0
    for case_id, case in gold.items():
        expected = case.get("gold")
        if not isinstance(expected, dict) or not {
            "supportVerdict", "citationRequired", "scopeMismatch", "completenessVerdict", "publicationStatus"
        } <= expected.keys():
            raise ValidationError(f"case {case_id}: gold assessment is incomplete")
        actual = predicted[case_id]
        support = actual.get("supportVerdict")
        if support not in VERDICTS or expected.get("supportVerdict") not in VERDICTS:
            raise ValidationError(f"case {case_id}: invalid support verdict")
        for field in ("citationRequired", "scopeMismatch"):
            if not isinstance(actual.get(field), bool) or not isinstance(expected.get(field), bool):
                raise ValidationError(f"case {case_id}: {field} must be boolean")
        for field in ("reportAssertionSupported", "confidentialityAllowed"):
            if field in actual and not isinstance(actual[field], bool):
                raise ValidationError(f"case {case_id}: {field} must be boolean")
        support_correct += support == expected["supportVerdict"]
        if support in ADVERSE:
            predicted_adverse += 1
            false_adverse += expected["supportVerdict"] not in ADVERSE
        if support in SUPPORTING:
            predicted_supports += 1
            if expected["supportVerdict"] not in SUPPORTING:
                false_supports += 1
        if support == "abstain":
            predicted_abstentions += 1
            if expected["supportVerdict"] == "abstain":
                correct_abstentions += 1
        if expected["supportVerdict"] == "abstain":
            required_abstentions += 1
        if expected.get("scopeMismatch"):
            scope_total += 1
            scope_detected += bool(actual.get("scopeMismatch"))
        citation_correct += actual.get("citationRequired") == expected["citationRequired"]
        completeness_correct += actual.get("completenessVerdict") == expected["completenessVerdict"]
        if expected["publicationStatus"] != "not-applicable":
            status_total += 1
            status_correct += actual.get("publicationStatus") == expected["publicationStatus"]
        if expected.get("reviewAssertionFactual"):
            factual_total += 1
            factual_supported += bool(actual.get("reportAssertionSupported"))
        if expected.get("confidentialityAllowed") is not None:
            confidentiality_total += 1
            confidentiality_correct += (
                actual.get("confidentialityAllowed") == expected["confidentialityAllowed"]
            )
        per_case.append(
            {
                "caseId": case_id,
                "supportCorrect": support == expected["supportVerdict"],
                "citationRequiredCorrect": actual.get("citationRequired")
                == expected["citationRequired"],
                "completenessCorrect": actual.get("completenessVerdict")
                == expected["completenessVerdict"],
            }
        )
    count = len(gold)
    return {
        "schemaVersion": 1,
        "cases": count,
        "status": "measured",
        "samplingDesign": "adversarial-case-set",
        "populationEstimate": False,
        "supportAccuracy": _ratio(support_correct, count),
        "falseAdverseRate": _ratio(false_adverse, predicted_adverse),
        "falseAdverseVerdicts": false_adverse,
        "predictedAdverseVerdicts": predicted_adverse,
        "falseSupportRate": _ratio(false_supports, predicted_supports),
        "falseSupports": false_supports,
        "predictedSupports": predicted_supports,
        "abstentionPrecision": _ratio(correct_abstentions, predicted_abstentions),
        "abstentionRecall": _ratio(correct_abstentions, required_abstentions),
        "scopeMismatchDetection": _ratio(scope_detected, scope_total),
        "citationNeededAccuracy": _ratio(citation_correct, count),
        "completenessDetectionAccuracy": _ratio(completeness_correct, count),
        "publicationStatusRecall": _ratio(status_correct, status_total),
        "reviewReportFactuality": _ratio(factual_supported, factual_total),
        "confidentialityDecisionAccuracy": _ratio(confidentiality_correct, confidentiality_total),
        "metricDenominators": {
            "supportAccuracy": count, "falseSupportRate": predicted_supports,
            "falseAdverseRate": predicted_adverse, "abstentionPrecision": predicted_abstentions,
            "abstentionRecall": required_abstentions, "scopeMismatchDetection": scope_total,
            "citationNeededAccuracy": count, "completenessDetectionAccuracy": count,
            "publicationStatusRecall": status_total, "reviewReportFactuality": factual_total,
            "confidentialityDecisionAccuracy": confidentiality_total,
        },
        "perCase": per_case,
    }


def compare_passes(first: Iterable[dict[str, Any]], final: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Preserve both readings and report changes without calling them errors."""
    before_list, after_list = list(first), list(final)
    for record in before_list + after_list:
        if not isinstance(record.get("caseId"), str) or not record["caseId"]:
            raise ValidationError("repair comparison records need nonempty caseId values")
        if record.get("supportVerdict") not in VERDICTS:
            raise ValidationError("repair comparison contains an invalid support verdict")
    before = {item["caseId"]: item for item in before_list}
    after = {item["caseId"]: item for item in after_list}
    if not before or len(before) != len(before_list) or len(after) != len(after_list) or set(before) != set(after):
        raise ValidationError("repair comparison needs identical nonempty unique case sets")
    changes = []
    agreements = {field: 0 for field in ("supportVerdict", "severity", "confidence")}
    denominators = {field: 0 for field in agreements}
    favorable = adverse = 0
    for case_id in sorted(before):
        old, new = before[case_id], after[case_id]
        for field in agreements:
            if field in old and field in new:
                denominators[field] += 1
                agreements[field] += old[field] == new[field]
        if old != new:
            verdict_changed = old.get("supportVerdict") != new.get("supportVerdict")
            direction = "other"
            if verdict_changed and new.get("supportVerdict") in SUPPORTING:
                favorable += 1
                direction = "toward-support"
            elif verdict_changed and new.get("supportVerdict") in ADVERSE:
                adverse += 1
                direction = "toward-adverse"
            changes.append({"caseId": case_id, "before": old, "after": new, "direction": direction})
    return {
        "cases": len(before), "changedRecords": len(changes),
        "changeRate": _ratio(len(changes), len(before)),
        "towardSupport": favorable, "towardAdverse": adverse,
        "agreement": {field: _ratio(agreements[field], denominators[field]) for field in agreements},
        "agreementDenominators": denominators,
        "changes": changes, "interpretation": "Change and agreement measure reproducibility, not correctness or population error.",
    }


def evaluate_file(
    predictions: Path, *, gold: Path | None = None, output: Path | None = None,
    first_predictions: Path | None = None,
) -> dict[str, Any]:
    gold_path = (
        gold or Path(__file__).resolve().parents[2] / "evaluations" / "adversarial" / "cases.jsonl"
    )
    report = score_predictions(load_jsonl(gold_path), load_jsonl(predictions))
    if first_predictions is not None:
        report["repairComparison"] = compare_passes(load_jsonl(first_predictions), load_jsonl(predictions))
    if output:
        atomic_json(output, report)
    return report


def cmd_calibrate(args: Any) -> dict[str, Any]:
    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir() or workspace == Path(workspace.anchor):
        raise ReviewError("calibration requires a concrete workspace directory")
    for label, protected in protected_workspace_roots(args.paths):
        if is_within(workspace, protected) or is_within(protected, workspace):
            raise ReviewError(f"calibration workspace overlaps protected {label}: {protected}")
    declared = os.environ.get("LAB_WORKSPACE_ROOT")
    if declared and not is_within(workspace, Path(declared).expanduser().resolve()):
        raise ReviewError("selected calibration workspace is outside LAB_WORKSPACE_ROOT")

    def local(value: str | Path, *, exists: bool) -> Path:
        selected = Path(value).expanduser()
        if not selected.is_absolute():
            selected = workspace / selected
        selected = selected.resolve()
        if not is_within(selected, workspace):
            raise ReviewError(f"calibration path escapes the selected workspace: {selected}")
        if exists and not selected.is_file():
            raise ReviewError(f"calibration input does not exist: {selected}")
        return selected

    predictions = local(args.predictions, exists=True)
    gold = local(args.gold, exists=True) if getattr(args, "gold", None) else None
    output = local(args.output, exists=False) if getattr(args, "output", None) else None
    first = local(args.first_predictions, exists=True) if getattr(args, "first_predictions", None) else None
    return evaluate_file(predictions, gold=gold, output=output, first_predictions=first)
