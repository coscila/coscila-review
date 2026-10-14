"""Review skill catalog validation and reversible global installation."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
from .errors import ReviewError, ValidationError
from .util import atomic_json, is_within, load_toml, sha256_bytes, sha256_file, tree_digest

REQUIRED_SKILLS = (
    "ocsl-review-router",
    "ocsl-review-manuscript-ingest",
    "ocsl-review-claim-inventory",
    "ocsl-review-reference-integrity",
    "ocsl-review-reference-analysis",
    "ocsl-review-claim-source-verification",
    "ocsl-review-citation-census",
    "ocsl-review-citation-completeness",
    "ocsl-review-literature-search-update",
    "ocsl-review-novelty-priority",
    "ocsl-review-evidence-balance",
    "ocsl-review-methods-statistics",
    "ocsl-review-reporting-guidelines",
    "ocsl-review-results-figures-tables",
    "ocsl-review-reproducibility-data-code",
    "ocsl-review-ethics-transparency",
    "ocsl-review-journal-policy",
    "ocsl-review-systematic-review",
    "ocsl-review-peer-report",
    "ocsl-review-revision-response",
    "ocsl-review-calibration",
    "ocsl-review-environments",
)
ENVIRONMENT_IDS = {
    "review-core",
    "review-documents",
    "review-literature",
    "review-nlp",
    "review-statistics",
    "mcp-review",
}
MCP_IDS = {"ocsl_review_catalog", "ocsl_scholar"}
CATALOG_FIELDS = {
    "id",
    "title",
    "summary",
    "primary_environment",
    "secondary_environments",
    "mcps",
    "outputs",
    "confidential_by_default",
}
DESCRIPTOR_FIELDS = CATALOG_FIELDS | {"schema_version"}
OWNER_FILE = ".ocsl-review-owner.json"
MANIFEST_NAME = "skills-install.json"


def _catalog(paths: Any) -> list[dict[str, Any]]:
    data = load_toml(paths.skill_catalog)
    if set(data) != {"schema_version", "skill"} or data.get("schema_version") != 1:
        raise ValidationError("skill catalog must contain only schema_version=1 and skill")
    skills = data.get("skill")
    if not isinstance(skills, list):
        raise ValidationError("skill catalog field 'skill' must be an array")
    return skills


def _arg(args: Any, *names: str) -> Any:
    for name in names:
        value = getattr(args, name, None)
        if value not in (None, ""):
            return value
    return None


def resolve_skill_roots(args: Any | None = None) -> dict[str, Path]:
    """Resolve independently configurable native-agent and Codex skill roots."""
    args = args or object()
    native = _arg(args, "native_skills_root", "native_skill_root")
    native = native or os.environ.get("OCSL_REVIEW_NATIVE_SKILLS_ROOT")
    if not native:
        config_root = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("LAB_CLAUDE_CONFIG_DIR")
        if config_root:
            native = str(Path(config_root) / "skills")

    codex = _arg(args, "codex_skills_root", "codex_skill_root")
    codex = codex or os.environ.get("OCSL_REVIEW_CODEX_SKILLS_ROOT")
    if not codex:
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home:
            codex = str(Path(codex_home) / "skills")
    if not codex:
        mirror_home = os.environ.get("CLAUDE_HOME") or os.environ.get("LAB_CLAUDE_HOME")
        if mirror_home:
            codex = str(Path(mirror_home) / ".claude" / "skills")

    missing = [name for name, value in (("native", native), ("codex", codex)) if not value]
    if missing:
        joined = ", ".join(missing)
        raise ReviewError(
            f"missing {joined} skill root; set OCSL_REVIEW_NATIVE_SKILLS_ROOT and "
            "OCSL_REVIEW_CODEX_SKILLS_ROOT or pass explicit roots"
        )
    return {
        "native": Path(native).expanduser().resolve(),
        "codex": Path(codex).expanduser().resolve(),
    }


def manifest_path(paths: Any) -> Path:
    return paths.integration / MANIFEST_NAME


def _node_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """Describe files, directories, modes, and symlinks without following links."""
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink():
        return {".": {"kind": "symlink", "target": os.readlink(path)}}
    if path.is_file():
        return {
            ".": {
                "kind": "file",
                "sha256": sha256_file(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        }
    result: dict[str, dict[str, Any]] = {
        ".": {"kind": "directory", "mode": stat.S_IMODE(path.stat().st_mode)}
    }
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            result[relative] = {"kind": "symlink", "target": os.readlink(child)}
        elif child.is_dir():
            result[relative] = {
                "kind": "directory",
                "mode": stat.S_IMODE(child.stat().st_mode),
            }
        elif child.is_file():
            result[relative] = {
                "kind": "file",
                "sha256": sha256_file(child),
                "mode": stat.S_IMODE(child.stat().st_mode),
            }
    return result


def _remove_node(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_node(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValidationError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise ValidationError(f"cannot copy unsupported filesystem node: {source}")


def _backup_existing(target: Path, backup: Path) -> dict[str, Any]:
    before = _node_manifest(target)
    if not before:
        return {"kind": "absent", "path": None, "manifest": {}}
    _copy_node(target, backup)
    after = _node_manifest(backup)
    if after != before:
        _remove_node(backup)
        raise ValidationError(f"recoverability check failed while backing up {target}")
    return {"kind": before["."]["kind"], "path": str(backup), "manifest": before}


def _restore_backup(target: Path, backup: Mapping[str, Any]) -> None:
    _remove_node(target)
    if backup.get("kind") == "absent":
        return
    backup_path = Path(str(backup["path"]))
    if _node_manifest(backup_path) != backup.get("manifest"):
        raise ValidationError(f"stored backup changed or is incomplete: {backup_path}")
    _copy_node(backup_path, target)


def validate_catalog(paths: Any) -> dict[str, Any]:
    records = _catalog(paths)
    problems: list[str] = []
    ids = [record.get("id") for record in records]
    if tuple(ids) != REQUIRED_SKILLS:
        missing = sorted(set(REQUIRED_SKILLS) - set(ids))
        extra = sorted(set(ids) - set(REQUIRED_SKILLS))
        problems.append(f"skill ids/order differ from contract; missing={missing}, extra={extra}")
    if len(ids) != len(set(ids)):
        problems.append("skill ids are not unique")

    catalog_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        skill_id = str(record.get("id", "<missing>"))
        catalog_by_id[skill_id] = record
        if set(record) != CATALOG_FIELDS:
            problems.append(f"{skill_id}: catalog fields do not match the closed contract")
        if record.get("primary_environment") not in ENVIRONMENT_IDS:
            problems.append(f"{skill_id}: unknown primary environment")
        secondary = record.get("secondary_environments", [])
        if not isinstance(secondary, list) or any(
            value not in ENVIRONMENT_IDS for value in secondary
        ):
            problems.append(f"{skill_id}: invalid secondary environment")
        elif len(secondary) != len(set(secondary)):
            problems.append(f"{skill_id}: duplicate secondary environment")
        if record.get("primary_environment") in secondary:
            problems.append(f"{skill_id}: primary environment repeated as secondary")
        mcps = record.get("mcps", [])
        if not isinstance(mcps, list) or any(value not in MCP_IDS for value in mcps):
            problems.append(f"{skill_id}: invalid MCP mapping")
        elif len(mcps) != len(set(mcps)):
            problems.append(f"{skill_id}: duplicate MCP mapping")
        if record.get("confidential_by_default") is not True:
            problems.append(f"{skill_id}: confidentiality default must be true")
        if not isinstance(record.get("outputs"), list) or not record.get("outputs"):
            problems.append(f"{skill_id}: at least one output is required")
        elif len(record["outputs"]) != len(set(record["outputs"])):
            problems.append(f"{skill_id}: duplicate output")

    if not paths.environment_catalog.is_file():
        problems.append("environment catalog is missing")
    else:
        environment_data = load_toml(paths.environment_catalog)
        assigned: dict[str, str] = {}
        for environment in environment_data.get("environment", []):
            environment_id = environment.get("id")
            for skill_id in environment.get("skills", []):
                if skill_id in assigned:
                    problems.append(
                        f"{skill_id}: assigned to multiple primary environments "
                        f"({assigned[skill_id]}, {environment_id})"
                    )
                assigned[skill_id] = environment_id
        expected_assignments = {
            record["id"]: record["primary_environment"]
            for record in records
            if record.get("id") in REQUIRED_SKILLS
        }
        if assigned != expected_assignments:
            for skill_id in REQUIRED_SKILLS:
                if assigned.get(skill_id) != expected_assignments.get(skill_id):
                    problems.append(
                        f"{skill_id}: environment catalog assigns {assigned.get(skill_id)!r}, "
                        f"skill catalog assigns {expected_assignments.get(skill_id)!r}"
                    )

    if not paths.mcp_catalog.is_file():
        problems.append("MCP catalog is missing")
    else:
        mcp_data = load_toml(paths.mcp_catalog)
        enabled_mcps = {
            record.get("name")
            for record in mcp_data.get("server", [])
            if record.get("enabled") is True
        }
        if not MCP_IDS.issubset(enabled_mcps):
            problems.append(f"enabled MCP catalog is missing {sorted(MCP_IDS - enabled_mcps)}")

    skill_root = paths.home / "skills"
    directory_ids = {
        path.name
        for path in skill_root.iterdir()
        if path.is_dir() and path.name.startswith("ocsl-review-")
    }
    if directory_ids != set(REQUIRED_SKILLS):
        problems.append(
            "skill directories do not match catalog: "
            f"missing={sorted(set(REQUIRED_SKILLS) - directory_ids)}, "
            f"extra={sorted(directory_ids - set(REQUIRED_SKILLS))}"
        )

    for skill_id in REQUIRED_SKILLS:
        directory = skill_root / skill_id
        descriptor_path = directory / "skill.toml"
        markdown_path = directory / "SKILL.md"
        if not descriptor_path.is_file() or not markdown_path.is_file():
            problems.append(f"{skill_id}: missing skill.toml or SKILL.md")
            continue
        descriptor = load_toml(descriptor_path)
        if set(descriptor) != DESCRIPTOR_FIELDS or descriptor.get("schema_version") != 1:
            problems.append(f"{skill_id}: descriptor does not match the closed v1 contract")
        expected = catalog_by_id.get(skill_id)
        if expected and {key: descriptor.get(key) for key in CATALOG_FIELDS} != expected:
            problems.append(f"{skill_id}: descriptor and catalog differ")
        markdown = markdown_path.read_text(encoding="utf-8")
        required_fragments = (
            f"name: {skill_id}",
            f"Primary environment: `{descriptor.get('primary_environment')}`",
            '"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \\',
            f"--skill {skill_id} \\",
            '--workspace "$PWD" \\',
            '-- "$OCSL_REVIEW_HOME/bin/ocsl-review"',
        )
        for fragment in required_fragments:
            if fragment not in markdown:
                problems.append(f"{skill_id}: SKILL.md lacks runtime fragment {fragment!r}")
        lowered = markdown.lower()
        if "confidential" not in lowered:
            problems.append(f"{skill_id}: SKILL.md lacks a confidentiality boundary")
        if not any(
            term in lowered
            for term in ("abstain", "abstention", "not-verifiable", "not-fully-verifiable")
        ):
            problems.append(f"{skill_id}: SKILL.md lacks an abstention rule")
        if not any(
            term in lowered for term in ("boundary", "role", "belongs to", "does not", "do not")
        ):
            problems.append(f"{skill_id}: SKILL.md lacks a role boundary")

    if problems:
        raise ValidationError("skill validation failed:\n- " + "\n- ".join(problems))
    return {"ok": True, "skills": len(records), "environments": sorted(ENVIRONMENT_IDS)}


def _manifest_matches_installation(manifest: Mapping[str, Any], roots: Mapping[str, Path]) -> None:
    recorded_roles = {
        role: Path(root_record["root"]).resolve()
        for root_record in manifest.get("roots", [])
        for role in root_record.get("roles", [])
    }
    expected_roles = {role: path.resolve() for role, path in roots.items()}
    if recorded_roles != expected_roles:
        raise ValidationError(
            "skills are already installed into different roots; uninstall that installation first"
        )
    for root_record in manifest.get("roots", []):
        for skill in root_record.get("skills", []):
            target = Path(skill["target"])
            if _node_manifest(target) != skill.get("installedManifest"):
                raise ValidationError(f"installed skill was edited after installation: {target}")


def install_skill_set(
    paths: Any,
    roots: Mapping[str, Path],
    *,
    installation_id: str | None = None,
) -> dict[str, Any]:
    """Install the complete skill set, rolling back all roots on any failure."""
    validate_catalog(paths)
    manifest_file = manifest_path(paths)
    normalized_roots = {role: Path(path).expanduser().resolve() for role, path in roots.items()}
    if set(normalized_roots) != {"native", "codex"}:
        raise ValidationError("both native and codex skill roots are required")
    unique_roots = list(dict.fromkeys(normalized_roots.values()))
    protected_roots = config.protected_workspace_roots(paths)
    for root in unique_roots:
        for label, protected in protected_roots:
            if is_within(root, protected) or is_within(protected, root):
                raise ValidationError(
                    f"skill root overlaps protected {label}: {root} and {protected}"
                )
    for index, first in enumerate(unique_roots):
        for second in unique_roots[index + 1 :]:
            if is_within(first, second) or is_within(second, first):
                raise ValidationError(f"skill roots overlap: {first} and {second}")
    if manifest_file.exists():
        existing = json.loads(manifest_file.read_text(encoding="utf-8"))
        _manifest_matches_installation(existing, normalized_roots)
        return {"installed": True, "reused": True, "manifest": str(manifest_file)}

    install_id = installation_id or uuid.uuid4().hex
    paths.integration.mkdir(parents=True, exist_ok=True)
    grouped: dict[Path, list[str]] = {}
    for role, root in normalized_roots.items():
        grouped.setdefault(root, []).append(role)

    root_records: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    staged: list[Path] = []
    try:
        for root, roles in sorted(grouped.items(), key=lambda item: str(item[0])):
            root.mkdir(parents=True, exist_ok=True)
            root_key = sha256_bytes(str(root).encode("utf-8"))[:16]
            root_record: dict[str, Any] = {
                "roles": sorted(roles),
                "root": str(root),
                "skills": [],
            }
            for skill_id in REQUIRED_SKILLS:
                source = paths.home / "skills" / skill_id
                target = root / skill_id
                stage = root / f".ocsl-review-stage-{install_id}-{skill_id}"
                if stage.exists() or stage.is_symlink():
                    raise ValidationError(f"staging path already exists: {stage}")
                shutil.copytree(source, stage, symlinks=True, copy_function=shutil.copy2)
                staged.append(stage)
                source_digest = tree_digest(source)
                atomic_json(
                    stage / OWNER_FILE,
                    {
                        "schemaVersion": 1,
                        "installationId": install_id,
                        "skillId": skill_id,
                        "sourceDigest": source_digest,
                    },
                )
                installed_manifest = _node_manifest(stage)
                backup_path = (
                    paths.integration / "backups" / install_id / "skills" / root_key / skill_id
                )
                backup = _backup_existing(target, backup_path)
                entry = {
                    "id": skill_id,
                    "target": str(target),
                    "sourceDigest": source_digest,
                    "installedManifest": installed_manifest,
                    "backup": backup,
                }
                # From this point onward rollback owns restoration of the target,
                # including failures between removal and final verification.
                completed.append(entry)
                _remove_node(target)
                os.replace(stage, target)
                staged.remove(stage)
                if _node_manifest(target) != installed_manifest:
                    raise ValidationError(f"installed copy failed verification: {target}")
                root_record["skills"].append(entry)
            root_records.append(root_record)
        manifest = {
            "schemaVersion": 1,
            "installationId": install_id,
            "installedAt": datetime.now(UTC).isoformat(),
            "roots": root_records,
        }
        atomic_json(manifest_file, manifest, mode=0o600)
        return {
            "installed": True,
            "reused": False,
            "skills": len(REQUIRED_SKILLS),
            "roots": {role: str(root) for role, root in normalized_roots.items()},
            "manifest": str(manifest_file),
        }
    except Exception:
        for stage in staged:
            _remove_node(stage)
        rollback_errors: list[str] = []
        for entry in reversed(completed):
            try:
                _restore_backup(Path(entry["target"]), entry["backup"])
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"{entry['target']}: {exc}")
        if rollback_errors:
            raise ReviewError(
                "skill installation failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise


def verify_skill_installation(
    paths: Any, roots: Mapping[str, Path] | None = None
) -> dict[str, Any]:
    manifest_file = manifest_path(paths)
    if not manifest_file.is_file():
        return {"ok": False, "installed": False, "reason": "skill manifest is absent"}
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    try:
        if roots is not None:
            _manifest_matches_installation(manifest, roots)
        else:
            for root_record in manifest.get("roots", []):
                for entry in root_record.get("skills", []):
                    target = Path(entry["target"])
                    if _node_manifest(target) != entry.get("installedManifest"):
                        raise ValidationError(
                            f"installed skill was edited after installation: {target}"
                        )
    except ValidationError as exc:
        return {"ok": False, "installed": True, "reason": str(exc)}
    return {
        "ok": True,
        "installed": True,
        "installationId": manifest.get("installationId"),
        "roots": [record.get("root") for record in manifest.get("roots", [])],
    }


def uninstall_skill_set(paths: Any) -> dict[str, Any]:
    """Remove an unedited installation and restore every exact pre-install node."""
    manifest_file = manifest_path(paths)
    if not manifest_file.exists():
        return {"uninstalled": True, "reused": True, "removed": 0}
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    entries = [
        entry
        for root_record in manifest.get("roots", [])
        for entry in root_record.get("skills", [])
    ]
    for entry in entries:
        target = Path(entry["target"])
        if _node_manifest(target) != entry.get("installedManifest"):
            raise ValidationError(
                f"refusing to uninstall a skill edited after installation: {target}"
            )
        backup = entry.get("backup", {})
        if backup.get("kind") != "absent":
            backup_path = Path(str(backup.get("path")))
            if _node_manifest(backup_path) != backup.get("manifest"):
                raise ValidationError(f"refusing uninstall because backup changed: {backup_path}")

    rollback_root = (
        paths.integration / "backups" / f"uninstall-{uuid.uuid4().hex}" / "installed-skills"
    )
    processed: list[tuple[dict[str, Any], Path]] = []
    try:
        for number, entry in enumerate(entries):
            target = Path(entry["target"])
            transient = rollback_root / f"{number:03d}-{entry['id']}"
            _copy_node(target, transient)
            _restore_backup(target, entry["backup"])
            processed.append((entry, transient))
        manifest_file.unlink()
        return {"uninstalled": True, "reused": False, "removed": len(entries)}
    except Exception:
        rollback_errors: list[str] = []
        for entry, transient in reversed(processed):
            target = Path(entry["target"])
            try:
                _remove_node(target)
                _copy_node(transient, target)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"{target}: {exc}")
        if rollback_errors:
            raise ReviewError(
                "skill uninstall failed and rollback was incomplete: " + "; ".join(rollback_errors)
            )
        raise


def cmd_list(args: Any) -> list[dict[str, Any]]:
    return _catalog(args.paths)


def cmd_show(args: Any) -> dict[str, Any]:
    skill_id = _arg(args, "skill")
    if not skill_id:
        extras = list(getattr(args, "extra", []) or [])
        skill_id = extras[0] if extras else None
    for record in _catalog(args.paths):
        if record["id"] == skill_id:
            path = args.paths.home / "skills" / skill_id / "SKILL.md"
            return {"skill": record, "path": str(path), "content": path.read_text(encoding="utf-8")}
    raise ReviewError(f"unknown review skill: {skill_id}")


def cmd_validate(args: Any) -> dict[str, Any]:
    return validate_catalog(args.paths)


def cmd_install(args: Any) -> dict[str, Any]:
    return install_skill_set(args.paths, resolve_skill_roots(args))


def cmd_uninstall(args: Any) -> dict[str, Any]:
    return uninstall_skill_set(args.paths)
