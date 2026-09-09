"""Local manuscript workflow and canonical audit-artifact commands."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema

from .audit import build_audit_summary, validate_audit_graph
from .claim_inventory import build_claim_inventory
from .config import protected_workspace_roots
from .errors import ReviewError, ValidationError
from .ingest import ingest_manuscript
from .rendering import render_review_package, validate_review_package
from .review_plan import build_review_plan, new_review_package
from .util import atomic_json, is_within

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def _workspace(args: Any) -> Path:
    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir():
        raise ReviewError(f"review workspace does not exist: {workspace}")
    if workspace == Path(workspace.anchor):
        raise ReviewError("the filesystem root is not a review workspace")
    for label, protected in protected_workspace_roots(args.paths):
        if is_within(workspace, protected) or is_within(protected, workspace):
            raise ReviewError(f"review workspace overlaps protected {label}: {protected}")
    declared = os.environ.get("LAB_WORKSPACE_ROOT")
    if declared and not is_within(workspace, Path(declared).expanduser().resolve()):
        raise ReviewError("selected workspace is outside LAB_WORKSPACE_ROOT")
    return workspace


def _inside(workspace: Path, value: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    if not is_within(path, workspace):
        raise ReviewError(f"review path escapes the selected workspace: {path}")
    if must_exist and not path.is_file():
        raise ReviewError(f"review artifact does not exist: {path}")
    return path


def _artifact_path(args: Any, name: str, default: str, *, must_exist: bool = False) -> Path:
    workspace = _workspace(args)
    return _inside(workspace, getattr(args, name, None) or default, must_exist=must_exist)


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValidationError(f"review artifact exceeds {MAX_ARTIFACT_BYTES} bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON review artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"review artifact must be one JSON object: {path}")
    return value


def _validate_schema(paths: Any, value: Mapping[str, Any], schema_name: str) -> None:
    schema_path = paths.home / "schemas" / schema_name
    schema = _read_json(schema_path)
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ValidationError(f"{schema_name} validation failed at {location}: {error.message}")


def _load_manuscript(args: Any) -> tuple[Path, dict[str, Any]]:
    path = _artifact_path(args, "manuscript", ".ocsl-review/manuscript.json", must_exist=True)
    value = _read_json(path)
    _validate_schema(args.paths, value, "manuscript.schema.json")
    return path, value


def cmd_ingest(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    source = _inside(workspace, args.input, must_exist=True)
    output = _artifact_path(args, "output", ".ocsl-review/manuscript.json")
    manuscript = ingest_manuscript(
        source,
        confidential=not bool(getattr(args, "public", False)),
        pdftotext=getattr(args, "pdftotext", "pdftotext"),
        pdf_timeout=float(getattr(args, "timeout", 30)),
        max_input_bytes=int(getattr(args, "max_input_bytes", 100 * 1024 * 1024)),
        max_extracted_bytes=int(getattr(args, "max_extracted_bytes", 64 * 1024 * 1024)),
    )
    _validate_schema(args.paths, manuscript, "manuscript.schema.json")
    atomic_json(output, manuscript, mode=0o600 if manuscript["confidential"] else 0o644)
    return {
        "manuscript": str(output),
        "manuscriptId": manuscript["manuscriptId"],
        "confidential": manuscript["confidential"],
        "complete": manuscript["ingestion"]["complete"],
        "coverage": manuscript["ingestion"]["coverage"],
    }


def cmd_inventory(args: Any) -> dict[str, Any]:
    _, manuscript = _load_manuscript(args)
    output = _artifact_path(args, "output", ".ocsl-review/audit-graph.json")
    graph = build_claim_inventory(manuscript)
    atomic_json(output, graph, mode=0o600 if manuscript["confidential"] else 0o644)
    return {
        "auditGraph": str(output),
        "auditId": graph["auditId"],
        "claims": len(graph["claims"]),
        "citationRequiredClaims": sum(item["citationRequired"] for item in graph["claims"]),
    }


def cmd_plan(args: Any) -> dict[str, Any]:
    _, manuscript = _load_manuscript(args)
    output = _artifact_path(args, "output", ".ocsl-review/review-plan.json")
    plan = build_review_plan(manuscript)
    atomic_json(output, plan, mode=0o600 if manuscript["confidential"] else 0o644)
    return {"reviewPlan": str(output), "planId": plan["planId"], "lenses": len(plan["lenses"])}


def cmd_package(args: Any) -> dict[str, Any]:
    _, manuscript = _load_manuscript(args)
    plan_path = _artifact_path(args, "plan", ".ocsl-review/review-plan.json")
    plan = _read_json(plan_path) if plan_path.is_file() else build_review_plan(manuscript)
    graph_path = _artifact_path(args, "audit_graph", ".ocsl-review/audit-graph.json")
    graph = _read_json(graph_path) if graph_path.is_file() else build_claim_inventory(manuscript)
    if not graph_path.is_file():
        atomic_json(graph_path, graph, mode=0o600 if manuscript["confidential"] else 0o644)
    from .literature import guard_graph

    guard_graph(_workspace(args), graph)
    summary_path_value = getattr(args, "audit_summary", None)
    summary = (
        _read_json(_inside(_workspace(args), summary_path_value, must_exist=True))
        if summary_path_value
        else None
    )
    package = new_review_package(
        manuscript,
        plan,
        title=getattr(args, "title", None) or "Scientific review evidence audit",
        audit_graph=graph,
        audit_summary=summary,
    )
    validate_review_package(package)
    _validate_schema(args.paths, package, "review-package.schema.json")
    output = _artifact_path(args, "output", ".ocsl-review/review-package.json")
    atomic_json(output, package, mode=0o600 if package["confidential"] else 0o644)
    summary_output = _artifact_path(args, "summary_output", ".ocsl-review/audit-summary.json")
    atomic_json(summary_output, package["auditSummary"], mode=0o600)
    return {
        "reviewPackage": str(output),
        "auditSummary": str(summary_output),
        "packageId": package["packageId"],
        "auditAttached": True,
    }


def cmd_render(args: Any) -> dict[str, Any]:
    package_path = _artifact_path(
        args, "package", ".ocsl-review/review-package.json", must_exist=True
    )
    package = _read_json(package_path)
    if package.get("auditGraph") is not None:
        from .literature import guard_graph

        guard_graph(_workspace(args), package["auditGraph"])
    output_stem = _artifact_path(args, "output_stem", ".ocsl-review/review-report")
    rendered = render_review_package(package, output_stem)
    return {key.lstrip("."): str(value) for key, value in rendered.items()}


def cmd_prepare(args: Any) -> dict[str, Any]:
    ingest_result = cmd_ingest(args)
    manuscript_path = Path(ingest_result["manuscript"])
    manuscript = _read_json(manuscript_path)
    graph = build_claim_inventory(manuscript)
    graph_path = _artifact_path(args, "audit_output", ".ocsl-review/audit-graph.json")
    atomic_json(graph_path, graph, mode=0o600 if manuscript["confidential"] else 0o644)
    plan = build_review_plan(manuscript)
    plan_path = _artifact_path(args, "plan_output", ".ocsl-review/review-plan.json")
    atomic_json(plan_path, plan, mode=0o600 if manuscript["confidential"] else 0o644)
    package = new_review_package(
        manuscript,
        plan,
        title=getattr(args, "title", None) or "Scientific review evidence audit",
        audit_graph=graph,
    )
    validate_review_package(package)
    package_path = _artifact_path(args, "package_output", ".ocsl-review/review-package.json")
    atomic_json(package_path, package, mode=0o600 if manuscript["confidential"] else 0o644)
    summary_path = _artifact_path(args, "summary_output", ".ocsl-review/audit-summary.json")
    atomic_json(summary_path, package["auditSummary"], mode=0o600)
    return {
        **ingest_result,
        "manuscript": str(manuscript_path),
        "auditGraph": str(graph_path),
        "reviewPlan": str(plan_path),
        "reviewPackage": str(package_path),
        "auditSummary": str(summary_path),
        "claims": len(graph["claims"]),
        "verificationLimits": package["auditSummary"]["verificationLimits"],
    }


def _load_graph(args: Any) -> tuple[Path, dict[str, Any]]:
    path = _artifact_path(args, "audit_graph", ".ocsl-review/audit-graph.json", must_exist=True)
    graph = _read_json(path)
    validate_audit_graph(graph)
    from .literature import guard_graph

    guard_graph(_workspace(args), graph)
    return path, graph


def cmd_audit_validate(args: Any) -> dict[str, Any]:
    path, graph = _load_graph(args)
    summary = build_audit_summary(graph)
    return {
        "ok": True,
        "auditGraph": str(path),
        "claims": len(graph["claims"]),
        "references": len(graph["references"]),
        "summary": summary,
    }


def cmd_audit_summarize(args: Any) -> dict[str, Any]:
    _, graph = _load_graph(args)
    summary = build_audit_summary(graph)
    output = _artifact_path(args, "output", ".ocsl-review/audit-summary.json")
    atomic_json(output, summary, mode=0o600)
    return {"auditSummary": str(output), **summary}
