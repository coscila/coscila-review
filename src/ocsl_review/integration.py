"""Transactional, additive integration with an OC Science Lab deployment."""

from __future__ import annotations

import json
import os
import shlex
import stat
import uuid
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config as review_config
from . import skills
from .errors import ReviewError, ValidationError
from .util import (
    atomic_json,
    atomic_write,
    is_within,
    load_toml,
    require_relative_path,
    run_command,
    sha256_bytes,
    sha256_file,
)

OWNED_MCP_NAMES = ("ocsl_review_catalog", "ocsl_scholar")
INSTALL_STATE_NAME = "install-state.json"


def install_state_path(paths: Any) -> Path:
    return paths.integration / INSTALL_STATE_NAME


def _arg(args: Any, *names: str) -> Any:
    for name in names:
        value = getattr(args, name, None)
        if value not in (None, ""):
            return value
    return None


def _normalize_mcp_entries(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    candidate: Any = value.get("mcpServers", value.get("entries", value))
    if not isinstance(candidate, Mapping):
        raise ValidationError("MCP entries must be an object or an object with mcpServers")
    names = set(candidate)
    if names != set(OWNED_MCP_NAMES):
        raise ValidationError(
            "integration accepts exactly the two owned MCP entries; "
            f"missing={sorted(set(OWNED_MCP_NAMES) - names)}, "
            f"extra={sorted(names - set(OWNED_MCP_NAMES))}"
        )
    result: dict[str, dict[str, Any]] = {}
    for name in OWNED_MCP_NAMES:
        entry = candidate[name]
        if not isinstance(entry, Mapping):
            raise ValidationError(f"MCP entry {name} must be an object")
        if not isinstance(entry.get("command"), str) or not entry["command"]:
            raise ValidationError(f"MCP entry {name} needs a nonempty command")
        if not isinstance(entry.get("args", []), list) or not all(
            isinstance(item, str) for item in entry.get("args", [])
        ):
            raise ValidationError(f"MCP entry {name} args must be an array of strings")
        if "env" in entry and not isinstance(entry["env"], Mapping):
            raise ValidationError(f"MCP entry {name} env must be an object")
        result[name] = deepcopy(dict(entry))
    return result


def load_owned_mcp_entries(
    paths: Any,
    supplied: Mapping[str, Any] | None = None,
    *,
    generated_file: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load exactly the owned entries without importing an MCP implementation module."""
    if supplied is not None:
        return _normalize_mcp_entries(supplied)

    candidates = [
        generated_file,
        paths.integration / "generated-mcp.json",
        paths.home / "mcp" / "deployment.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            value = json.loads(candidate.read_text(encoding="utf-8"))
            return _normalize_mcp_entries(value)

    if not paths.mcp_catalog.is_file():
        raise ReviewError(
            "no generated MCP entries or MCP catalog found; pass mcp_entries explicitly"
        )
    catalog = load_toml(paths.mcp_catalog)
    records = catalog.get("server", [])
    by_name = {record.get("name"): record for record in records if isinstance(record, dict)}
    entries: dict[str, dict[str, Any]] = {}
    for name in OWNED_MCP_NAMES:
        record = by_name.get(name)
        if not record:
            raise ValidationError(f"MCP catalog is missing required server {name}")
        launcher_value = record.get("launcher")
        if not isinstance(launcher_value, str):
            raise ValidationError(f"MCP server {name} has no launcher")
        launcher = paths.home / require_relative_path(launcher_value)
        if not launcher.is_file():
            raise ValidationError(f"MCP launcher does not exist: {launcher}")
        entries[name] = {
            "command": "bash",
            "args": [str(launcher)],
            "env": {
                "OCSL_REVIEW_HOME": str(paths.home),
                "OCSL_REVIEW_STATE": str(paths.need_state()),
            },
        }
    return _normalize_mcp_entries(entries)


def resolve_mcp_config(args: Any | None = None) -> Path:
    args = args or object()
    value = _arg(args, "mcp_config", "mcp_config_path")
    value = value or os.environ.get("OCSL_REVIEW_MCP_CONFIG")
    value = value or os.environ.get("LAB_CODEX_MCP_CONFIG")
    if value:
        # Keep the final path component unresolved so the transaction can
        # reject a deployment config that is itself a symbolic link.
        return Path(value).expanduser().absolute()
    config_root = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("LAB_CLAUDE_CONFIG_DIR")
    if config_root:
        return Path(config_root).expanduser().resolve() / ".claude.json"
    raise ReviewError(
        "deployment MCP config is not configured; set OCSL_REVIEW_MCP_CONFIG or pass --mcp-config"
    )


def resolve_staged_mcp_config(
    args: Any | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    required: bool = False,
) -> Path | None:
    """Resolve the generated MCP config without confusing it with its source file."""
    args = args or object()
    value = _arg(args, "staged_config", "acceptance_config")
    value = value or os.environ.get("OCSL_REVIEW_STAGED_MCP_CONFIG")
    if value is None and state is not None:
        mcp = state.get("mcp", {})
        if isinstance(mcp, Mapping):
            value = mcp.get("stagedConfigPath")
    if value:
        return Path(os.fspath(value)).expanduser().resolve()
    if required:
        raise ValidationError(
            "the generated deployment MCP config is unknown; pass --staged-config "
            "or set OCSL_REVIEW_STAGED_MCP_CONFIG"
        )
    return None


def resolve_staging(
    paths: Any,
    args: Any | None = None,
) -> tuple[list[str], Path, float]:
    args = args or object()
    command = _arg(args, "stage_command", "staging_command")
    command = command or os.environ.get("OCSL_REVIEW_STAGE_COMMAND")
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, Sequence):
        argv = [os.fspath(item) for item in command]
    elif paths.lab_repo and (paths.lab_repo / "package.json").is_file():
        argv = ["corepack", "pnpm", "puppenclaw:config"]
    else:
        raise ReviewError(
            "a deployment staging command is required; set OCSL_REVIEW_STAGE_COMMAND "
            "or pass --stage-command"
        )
    if not argv:
        raise ReviewError("deployment staging command cannot be empty")

    cwd_value = _arg(args, "stage_cwd", "staging_cwd")
    cwd_value = cwd_value or os.environ.get("OCSL_REVIEW_STAGE_CWD")
    cwd = Path(cwd_value).expanduser().resolve() if cwd_value else paths.lab_repo
    if cwd is None:
        raise ReviewError("deployment staging requires an OC Science Lab checkout or stage cwd")
    if not cwd.is_dir():
        raise ReviewError(f"deployment staging cwd does not exist: {cwd}")

    replacements = {
        "{ocsl_repo}": str(paths.lab_repo or cwd),
        "{review_home}": str(paths.home),
        "{review_state}": str(paths.need_state()),
    }
    argv = [replacements.get(item, item) for item in argv]
    timeout_value = _arg(args, "stage_timeout", "staging_timeout")
    if timeout_value is None:
        timeout_value = os.environ.get("OCSL_REVIEW_STAGE_TIMEOUT", "900")
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise ReviewError("deployment staging timeout must be numeric") from exc
    if timeout <= 0:
        raise ReviewError("deployment staging timeout must be positive")
    return argv, cwd, timeout


def _run_staging(argv: Sequence[str], cwd: Path, timeout: float) -> dict[str, Any]:
    result = run_command(argv, cwd=cwd, timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ReviewError(f"deployment staging failed ({result.returncode}): {detail[:500]}")
    return {"argv": list(argv), "cwd": str(cwd), "returncode": result.returncode}


def _git_snapshot(repository: Path) -> dict[str, str] | None:
    if not (repository / ".git").exists():
        return None
    head = run_command(["git", "-C", str(repository), "rev-parse", "HEAD"])
    if head.returncode:
        raise ReviewError(f"cannot inspect sibling Git repository: {repository}")
    index = run_command(
        ["git", "-C", str(repository), "diff", "--cached", "--no-ext-diff", "--binary", "HEAD"]
    )
    worktree = run_command(
        ["git", "-C", str(repository), "diff", "--no-ext-diff", "--binary", "HEAD"]
    )
    if index.returncode or worktree.returncode:
        raise ReviewError(f"cannot inspect tracked changes in sibling: {repository}")
    return {
        "head": head.stdout.strip(),
        "indexDiffSha256": sha256_bytes(index.stdout.encode("utf-8")),
        "worktreeDiffSha256": sha256_bytes(worktree.stdout.encode("utf-8")),
    }


def snapshot_siblings(paths: Any) -> dict[str, dict[str, str]]:
    # The lab checkout in use and the first bio sibling must be Git worktrees, as before; every further sibling under an
    # old or a new name is snapshotted too when it is one.
    candidates: list[tuple[Path, bool]] = []
    if paths.lab_repo:
        candidates.append((paths.lab_repo.resolve(), True))
    for index, bio in enumerate(review_config.sibling_checkouts(paths.home, review_config.BIO_REPO_NAMES)):
        candidates.append((bio.resolve(), index == 0))
    for lab in review_config.sibling_checkouts(paths.home, review_config.LAB_REPO_NAMES):
        candidates.append((lab.resolve(), False))
    snapshots: dict[str, dict[str, str]] = {}
    seen: set[Path] = set()
    for repository, required in candidates:
        if repository in seen:
            continue
        seen.add(repository)
        snapshot = _git_snapshot(repository)
        if snapshot is None:
            if required:
                raise ValidationError(f"sibling is not a Git worktree: {repository}")
            continue
        snapshots[str(repository)] = snapshot
    return snapshots


def _assert_siblings_unchanged(
    before: Mapping[str, Mapping[str, str]],
    after: Mapping[str, Mapping[str, str]],
) -> None:
    if before != after:
        changed = sorted(set(before) | set(after))
        raise ValidationError(
            "deployment operation changed a sibling repository's tracked tree: "
            + ", ".join(changed)
        )


def _read_json_config(path: Path) -> tuple[bool, bytes, dict[str, Any]]:
    if path.is_symlink():
        raise ValidationError(f"MCP config must not be a symbolic link: {path}")
    if not path.exists():
        return False, b"", {}
    if not path.is_file():
        raise ValidationError(f"MCP config is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"MCP config is not valid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("MCP config root must be an object")
    servers = value.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValidationError("MCP config mcpServers must be an object")
    return True, raw, value


def _merge_mcp_config(
    paths: Any,
    config_path: Path,
    entries: Mapping[str, Mapping[str, Any]],
    install_id: str,
) -> dict[str, Any]:
    existed, raw, config = _read_json_config(config_path)
    config_mode = stat.S_IMODE(config_path.stat().st_mode) if existed else 0o600
    backup = paths.integration / "backups" / install_id / "mcp-config.before"
    atomic_write(backup, raw, mode=0o600)
    servers = deepcopy(config.get("mcpServers", {}))
    previous = {
        name: {"present": name in servers, "value": deepcopy(servers.get(name))}
        for name in OWNED_MCP_NAMES
    }
    try:
        for name in OWNED_MCP_NAMES:
            servers[name] = deepcopy(entries[name])
        config["mcpServers"] = servers
        atomic_json(config_path, config, mode=config_mode)
        _, _, verified = _read_json_config(config_path)
        if any(verified["mcpServers"].get(name) != entries[name] for name in OWNED_MCP_NAMES):
            raise ValidationError("MCP config merge did not verify")
    except Exception:
        if existed:
            atomic_write(config_path, raw, mode=config_mode)
        elif config_path.exists():
            config_path.unlink()
        raise
    return {
        "configPath": str(config_path),
        "configExisted": existed,
        "configMode": config_mode,
        "backupPath": str(backup),
        "backupSha256": sha256_file(backup),
        "previousOwned": previous,
        "installedOwned": deepcopy(dict(entries)),
    }


def _restore_mcp_file(record: Mapping[str, Any]) -> None:
    config_path = Path(record["configPath"])
    backup = Path(record["backupPath"])
    if not backup.is_file() or sha256_file(backup) != record.get("backupSha256"):
        raise ValidationError(f"MCP backup is missing or changed: {backup}")
    if record.get("configExisted"):
        atomic_write(
            config_path,
            backup.read_bytes(),
            mode=int(record.get("configMode", 0o600)),
        )
    elif config_path.exists():
        config_path.unlink()


def _config_with_previous_owned(
    record: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any], bool]:
    config_path = Path(record["configPath"])
    _, raw, config = _read_json_config(config_path)
    servers = deepcopy(config.get("mcpServers", {}))
    for name in OWNED_MCP_NAMES:
        if servers.get(name) != record["installedOwned"][name]:
            raise ValidationError(f"refusing to remove MCP entry edited after installation: {name}")
    for name in OWNED_MCP_NAMES:
        previous = record["previousOwned"][name]
        if previous["present"]:
            servers[name] = deepcopy(previous["value"])
        else:
            servers.pop(name, None)
    config["mcpServers"] = servers
    backup = Path(record["backupPath"])
    backup_raw = backup.read_bytes()
    if record.get("configExisted"):
        original = json.loads(backup_raw.decode("utf-8"))
    else:
        original = {}
    expected = deepcopy(original)
    expected_servers = deepcopy(expected.get("mcpServers", {}))
    for name in OWNED_MCP_NAMES:
        expected_servers[name] = deepcopy(record["installedOwned"][name])
    expected["mcpServers"] = expected_servers
    return raw, config, expected == json.loads(raw.decode("utf-8"))


def _requirements(paths: Any) -> dict[str, Any]:
    return {
        "environmentVariables": {
            "OCSL_REVIEW_HOME": str(paths.home),
            "OCSL_REVIEW_STATE": str(paths.need_state()),
            "LAB_CONDA_INSTALL_ROOT": os.environ.get("LAB_CONDA_INSTALL_ROOT"),
            "LAB_CONDA_ROOT": os.environ.get("LAB_CONDA_ROOT"),
        },
        "readOnlyRoots": [
            str(paths.home),
            str(paths.templates),
            str(paths.environment_locks),
            str(paths.repositories),
            str(paths.standards),
        ],
    }


def _validate_install_state(state: Mapping[str, Any]) -> None:
    required = {
        "schemaVersion",
        "installId",
        "installedAt",
        "reviewHome",
        "reviewState",
        "skillManifest",
        "mcp",
        "requirements",
        "staging",
        "siblings",
    }
    if set(state) != required or state.get("schemaVersion") != 1:
        raise ValidationError("install state does not match the closed v1 contract")
    mcp = state.get("mcp")
    if not isinstance(mcp, Mapping) or set(mcp.get("installedOwned", {})) != set(OWNED_MCP_NAMES):
        raise ValidationError("install state has invalid owned MCP records")
    staged_config = mcp.get("stagedConfigPath")
    if staged_config is not None and (
        not isinstance(staged_config, str) or not staged_config.strip()
    ):
        raise ValidationError("install state has invalid staged MCP config identity")
    config_mode = mcp.get("configMode")
    if (
        not isinstance(config_mode, int)
        or isinstance(config_mode, bool)
        or not 0 <= config_mode <= 0o777
    ):
        raise ValidationError("install state has invalid MCP config mode")
    manifest = state.get("skillManifest", {})
    if not isinstance(manifest, Mapping) or not manifest.get("path") or not manifest.get("sha256"):
        raise ValidationError("install state has invalid skill manifest identity")


def _verify_active_state(paths: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    _validate_install_state(state)
    skill_manifest = Path(state["skillManifest"]["path"])
    if (
        not skill_manifest.is_file()
        or sha256_file(skill_manifest) != state["skillManifest"]["sha256"]
    ):
        raise ValidationError("installed skill manifest is absent or changed")
    skill_check = skills.verify_skill_installation(paths)
    if not skill_check.get("ok"):
        raise ValidationError(skill_check.get("reason", "skill installation verification failed"))
    mcp = state["mcp"]
    config_path = Path(mcp["configPath"])
    _, _, config = _read_json_config(config_path)
    if stat.S_IMODE(config_path.stat().st_mode) != mcp["configMode"]:
        raise ValidationError("installed MCP config mode changed")
    servers = config.get("mcpServers", {})
    for name in OWNED_MCP_NAMES:
        if servers.get(name) != mcp["installedOwned"][name]:
            raise ValidationError(f"installed MCP entry changed: {name}")
    backup = Path(mcp["backupPath"])
    if not backup.is_file() or sha256_file(backup) != mcp["backupSha256"]:
        raise ValidationError("MCP rollback backup is absent or changed")
    return {"ok": True, "skills": skill_check, "mcp": list(OWNED_MCP_NAMES)}


def _rollback_install(
    paths: Any,
    mcp_record: Mapping[str, Any] | None,
    *,
    stage_argv: Sequence[str],
    stage_cwd: Path,
    stage_timeout: float,
    rerun_staging: bool,
) -> list[str]:
    errors: list[str] = []
    if mcp_record is not None:
        try:
            _restore_mcp_file(mcp_record)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"MCP restore: {exc}")
    try:
        skills.uninstall_skill_set(paths)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"skill restore: {exc}")
    if rerun_staging and mcp_record is not None:
        try:
            _run_staging(stage_argv, stage_cwd, stage_timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"restored-config staging: {exc}")
    return errors


def install_integration(
    paths: Any,
    *,
    skill_roots: Mapping[str, Path],
    mcp_entries: Mapping[str, Any],
    mcp_config: Path,
    stage_argv: Sequence[str],
    stage_cwd: Path,
    stage_timeout: float = 900,
    staged_mcp_config: Path | None = None,
) -> dict[str, Any]:
    """Install skills and MCP entries as one rollback-capable transaction."""
    state_root = paths.need_state().resolve()
    if is_within(state_root, paths.home) or is_within(paths.home, state_root):
        raise ValidationError("review state and companion source must not overlap")
    protected_sources = [source for _, source in review_config.protected_source_roots(paths)]
    config_resolved = Path(mcp_config).resolve()
    staged_config_resolved = (
        Path(staged_mcp_config).resolve() if staged_mcp_config is not None else None
    )
    if staged_config_resolved == config_resolved:
        raise ValidationError(
            "the staged MCP config must differ from the deployment MCP source config"
        )
    for source in protected_sources:
        if is_within(config_resolved, source):
            raise ValidationError(
                f"MCP config must not be inside tracked source: {config_resolved}"
            )
    state_path = install_state_path(paths)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        verification = _verify_active_state(paths, state)
        requested_entries = _normalize_mcp_entries(mcp_entries)
        requested_roots = {role: Path(root).resolve() for role, root in skill_roots.items()}
        root_check = skills.verify_skill_installation(paths, requested_roots)
        if not root_check.get("ok"):
            raise ValidationError(root_check.get("reason", "requested skill roots differ"))
        if Path(state["mcp"]["configPath"]).resolve() != Path(mcp_config).resolve():
            raise ValidationError("active installation uses a different MCP config path")
        recorded_staged = state["mcp"].get("stagedConfigPath")
        if (
            recorded_staged is not None
            and staged_config_resolved is not None
            and Path(recorded_staged).resolve() != staged_config_resolved
        ):
            raise ValidationError("active installation uses a different staged MCP config path")
        if state["mcp"]["installedOwned"] != requested_entries:
            raise ValidationError("active installation uses different owned MCP entries")
        if state["staging"] != {"argv": list(stage_argv), "cwd": str(stage_cwd)}:
            raise ValidationError("active installation uses a different staging command or cwd")
        return {"installed": True, "reused": True, "verification": verification}
    if skills.manifest_path(paths).exists():
        raise ReviewError(
            "a standalone review-skill installation already exists; remove it before integration"
        )

    entries = _normalize_mcp_entries(mcp_entries)
    install_id = uuid.uuid4().hex
    siblings_before = snapshot_siblings(paths)
    mcp_record: dict[str, Any] | None = None
    staging_attempted = False
    try:
        skills.install_skill_set(paths, skill_roots, installation_id=install_id)
        skill_manifest = skills.manifest_path(paths)
        mcp_record = _merge_mcp_config(paths, Path(mcp_config), entries, install_id)
        if staged_config_resolved is not None:
            mcp_record["stagedConfigPath"] = str(staged_config_resolved)
        staging_attempted = True
        _run_staging(stage_argv, stage_cwd, stage_timeout)
        siblings_after = snapshot_siblings(paths)
        _assert_siblings_unchanged(siblings_before, siblings_after)
        state = {
            "schemaVersion": 1,
            "installId": install_id,
            "installedAt": datetime.now(UTC).isoformat(),
            "reviewHome": str(paths.home),
            "reviewState": str(paths.need_state()),
            "skillManifest": {
                "path": str(skill_manifest),
                "sha256": sha256_file(skill_manifest),
            },
            "mcp": mcp_record,
            "requirements": _requirements(paths),
            "staging": {"argv": list(stage_argv), "cwd": str(stage_cwd)},
            "siblings": siblings_after,
        }
        _validate_install_state(state)
        atomic_json(state_path, state, mode=0o600)
        return {
            "installed": True,
            "reused": False,
            "installId": install_id,
            "skills": len(skills.REQUIRED_SKILLS),
            "mcp": list(OWNED_MCP_NAMES),
            "state": str(state_path),
        }
    except Exception as exc:
        rollback_errors = _rollback_install(
            paths,
            mcp_record,
            stage_argv=stage_argv,
            stage_cwd=stage_cwd,
            stage_timeout=stage_timeout,
            rerun_staging=staging_attempted,
        )
        if state_path.exists():
            state_path.unlink()
        detail = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise ReviewError(f"integration install failed: {exc}{detail}") from exc


def verify_integration(paths: Any) -> dict[str, Any]:
    state_path = install_state_path(paths)
    if not state_path.is_file():
        return {"ok": False, "installed": False, "state": str(state_path)}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        detail = _verify_active_state(paths, state)
        return {"ok": True, "installed": True, "detail": detail, "state": str(state_path)}
    except (OSError, ValueError, ValidationError) as exc:
        return {
            "ok": False,
            "installed": True,
            "reason": str(exc),
            "state": str(state_path),
        }


def uninstall_integration(paths: Any, *, stage_timeout: float = 900) -> dict[str, Any]:
    """Remove an unedited installation and restore exact previous MCP values and skills."""
    state_path = install_state_path(paths)
    if not state_path.exists():
        return {"uninstalled": True, "reused": True}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    _verify_active_state(paths, state)
    mcp = state["mcp"]
    current_raw, restored_config, restore_exact_original = _config_with_previous_owned(mcp)
    backup_path = Path(mcp["backupPath"])
    if not backup_path.is_file() or sha256_file(backup_path) != mcp["backupSha256"]:
        raise ValidationError("cannot uninstall without the verified MCP backup")

    stage_argv = list(state["staging"]["argv"])
    stage_cwd = Path(state["staging"]["cwd"])
    siblings_before = snapshot_siblings(paths)
    config_path = Path(mcp["configPath"])
    config_changed = False
    try:
        if restore_exact_original:
            if mcp["configExisted"]:
                atomic_write(
                    config_path,
                    backup_path.read_bytes(),
                    mode=int(mcp.get("configMode", 0o600)),
                )
            elif config_path.exists():
                config_path.unlink()
        else:
            atomic_json(
                config_path,
                restored_config,
                mode=int(mcp.get("configMode", 0o600)),
            )
        config_changed = True
        _run_staging(stage_argv, stage_cwd, stage_timeout)
        _assert_siblings_unchanged(siblings_before, snapshot_siblings(paths))
        skill_result = skills.uninstall_skill_set(paths)
        state_path.unlink()
        return {
            "uninstalled": True,
            "reused": False,
            "skills": skill_result.get("removed", 0),
            "mcp": list(OWNED_MCP_NAMES),
        }
    except Exception as exc:
        rollback_errors: list[str] = []
        if config_changed:
            try:
                atomic_write(
                    config_path,
                    current_raw,
                    mode=int(mcp.get("configMode", 0o600)),
                )
                _run_staging(stage_argv, stage_cwd, stage_timeout)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(f"MCP rollback: {rollback_exc}")
        detail = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise ReviewError(f"integration uninstall failed: {exc}{detail}") from exc


def cmd_install(args: Any) -> dict[str, Any]:
    paths = args.paths
    roots = skills.resolve_skill_roots(args)
    supplied = _arg(args, "mcp_entries")
    generated_value = _arg(args, "mcp_entries_file", "generated_mcp")
    if supplied is not None and not isinstance(supplied, Mapping):
        supplied_text = os.fspath(supplied)
        if supplied_text.lstrip().startswith("{"):
            supplied = json.loads(supplied_text)
        else:
            generated_value = generated_value or supplied_text
            supplied = None
    generated = Path(generated_value).expanduser().resolve() if generated_value else None
    entries = load_owned_mcp_entries(paths, supplied, generated_file=generated)
    config_path = resolve_mcp_config(args)
    staged_config = resolve_staged_mcp_config(args)
    argv, cwd, timeout = resolve_staging(paths, args)
    return install_integration(
        paths,
        skill_roots=roots,
        mcp_entries=entries,
        mcp_config=config_path,
        stage_argv=argv,
        stage_cwd=cwd,
        stage_timeout=timeout,
        staged_mcp_config=staged_config,
    )


def cmd_status(args: Any) -> dict[str, Any]:
    result = verify_integration(args.paths)
    result["skillManifest"] = str(skills.manifest_path(args.paths))
    return result


def cmd_verify(args: Any) -> dict[str, Any]:
    return verify_integration(args.paths)


def cmd_uninstall(args: Any) -> dict[str, Any]:
    timeout_value = _arg(args, "stage_timeout", "staging_timeout")
    if timeout_value is None:
        timeout_value = os.environ.get("OCSL_REVIEW_STAGE_TIMEOUT", "900")
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise ReviewError("deployment staging timeout must be numeric") from exc
    if timeout <= 0:
        raise ReviewError("deployment staging timeout must be positive")
    return uninstall_integration(args.paths, stage_timeout=timeout)
