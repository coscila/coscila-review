from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ocsl_review.mcp_protocol import CATALOG_TOOLS, SCHOLAR_TOOLS, schema_hash, serve_stdio

ROOT = Path(__file__).resolve().parents[1]


class McpProtocolTests(unittest.TestCase):
    def test_initialize_list_call_error_and_shutdown(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_skills", "arguments": {"limit": 1}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "bogus", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 5, "method": "shutdown", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/exit", "params": {}},
        ]
        source = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        sink = io.StringIO()
        rc = serve_stdio(
            server_name="test",
            tools=CATALOG_TOOLS,
            dispatch=lambda name, args: [{"id": "x"}],
            input_stream=source,
            output_stream=sink,
        )
        self.assertEqual(rc, 0)
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(responses[1]["result"]["tools"], CATALOG_TOOLS)
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])

    def test_invalid_arguments_are_rejected_before_dispatch(self):
        called = []
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "resolve_identifier", "arguments": {"doi": "bad"}},
            },
            {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/exit", "params": {}},
        ]
        source = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        sink = io.StringIO()
        serve_stdio(
            server_name="test",
            tools=SCHOLAR_TOOLS,
            dispatch=lambda name, args: called.append(name),
            input_stream=source,
            output_stream=sink,
        )
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertEqual(called, [])

    def test_schema_hash_is_order_and_content_stable(self):
        self.assertEqual(
            schema_hash(CATALOG_TOOLS), schema_hash(json.loads(json.dumps(CATALOG_TOOLS)))
        )
        self.assertNotEqual(schema_hash(CATALOG_TOOLS), schema_hash(SCHOLAR_TOOLS))

    def test_launchers_reject_a_current_pointer_outside_the_template_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            template_root = state / "environment-templates" / "linux-64" / "mcp-review"
            template_root.mkdir(parents=True)
            external = root / "external-prefix"
            (external / "bin").mkdir(parents=True)
            (external / ".ocsl-review-template.json").write_text("{}", encoding="utf-8")
            python = external / "bin" / "python"
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
            (template_root / "current").symlink_to(external)
            environment = dict(os.environ)
            environment.pop("OCSL_REVIEW_MCP_PYTHON", None)
            environment.update(
                {
                    "OCSL_REVIEW_HOME": str(ROOT),
                    "OCSL_REVIEW_STATE": str(state),
                    "OCSL_REVIEW_PLATFORM": "linux-64",
                }
            )
            for name in ("ocsl_review_catalog.sh", "ocsl_scholar.sh"):
                with self.subTest(launcher=name):
                    result = subprocess.run(
                        ["bash", str(ROOT / "mcp" / "launchers" / name)],
                        input="",
                        capture_output=True,
                        text=True,
                        env=environment,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 78)
                    self.assertIn("escapes its template root", result.stderr)


if __name__ == "__main__":
    unittest.main()
