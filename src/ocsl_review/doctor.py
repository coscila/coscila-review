"""Aggregate static, state, source, environment, and MCP health."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import environments, integration, mcps, repositories, standards
from .catalogs import validate_catalogs
from .errors import ValidationError


def doctor(
    paths: Any,
    *,
    live: bool = False,
    staged_config: Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    catalog_report = validate_catalogs(paths)
    environment_report = environments.list_environments(paths, platform=platform)
    repository_report = {
        "repositories": [
            repositories.verify_repository(paths, record["id"])
            for record in repositories.cmd_list(type("Args", (), {"paths": paths})())
        ]
    }
    repository_report["ok"] = all(item["ok"] for item in repository_report["repositories"])
    standard_report = {
        "standards": [
            standards.verify_standard(paths, identifier)
            for identifier in sorted(standards.catalog(paths))
        ]
    }
    standard_report["ok"] = all(item["ok"] for item in standard_report["standards"])
    integration_report = integration.verify_integration(paths)
    selected_staged = staged_config
    if selected_staged is None and integration_report.get("installed"):
        state_path = integration.install_state_path(paths)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            staged_value = state["mcp"].get("stagedConfigPath")
            selected_staged = Path(staged_value) if staged_value else None
        except (OSError, KeyError, json.JSONDecodeError):
            selected_staged = None
    try:
        mcp_report = mcps.doctor(paths, live=live, staged_config=selected_staged)
    except Exception as exc:  # noqa: BLE001 - doctor reports all independent surfaces
        mcp_report = {
            "ok": False,
            "acceptanceComplete": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    checks = {
        "catalogs": bool(catalog_report.get("ok")),
        "environmentTemplates": all(item["ready"] for item in environment_report),
        "repositories": bool(repository_report["ok"]),
        "standards": bool(standard_report["ok"]),
        "integration": bool(integration_report.get("ok")),
        "mcpAcceptance": bool(mcp_report.get("acceptanceComplete")),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "catalogs": catalog_report,
        "environments": environment_report,
        "repositories": repository_report,
        "standards": standard_report,
        "integration": integration_report,
        "mcp": mcp_report,
    }


def cmd_doctor(args: Any) -> dict[str, Any]:
    staged = (
        Path(args.staged_config).expanduser().resolve()
        if getattr(args, "staged_config", None)
        else None
    )
    result = doctor(
        args.paths,
        live=bool(getattr(args, "live", False)),
        staged_config=staged,
        platform=getattr(args, "platform", None),
    )
    if getattr(args, "strict", False) and not result["ok"]:
        failed = [name for name, value in result["checks"].items() if not value]
        raise ValidationError("strict doctor failed: " + ", ".join(failed))
    return result
