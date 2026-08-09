"""MCP entrypoint for policy-gated scholarly-provider operations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .confidentiality import EgressPlan
from .mcp_protocol import SCHOLAR_TOOLS, serve_stdio
from .scholarly import ScholarlyClient, call_tool


def make_dispatch():
    plan_value = os.environ.get("OCSL_REVIEW_EGRESS_PLAN")
    workspace_value = os.environ.get("OCSL_REVIEW_WORKSPACE")
    plan = EgressPlan.load(Path(plan_value).resolve() if plan_value else None)
    workspace = Path(workspace_value).resolve() if workspace_value else None
    client = ScholarlyClient(plan=plan, workspace=workspace)

    def dispatch(name: str, arguments: Mapping[str, Any]) -> Any:
        return call_tool(client, name, arguments)

    return dispatch


def main() -> int:
    return serve_stdio(server_name="ocsl_scholar", tools=SCHOLAR_TOOLS, dispatch=make_dispatch())


if __name__ == "__main__":
    raise SystemExit(main())
