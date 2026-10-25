"""Closed-world validation for every tracked companion contract."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from . import config, repositories, skills
from .errors import ValidationError
from .mcp_protocol import CATALOG_TOOLS, SCHOLAR_TOOLS, schema_hash
from .util import is_within, load_toml, sha256_file

CATALOG_SCHEMAS = {
    "environments": ("environment-catalog.schema.json", "environments/catalog.toml"),
    "skills": ("skill-catalog.schema.json", "skills/catalog.toml"),
    "mcp": ("mcp-catalog.schema.json", "mcp/catalog.toml"),
    "repositories": ("repository-catalog.schema.json", "repos/catalog.toml"),
    "repositoryLock": ("repository-lock.schema.json", "repos/repositories.lock.json"),
    "standards": ("standards-catalog.schema.json", "standards/catalog.toml"),
}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON contract {path}: {exc}") from exc


def _validate(value: Any, schema: Mapping[str, Any], label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        detail = []
        for error in errors[:20]:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            detail.append(f"{location}: {error.message}")
        raise ValidationError(f"{label} violates its schema:\n- " + "\n- ".join(detail))


def _tracked_contracts(paths: Any) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    schema_root = paths.home / "schemas"
    for schema_file in sorted(schema_root.glob("*.schema.json")):
        schema = _load_json(schema_file)
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise ValidationError(f"invalid JSON Schema {schema_file.name}: {exc.message}") from exc
    for label, (schema_name, relative) in CATALOG_SCHEMAS.items():
        source = paths.home / relative
        if not source.is_file():
            raise ValidationError(f"tracked contract is missing: {source}")
        value = _load_json(source) if source.suffix == ".json" else load_toml(source)
        schema = _load_json(schema_root / schema_name)
        _validate(value, schema, label)
        reports[label] = {"path": str(source), "sha256": sha256_file(source)}
    reports["jsonSchemas"] = len(list(schema_root.glob("*.schema.json")))
    return reports


def _environment_contracts(paths: Any) -> dict[str, Any]:
    catalog = load_toml(paths.environment_catalog)
    records = catalog["environment"]
    identifiers = [record["id"] for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("environment identifiers must be unique")
    for record in records:
        environment_id = record["id"]
        spec = (paths.home / "environments" / record["spec"]).resolve()
        input_lock = (paths.home / "environments" / record["input_lock"]).resolve()
        environment_root = (paths.home / "environments").resolve()
        if not is_within(spec, environment_root) or not is_within(input_lock, environment_root):
            raise ValidationError(f"{environment_id}: environment path escapes its source root")
        if not spec.is_file() or not input_lock.is_file():
            raise ValidationError(f"{environment_id}: spec or input lock is missing")
        if sha256_file(input_lock) != record["input_lock_sha256"]:
            raise ValidationError(f"{environment_id}: tracked input-lock digest differs")
        for label, source in (("spec", spec), ("input lock", input_lock)):
            try:
                value = yaml.safe_load(source.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise ValidationError(f"{environment_id}: invalid {label}: {exc}") from exc
            if not isinstance(value, dict) or not isinstance(value.get("dependencies"), list):
                raise ValidationError(f"{environment_id}: {label} needs a dependency array")
            if value.get("channels") != ["conda-forge", "nodefaults"]:
                raise ValidationError(f"{environment_id}: {label} must close channel resolution")
        locked = yaml.safe_load(input_lock.read_text(encoding="utf-8"))["dependencies"]
        if any(not isinstance(item, str) or "=" not in item for item in locked):
            raise ValidationError(f"{environment_id}: every input-lock dependency must be pinned")
    return {"ok": True, "environments": len(records)}


def _mcp_contracts(paths: Any) -> dict[str, Any]:
    records = load_toml(paths.mcp_catalog)["server"]
    names = [record["name"] for record in records]
    if set(names) != {"ocsl_review_catalog", "ocsl_scholar"} or len(names) != 2:
        raise ValidationError("the MCP catalog must own exactly the two review servers")
    tools_by_server = {
        "ocsl_review_catalog": CATALOG_TOOLS,
        "ocsl_scholar": SCHOLAR_TOOLS,
    }
    lock_path = paths.home / "mcp" / "tool-schema-lock.json"
    lock = _load_json(lock_path)
    locked = lock.get("servers", {})
    if set(locked) != set(names):
        raise ValidationError("MCP schema-lock server set differs from the enabled catalog")
    for record in records:
        if record["enabled"] is not True:
            raise ValidationError(f"required MCP is not enabled: {record['name']}")
        launcher = (paths.home / record["launcher"]).resolve()
        if not is_within(launcher, paths.home) or not launcher.is_file():
            raise ValidationError(f"MCP launcher does not resolve: {launcher}")
        if not os.access(launcher, os.X_OK):
            raise ValidationError(f"MCP launcher is not executable: {launcher}")
        tools = tools_by_server[record["name"]]
        expected = schema_hash(tools)
        entry = locked[record["name"]]
        if entry.get("sha256") != expected or entry.get("toolCount") != len(tools):
            raise ValidationError(
                f"MCP tool schema changed without a matching lock: {record['name']}"
            )
        tool = next((item for item in tools if item["name"] == record["live_tool"]), None)
        if tool is None:
            raise ValidationError(f"MCP live probe names an unknown tool: {record['live_tool']}")
        try:
            arguments = json.loads(record["live_arguments"])
        except json.JSONDecodeError as exc:
            raise ValidationError(f"MCP live arguments are invalid JSON: {record['name']}") from exc
        _validate(arguments, tool["inputSchema"], f"{record['name']} live arguments")
    return {"ok": True, "servers": len(records), "schemaLockSha256": sha256_file(lock_path)}


def _standard_contracts(paths: Any) -> dict[str, Any]:
    records = load_toml(paths.standards_catalog)["standard"]
    identifiers = [record["id"] for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("standards identifiers must be unique")
    archive_pattern = re.compile(
        r"^https://web\.archive\.org/web/[0-9]{14}id_/(https://.+)$"
    )
    for record in records:
        archive_url = record.get("archive_url")
        if not archive_url:
            continue
        if record["version_policy"] == "current-official-page":
            raise ValidationError(
                f"{record['id']}: a current-page standard cannot use a fixed archive"
            )
        match = archive_pattern.fullmatch(archive_url)
        if not match or match.group(1) != record["url"]:
            raise ValidationError(
                f"{record['id']}: archive capture does not embed the official URL"
            )
    return {"ok": True, "standards": len(records)}


def validate_path_boundaries(paths: Any) -> dict[str, Any]:
    if paths.state is None:
        return {"ok": True, "state": None}
    state = paths.state.resolve()
    protected = [source for _, source in config.protected_source_roots(paths)]
    for source in protected:
        if is_within(state, source) or is_within(source, state):
            raise ValidationError(f"mutable review state overlaps protected source: {source}")
    return {"ok": True, "state": str(state), "protectedSources": [str(item) for item in protected]}


def validate_catalogs(paths: Any) -> dict[str, Any]:
    tracked = _tracked_contracts(paths)
    environment_report = _environment_contracts(paths)
    skill_report = skills.validate_catalog(paths)
    source_report = repositories.validate_sources(paths)
    mcp_report = _mcp_contracts(paths)
    standard_report = _standard_contracts(paths)
    boundaries = validate_path_boundaries(paths)
    return {
        "ok": True,
        "tracked": tracked,
        "environments": environment_report,
        "skills": skill_report,
        "repositories": source_report,
        "mcp": mcp_report,
        "standards": standard_report,
        "boundaries": boundaries,
    }


def cmd_validate(args: Any) -> dict[str, Any]:
    return validate_catalogs(args.paths)
