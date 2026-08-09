"""Operator provisioning for immutable review dependencies."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import environments, mcps, repositories, standards
from .catalogs import validate_catalogs
from .errors import ReviewError
from .util import atomic_json

COMPONENTS = ("environments", "repositories", "standards", "mcp")


def _initialize_state(paths: Any) -> Path:
    state = paths.need_state().resolve()
    if state.exists() and not state.is_dir():
        raise ReviewError(f"review state is not a directory: {state}")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    for child in (
        paths.templates,
        paths.repositories,
        paths.standards,
        paths.integration,
        paths.cache,
    ):
        child.mkdir(parents=True, exist_ok=True, mode=0o700)
    return state


def bootstrap(
    paths: Any,
    *,
    components: tuple[str, ...] = COMPONENTS,
    executable: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    solve_timeout: float = 3600,
    probe_timeout: float = 120,
    refresh_repositories: bool = False,
    standard_timeout: float = 30,
) -> dict[str, Any]:
    unknown = sorted(set(components) - set(COMPONENTS))
    if unknown:
        raise ReviewError(f"unknown bootstrap component(s): {', '.join(unknown)}")
    if not components:
        raise ReviewError("bootstrap requires at least one component")
    state = _initialize_state(paths)
    catalog_report = validate_catalogs(paths)
    results: dict[str, Any] = {}
    if "environments" in components:
        results["environments"] = environments.build_all_templates(
            paths,
            executable=executable,
            platform=platform,
            solve_timeout=solve_timeout,
            probe_timeout=probe_timeout,
        )
    if "repositories" in components:
        results["repositories"] = repositories.sync_all(paths, refresh_mirrors=refresh_repositories)
    if "standards" in components:
        results["standards"] = {
            "standards": [
                standards.sync_standard(paths, identifier, timeout=standard_timeout)
                for identifier in sorted(standards.catalog(paths))
            ]
        }
    if "mcp" in components:
        if "environments" not in components and not all(
            item["ready"] for item in environments.list_environments(paths, platform=platform)
        ):
            raise ReviewError("MCP bootstrap requires all promoted environment templates")
        mcp_report = mcps.doctor(paths, live=True, staged_config=None)
        if not mcp_report["ok"]:
            raise ReviewError("one or more source MCP acceptance probes failed")
        results["mcp"] = mcp_report
    receipt = {
        "schemaVersion": 1,
        "completedAt": datetime.now(UTC).isoformat(),
        "reviewHome": str(paths.home),
        "reviewState": str(state),
        "components": list(components),
        "catalogs": catalog_report,
        "results": results,
    }
    receipt_path = state / "bootstrap.json"
    atomic_json(receipt_path, receipt, mode=0o600)
    return {"ok": True, "receipt": str(receipt_path), **receipt}


def cmd_bootstrap(args: Any) -> dict[str, Any]:
    requested = tuple(getattr(args, "component", None) or COMPONENTS)
    return bootstrap(
        args.paths,
        components=requested,
        executable=getattr(args, "conda_executable", None),
        platform=getattr(args, "platform", None),
        solve_timeout=float(getattr(args, "timeout", 3600)),
        probe_timeout=float(getattr(args, "probe_timeout", 120)),
        refresh_repositories=bool(getattr(args, "refresh_repositories", False)),
        standard_timeout=float(getattr(args, "standard_timeout", 30)),
    )
