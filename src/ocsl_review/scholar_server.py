"""MCP entrypoint for policy-gated scholarly-provider operations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .config import resolve_paths
from .confidentiality import load_scholarly_context
from .mcp_protocol import SCHOLAR_TOOLS, serve_stdio
from .scholarly import ScholarlyClient, call_tool


def make_dispatch():
    paths = resolve_paths(state_required=False)
    plan, workspace = load_scholarly_context(paths, os.environ)
    client = ScholarlyClient(plan=plan, workspace=workspace)

    def dispatch(name: str, arguments: Mapping[str, Any]) -> Any:
        return call_tool(client, name, arguments)

    return dispatch


def main() -> int:
    return serve_stdio(server_name="ocsl_scholar", tools=SCHOLAR_TOOLS, dispatch=make_dispatch())


if __name__ == "__main__":
    raise SystemExit(main())
