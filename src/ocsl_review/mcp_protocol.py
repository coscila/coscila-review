"""Minimal MCP stdio server and canonical tool-schema definitions."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from . import __version__

PROTOCOL_VERSION = "2025-06-18"


def object_schema(
    properties: Mapping[str, Any] | None = None,
    *,
    required: list[str] | None = None,
    one_of: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties or {}),
    }
    if required:
        schema["required"] = required
    if one_of:
        schema["oneOf"] = one_of
    return schema


STRING = {"type": "string", "minLength": 1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 25, "default": 10}
DOI = {"type": "string", "pattern": "^10\\.\\d{4,9}/\\S+$"}
PMID = {"type": "string", "pattern": "^\\d{1,10}$"}
PMCID = {"type": "string", "pattern": "^PMC\\d+$"}

CATALOG_TOOLS = [
    {
        "name": "list_skills",
        "description": "List review skills and their declared runtime environment, with an optional result bound.",
        "inputSchema": object_schema({"limit": LIMIT}),
    },
    {
        "name": "describe_skill",
        "description": "Return the catalog record and source paths for one namespaced review skill.",
        "inputSchema": object_schema({"skill": STRING}, required=["skill"]),
    },
    {
        "name": "resolve_skill_runtime",
        "description": "Resolve a skill to its immutable template and declared companion dependencies.",
        "inputSchema": object_schema({"skill": STRING}, required=["skill"]),
    },
    {
        "name": "list_environments",
        "description": "List task environments, lock identities, and template purposes.",
        "inputSchema": object_schema({"limit": LIMIT}),
    },
    {
        "name": "list_repositories",
        "description": "List immutable source-archive identities without reading checkout contents.",
        "inputSchema": object_schema({"limit": LIMIT}),
    },
    {
        "name": "list_standards",
        "description": "List the official standards snapshots known to the companion.",
        "inputSchema": object_schema({"limit": LIMIT}),
    },
    {
        "name": "describe_contract",
        "description": "Describe a machine-readable review schema and its SHA-256 identity.",
        "inputSchema": object_schema({"name": STRING}, required=["name"]),
    },
]

SEARCH_SCHEMA = object_schema({"query": STRING, "limit": LIMIT}, required=["query"])
IDENTIFIER_SCHEMA = object_schema(
    {"doi": DOI, "pmid": PMID, "pmcid": PMCID},
    one_of=[{"required": ["doi"]}, {"required": ["pmid"]}, {"required": ["pmcid"]}],
)
STATUS_SCHEMA = object_schema(
    {"doi": DOI, "pmid": PMID}, one_of=[{"required": ["doi"]}, {"required": ["pmid"]}]
)
LINK_SCHEMA = object_schema(
    {
        "doi": DOI,
        "source": {"type": "string", "enum": ["MED", "PMC", "AGR", "CBA", "CTX", "ETH", "PAT"]},
        "identifier": STRING,
        "limit": LIMIT,
    },
    one_of=[{"required": ["doi"]}, {"required": ["source", "identifier"]}],
)

SCHOLAR_TOOLS = [
    {
        "name": "resolve_identifier",
        "description": "Resolve one DOI, PMID, or PMCID against explicit public metadata providers.",
        "inputSchema": IDENTIFIER_SCHEMA,
    },
    {
        "name": "resolve_reference",
        "description": "Resolve a structured published reference; confidential title/author egress needs explicit approval.",
        "inputSchema": object_schema(
            {
                "reference": object_schema(
                    {
                        "doi": DOI,
                        "pmid": PMID,
                        "pmcid": PMCID,
                        "title": STRING,
                        "author": STRING,
                        "year": {"type": ["string", "integer"]},
                    }
                )
            },
            required=["reference"],
        ),
    },
    {
        "name": "reconcile_reference_metadata",
        "description": "Compare provider records locally and report field-level disagreement.",
        "inputSchema": object_schema(
            {
                "records": {
                    "type": "array",
                    "items": {"type": "object"},
                    "minItems": 1,
                    "maxItems": 10,
                }
            },
            required=["records"],
        ),
    },
    {
        "name": "search_crossref",
        "description": "Run a bounded bibliographic query against Crossref.",
        "inputSchema": SEARCH_SCHEMA,
    },
    {
        "name": "search_pubmed",
        "description": "Run a bounded biomedical query through NCBI E-utilities.",
        "inputSchema": SEARCH_SCHEMA,
    },
    {
        "name": "search_europe_pmc",
        "description": "Run a bounded query against Europe PMC metadata.",
        "inputSchema": SEARCH_SCHEMA,
    },
    {
        "name": "get_publication_status",
        "description": "Check current correction, concern, retraction, preprint, and version signals.",
        "inputSchema": STATUS_SCHEMA,
    },
    {
        "name": "get_corrections_retractions",
        "description": "Return explicit post-publication notices from current production metadata.",
        "inputSchema": STATUS_SCHEMA,
    },
    {
        "name": "get_related_versions",
        "description": "Retrieve version and update relationships deposited for a DOI.",
        "inputSchema": object_schema({"doi": DOI}, required=["doi"]),
    },
    {
        "name": "get_abstract_or_open_sections",
        "description": "Retrieve an abstract and, where licensed in Europe PMC, bounded open full-text sections.",
        "inputSchema": IDENTIFIER_SCHEMA,
    },
    {
        "name": "get_references",
        "description": "Retrieve a bounded outgoing-reference list with the provider named.",
        "inputSchema": LINK_SCHEMA,
    },
    {
        "name": "get_citations",
        "description": "Retrieve a bounded incoming-citation list with the provider named.",
        "inputSchema": LINK_SCHEMA,
    },
    {
        "name": "resolve_dataset_or_software",
        "description": "Resolve or search DataCite records for research data or software.",
        "inputSchema": object_schema(
            {
                "doi": DOI,
                "query": STRING,
                "kind": {"enum": ["dataset", "software"], "default": "dataset"},
                "limit": LIMIT,
            },
            one_of=[{"required": ["doi"]}, {"required": ["query"]}],
        ),
    },
]


def schema_hash(tools: list[dict[str, Any]]) -> str:
    canonical = json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result(value: Any) -> dict[str, Any]:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": rendered}],
        "structuredContent": value,
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _validate_arguments(tool: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is unavailable in the MCP runtime"]
    validator = jsonschema.Draft202012Validator(tool["inputSchema"])
    return [error.message for error in validator.iter_errors(arguments)]


def _write(stream: TextIO, value: Mapping[str, Any]) -> None:
    stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
    stream.flush()


def serve_stdio(
    *,
    server_name: str,
    tools: list[dict[str, Any]],
    dispatch: Callable[[str, Mapping[str, Any]], Any],
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Serve newline-delimited MCP JSON-RPC until shutdown/exit or EOF."""

    source = input_stream or sys.stdin
    sink = output_stream or sys.stdout
    initialized = False
    shutdown = False
    for line in source:
        rpc_error = None
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _write(
                sink,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
            continue
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if method == "initialize":
            initialized = True
            response = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server_name, "version": __version__},
                "instructions": "Responses name their data source and access level. Confidential egress is policy-gated.",
            }
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            response = {}
        elif method == "tools/list":
            if not initialized:
                response = None
                rpc_error = {"code": -32002, "message": "Server has not been initialized"}
            else:
                response = {"tools": tools}
        elif method == "tools/call":
            if not initialized:
                response = None
                rpc_error = {"code": -32002, "message": "Server has not been initialized"}
            else:
                name = params.get("name")
                arguments = params.get("arguments") or {}
                test_delay = os.environ.get("OCSL_REVIEW_MCP_TEST_DELAY_MS")
                if name == "ocsl_test_delay" and test_delay:
                    time.sleep(min(int(test_delay), 5000) / 1000)
                    response = _result({"delayedMs": int(test_delay)})
                elif name not in {tool["name"] for tool in tools}:
                    response = _tool_error(f"unknown tool: {name}")
                elif not isinstance(arguments, dict):
                    response = _tool_error("tool arguments must be an object")
                else:
                    tool = next(item for item in tools if item["name"] == name)
                    argument_errors = _validate_arguments(tool, arguments)
                    if argument_errors:
                        response = _tool_error("invalid arguments: " + "; ".join(argument_errors))
                    else:
                        try:
                            response = _result(dispatch(name, arguments))
                        except Exception as exc:  # noqa: BLE001 - tool errors are data, not server crashes
                            response = _tool_error(f"{type(exc).__name__}: {exc}")
        elif method == "shutdown":
            shutdown = True
            response = None
        elif method == "notifications/exit":
            return 0 if shutdown else 1
        else:
            response = None
            rpc_error = {"code": -32601, "message": f"Method not found: {method}"}
        if request_id is None:
            continue
        if rpc_error is not None:
            payload = {"jsonrpc": "2.0", "id": request_id, "error": rpc_error}
        else:
            payload = {"jsonrpc": "2.0", "id": request_id, "result": response}
        _write(sink, payload)
    return 0 if shutdown else 1
