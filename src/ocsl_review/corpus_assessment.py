# SPDX-License-Identifier: Apache-2.0
"""Pure planning and validation for citation-support v2.

This protocol is independent of the archived census v1 instrument. Transport,
source acquisition, artifact verification and persistence belong to the caller.
A source hash identifies the caller-verified artifact; evidenceDigest binds the
exact units supplied to this assessment. Neither is a model reading attestation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import jsonschema

from .errors import ValidationError

PROTOCOL_VERSION = "citation-support-v2"
HOME = Path(__file__).resolve().parents[2]
PROMPT_PATH = HOME / "prompts/citation-support-v2.txt"
SCHEMA_PATH = HOME / "schemas/corpus-assessment-v2.schema.json"
MAX_BATCH_ITEMS = 40
TIERS = {"abstract": "abstract", "abstract-only": "abstract", "full-text": "full-text"}


class AssessmentError(ValidationError):
    """A classified failure: unchanged scientific inputs must not be retried."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def output_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _tier(value: Any) -> str:
    if not isinstance(value, str) or value not in TIERS:
        raise AssessmentError("invalid_task", "evidence tier must be abstract or full-text")
    return TIERS[value]


class TaskPlanner:
    """Keep every locus distinct, regardless of shared cited work or evidence."""

    def prepare(self, task: dict[str, Any], tier: str | None = None) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise AssessmentError("invalid_task", "task must be an object")
        result = copy.deepcopy(task)
        for key in ("id", "claim"):
            if not isinstance(result.get(key), str) or not result[key].strip():
                raise AssessmentError("invalid_task", f"task {key} must be a nonempty string")
        supplied = task.get("evidenceTier", tier)
        result["evidenceTier"] = _tier(supplied)
        if tier is not None and result["evidenceTier"] != _tier(tier):
            raise AssessmentError("invalid_task", "task evidence tier differs from the batch tier")
        if not isinstance(result.get("complete"), bool):
            raise AssessmentError("invalid_task", "source completeness must be explicit")
        units = result.get("evidenceUnits")
        if not isinstance(units, list):
            raise AssessmentError("invalid_task", "evidenceUnits must be an array")
        ids = []
        for unit in units:
            if (not isinstance(unit, dict) or not isinstance(unit.get("id"), str)
                    or not unit["id"].strip() or not isinstance(unit.get("text"), str)):
                raise AssessmentError("invalid_task", "every evidence unit needs an ID and text")
            ids.append(unit["id"])
        if len(set(ids)) != len(ids):
            raise AssessmentError("invalid_task", "evidence unit IDs must be unique within a task")
        source_hash = result.get("sourceSha256")
        if units and (not isinstance(source_hash, str)
                      or not re.fullmatch(r"[a-f0-9]{64}", source_hash)):
            raise AssessmentError("invalid_task", "supplied evidence needs its verified source SHA-256")
        for key, default, expected in (("context", [], list), ("citationLocus", {}, dict),
                                       ("citedWork", {}, dict), ("identifiers", {}, dict)):
            result.setdefault(key, default)
            if not isinstance(result[key], expected):
                raise AssessmentError("invalid_task", f"{key} has an invalid shape")
        if not all(isinstance(value, str) for value in result["context"]):
            raise AssessmentError("invalid_task", "context must contain only sentences")
        result["evidenceDigest"] = _digest(units)
        result["evidenceCoverage"] = (
            "complete" if units and result["complete"] else "excerpt" if units else "missing"
        )
        return result

    def plan(self, task: dict[str, Any], tier: str | None = None) -> dict[str, Any]:
        prepared = self.prepare(task, tier)
        reason = prepared.get("blockedReason")
        if prepared.get("identityVerified") is False:
            reason = reason or "cited work identity is unverified"
        binding = prepared["citationLocus"].get("bindingStatus", "exact")
        if binding != "exact":
            reason = reason or "citation binding is unverified"
        if not any(unit["text"].strip() for unit in prepared["evidenceUnits"]):
            reason = reason or "source evidence is unavailable"
        if prepared["evidenceTier"] == "abstract" and not prepared["complete"]:
            reason = reason or "complete abstract is unavailable"
        return {"task": prepared, "action": "blocked" if reason else "judge", "reason": reason}

    def build_messages(self, tasks: list[dict[str, Any]], tier: str) -> list[dict[str, str]]:
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_BATCH_ITEMS:
            raise AssessmentError("invalid_batch", f"a batch needs 1–{MAX_BATCH_ITEMS} tasks")
        planned = [self.plan(task, tier) for task in tasks]
        ids = [item["task"]["id"] for item in planned]
        if len(set(ids)) != len(ids):
            raise AssessmentError("invalid_batch", "duplicate task IDs must not enter a batch")
        if any(item["action"] != "judge" for item in planned):
            raise AssessmentError("blocked_task", "blocked inputs must be recorded before dispatch")
        fields = ("id", "claim", "claimType", "citationLocus", "context", "citedWork",
                  "identifiers", "evidenceUnits", "sourceSha256", "evidenceDigest", "complete",
                  "evidenceCoverage")
        payload = {"protocol": PROTOCOL_VERSION, "evidenceTier": _tier(tier),
                   "items": [{key: item["task"][key] for key in fields if key in item["task"]}
                             for item in planned]}
        return [{"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


class AssessmentValidator:
    """Validate each item independently; no confidence score or ID-list echo."""

    def __init__(self):
        self.schema = output_schema()
        jsonschema.Draft202012Validator.check_schema(self.schema)
        self.item_schema = self.schema["properties"]["items"]["items"]
        self.item_validator = jsonschema.Draft202012Validator(self.item_schema)
        self.planner = TaskPlanner()

    def validate_item(self, task: dict[str, Any], output: Any) -> dict[str, Any]:
        prepared = self.planner.prepare(task)
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError as exc:
                raise AssessmentError("invalid_json", "item is not JSON", retryable=True) from exc
        if not isinstance(output, dict):
            raise AssessmentError("invalid_schema", "item must be an object", retryable=True)
        if output.get("id") != prepared["id"]:
            raise AssessmentError("wrong_task_id", "response ID does not match its task")
        error = next(self.item_validator.iter_errors(output), None)
        if error:
            raise AssessmentError("invalid_schema", error.message, retryable=True)
        if not output["rationale"].strip():
            raise AssessmentError("invalid_schema", "rationale must not be blank", retryable=True)
        if output["escalate"] != bool(output["escalationReason"].strip()):
            raise AssessmentError("invalid_escalation", "escalation needs an explicit reason, or none")
        units = {unit["id"]: unit["text"] for unit in prepared["evidenceUnits"]}
        for quote in output["evidence"]:
            if quote["unitId"] not in units or not quote["quote"].strip():
                raise AssessmentError("invalid_quote", "quote needs a named supplied evidence unit")
            if quote["quote"] not in units[quote["unitId"]]:
                raise AssessmentError("invalid_quote", "quote is absent from its named evidence unit")
        verdict = output["supportVerdict"]
        if verdict in {"abstain", "partial-indirect"} and not output["escalate"]:
            raise AssessmentError("invalid_escalation", "unsettled support requires explicit escalation")
        if verdict != "abstain":
            if self.planner.plan(prepared)["action"] == "blocked":
                raise AssessmentError("insufficient_evidence", "blocked evidence cannot support a verdict")
            if not output["evidence"]:
                raise AssessmentError("missing_quote", "every substantive verdict needs an exact quote")
            if verdict == "not-addressed" and (
                prepared["evidenceTier"] != "full-text" or not prepared["complete"]
            ):
                raise AssessmentError("insufficient_coverage", "absence requires complete full-text coverage")
        return {**copy.deepcopy(output), "taskId": prepared["id"], "protocol": PROTOCOL_VERSION,
                "evidenceTier": prepared["evidenceTier"],
                "evidenceCoverage": prepared["evidenceCoverage"],
                "sourceSha256": prepared.get("sourceSha256"),
                "evidenceDigest": prepared["evidenceDigest"]}

    def validate_response(self, tasks: list[dict[str, Any]], output: Any) -> dict[str, Any]:
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_BATCH_ITEMS:
            raise AssessmentError("invalid_batch", f"a batch needs 1–{MAX_BATCH_ITEMS} tasks")
        prepared = [self.planner.prepare(task) for task in tasks]
        by_id = {task["id"]: task for task in prepared}
        if len(by_id) != len(prepared):
            raise AssessmentError("invalid_batch", "input task IDs are not unique")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError as exc:
                raise AssessmentError("invalid_json", "response is not JSON", retryable=True) from exc
        if not isinstance(output, dict) or set(output) != {"items"} or not isinstance(output["items"], list):
            raise AssessmentError("invalid_schema", "response must contain an items array", retryable=True)
        items = output["items"]
        if len(items) > MAX_BATCH_ITEMS:
            raise AssessmentError("invalid_schema", "response exceeds batch item limit", retryable=True)
        counts = Counter(item.get("id") for item in items
                         if isinstance(item, dict) and isinstance(item.get("id"), str))
        accepted, rejected = {}, {}
        for task_id, task in by_id.items():
            if counts[task_id] != 1:
                rejected[task_id] = {"code": "missing_item" if not counts[task_id] else "duplicate_item",
                                     "message": "expected exactly one response for this task",
                                     "retryable": True}
                continue
            item = next(item for item in items if isinstance(item, dict) and item.get("id") == task_id)
            try:
                accepted[task_id] = self.validate_item(task, item)
            except AssessmentError as exc:
                rejected[task_id] = {"code": exc.code, "message": str(exc), "retryable": exc.retryable}
        return {"accepted": accepted, "rejected": rejected,
                "unexpected": sorted(task_id for task_id in counts if task_id not in by_id),
                "unidentifiedItems": sum(not isinstance(item, dict)
                                         or not isinstance(item.get("id"), str) for item in items)}


def build_messages(tasks: list[dict[str, Any]], tier: str) -> list[dict[str, str]]:
    return TaskPlanner().build_messages(tasks, tier)


def validate_item(task: dict[str, Any], output: Any) -> dict[str, Any]:
    return AssessmentValidator().validate_item(task, output)


def plan_escalation(assessment: dict[str, Any], coverage: dict[str, Any]) -> dict[str, str]:
    """Route an accepted observation; never replay unchanged evidence indefinitely."""
    if (not isinstance(coverage, dict) or not isinstance(assessment, dict)
            or not isinstance(assessment.get("escalate"), bool)):
        raise AssessmentError("invalid_escalation", "assessment and coverage must be explicit")
    if not assessment["escalate"]:
        return {"action": "none", "reason": "supplied evidence settled the assessment"}
    tier = _tier(assessment.get("evidenceTier"))
    if coverage.get("withinContext") is False:
        return {"action": "blocked", "reason": "full-text evidence exceeds the configured context"}
    if tier == "abstract":
        if coverage.get("fullTextAvailable") is False:
            return {"action": "blocked", "reason": "full text is unavailable"}
        return {"action": "full-text", "reason": assessment["escalationReason"]}
    if coverage.get("moreEvidenceAvailable") is True and assessment.get("evidenceCoverage") != "complete":
        return {"action": "full-text", "reason": "acquire additional full-text evidence"}
    return {"action": "blocked", "reason": "assessment remains uncertain after available full-text evidence"}
