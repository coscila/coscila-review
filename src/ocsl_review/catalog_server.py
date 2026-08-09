"""Read-only MCP over the review companion's source catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import resolve_paths
from .errors import ReviewError
from .mcp_protocol import CATALOG_TOOLS, serve_stdio
from .util import load_toml, sha256_file


def _records(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ReviewError(f"catalog is missing: {path}")
    return list(load_toml(path).get(key, []))


def make_dispatch(home: str | None = None):
    paths = resolve_paths(home=home, state_required=False)

    def dispatch(name: str, arguments: Mapping[str, Any]) -> Any:
        limit = min(max(int(arguments.get("limit", 25)), 1), 25)
        skills = _records(paths.skill_catalog, "skill")
        environments = _records(paths.environment_catalog, "environment")
        if name == "list_skills":
            return [
                {key: record.get(key) for key in ("id", "summary", "primary_environment", "mcps")}
                for record in skills[:limit]
            ]
        if name == "describe_skill":
            skill_id = arguments["skill"]
            record = next((item for item in skills if item["id"] == skill_id), None)
            if not record:
                raise ReviewError(f"unknown skill: {skill_id}")
            return {
                "record": record,
                "skillMd": str(paths.home / "skills" / skill_id / "SKILL.md"),
                "descriptor": str(paths.home / "skills" / skill_id / "skill.toml"),
            }
        if name == "resolve_skill_runtime":
            skill_id = arguments["skill"]
            skill = next((item for item in skills if item["id"] == skill_id), None)
            if not skill:
                raise ReviewError(f"unknown skill: {skill_id}")
            environment = next(
                (item for item in environments if item["id"] == skill["primary_environment"]), None
            )
            if not environment:
                raise ReviewError(f"skill {skill_id} maps to an unknown environment")
            return {"skill": skill, "environment": environment}
        if name == "list_environments":
            return environments[:limit]
        if name == "list_repositories":
            lock = __import__("json").loads(paths.repository_lock.read_text(encoding="utf-8"))
            return lock.get("repositories", [])[:limit]
        if name == "list_standards":
            return _records(paths.standards_catalog, "standard")[:limit]
        if name == "describe_contract":
            requested = arguments["name"]
            candidate = paths.home / "schemas" / requested
            if candidate.suffix != ".json":
                candidate = candidate.with_suffix(".schema.json")
            if candidate.parent != paths.home / "schemas" or not candidate.is_file():
                raise ReviewError(f"unknown review schema: {requested}")
            return {
                "name": candidate.name,
                "path": str(candidate),
                "sha256": sha256_file(candidate),
            }
        raise ReviewError(f"unknown catalog operation: {name}")

    return dispatch


def main() -> int:
    return serve_stdio(
        server_name="ocsl_review_catalog", tools=CATALOG_TOOLS, dispatch=make_dispatch()
    )


if __name__ == "__main__":
    raise SystemExit(main())
