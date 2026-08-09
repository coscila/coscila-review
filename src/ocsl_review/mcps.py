"""MCP rendering, protocol acceptance probes, direct calls, and staged checks."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ReviewError, ValidationError
from .mcp_protocol import schema_hash
from .util import load_toml


def load_catalog(paths) -> list[dict[str, Any]]:
    data = load_toml(paths.mcp_catalog)
    return list(data.get("server", []))


def load_schema_lock(paths) -> dict[str, str]:
    lock_path = paths.home / "mcp" / "tool-schema-lock.json"
    if not lock_path.is_file():
        raise ValidationError(f"MCP tool-schema lock is missing: {lock_path}")
    value = json.loads(lock_path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1 or not isinstance(value.get("servers"), dict):
        raise ValidationError("MCP tool-schema lock has an unsupported structure")
    return {name: record["sha256"] for name, record in value["servers"].items()}


def companion_entries(paths) -> dict[str, Any]:
    state = paths.need_state()
    entries: dict[str, Any] = {}
    for record in load_catalog(paths):
        if not record.get("enabled"):
            continue
        launcher = (paths.home / record["launcher"]).resolve()
        if not launcher.is_file():
            raise ValidationError(f"enabled MCP launcher is missing: {launcher}")
        entries[record["name"]] = {
            "command": "bash",
            "args": [str(launcher)],
            "env": {
                "OCSL_REVIEW_HOME": str(paths.home),
                "OCSL_REVIEW_STATE": str(state),
            },
        }
    expected = {"ocsl_review_catalog", "ocsl_scholar"}
    if set(entries) != expected:
        raise ValidationError(f"enabled MCP set must be exactly {sorted(expected)}")
    return entries


def _source_launch(paths, record: Mapping[str, Any]) -> tuple[list[str], dict[str, str]]:
    launcher = (paths.home / str(record["launcher"])).resolve()
    if not launcher.is_file():
        raise ValidationError(f"MCP launcher does not resolve: {launcher}")
    env = dict(os.environ)
    env.update(
        {
            "OCSL_REVIEW_HOME": str(paths.home),
            "OCSL_REVIEW_STATE": str(paths.need_state()),
        }
    )
    return ["bash", str(launcher)], env


def _entry_launch(entry: Mapping[str, Any]) -> tuple[list[str], dict[str, str]]:
    command = str(entry.get("command", ""))
    if not command:
        raise ValidationError("staged MCP entry has no command")
    if os.path.sep not in command and shutil.which(command) is None:
        raise ValidationError(f"staged MCP command is not executable: {command}")
    if os.path.sep in command and not Path(command).is_file():
        raise ValidationError(f"staged MCP command does not resolve: {command}")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValidationError("staged MCP args must be a string array")
    env = dict(os.environ)
    raw_env = entry.get("env", {})
    if not isinstance(raw_env, dict):
        raise ValidationError("staged MCP env must be an object")
    env.update({str(key): str(value) for key, value in raw_env.items()})
    return [command, *args], env


@dataclass
class RpcProcess:
    process: subprocess.Popen[str]

    @classmethod
    def start(cls, argv: Sequence[str], env: Mapping[str, str]) -> RpcProcess:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=dict(env),
        )
        return cls(process)

    def send(self, message: Mapping[str, Any]) -> None:
        if self.process.stdin is None:
            raise ReviewError("MCP stdin is closed")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def receive(self, request_id: int | str, timeout: float) -> dict[str, Any]:
        if self.process.stdout is None:
            raise ReviewError("MCP stdout is closed")
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                events = selector.select(max(remaining, 0))
                if not events:
                    break
                line = self.process.stdout.readline()
                if not line:
                    if self.process.poll() is not None:
                        stderr = self.process.stderr.read(1000) if self.process.stderr else ""
                        raise ReviewError(
                            f"MCP exited before response {request_id} (code {self.process.returncode}): {stderr.strip()}"
                        )
                    continue
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") == request_id:
                    return response
        finally:
            selector.close()
        raise TimeoutError(f"MCP response {request_id} exceeded {timeout:.2f}s")

    def rpc(
        self, request_id: int, method: str, params: Mapping[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        return self.receive(request_id, timeout)

    def initialize(self, timeout: float) -> dict[str, Any]:
        response = self.rpc(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ocsl-review-doctor", "version": "0.1.0"},
            },
            timeout,
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return response

    def graceful_shutdown(self, timeout: float = 5.0) -> bool:
        try:
            response = self.rpc(99, "shutdown", {}, timeout)
            if "error" in response:
                return False
            self.send({"jsonrpc": "2.0", "method": "notifications/exit", "params": {}})
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=timeout)
            return self.process.returncode == 0
        except (ReviewError, TimeoutError, OSError, ValueError, subprocess.SubprocessError):
            return False

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _probe_timeout_path(argv: Sequence[str], env: Mapping[str, str]) -> bool:
    delayed_env = dict(env)
    delayed_env["OCSL_REVIEW_MCP_TEST_DELAY_MS"] = "500"
    session = RpcProcess.start(argv, delayed_env)
    try:
        session.initialize(5)
        session.send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "ocsl_test_delay", "arguments": {}},
            }
        )
        try:
            session.receive(7, 0.05)
            return False
        except TimeoutError:
            return True
    finally:
        session.kill()


def probe_server(
    *,
    name: str,
    argv: Sequence[str],
    env: Mapping[str, str],
    expected_schema_hash: str,
    live_tool: str | None,
    live_arguments: Mapping[str, Any] | None,
    startup_timeout: float,
    call_timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    report: dict[str, Any] = {
        "name": name,
        "launcherResolved": True,
        "initialized": False,
        "toolsListed": False,
        "schemaHash": None,
        "schemaHashMatches": False,
        "boundedCall": False if live_tool else None,
        "errorPath": False,
        "timeoutPath": False,
        "cleanShutdown": False,
    }
    session = RpcProcess.start(argv, env)
    try:
        initialized = session.initialize(startup_timeout)
        report["initialized"] = "result" in initialized
        tool_response = session.rpc(2, "tools/list", {}, startup_timeout)
        tools = tool_response.get("result", {}).get("tools", [])
        report["toolsListed"] = bool(tools)
        report["toolCount"] = len(tools)
        report["schemaHash"] = schema_hash(tools)
        report["schemaHashMatches"] = report["schemaHash"] == expected_schema_hash
        unknown = session.rpc(
            3,
            "tools/call",
            {"name": "not_a_real_review_tool", "arguments": {}},
            call_timeout,
        )
        report["errorPath"] = bool(unknown.get("result", {}).get("isError"))
        if live_tool:
            called = session.rpc(
                4,
                "tools/call",
                {"name": live_tool, "arguments": dict(live_arguments or {})},
                call_timeout,
            )
            result = called.get("result", {})
            report["boundedCall"] = bool(result) and not result.get("isError", True)
            if result.get("isError"):
                report["boundedCallError"] = result.get("content", [{}])[0].get("text")
        report["cleanShutdown"] = session.graceful_shutdown()
    except Exception as exc:  # noqa: BLE001 - aggregate health must preserve all server results
        report["error"] = f"{type(exc).__name__}: {exc}"
        session.kill()
    try:
        report["timeoutPath"] = _probe_timeout_path(argv, env)
    except Exception as exc:  # noqa: BLE001
        report["timeoutPathError"] = f"{type(exc).__name__}: {exc}"
    report["elapsedMs"] = round((time.monotonic() - started) * 1000)
    required = (
        "launcherResolved",
        "initialized",
        "toolsListed",
        "schemaHashMatches",
        "errorPath",
        "timeoutPath",
        "cleanShutdown",
    )
    report["ok"] = all(report.get(key) is True for key in required) and (
        live_tool is None or report.get("boundedCall") is True
    )
    return report


def load_staged_entries(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"staged MCP configuration does not exist: {path}")
    if path.suffix.lower() == ".json" or path.name == ".claude.json":
        value = json.loads(path.read_text(encoding="utf-8"))
        entries = value.get("mcpServers", {})
    elif path.suffix.lower() == ".toml":
        with path.open("rb") as handle:
            value = tomllib.load(handle)
        entries = value.get("mcp_servers", {})
    else:
        raise ValidationError("staged MCP config must be JSON or TOML")
    if not isinstance(entries, dict):
        raise ValidationError("staged MCP config has no server table")
    return entries


def doctor(
    paths,
    *,
    live: bool,
    staged_config: Path | None,
) -> dict[str, Any]:
    locks = load_schema_lock(paths)
    source_reports = []
    staged_reports = []
    records = [record for record in load_catalog(paths) if record.get("enabled")]
    for record in records:
        name = record["name"]
        if name not in locks:
            raise ValidationError(f"no tool-schema lock for enabled MCP {name}")
        argv, env = _source_launch(paths, record)
        source_reports.append(
            probe_server(
                name=name,
                argv=argv,
                env=env,
                expected_schema_hash=locks[name],
                live_tool=record["live_tool"] if live else None,
                live_arguments=json.loads(record["live_arguments"]) if live else None,
                startup_timeout=float(record["startup_timeout_seconds"]),
                call_timeout=float(record["call_timeout_seconds"]),
            )
        )
    if staged_config:
        staged = load_staged_entries(staged_config)
        for record in records:
            name = record["name"]
            if name not in staged:
                staged_reports.append(
                    {"name": name, "ok": False, "error": "missing from staged config"}
                )
                continue
            argv, env = _entry_launch(staged[name])
            staged_reports.append(
                probe_server(
                    name=name,
                    argv=argv,
                    env=env,
                    expected_schema_hash=locks[name],
                    live_tool=record["live_tool"] if live else None,
                    live_arguments=json.loads(record["live_arguments"]) if live else None,
                    startup_timeout=float(record["startup_timeout_seconds"]),
                    call_timeout=float(record["call_timeout_seconds"]),
                )
            )
    source_ok = len(source_reports) == len(records) and all(
        item.get("ok") for item in source_reports
    )
    staged_ok = (
        bool(staged_config)
        and len(staged_reports) == len(records)
        and all(item.get("ok") for item in staged_reports)
    )
    return {
        "ok": source_ok and (staged_ok if staged_config else True),
        "acceptanceComplete": source_ok and staged_ok and live,
        "liveCallsIncluded": live,
        "stagedConfig": str(staged_config) if staged_config else None,
        "source": source_reports,
        "staged": staged_reports,
    }


def direct_call(paths, server: str, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    record = next((item for item in load_catalog(paths) if item["name"] == server), None)
    if not record or not record.get("enabled"):
        raise ReviewError(f"unknown or disabled MCP server: {server}")
    argv, env = _source_launch(paths, record)
    session = RpcProcess.start(argv, env)
    try:
        session.initialize(float(record["startup_timeout_seconds"]))
        response = session.rpc(
            2,
            "tools/call",
            {"name": tool, "arguments": dict(arguments)},
            float(record["call_timeout_seconds"]),
        )
        if not session.graceful_shutdown():
            raise ReviewError("MCP call succeeded but the server did not shut down cleanly")
        return response.get("result", {})
    finally:
        session.kill()


def cmd_list(args):
    return [
        {
            "name": record["name"],
            "enabled": record["enabled"],
            "environment": record["environment"],
            "transport": record["transport"],
        }
        for record in load_catalog(args.paths)
    ]


def cmd_render(args):
    return {"mcpServers": companion_entries(args.paths)}


def cmd_doctor(args):
    result = doctor(
        args.paths,
        live=bool(getattr(args, "live", False)),
        staged_config=Path(args.staged_config).resolve()
        if getattr(args, "staged_config", None)
        else None,
    )
    if getattr(args, "strict", False) and not result["acceptanceComplete"]:
        raise ValidationError(
            "strict MCP acceptance requires successful live source and staged-config probes"
        )
    return result


def cmd_call(args):
    raw = getattr(args, "arguments", "{}")
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError("--arguments must contain one JSON object") from exc
    if not isinstance(arguments, dict):
        raise ReviewError("--arguments must contain one JSON object")
    return direct_call(args.paths, args.server, args.tool, arguments)
