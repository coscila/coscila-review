"""Deployment integration plus mandatory post-stage MCP acceptance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import integration, mcps
from .errors import ReviewError, ValidationError


def _read_install_state(args: Any) -> dict[str, Any]:
    path = integration.install_state_path(args.paths)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read integration state: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"integration state is not an object: {path}")
    return value


def _staged_config(args: Any, state: dict[str, Any] | None = None) -> Path:
    selected = integration.resolve_staged_mcp_config(args, state=state, required=True)
    assert selected is not None
    source_value = state.get("mcp", {}).get("configPath") if state is not None else None
    source = (
        Path(source_value).expanduser().resolve()
        if isinstance(source_value, str) and source_value
        else integration.resolve_mcp_config(args)
    )
    if selected == source:
        raise ValidationError(
            "the staged MCP config must differ from the deployment MCP source config"
        )
    return selected


def cmd_install(args: Any) -> dict[str, Any]:
    state_path = integration.install_state_path(args.paths)
    existing_state = _read_install_state(args) if state_path.is_file() else None
    config = _staged_config(args, existing_state)
    result = integration.cmd_install(args)
    try:
        acceptance = mcps.doctor(args.paths, live=True, staged_config=config)
        if not acceptance["acceptanceComplete"]:
            raise ReviewError("MCP acceptance did not pass source and staged live probes")
    except Exception as exc:
        rollback = None
        if not result.get("reused"):
            try:
                rollback = integration.cmd_uninstall(args)
            except Exception as rollback_exc:  # noqa: BLE001
                raise ReviewError(
                    f"integration acceptance failed ({exc}); rollback also failed: {rollback_exc}"
                ) from exc
        suffix = f"; rolled back: {bool(rollback)}" if rollback is not None else ""
        raise ReviewError(f"integration acceptance failed: {exc}{suffix}") from exc
    return {**result, "mcpAcceptance": acceptance}


def cmd_status(args: Any) -> dict[str, Any]:
    return integration.cmd_status(args)


def cmd_verify(args: Any) -> dict[str, Any]:
    result = integration.cmd_verify(args)
    if not result.get("ok"):
        reason = result.get("reason") or "review integration is not installed or is invalid"
        raise ValidationError(f"integration verification failed: {reason}")
    value = _read_install_state(args)
    config = _staged_config(args, value)
    acceptance = mcps.doctor(
        args.paths,
        live=bool(getattr(args, "live", False)),
        staged_config=config,
    )
    result["mcpAcceptance"] = acceptance
    if not acceptance.get("ok"):
        raise ValidationError("integration MCP verification failed for source or staged config")
    if getattr(args, "strict", False) and not acceptance["acceptanceComplete"]:
        raise ValidationError("strict integration verification requires live MCP acceptance")
    return result


def cmd_uninstall(args: Any) -> dict[str, Any]:
    return integration.cmd_uninstall(args)
