"""Immutable Conda templates and isolated, chat-scoped review environments.

There are two deliberately separate dependency phases:

* An operator build may solve the source-controlled input lock once.  The
  resulting prefix is probed and Conda exports its exact ``@EXPLICIT`` package
  set.  The normalized export is content-addressed in deployment state.
* Review-time operations either clone a promoted template with copy semantics
  or install its exact export.  They never feed package names or channels to a
  solver.  Optional rebuild overlays must themselves be hash-pinned artifacts.

Promoted templates are never edited or removed by this module.  Failed staging
prefixes and failed chat prefixes are cleaned only inside their dedicated
transient roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from . import config
from .config import Paths
from .errors import ReviewError, TrustBoundaryError, ValidationError
from .util import (
    CommandResult,
    atomic_json,
    atomic_write,
    is_within,
    load_toml,
    require_relative_path,
    run_command,
    sha256_bytes,
    sha256_file,
)

_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONDA_FRAGMENT = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{64}|md5=[0-9a-f]{32}|sha256=[0-9a-f]{64})$"
)
_PIP_HASH = re.compile(r"(?:^|\s)--hash=sha256:[0-9a-f]{64}(?:\s|$)")
_TEMPLATE_RECEIPT = ".ocsl-review-template.json"
_TEMPLATE_BUILDING = ".ocsl-review-building.json"
_DESCRIPTOR_NAME = "environment.json"


@dataclass(frozen=True)
class EnvironmentDefinition:
    id: str
    purpose: str
    spec: Path
    input_lock: Path
    input_lock_sha256: str
    platforms: tuple[str, ...]
    channels: tuple[str, ...]
    health_checks: tuple[tuple[str, ...], ...]
    skills: tuple[str, ...]


@dataclass(frozen=True)
class TemplateRecord:
    definition: EnvironmentDefinition
    platform: str
    build_id: str
    prefix: Path
    exact_lock: Path
    exact_lock_sha256: str
    created_at: str


@dataclass(frozen=True)
class InstanceRecord:
    prefix: Path
    descriptor_path: Path
    descriptor: Mapping[str, Any]
    reused: bool


def _template_build_marker(prefix: Path) -> Path:
    return prefix.parent / f"{_TEMPLATE_BUILDING}.{prefix.name}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def current_conda_platform() -> str:
    machine = platform_module.machine().lower()
    if sys.platform.startswith("linux") and machine in {"x86_64", "amd64"}:
        return "linux-64"
    if sys.platform.startswith("linux") and machine in {"arm64", "aarch64"}:
        return "linux-aarch64"
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return "osx-arm64"
    if sys.platform == "darwin" and machine in {"x86_64", "amd64"}:
        return "osx-64"
    if sys.platform.startswith("win") and machine in {"x86_64", "amd64"}:
        return "win-64"
    raise ReviewError(f"unsupported Conda platform: {sys.platform}/{machine}")


def _schema(paths: Paths, name: str) -> Mapping[str, Any]:
    schema_path = paths.home / "schemas" / f"{name}.schema.json"
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot load {name} schema: {schema_path}: {error}") from error


def _validate_json(value: Any, schema: Mapping[str, Any], label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ValidationError(f"{label} is invalid at {location}: {error.message}")


def load_environment_catalog(paths: Paths) -> dict[str, EnvironmentDefinition]:
    try:
        raw = load_toml(paths.environment_catalog)
    except (OSError, ValueError) as error:
        raise ValidationError(
            f"cannot load environment catalog {paths.environment_catalog}: {error}"
        ) from error
    _validate_json(raw, _schema(paths, "environment-catalog"), "environment catalog")

    environment_root = paths.environment_catalog.parent.resolve()
    result: dict[str, EnvironmentDefinition] = {}
    mapped_skills: set[str] = set()
    for item in raw["environment"]:
        environment_id = item["id"]
        if environment_id in result:
            raise ValidationError(f"duplicate environment id: {environment_id}")
        spec = (environment_root / require_relative_path(item["spec"])).resolve()
        input_lock = (environment_root / require_relative_path(item["input_lock"])).resolve()
        for label, candidate in (("spec", spec), ("input lock", input_lock)):
            if not is_within(candidate, environment_root) or not candidate.is_file():
                raise ValidationError(
                    f"environment {environment_id} has a missing or escaping {label}: {candidate}"
                )
        try:
            spec_data = yaml.safe_load(spec.read_text(encoding="utf-8"))
            input_data = yaml.safe_load(input_lock.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValidationError(
                f"environment {environment_id} has invalid YAML build inputs: {error}"
            ) from error
        for label, data in (("spec", spec_data), ("input lock", input_data)):
            if not isinstance(data, dict) or set(data) != {"channels", "dependencies"}:
                raise ValidationError(
                    f"environment {environment_id} {label} must contain only "
                    "channels and dependencies"
                )
            channels = data.get("channels")
            dependencies = data.get("dependencies")
            if not isinstance(channels, list) or channels[-1:] != ["nodefaults"]:
                raise ValidationError(
                    f"environment {environment_id} {label} must end channels with nodefaults"
                )
            if channels[:-1] != item["channels"]:
                raise ValidationError(
                    f"environment {environment_id} {label} channels differ from the catalog"
                )
            if (
                not isinstance(dependencies, list)
                or not dependencies
                or not all(isinstance(dependency, str) for dependency in dependencies)
            ):
                raise ValidationError(
                    f"environment {environment_id} {label} dependencies must be string entries"
                )
        for dependency in input_data["dependencies"]:
            if "=" not in dependency or any(token in dependency for token in ("<", ">", "*")):
                raise ValidationError(
                    f"environment {environment_id} input-lock dependency is not exact: {dependency}"
                )
        actual_lock_sha = sha256_file(input_lock)
        if actual_lock_sha != item["input_lock_sha256"]:
            raise ValidationError(
                f"environment {environment_id} input-lock hash mismatch: "
                f"catalog={item['input_lock_sha256']} actual={actual_lock_sha}"
            )
        duplicates = mapped_skills.intersection(item["skills"])
        if duplicates:
            raise ValidationError(
                f"skills map to more than one environment: {', '.join(sorted(duplicates))}"
            )
        mapped_skills.update(item["skills"])
        result[environment_id] = EnvironmentDefinition(
            id=environment_id,
            purpose=item["purpose"],
            spec=spec,
            input_lock=input_lock,
            input_lock_sha256=item["input_lock_sha256"],
            platforms=tuple(item["platforms"]),
            channels=tuple(item["channels"]),
            health_checks=tuple(tuple(command) for command in item["health_checks"]),
            skills=tuple(item["skills"]),
        )
    return result


def environment_for_skill(paths: Paths, skill_id: str) -> EnvironmentDefinition:
    matches = [item for item in load_environment_catalog(paths).values() if skill_id in item.skills]
    if not matches:
        raise ReviewError(f"no review environment is mapped to skill: {skill_id}")
    if len(matches) != 1:
        raise ValidationError(f"skill has ambiguous environment mapping: {skill_id}")
    return matches[0]


def _definition(paths: Paths, environment_id: str) -> EnvironmentDefinition:
    try:
        return load_environment_catalog(paths)[environment_id]
    except KeyError as error:
        raise ReviewError(f"unknown review environment: {environment_id}") from error


def _conda_executable(explicit: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(explicit).resolve() if explicit is not None else config.conda_executable()
    if candidate is None or not candidate.is_file():
        raise ReviewError("no Conda-compatible executable was found under LAB_CONDA_INSTALL_ROOT")
    if not os.access(candidate, os.X_OK):
        raise ReviewError(f"Conda-compatible executable is not executable: {candidate}")
    return candidate


def _copy_option(executable: Path) -> str:
    if executable.name.lower().startswith("micromamba"):
        return "--always-copy"
    return "--copy"


def _platform_option(executable: Path) -> str:
    # Conda exposed --subdir before adding --platform as an alias. Mamba-family
    # frontends use --platform consistently.
    if executable.name.lower() == "conda":
        return "--subdir"
    return "--platform"


def _no_default_packages_options(executable: Path) -> list[str]:
    # micromamba has no Conda create_default_packages injection. Its CLI does
    # not consistently expose Conda's suppression flag across releases.
    if executable.name.lower().startswith("micromamba"):
        return []
    return ["--no-default-packages"]


def _operator_create_argv(
    executable: Path,
    prefix: Path,
    input_lock: Path,
    platform: str,
) -> list[str]:
    return [
        str(executable),
        "create",
        "--yes",
        _copy_option(executable),
        _platform_option(executable),
        platform,
        *_no_default_packages_options(executable),
        "--prefix",
        str(prefix),
        "--file",
        str(input_lock),
    ]


def _run_checked(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
) -> CommandResult:
    try:
        return run_command(argv, cwd=cwd, env=env, timeout=timeout, check=True)
    except subprocess.TimeoutExpired as error:
        raise ReviewError(f"command exceeded its {timeout:g}-second bound") from error
    except OSError as error:
        raise ReviewError(f"cannot execute {os.fspath(argv[0])}: {error}") from error


def _run_in_prefix_argv(executable: Path, prefix: Path, command: Sequence[str]) -> list[str]:
    argv = [str(executable), "run"]
    # Conda and Mamba expose this option; micromamba 2.x does not and treats
    # the unknown token as part of the launched command.
    if not executable.name.lower().startswith("micromamba"):
        argv.append("--no-capture-output")
    return [*argv, "--prefix", str(prefix), *command]


def _micromamba_process_env(executable: Path, cache_root: Path) -> Mapping[str, str] | None:
    if not executable.name.lower().startswith("micromamba"):
        return None
    if cache_root.is_symlink():
        raise ValidationError(f"micromamba process cache cannot be a symlink: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not cache_root.is_dir():
        raise ValidationError(f"micromamba process cache is not a directory: {cache_root}")
    environment = dict(os.environ)
    environment["XDG_CACHE_HOME"] = str(cache_root.resolve())
    return environment


def _run_health_checks(
    executable: Path,
    prefix: Path,
    definition: EnvironmentDefinition,
    *,
    cache_root: Path,
    timeout: float,
    read_only: bool = False,
) -> None:
    environment = _micromamba_process_env(executable, cache_root)
    if read_only:
        environment = dict(os.environ) if environment is None else dict(environment)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["XDG_CACHE_HOME"] = str(cache_root.resolve())
        environment["MPLCONFIGDIR"] = str((cache_root / "matplotlib").resolve())
    for command in definition.health_checks:
        _run_checked(
            _run_in_prefix_argv(executable, prefix, command),
            env=environment,
            timeout=timeout,
        )


def _assert_conda_prefix(prefix: Path) -> None:
    if not prefix.is_dir() or prefix.is_symlink():
        raise ValidationError(f"Conda prefix is missing or not a real directory: {prefix}")
    _assert_links_contained(prefix)
    _assert_no_external_hardlinks(prefix)
    metadata = prefix / "conda-meta"
    if metadata.is_symlink() or not metadata.is_dir() or not any(metadata.glob("*.json")):
        raise ValidationError(f"Conda prefix has no installed-package metadata: {prefix}")
    python_candidates = (prefix / "bin" / "python", prefix / "python.exe")
    if not any(candidate.is_file() for candidate in python_candidates):
        raise ValidationError(f"Conda prefix has no Python executable: {prefix}")


def _assert_links_contained(root: Path) -> None:
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(metadata.st_mode):
            continue
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValidationError(f"environment contains a broken symlink: {path}") from error
        if not is_within(target, resolved_root):
            raise ValidationError(
                f"environment symlink escapes its immutable prefix: {path} -> {target}"
            )


def _normalize_explicit_lock(raw: str, *, platform: str | None = None) -> str:
    lines = [line.strip() for line in raw.replace("\r\n", "\n").split("\n")]
    try:
        marker = lines.index("@EXPLICIT")
    except ValueError as error:
        raise ValidationError("Conda did not emit an @EXPLICIT package lock") from error
    artifacts: list[str] = []
    for line in lines[marker + 1 :]:
        if not line or line.startswith("#"):
            continue
        parsed = urlsplit(line)
        if parsed.scheme not in {"https", "http", "file"}:
            raise ValidationError(f"explicit lock contains a non-artifact entry: {line}")
        if parsed.username or parsed.password or parsed.query:
            raise ValidationError(
                "explicit lock artifact URLs cannot contain credentials or queries"
            )
        if not _CONDA_FRAGMENT.fullmatch(parsed.fragment.lower()):
            raise ValidationError(f"explicit lock artifact is not checksum-pinned: {line}")
        if platform is not None and parsed.scheme in {"https", "http"}:
            segments = set(parsed.path.split("/"))
            if platform not in segments and "noarch" not in segments:
                raise ValidationError(
                    f"explicit lock artifact does not match platform {platform}: {line}"
                )
        artifacts.append(line)
    if not artifacts:
        raise ValidationError("Conda emitted an empty @EXPLICIT package lock")
    if len(artifacts) != len(set(artifacts)):
        raise ValidationError("Conda emitted duplicate artifacts in its @EXPLICIT package lock")
    return "@EXPLICIT\n" + "\n".join(sorted(artifacts)) + "\n"


def _explicit_artifacts(
    raw: str,
    *,
    platform: str | None = None,
) -> dict[str, tuple[str, str]]:
    normalized = _normalize_explicit_lock(raw, platform=platform)
    result: dict[str, tuple[str, str]] = {}
    for line in normalized.splitlines()[1:]:
        parsed = urlsplit(line)
        filename = unquote(Path(parsed.path).name)
        if not filename:
            raise ValidationError(f"explicit lock artifact has no filename: {line}")
        fragment = parsed.fragment.lower()
        if fragment.startswith("md5="):
            algorithm, digest = "md5", fragment.removeprefix("md5=")
        elif fragment.startswith("sha256="):
            algorithm, digest = "sha256", fragment.removeprefix("sha256=")
        elif len(fragment) == 32:
            algorithm, digest = "md5", fragment
        else:
            algorithm, digest = "sha256", fragment
        if filename in result:
            raise ValidationError(f"explicit lock contains more than one artifact named {filename}")
        result[filename] = (algorithm, digest)
    return result


def _installed_conda_artifacts(prefix: Path) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record_path in sorted((prefix / "conda-meta").glob("*.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError(
                f"cannot read installed-package record {record_path}: {error}"
            ) from error
        url = record.get("url")
        filename = record.get("fn")
        if isinstance(url, str) and url:
            filename = unquote(Path(urlsplit(url).path).name)
        if not isinstance(filename, str) or not filename:
            raise ValidationError(
                f"installed-package record has no artifact filename: {record_path}"
            )
        if filename in result:
            raise ValidationError(f"installed prefix repeats package artifact {filename}")
        result[filename] = record
    return result


def _verify_prefix_matches_explicit_lock(
    prefix: Path,
    raw_lock: str,
    *,
    platform: str | None = None,
) -> None:
    _verify_locked_artifacts_present(
        prefix,
        raw_lock,
        platform=platform,
        allow_unexpected=False,
    )


def _verify_locked_artifacts_present(
    prefix: Path,
    raw_lock: str,
    *,
    platform: str | None,
    allow_unexpected: bool,
) -> None:
    expected = _explicit_artifacts(raw_lock, platform=platform)
    installed = _installed_conda_artifacts(prefix)
    missing = sorted(set(expected) - set(installed))
    unexpected = sorted(set(installed) - set(expected))
    if missing or (unexpected and not allow_unexpected):
        raise ValidationError(
            "installed prefix differs from its exact lock; "
            f"missing={missing[:10]} unexpected={unexpected[:10]}"
        )
    for filename, (algorithm, digest) in expected.items():
        installed_digest = installed[filename].get(algorithm)
        if not isinstance(installed_digest, str) or installed_digest.lower() != digest:
            raise ValidationError(
                f"installed artifact checksum differs from exact lock: {filename}"
            )


def _export_exact_lock(
    executable: Path,
    prefix: Path,
    *,
    platform: str,
    timeout: float,
) -> str:
    if executable.name.lower().startswith("micromamba"):
        artifacts: list[str] = []
        for record_path in sorted((prefix / "conda-meta").glob("*.json")):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValidationError(
                    f"cannot read installed-package record {record_path}: {error}"
                ) from error
            url = record.get("url")
            checksum = record.get("md5")
            if not isinstance(url, str) or not url:
                raise ValidationError(
                    f"installed-package record has no artifact URL: {record_path}"
                )
            if isinstance(checksum, str) and re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
                fragment = checksum.lower()
            else:
                checksum = record.get("sha256")
                if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
                    raise ValidationError(
                        f"installed-package record has no usable checksum: {record_path}"
                    )
                fragment = f"sha256={checksum.lower()}"
            artifacts.append(f"{url.split('#', 1)[0]}#{fragment}")
        return _normalize_explicit_lock(
            "@EXPLICIT\n" + "\n".join(artifacts) + "\n",
            platform=platform,
        )
    result = _run_checked(
        [str(executable), "list", "--explicit", "--md5", "--prefix", str(prefix)],
        timeout=timeout,
    )
    return _normalize_explicit_lock(result.stdout, platform=platform)


def _clear_write_bits(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    paths.append(root)
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            continue
        os.chmod(path, stat.S_IMODE(mode) & ~0o222)


def _assert_read_only_tree(root: Path) -> None:
    _assert_links_contained(root)
    writable: list[str] = []
    for path in [root, *root.rglob("*")]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if stat.S_IMODE(mode) & 0o222:
            writable.append(path.relative_to(root).as_posix() or ".")
    if writable:
        raise ValidationError(
            f"promoted template contains writable paths: {', '.join(writable[:10])}"
        )


def _make_tree_removable(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            continue
        addition = 0o700 if stat.S_ISDIR(mode) else 0o600
        os.chmod(path, stat.S_IMODE(mode) | addition)


def _cleanup_transient(path: Path, allowed_root: Path) -> None:
    if not path.exists():
        return
    if path == allowed_root or not is_within(path, allowed_root):
        raise ReviewError(f"refusing to clean a path outside the transient root: {path}")
    _make_tree_removable(path)
    shutil.rmtree(path)


def _exact_lock_root(paths: Paths) -> Path:
    return _validated_state_root(paths) / "environment-locks"


def _validated_state_root(paths: Paths) -> Path:
    state = paths.need_state().resolve()
    if state == Path(state.anchor):
        raise ReviewError("OCSL_REVIEW_STATE cannot be the filesystem root")
    for label, source in config.protected_source_roots(paths):
        if is_within(state, source) or is_within(source, state):
            raise ReviewError(f"OCSL_REVIEW_STATE must not overlap {label}: {source}")
    return state


def _template_parent(paths: Paths, platform: str, environment_id: str) -> Path:
    return _validated_state_root(paths) / "environment-templates" / platform / environment_id


def _read_current_template(
    paths: Paths,
    definition: EnvironmentDefinition,
    platform: str,
) -> TemplateRecord | None:
    parent = _template_parent(paths, platform, definition.id)
    current = parent / "current"
    try:
        metadata = current.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(metadata.st_mode):
        raise ValidationError(f"template current pointer is not a symlink: {current}")
    target_text = os.readlink(current)
    target = Path(target_text)
    if target.is_absolute() or len(target.parts) != 1 or not _HASH_PATTERN.fullmatch(target.name):
        raise ValidationError(f"template current pointer has an unsafe target: {current}")
    prefix = parent / target
    if prefix.is_symlink() or not prefix.is_dir():
        raise ValidationError(f"template current pointer is broken: {current}")
    return _read_template(paths, definition, prefix)


def _publish_current_template(
    paths: Paths,
    record: TemplateRecord,
) -> None:
    """Atomically select a validated immutable build for launcher resolution."""

    parent = _template_parent(paths, record.platform, record.definition.id)
    current = parent / "current"
    existing = _read_current_template(paths, record.definition, record.platform)
    if existing is not None and existing.build_id == record.build_id:
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        os.symlink(record.build_id, temporary, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)
    selected = _read_current_template(paths, record.definition, record.platform)
    if selected is None or selected.build_id != record.build_id:
        raise ValidationError(
            f"template current pointer did not select promoted build {record.build_id}"
        )


def _receipt_value(
    definition: EnvironmentDefinition,
    platform: str,
    build_id: str,
    exact_lock_sha256: str,
    exact_lock: Path,
    created_at: str,
    paths: Paths,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "templateId": definition.id,
        "platform": platform,
        "buildId": build_id,
        "inputLockSha256": definition.input_lock_sha256,
        "exactLockSha256": exact_lock_sha256,
        "exactLock": exact_lock.relative_to(_validated_state_root(paths)).as_posix(),
        "healthChecks": [list(command) for command in definition.health_checks],
        "createdAt": created_at,
    }


def _read_template(
    paths: Paths,
    definition: EnvironmentDefinition,
    prefix: Path,
    *,
    require_read_only: bool = True,
) -> TemplateRecord:
    if _template_build_marker(prefix).exists():
        raise ValidationError(f"template build has not been promoted: {prefix}")
    _assert_conda_prefix(prefix)
    receipt_path = prefix / _TEMPLATE_RECEIPT
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"invalid template receipt at {receipt_path}: {error}") from error
    required = {
        "schemaVersion",
        "templateId",
        "platform",
        "buildId",
        "inputLockSha256",
        "exactLockSha256",
        "exactLock",
        "healthChecks",
        "createdAt",
    }
    if set(receipt) != required or receipt.get("schemaVersion") != 1:
        raise ValidationError(f"template receipt has an unexpected shape: {receipt_path}")
    build_id = receipt.get("buildId")
    if receipt.get("templateId") != definition.id or not isinstance(build_id, str):
        raise ValidationError(f"template receipt identity mismatch: {receipt_path}")
    receipt_platform = receipt.get("platform")
    if (
        not isinstance(receipt_platform, str)
        or receipt_platform not in definition.platforms
        or prefix.parent.parent.name != receipt_platform
    ):
        raise ValidationError(f"template platform identity mismatch: {receipt_path}")
    if not _HASH_PATTERN.fullmatch(build_id) or prefix.name != build_id:
        raise ValidationError(f"template build id is not content-addressed: {prefix}")
    exact_lock_sha256 = receipt.get("exactLockSha256")
    if not isinstance(exact_lock_sha256, str) or not _HASH_PATTERN.fullmatch(exact_lock_sha256):
        raise ValidationError(f"template exact-lock identity is invalid: {prefix}")
    if receipt.get("inputLockSha256") != definition.input_lock_sha256:
        raise ValidationError(f"template was built from a superseded input lock: {prefix}")
    if receipt.get("healthChecks") != [list(command) for command in definition.health_checks]:
        raise ValidationError(f"template health-check contract changed after build: {prefix}")
    relative_lock = require_relative_path(str(receipt.get("exactLock", "")))
    exact_lock = (_validated_state_root(paths) / relative_lock).resolve()
    lock_root = _exact_lock_root(paths).resolve()
    if not is_within(exact_lock, lock_root) or exact_lock.is_symlink() or not exact_lock.is_file():
        raise ValidationError(f"template exact lock is missing or outside state: {exact_lock}")
    if stat.S_IMODE(exact_lock.stat().st_mode) & 0o222:
        raise ValidationError(f"template exact lock is writable: {exact_lock}")
    if sha256_file(exact_lock) != exact_lock_sha256:
        raise ValidationError(f"template exact lock failed its SHA-256 check: {exact_lock}")
    _normalize_explicit_lock(
        exact_lock.read_text(encoding="utf-8"),
        platform=receipt_platform,
    )
    expected_build_id = sha256_bytes(
        (f"{definition.input_lock_sha256}\0{receipt_platform}\0{exact_lock_sha256}").encode("ascii")
    )
    if build_id != expected_build_id:
        raise ValidationError(f"template build identity failed recomputation: {prefix}")
    if require_read_only:
        _assert_read_only_tree(prefix)
    return TemplateRecord(
        definition=definition,
        platform=str(receipt["platform"]),
        build_id=build_id,
        prefix=prefix,
        exact_lock=exact_lock,
        exact_lock_sha256=exact_lock_sha256,
        created_at=str(receipt["createdAt"]),
    )


def build_template(
    paths: Paths,
    environment_id: str,
    *,
    executable: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    solve_timeout: float = 3600,
    probe_timeout: float = 120,
) -> dict[str, Any]:
    """Build and atomically promote one immutable template.

    This is the sole solving operation in the environment layer and belongs to
    the operator lifecycle.  Existing promoted directories are only verified;
    they are never chmodded, replaced, or removed.
    """

    definition = _definition(paths, environment_id)
    target_platform = platform or current_conda_platform()
    if target_platform not in definition.platforms:
        raise ReviewError(
            f"environment {environment_id} does not support platform {target_platform}"
        )
    conda = _conda_executable(executable)
    parent = _template_parent(paths, target_platform, environment_id)
    parent.mkdir(parents=True, exist_ok=True)
    # The seed prefix exists only to solve and export an exact artifact set.
    # A second, no-solve materialization uses the final content-addressed path.
    staging_root = parent
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=staging_root))
    exact_lock_created: Path | None = None
    temporary_input: Path | None = None
    destination: Path | None = None
    building_marker: Path | None = None
    destination_created = False
    promoted = False
    try:
        operator_input = definition.input_lock
        # Micromamba interprets any filename ending in ``lock.yaml`` as the
        # separate conda-lock document format.  Our tracked file is a pinned
        # environment YAML, so provide identical verified bytes under a
        # neutral ephemeral name for that frontend.
        if conda.name.lower().startswith("micromamba"):
            descriptor, input_name = tempfile.mkstemp(
                prefix=".operator-input-", suffix=".environment.yml", dir=parent
            )
            temporary_input = Path(input_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(definition.input_lock.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_input, 0o400)
            operator_input = temporary_input
        _run_checked(
            _operator_create_argv(
                conda,
                staging,
                operator_input,
                target_platform,
            ),
            timeout=solve_timeout,
        )
        _assert_conda_prefix(staging)
        exact_text = _export_exact_lock(
            conda,
            staging,
            platform=target_platform,
            timeout=probe_timeout,
        )
        _verify_prefix_matches_explicit_lock(
            staging,
            exact_text,
            platform=target_platform,
        )
        exact_lock_sha256 = sha256_bytes(exact_text.encode("utf-8"))
        build_id = sha256_bytes(
            (f"{definition.input_lock_sha256}\0{target_platform}\0{exact_lock_sha256}").encode(
                "ascii"
            )
        )
        exact_lock = (
            _exact_lock_root(paths)
            / target_platform
            / environment_id
            / f"{exact_lock_sha256}.explicit.txt"
        )
        exact_lock.parent.mkdir(parents=True, exist_ok=True)
        if exact_lock.exists():
            if exact_lock.read_text(encoding="utf-8") != exact_text:
                raise ValidationError(f"content-address collision at {exact_lock}")
        else:
            atomic_write(exact_lock, exact_text, mode=0o444)
            exact_lock_created = exact_lock

        destination = parent / build_id
        if destination.exists():
            existing = _read_template(paths, definition, destination)
            _publish_current_template(paths, existing)
            return {
                "id": environment_id,
                "platform": target_platform,
                "buildId": existing.build_id,
                "template": str(existing.prefix),
                "exactLock": str(existing.exact_lock),
                "reused": True,
            }
        # Prefixes may contain absolute links and embedded prefix strings, so
        # renaming a solved environment can corrupt it.  Materialize the exact
        # export at its final content-addressed path and publish it only by the
        # atomic `current` pointer after every check succeeds.  The marker keeps
        # concurrent readers from treating the in-progress directory as ready.
        building_marker = _template_build_marker(destination)
        marker_payload = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "templateId": environment_id,
                    "platform": target_platform,
                    "buildId": build_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        try:
            marker_descriptor = os.open(
                building_marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as error:
            raise ReviewError(f"template build is already in progress: {destination}") from error
        with os.fdopen(marker_descriptor, "w", encoding="utf-8") as handle:
            handle.write(marker_payload)
            handle.flush()
            os.fsync(handle.fileno())
        destination_created = True
        _install_exact_prefix(
            conda,
            exact_lock,
            destination,
            platform=target_platform,
            timeout=solve_timeout,
        )
        _run_health_checks(
            conda,
            destination,
            definition,
            cache_root=paths.cache / "micromamba-operator",
            timeout=probe_timeout,
        )
        created_at = _utc_now()
        atomic_json(
            destination / _TEMPLATE_RECEIPT,
            _receipt_value(
                definition,
                target_platform,
                build_id,
                exact_lock_sha256,
                exact_lock,
                created_at,
                paths,
            ),
        )
        _clear_write_bits(destination)
        _assert_read_only_tree(destination)
        building_marker.unlink()
        record = _read_template(paths, definition, destination)
        _publish_current_template(paths, record)
        promoted = True
        return {
            "id": environment_id,
            "platform": target_platform,
            "buildId": record.build_id,
            "template": str(record.prefix),
            "exactLock": str(record.exact_lock),
            "reused": False,
        }
    except Exception:
        if (
            destination_created
            and not promoted
            and destination is not None
            and destination.exists()
        ):
            _cleanup_transient(destination, parent)
        if exact_lock_created is not None and not promoted and exact_lock_created.exists():
            lock_parent = exact_lock_created.parent
            exact_lock_created.unlink()
            try:
                lock_parent.rmdir()
            except OSError:
                pass
        raise
    finally:
        if staging.exists():
            _cleanup_transient(staging, staging_root)
        if temporary_input is not None:
            temporary_input.unlink(missing_ok=True)
        if building_marker is not None:
            building_marker.unlink(missing_ok=True)


def build_all_templates(
    paths: Paths,
    *,
    executable: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    solve_timeout: float = 3600,
    probe_timeout: float = 120,
) -> list[dict[str, Any]]:
    return [
        build_template(
            paths,
            environment_id,
            executable=executable,
            platform=platform,
            solve_timeout=solve_timeout,
            probe_timeout=probe_timeout,
        )
        for environment_id in load_environment_catalog(paths)
    ]


def available_templates(
    paths: Paths,
    environment_id: str,
    *,
    platform: str | None = None,
) -> list[TemplateRecord]:
    definition = _definition(paths, environment_id)
    target_platform = platform or current_conda_platform()
    parent = _template_parent(paths, target_platform, environment_id)
    if not parent.is_dir():
        return []
    records: list[TemplateRecord] = []
    for candidate in sorted(parent.iterdir()):
        if (
            candidate.is_dir()
            and _HASH_PATTERN.fullmatch(candidate.name)
            and not _template_build_marker(candidate).exists()
        ):
            records.append(_read_template(paths, definition, candidate))
    return sorted(records, key=lambda item: (item.created_at, item.build_id), reverse=True)


def resolve_template(
    paths: Paths,
    environment_id: str,
    *,
    build_id: str | None = None,
    platform: str | None = None,
) -> TemplateRecord:
    definition = _definition(paths, environment_id)
    target_platform = platform or current_conda_platform()
    selected = _read_current_template(paths, definition, target_platform)
    if selected is None:
        raise ReviewError(
            f"no current promoted template is available for {environment_id}; "
            "an operator must build it first"
        )
    if build_id is None:
        return selected
    if not _HASH_PATTERN.fullmatch(build_id):
        raise ReviewError(f"invalid template build identifier: {build_id}")
    candidate = _template_parent(paths, target_platform, environment_id) / build_id
    if not candidate.is_dir() or candidate.is_symlink():
        detail = f" build {build_id}" if build_id else ""
        raise ReviewError(
            f"no promoted template is available for {environment_id}{detail}; "
            "an operator must build it first"
        )
    return _read_template(paths, definition, candidate)


def list_environments(paths: Paths, *, platform: str | None = None) -> list[dict[str, Any]]:
    target_platform = platform or current_conda_platform()
    result: list[dict[str, Any]] = []
    for definition in load_environment_catalog(paths).values():
        templates = available_templates(paths, definition.id, platform=target_platform)
        current = _read_current_template(paths, definition, target_platform)
        result.append(
            {
                "id": definition.id,
                "purpose": definition.purpose,
                "platform": target_platform,
                "inputLockSha256": definition.input_lock_sha256,
                "templateBuilds": [record.build_id for record in templates],
                "currentBuildId": current.build_id if current is not None else None,
                "currentExactLockSha256": (
                    current.exact_lock_sha256 if current is not None else None
                ),
                "ready": current is not None,
                "skills": list(definition.skills),
            }
        )
    return result


def _regular_file_inodes(root: Path) -> dict[tuple[int, int], list[Path]]:
    """Return every regular-file inode below ``root`` without following links."""

    result: dict[tuple[int, int], list[Path]] = {}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        # os.walk will otherwise descend through a directory symlink on some
        # platforms when it appears in directory_names.
        directory_names[:] = [
            name for name in directory_names if not (directory_path / name).is_symlink()
        ]
        for name in file_names:
            path = directory_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            result.setdefault((metadata.st_dev, metadata.st_ino), []).append(path.relative_to(root))
    return result


def _assert_no_external_hardlinks(root: Path) -> None:
    for relative_paths in _regular_file_inodes(root).values():
        representative = root / relative_paths[0]
        link_count = representative.lstat().st_nlink
        if link_count > len(relative_paths):
            raise ValidationError(
                f"environment regular file has hard links outside its prefix: {representative}"
            )


def audit_inode_isolation(source: Path, destination: Path) -> dict[str, int]:
    """Exhaustively reject shared regular-file inodes between two prefixes."""

    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValidationError("inode audit source and destination are the same prefix")
    if not source.is_dir() or not destination.is_dir():
        raise ValidationError("inode audit requires two existing directories")
    source_inodes = _regular_file_inodes(source)
    destination_inodes = _regular_file_inodes(destination)
    shared = sorted(set(source_inodes).intersection(destination_inodes))
    if shared:
        examples: list[str] = []
        for inode in shared[:10]:
            for left in source_inodes[inode][:1]:
                for right in destination_inodes[inode][:1]:
                    examples.append(f"{left.as_posix()} <-> {right.as_posix()}")
        raise ValidationError(
            "environment isolation failed; regular files share inodes: " + ", ".join(examples)
        )
    return {
        "sourceRegularFiles": sum(len(items) for items in source_inodes.values()),
        "destinationRegularFiles": sum(len(items) for items in destination_inodes.values()),
        "sharedRegularFiles": 0,
    }


def _make_chat_prefix_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        addition = 0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600
        os.chmod(path, stat.S_IMODE(metadata.st_mode) | addition)


def _lexical_absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    lexical = _lexical_absolute(path)
    for candidate in reversed((lexical, *lexical.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(f"{label} contains a symlink component: {candidate}")


def _ensure_contained_directory(path: Path, root: Path, *, label: str) -> Path:
    _assert_no_symlink_components(root, label=label)
    _assert_no_symlink_components(path, label=label)
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_root or not is_within(resolved_path, resolved_root):
        raise ValidationError(f"{label} escapes its declared root: {path}")
    path.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(path, label=label)
    if not path.is_dir():
        raise ValidationError(f"{label} is not a directory: {path}")
    return path.resolve()


def _slug(value: str, *, fallback: str, limit: int = 48) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    normalized = normalized[:limit].rstrip("-")
    return normalized or fallback


def workspace_identifier(workspace: Path) -> str:
    workspace = workspace.resolve()
    marker = workspace / ".ocsl" / "chat.json"
    _assert_no_symlink_components(marker, label="workspace identity marker")
    raw_identity = "workspace"
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8")).get("chatId")
            if isinstance(value, (str, int)) and str(value).strip():
                raw_identity = str(value)
        except (OSError, json.JSONDecodeError, AttributeError):
            raw_identity = "workspace"
    slug = _slug(raw_identity, fallback="chat", limit=44)
    digest = hashlib.sha256(
        f"{workspace}\0{raw_identity}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]
    return f"{slug}-{digest}"


def _assert_runtime_workspace(paths: Paths, workspace: Path) -> Path:
    workspace = _lexical_absolute(workspace)
    _assert_no_symlink_components(workspace, label="review workspace")
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ReviewError(f"review workspace does not exist: {workspace}")
    if workspace == Path(workspace.anchor):
        raise ReviewError("the filesystem root cannot be used as a review workspace")
    for label, protected in config.protected_workspace_roots(paths):
        if is_within(workspace, protected) or is_within(protected, workspace):
            raise ReviewError(f"review workspace overlaps protected {label}: {protected}")
    return workspace


def _chat_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    paths: Paths | None = None,
) -> Path:
    configured = explicit
    if configured is None:
        configured = os.environ.get("LAB_CONDA_ROOT") or config.chat_conda_root()
    root = _lexical_absolute(configured) if configured is not None else None
    if root is None:
        raise ReviewError("LAB_CONDA_ROOT is required for chat environments")
    _assert_no_symlink_components(root, label="chat Conda root")
    root = root.resolve()
    if root == Path(root.anchor):
        raise ReviewError("LAB_CONDA_ROOT cannot be the filesystem root")
    if paths is not None:
        for label, separated in config.protected_workspace_roots(paths):
            if is_within(root, separated) or is_within(separated, root):
                raise ReviewError(f"LAB_CONDA_ROOT must not overlap {label}: {root}")
    return root


def _instance_parent(chat_root: Path, workspace_id: str) -> Path:
    return chat_root / "envs" / "ocsl-review" / workspace_id


def _assert_workspace_chat_separation(workspace: Path, chat_root: Path) -> None:
    if is_within(workspace, chat_root) or is_within(chat_root, workspace):
        raise ReviewError("review workspace and LAB_CONDA_ROOT must be separate directory trees")


def _descriptor_path(workspace: Path, logical_name: str) -> Path:
    return workspace / "envs" / logical_name / _DESCRIPTOR_NAME


def validate_descriptor(
    paths: Paths,
    descriptor: Mapping[str, Any],
    *,
    workspace: Path | None = None,
    chat_root: Path | None = None,
) -> dict[str, Any]:
    materialized = dict(descriptor)
    _validate_json(materialized, _schema(paths, "environment-descriptor"), "environment descriptor")
    try:
        definition = load_environment_catalog(paths)[materialized["templateId"]]
    except KeyError as error:
        raise ValidationError(
            f"environment descriptor names an unknown template: {materialized['templateId']}"
        ) from error
    if materialized["inputLockSha256"] != definition.input_lock_sha256:
        raise ValidationError("environment descriptor input-lock identity is stale")
    skill_id = materialized["skillId"]
    if skill_id is not None and skill_id not in definition.skills:
        raise ValidationError(
            f"environment descriptor skill {skill_id} is not mapped to {definition.id}"
        )

    mode = materialized["mode"]
    overlay = materialized["overlay"]
    if mode == "template-clone":
        if (
            overlay is not None
            or materialized["parentBuildId"] is not None
            or materialized["sourceWorkspaceId"] is not None
        ):
            raise ValidationError(
                "a template-clone descriptor cannot carry fork provenance or overlays"
            )
    else:
        if (
            not isinstance(overlay, dict)
            or materialized["parentBuildId"] != materialized["templateBuildId"]
            or materialized["sourceWorkspaceId"] is None
        ):
            raise ValidationError(
                "a fork descriptor requires matching build provenance and an overlay record"
            )
        _validate_conda_overlay_artifacts(
            overlay["conda"],
            platform=current_conda_platform(),
        )
        _validate_pip_overlay_requirements(overlay["pip"])

    prefix = Path(materialized["instancePrefix"])
    if not prefix.is_absolute():
        raise ValidationError("environment descriptor instancePrefix must be absolute")
    effective_chat_root = chat_root or config.chat_conda_root()
    if effective_chat_root is None:
        raise ValidationError(
            "LAB_CONDA_ROOT or an explicit chat_root is required to validate a descriptor"
        )
    _assert_no_symlink_components(
        Path(effective_chat_root),
        label="descriptor chat root",
    )
    resolved_chat_root = Path(effective_chat_root).resolve()
    for label, separated in config.protected_workspace_roots(paths):
        if is_within(resolved_chat_root, separated) or is_within(separated, resolved_chat_root):
            raise ValidationError(
                f"environment descriptor chat root overlaps {label}: {resolved_chat_root}"
            )
    expected_root = resolved_chat_root / "envs" / "ocsl-review" / materialized["workspaceId"]
    if prefix.resolve() == expected_root or not is_within(prefix, expected_root):
        raise ValidationError(
            f"environment descriptor prefix escapes its chat/workspace root: {prefix}"
        )

    prefix_name = prefix.resolve().name
    expected_stem = f"{materialized['logicalName']}-{materialized['templateBuildId'][:16]}"
    if mode == "template-clone":
        expected_name = expected_stem
    elif mode == "clone-fork":
        expected_name = f"{expected_stem}-clone"
    else:
        overlay_digest = sha256_bytes(
            json.dumps(
                overlay,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )[:12]
        expected_name = f"{expected_stem}-rebuild-{overlay_digest}"
    if prefix_name != expected_name:
        raise ValidationError(
            "environment descriptor prefix does not match its logical/build identity"
        )
    if workspace is not None:
        _assert_no_symlink_components(
            workspace,
            label="descriptor workspace",
        )
        expected_workspace_id = workspace_identifier(workspace)
        if materialized["workspaceId"] != expected_workspace_id:
            raise ValidationError(
                "environment descriptor belongs to a different chat/workspace identity"
            )
    return materialized


def _assert_descriptor_template(
    descriptor: Mapping[str, Any],
    template: TemplateRecord,
) -> None:
    if (
        descriptor["templateId"] != template.definition.id
        or descriptor["templateBuildId"] != template.build_id
        or descriptor["inputLockSha256"] != template.definition.input_lock_sha256
        or descriptor["lockSha256"] != template.exact_lock_sha256
    ):
        raise ValidationError(
            "environment descriptor does not match its immutable template receipt"
        )


def load_descriptor(
    paths: Paths,
    descriptor_path: Path,
    *,
    workspace: Path | None = None,
    chat_root: Path | None = None,
) -> dict[str, Any]:
    _assert_no_symlink_components(
        descriptor_path,
        label="environment descriptor path",
    )
    try:
        value = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(
            f"cannot load environment descriptor {descriptor_path}: {error}"
        ) from error
    return validate_descriptor(paths, value, workspace=workspace, chat_root=chat_root)


def describe_environment(
    paths: Paths,
    workspace: Path,
    logical_name: str,
    *,
    chat_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if not _ID_PATTERN.fullmatch(logical_name):
        raise ReviewError(f"invalid environment logical name: {logical_name}")
    workspace = _assert_runtime_workspace(paths, workspace)
    root = _chat_root(chat_root, paths=paths)
    _assert_workspace_chat_separation(workspace, root)
    descriptor_path = _descriptor_path(workspace, logical_name)
    if not descriptor_path.is_file():
        raise ReviewError(f"no environment descriptor exists for {logical_name}")
    descriptor = load_descriptor(
        paths,
        descriptor_path,
        workspace=workspace,
        chat_root=root,
    )
    template = resolve_template(
        paths,
        descriptor["templateId"],
        build_id=descriptor["templateBuildId"],
    )
    _assert_descriptor_template(descriptor, template)
    return descriptor


def _descriptor_value(
    *,
    logical_name: str,
    skill_id: str | None,
    template: TemplateRecord,
    workspace_id: str,
    prefix: Path,
    mode: str,
    overlay: Mapping[str, list[str]] | None,
    parent_build_id: str | None,
    source_workspace_id: str | None,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schemaVersion": 1,
        "logicalName": logical_name,
        "skillId": skill_id,
        "templateId": template.definition.id,
        "templateBuildId": template.build_id,
        "inputLockSha256": template.definition.input_lock_sha256,
        "lockSha256": template.exact_lock_sha256,
        "workspaceId": workspace_id,
        "instancePrefix": str(prefix),
        "mode": mode,
        "overlay": dict(overlay) if overlay is not None else None,
        "parentBuildId": parent_build_id,
        "sourceWorkspaceId": source_workspace_id,
        "createdAt": now,
        "verifiedAt": now,
    }


def _clone_prefix(
    executable: Path,
    source: Path,
    destination: Path,
    *,
    timeout: float,
) -> None:
    _run_checked(
        [
            str(executable),
            "create",
            "--yes",
            _copy_option(executable),
            "--offline",
            "--prefix",
            str(destination),
            "--clone",
            str(source),
        ],
        timeout=timeout,
    )


def _install_exact_prefix(
    executable: Path,
    exact_lock: Path,
    destination: Path,
    *,
    platform: str,
    timeout: float,
) -> None:
    # --file points to a validated @EXPLICIT artifact list; --copy also keeps
    # the new prefix isolated from Conda's package cache and the template.
    _normalize_explicit_lock(exact_lock.read_text(encoding="utf-8"))
    _run_checked(
        [
            str(executable),
            "create",
            "--yes",
            _copy_option(executable),
            "--offline",
            *_no_default_packages_options(executable),
            "--prefix",
            str(destination),
            "--file",
            str(exact_lock),
        ],
        timeout=timeout,
    )
    _assert_conda_prefix(destination)
    _verify_prefix_matches_explicit_lock(
        destination,
        exact_lock.read_text(encoding="utf-8"),
        platform=platform,
    )


def _write_validated_descriptor(
    paths: Paths,
    descriptor_path: Path,
    descriptor: Mapping[str, Any],
    *,
    workspace: Path,
    chat_root: Path,
) -> dict[str, Any]:
    value = validate_descriptor(paths, descriptor, workspace=workspace, chat_root=chat_root)
    _ensure_contained_directory(
        descriptor_path.parent,
        workspace,
        label="environment descriptor directory",
    )
    atomic_json(descriptor_path, value)
    return value


def ensure_environment(
    paths: Paths,
    *,
    workspace: Path,
    environment_id: str | None = None,
    skill_id: str | None = None,
    logical_name: str | None = None,
    executable: str | os.PathLike[str] | None = None,
    chat_root: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    timeout: float = 1800,
    probe_timeout: float = 120,
) -> InstanceRecord:
    """Clone a promoted template into a writable chat prefix without solving."""

    if bool(environment_id) == bool(skill_id):
        raise ReviewError("select exactly one of environment_id or skill_id")
    definition = (
        _definition(paths, environment_id)
        if environment_id is not None
        else environment_for_skill(paths, str(skill_id))
    )
    template = resolve_template(paths, definition.id, platform=platform)
    workspace = _assert_runtime_workspace(paths, workspace)
    root = _chat_root(chat_root, paths=paths)
    _assert_workspace_chat_separation(workspace, root)
    conda = _conda_executable(executable)
    workspace_id = workspace_identifier(workspace)
    name = logical_name or definition.id
    if not _ID_PATTERN.fullmatch(name):
        raise ReviewError(f"invalid environment logical name: {name}")
    parent = _instance_parent(root, workspace_id)
    parent = _ensure_contained_directory(
        parent,
        root,
        label="chat environment directory",
    )
    destination = parent / f"{name}-{template.build_id[:16]}"
    descriptor_path = _descriptor_path(workspace, name)
    _assert_no_symlink_components(
        descriptor_path,
        label="environment descriptor path",
    )

    if descriptor_path.is_file():
        descriptor = load_descriptor(
            paths,
            descriptor_path,
            workspace=workspace,
            chat_root=root,
        )
        if (
            descriptor["templateId"] == definition.id
            and descriptor["templateBuildId"] == template.build_id
            and descriptor["lockSha256"] == template.exact_lock_sha256
            and descriptor["mode"] == "template-clone"
            and Path(descriptor["instancePrefix"]).resolve() == destination.resolve()
            and destination.is_dir()
        ):
            _assert_conda_prefix(destination)
            audit_inode_isolation(template.prefix, destination)
            _make_chat_prefix_writable(destination)
            _run_health_checks(
                conda,
                destination,
                definition,
                cache_root=destination / ".ocsl-mamba-cache",
                timeout=probe_timeout,
            )
            audit_inode_isolation(template.prefix, destination)
            descriptor["verifiedAt"] = _utc_now()
            descriptor = _write_validated_descriptor(
                paths,
                descriptor_path,
                descriptor,
                workspace=workspace,
                chat_root=root,
            )
            return InstanceRecord(destination, descriptor_path, descriptor, True)
        raise ReviewError(
            f"existing descriptor does not own the requested environment: {descriptor_path}"
        )

    if destination.exists():
        raise ReviewError(
            f"refusing to reuse an unowned or stale chat environment prefix: {destination}"
        )

    created = True
    try:
        _clone_prefix(conda, template.prefix, destination, timeout=timeout)
        if not destination.is_dir():
            raise ValidationError(f"Conda did not materialize the requested prefix: {destination}")
        _assert_conda_prefix(destination)
        audit_inode_isolation(template.prefix, destination)
        _make_chat_prefix_writable(destination)
        _run_health_checks(
            conda,
            destination,
            definition,
            cache_root=destination / ".ocsl-mamba-cache",
            timeout=probe_timeout,
        )
        audit_inode_isolation(template.prefix, destination)
        descriptor = _descriptor_value(
            logical_name=name,
            skill_id=skill_id,
            template=template,
            workspace_id=workspace_id,
            prefix=destination,
            mode="template-clone",
            overlay=None,
            parent_build_id=None,
            source_workspace_id=None,
        )
        descriptor = _write_validated_descriptor(
            paths,
            descriptor_path,
            descriptor,
            workspace=workspace,
            chat_root=root,
        )
        return InstanceRecord(destination, descriptor_path, descriptor, False)
    except Exception:
        if created and destination.exists():
            _cleanup_transient(destination, parent)
        raise


def verify_environment(
    paths: Paths,
    *,
    workspace: Path,
    logical_name: str,
    executable: str | os.PathLike[str] | None = None,
    chat_root: str | os.PathLike[str] | None = None,
    probe_timeout: float = 120,
) -> dict[str, Any]:
    root = _chat_root(chat_root, paths=paths)
    descriptor = describe_environment(paths, workspace, logical_name, chat_root=root)
    prefix = Path(descriptor["instancePrefix"])
    if not prefix.is_dir():
        raise ValidationError(f"environment prefix is missing: {prefix}")
    _assert_conda_prefix(prefix)
    definition = _definition(paths, descriptor["templateId"])
    template = resolve_template(paths, definition.id, build_id=descriptor["templateBuildId"])
    audit = audit_inode_isolation(template.prefix, prefix)
    selected_conda = _conda_executable(executable)
    with tempfile.TemporaryDirectory(prefix=".ocsl-review-verify-cache-") as temporary_cache:
        _run_health_checks(
            selected_conda,
            prefix,
            definition,
            cache_root=Path(temporary_cache),
            timeout=probe_timeout,
            read_only=True,
        )
    audit = audit_inode_isolation(template.prefix, prefix)
    return {
        "logicalName": logical_name,
        "prefix": str(prefix),
        "ok": True,
        "inodeAudit": audit,
    }


def run_in_environment(
    paths: Paths,
    command: Sequence[str],
    *,
    workspace: Path,
    environment_id: str | None = None,
    skill_id: str | None = None,
    logical_name: str | None = None,
    executable: str | os.PathLike[str] | None = None,
    chat_root: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    ensure_timeout: float = 1800,
    probe_timeout: float = 120,
    command_timeout: float | None = None,
) -> CommandResult:
    if not command:
        raise ReviewError("an environment command is required after --")
    if bool(environment_id) == bool(skill_id):
        raise ReviewError("select exactly one of environment_id or skill_id")
    definition = (
        _definition(paths, str(environment_id))
        if environment_id is not None
        else environment_for_skill(paths, str(skill_id))
    )
    workspace = _assert_runtime_workspace(paths, workspace)
    root = _chat_root(chat_root, paths=paths)
    _assert_workspace_chat_separation(workspace, root)
    name = logical_name or definition.id
    if not _ID_PATTERN.fullmatch(name):
        raise ReviewError(f"invalid environment logical name: {name}")

    descriptor_path = _descriptor_path(workspace, name)
    if descriptor_path.is_file():
        descriptor = describe_environment(paths, workspace, name, chat_root=root)
        if descriptor["templateId"] != definition.id:
            raise ReviewError(
                f"environment {name} does not use the selected template {definition.id}"
            )
        if descriptor["mode"] in {"clone-fork", "rebuild-fork"}:
            verification = verify_environment(
                paths,
                workspace=workspace,
                logical_name=name,
                executable=executable,
                chat_root=root,
                probe_timeout=probe_timeout,
            )
            prefix = Path(verification["prefix"])
        else:
            prefix = ensure_environment(
                paths,
                workspace=workspace,
                environment_id=environment_id,
                skill_id=skill_id,
                logical_name=name,
                executable=executable,
                chat_root=root,
                platform=platform,
                timeout=ensure_timeout,
                probe_timeout=probe_timeout,
            ).prefix
    else:
        prefix = ensure_environment(
            paths,
            workspace=workspace,
            environment_id=environment_id,
            skill_id=skill_id,
            logical_name=name,
            executable=executable,
            chat_root=root,
            platform=platform,
            timeout=ensure_timeout,
            probe_timeout=probe_timeout,
        ).prefix
    conda = _conda_executable(executable)
    return run_command(
        _run_in_prefix_argv(conda, prefix, command),
        cwd=workspace,
        env=_micromamba_process_env(conda, prefix / ".ocsl-mamba-cache"),
        timeout=command_timeout,
        capture=False,
    )


def _validate_conda_overlay_artifacts(
    artifacts: Iterable[str],
    *,
    platform: str,
) -> list[str]:
    result: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, str) or not artifact or "\n" in artifact or "\r" in artifact:
            raise ValidationError("Conda overlay entries must be single-line artifact URLs")
        parsed = urlsplit(artifact)
        if parsed.scheme not in {"https", "http", "file"}:
            raise ValidationError(
                "Conda rebuild overlays must use exact http(s) or file artifact URLs"
            )
        if parsed.username or parsed.password or parsed.query:
            raise ValidationError("Conda overlay URLs cannot contain credentials or query tokens")
        if not _CONDA_FRAGMENT.fullmatch(parsed.fragment.lower()):
            raise ValidationError(
                f"Conda overlay artifact must carry an MD5 or SHA-256 fragment: {artifact}"
            )
        if parsed.scheme in {"https", "http"}:
            segments = set(parsed.path.split("/"))
            if platform not in segments and "noarch" not in segments:
                raise ValidationError(
                    f"Conda overlay artifact does not match platform {platform}: {artifact}"
                )
        result.append(artifact)
    if len(result) != len(set(result)):
        raise ValidationError("Conda rebuild overlay repeats an artifact")
    return sorted(result)


def _validate_pip_overlay_requirements(requirements: Iterable[str]) -> list[str]:
    result: list[str] = []
    for requirement in requirements:
        if (
            not isinstance(requirement, str)
            or not requirement.strip()
            or "\n" in requirement
            or "\r" in requirement
            or requirement.lstrip().startswith("-")
        ):
            raise ValidationError("pip overlay entries must be single requirements, not options")
        lowered = requirement.lower()
        if not _PIP_HASH.search(lowered):
            raise ValidationError(
                "pip rebuild overlays require an inline --hash=sha256:<64-hex> value"
            )
        without_hashes = _PIP_HASH.sub(" ", lowered)
        if "--" in without_hashes:
            raise ValidationError("pip rebuild overlay requirements cannot add pip options")
        if "==" not in requirement and " @ " not in requirement:
            raise ValidationError("pip rebuild overlays must pin a version or artifact URL")
        if re.search(r"https?://[^/\s]*@", requirement) or "?" in requirement:
            raise ValidationError("pip overlay URLs cannot contain credentials or query tokens")
        result.append(requirement.strip())
    if len(result) != len(set(result)):
        raise ValidationError("pip rebuild overlay repeats a requirement")
    return sorted(result)


def _apply_rebuild_overlay(
    executable: Path,
    destination: Path,
    parent: Path,
    *,
    platform: str,
    conda_artifacts: Sequence[str],
    pip_requirements: Sequence[str],
    cache_root: Path,
    timeout: float,
) -> None:
    temporary_files: list[Path] = []
    try:
        if conda_artifacts:
            descriptor, temporary = tempfile.mkstemp(prefix=".conda-overlay-", dir=parent)
            os.close(descriptor)
            overlay_lock = Path(temporary)
            temporary_files.append(overlay_lock)
            atomic_write(
                overlay_lock,
                "@EXPLICIT\n" + "\n".join(conda_artifacts) + "\n",
                mode=0o600,
            )
            _run_checked(
                [
                    str(executable),
                    "install",
                    "--yes",
                    _copy_option(executable),
                    "--prefix",
                    str(destination),
                    "--file",
                    str(overlay_lock),
                ],
                timeout=timeout,
            )
            _verify_locked_artifacts_present(
                destination,
                "@EXPLICIT\n" + "\n".join(conda_artifacts) + "\n",
                platform=platform,
                allow_unexpected=True,
            )
        if pip_requirements:
            descriptor, temporary = tempfile.mkstemp(prefix=".pip-overlay-", dir=parent)
            os.close(descriptor)
            requirements_file = Path(temporary)
            temporary_files.append(requirements_file)
            atomic_write(
                requirements_file,
                "\n".join(pip_requirements) + "\n",
                mode=0o600,
            )
            _run_checked(
                _run_in_prefix_argv(
                    executable,
                    destination,
                    [
                        "python",
                        "-m",
                        "pip",
                        "install",
                        "--no-input",
                        "--no-deps",
                        "--no-index",
                        "--require-hashes",
                        "--requirement",
                        str(requirements_file),
                    ],
                ),
                env=_micromamba_process_env(executable, cache_root),
                timeout=timeout,
            )
    finally:
        for path in temporary_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _source_descriptor_or_ensure(
    paths: Paths,
    *,
    source_workspace: Path,
    environment_id: str,
    source_logical_name: str,
    executable: str | os.PathLike[str] | None,
    chat_root: str | os.PathLike[str] | None,
    platform: str | None,
    timeout: float,
    probe_timeout: float,
) -> dict[str, Any]:
    descriptor_path = _descriptor_path(source_workspace.resolve(), source_logical_name)
    if descriptor_path.is_file():
        return describe_environment(
            paths,
            source_workspace,
            source_logical_name,
            chat_root=chat_root,
        )
    if source_logical_name != environment_id:
        raise ReviewError(f"source environment does not exist: {source_logical_name}")
    return dict(
        ensure_environment(
            paths,
            workspace=source_workspace,
            environment_id=environment_id,
            executable=executable,
            chat_root=chat_root,
            platform=platform,
            timeout=timeout,
            probe_timeout=probe_timeout,
        ).descriptor
    )


def _materialize_fork(
    paths: Paths,
    *,
    source_descriptor: Mapping[str, Any],
    source_workspace: Path,
    target_workspace: Path,
    logical_name: str,
    mode: str,
    conda_additions: Sequence[str],
    pip_additions: Sequence[str],
    executable: str | os.PathLike[str] | None,
    chat_root: str | os.PathLike[str] | None,
    platform: str | None,
    timeout: float,
    probe_timeout: float,
) -> InstanceRecord:
    if mode not in {"clone", "rebuild", "reproducible-rebuild"}:
        raise ReviewError("fork mode must be clone or reproducible-rebuild")
    normalized_mode = "rebuild" if mode in {"rebuild", "reproducible-rebuild"} else "clone"
    if normalized_mode == "clone" and (conda_additions or pip_additions):
        raise ReviewError("overlay additions are allowed only for reproducible rebuild forks")
    if not _ID_PATTERN.fullmatch(logical_name):
        raise ReviewError(f"invalid fork environment name: {logical_name}")

    root = _chat_root(chat_root, paths=paths)
    source_workspace = _assert_runtime_workspace(paths, source_workspace)
    target_workspace = _assert_runtime_workspace(paths, target_workspace)
    _assert_workspace_chat_separation(source_workspace, root)
    _assert_workspace_chat_separation(target_workspace, root)
    source_descriptor = validate_descriptor(
        paths,
        source_descriptor,
        workspace=source_workspace,
        chat_root=root,
    )
    source_prefix = Path(source_descriptor["instancePrefix"])
    if not source_prefix.is_dir():
        raise ValidationError(f"source environment prefix is missing: {source_prefix}")
    definition = _definition(paths, source_descriptor["templateId"])
    template = resolve_template(
        paths,
        definition.id,
        build_id=source_descriptor["templateBuildId"],
        platform=platform,
    )
    _assert_descriptor_template(source_descriptor, template)
    conda = _conda_executable(executable)

    existing_overlay = source_descriptor.get("overlay") or {"conda": [], "pip": []}
    if normalized_mode == "rebuild":
        all_conda = _validate_conda_overlay_artifacts(
            [*existing_overlay.get("conda", []), *conda_additions],
            platform=template.platform,
        )
        all_pip = _validate_pip_overlay_requirements(
            [*existing_overlay.get("pip", []), *pip_additions]
        )
    else:
        all_conda = list(existing_overlay.get("conda", []))
        all_pip = list(existing_overlay.get("pip", []))

    workspace_id = workspace_identifier(target_workspace)
    source_workspace_id = workspace_identifier(source_workspace)
    overlay_digest = sha256_bytes(
        json.dumps(
            {"conda": all_conda, "pip": all_pip},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )[:12]
    suffix = "clone" if normalized_mode == "clone" else f"rebuild-{overlay_digest}"
    parent = _instance_parent(root, workspace_id)
    parent = _ensure_contained_directory(
        parent,
        root,
        label="chat fork directory",
    )
    destination = parent / f"{logical_name}-{template.build_id[:16]}-{suffix}"
    descriptor_path = _descriptor_path(target_workspace, logical_name)
    _assert_no_symlink_components(
        descriptor_path,
        label="fork descriptor path",
    )
    if source_workspace == target_workspace and descriptor_path == _descriptor_path(
        source_workspace, source_descriptor["logicalName"]
    ):
        raise ReviewError("a fork must use another workspace or a distinct logical name")

    expected_mode = "clone-fork" if normalized_mode == "clone" else "rebuild-fork"
    if descriptor_path.is_file():
        descriptor = load_descriptor(
            paths,
            descriptor_path,
            workspace=target_workspace,
            chat_root=root,
        )
        if (
            descriptor["templateBuildId"] == template.build_id
            and descriptor["mode"] == expected_mode
            and descriptor["overlay"] == {"conda": all_conda, "pip": all_pip}
            and Path(descriptor["instancePrefix"]).resolve() == destination.resolve()
            and destination.is_dir()
        ):
            _assert_conda_prefix(destination)
            audit_inode_isolation(template.prefix, destination)
            if normalized_mode == "clone":
                audit_inode_isolation(source_prefix, destination)
            _make_chat_prefix_writable(destination)
            _run_health_checks(
                conda,
                destination,
                definition,
                cache_root=destination / ".ocsl-mamba-cache",
                timeout=probe_timeout,
            )
            audit_inode_isolation(template.prefix, destination)
            if normalized_mode == "clone":
                audit_inode_isolation(source_prefix, destination)
            descriptor["verifiedAt"] = _utc_now()
            descriptor = _write_validated_descriptor(
                paths,
                descriptor_path,
                descriptor,
                workspace=target_workspace,
                chat_root=root,
            )
            return InstanceRecord(destination, descriptor_path, descriptor, True)
        raise ReviewError(f"existing descriptor does not own the requested fork: {descriptor_path}")

    if destination.exists():
        raise ReviewError(f"refusing to reuse an unowned fork prefix: {destination}")
    created = True
    try:
        if normalized_mode == "clone":
            _clone_prefix(conda, source_prefix, destination, timeout=timeout)
        else:
            _install_exact_prefix(
                conda,
                template.exact_lock,
                destination,
                platform=template.platform,
                timeout=timeout,
            )
        if not destination.is_dir():
            raise ValidationError(f"Conda did not materialize the fork prefix: {destination}")
        _assert_conda_prefix(destination)
        audit_inode_isolation(template.prefix, destination)
        if normalized_mode == "clone":
            audit_inode_isolation(source_prefix, destination)
        _make_chat_prefix_writable(destination)
        if normalized_mode == "rebuild":
            _apply_rebuild_overlay(
                conda,
                destination,
                parent,
                platform=template.platform,
                conda_artifacts=all_conda,
                pip_requirements=all_pip,
                cache_root=destination / ".ocsl-mamba-cache",
                timeout=timeout,
            )
            _assert_conda_prefix(destination)
            audit_inode_isolation(template.prefix, destination)
        _run_health_checks(
            conda,
            destination,
            definition,
            cache_root=destination / ".ocsl-mamba-cache",
            timeout=probe_timeout,
        )
        audit_inode_isolation(template.prefix, destination)
        if normalized_mode == "clone":
            audit_inode_isolation(source_prefix, destination)
        descriptor = _descriptor_value(
            logical_name=logical_name,
            skill_id=source_descriptor.get("skillId"),
            template=template,
            workspace_id=workspace_id,
            prefix=destination,
            mode=expected_mode,
            overlay={"conda": all_conda, "pip": all_pip},
            parent_build_id=source_descriptor["templateBuildId"],
            source_workspace_id=source_workspace_id,
        )
        descriptor = _write_validated_descriptor(
            paths,
            descriptor_path,
            descriptor,
            workspace=target_workspace,
            chat_root=root,
        )
        return InstanceRecord(destination, descriptor_path, descriptor, False)
    except Exception:
        if created and destination.exists():
            _cleanup_transient(destination, parent)
        raise


def fork_environment(
    paths: Paths,
    *,
    source_workspace: Path,
    target_workspace: Path,
    environment_id: str,
    name: str,
    source_logical_name: str | None = None,
    mode: str = "clone",
    conda_additions: Sequence[str] = (),
    pip_additions: Sequence[str] = (),
    executable: str | os.PathLike[str] | None = None,
    chat_root: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    timeout: float = 1800,
    probe_timeout: float = 120,
) -> InstanceRecord:
    definition = _definition(paths, environment_id)
    source_name = source_logical_name or definition.id
    logical_name = _slug(name, fallback=f"{definition.id}-fork")
    source_descriptor = _source_descriptor_or_ensure(
        paths,
        source_workspace=source_workspace,
        environment_id=definition.id,
        source_logical_name=source_name,
        executable=executable,
        chat_root=chat_root,
        platform=platform,
        timeout=timeout,
        probe_timeout=probe_timeout,
    )
    return _materialize_fork(
        paths,
        source_descriptor=source_descriptor,
        source_workspace=source_workspace,
        target_workspace=target_workspace,
        logical_name=logical_name,
        mode=mode,
        conda_additions=conda_additions,
        pip_additions=pip_additions,
        executable=executable,
        chat_root=chat_root,
        platform=platform,
        timeout=timeout,
        probe_timeout=probe_timeout,
    )


def rematerialize_chat_fork(
    paths: Paths,
    *,
    source_workspace: Path,
    target_workspace: Path,
    mode: str = "clone",
    executable: str | os.PathLike[str] | None = None,
    chat_root: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    timeout: float = 1800,
    probe_timeout: float = 120,
) -> list[InstanceRecord]:
    """Materialize every source-chat descriptor for a newly forked chat."""

    source_workspace = _assert_runtime_workspace(paths, source_workspace)
    target_workspace = _assert_runtime_workspace(paths, target_workspace)
    if source_workspace == target_workspace:
        raise ReviewError("chat-fork rematerialization requires a distinct target workspace")
    descriptor_root = source_workspace / "envs"
    _assert_no_symlink_components(
        descriptor_root,
        label="source descriptor directory",
    )
    descriptor_paths = sorted(descriptor_root.glob(f"*/{_DESCRIPTOR_NAME}"))
    if not descriptor_paths:
        raise ReviewError(f"source chat has no review environment descriptors: {source_workspace}")
    root = _chat_root(chat_root, paths=paths)
    _assert_workspace_chat_separation(source_workspace, root)
    _assert_workspace_chat_separation(target_workspace, root)
    result: list[InstanceRecord] = []
    try:
        for descriptor_path in descriptor_paths:
            descriptor = load_descriptor(
                paths,
                descriptor_path,
                workspace=source_workspace,
                chat_root=root,
            )
            result.append(
                _materialize_fork(
                    paths,
                    source_descriptor=descriptor,
                    source_workspace=source_workspace,
                    target_workspace=target_workspace,
                    logical_name=descriptor["logicalName"],
                    mode=mode,
                    conda_additions=(),
                    pip_additions=(),
                    executable=executable,
                    chat_root=root,
                    platform=platform,
                    timeout=timeout,
                    probe_timeout=probe_timeout,
                )
            )
    except Exception as error:
        rollback_errors: list[str] = []
        for instance in reversed(result):
            if instance.reused:
                continue
            try:
                instance.descriptor_path.unlink(missing_ok=True)
                _cleanup_transient(instance.prefix, instance.prefix.parent)
            except Exception as rollback_error:  # noqa: BLE001 - finish best-effort rollback
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise ReviewError(
                "chat-fork rematerialization failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from error
        raise
    return result


def _namespace_value(args: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(args, name):
            value = getattr(args, name)
            if value is not None:
                return value
    return default


def _require_operator_context() -> None:
    cwd = Path.cwd().resolve()
    workspace_root = os.environ.get("LAB_WORKSPACE_ROOT")
    if workspace_root and is_within(cwd, Path(workspace_root)):
        raise TrustBoundaryError("environment template builds cannot run inside a chat workspace")
    if os.environ.get("OCSL_REVIEW_SANDBOX"):
        raise TrustBoundaryError("environment template builds cannot run inside a review sandbox")
    if (cwd / "WORKSPACE.md").is_file() and (cwd / ".ocsl").exists():
        raise TrustBoundaryError(
            "environment template builds cannot run inside a materialized workspace"
        )


def cmd_list(args: Any) -> list[dict[str, Any]]:
    return list_environments(args.paths, platform=_namespace_value(args, "platform"))


def cmd_build(args: Any) -> Any:
    _require_operator_context()
    common = {
        "executable": _namespace_value(args, "conda_executable", "executable"),
        "platform": _namespace_value(args, "platform"),
        "solve_timeout": float(_namespace_value(args, "timeout", default=3600)),
        "probe_timeout": float(_namespace_value(args, "probe_timeout", default=120)),
    }
    if bool(_namespace_value(args, "all", "build_all", default=False)):
        return build_all_templates(args.paths, **common)
    environment_id = _namespace_value(args, "environment", "environment_id", "env_id", "env")
    if not environment_id:
        raise ReviewError("env build requires --environment or --all")
    return build_template(args.paths, str(environment_id), **common)


def cmd_ensure(args: Any) -> dict[str, Any]:
    instance = ensure_environment(
        args.paths,
        workspace=Path(_namespace_value(args, "workspace", default=Path.cwd())),
        environment_id=_namespace_value(args, "environment", "environment_id", "env_id", "env"),
        skill_id=_namespace_value(args, "skill", "skill_id"),
        logical_name=_namespace_value(args, "name", "logical_name"),
        executable=_namespace_value(args, "conda_executable", "executable"),
        chat_root=_namespace_value(args, "chat_root"),
        platform=_namespace_value(args, "platform"),
        timeout=float(_namespace_value(args, "timeout", default=1800)),
        probe_timeout=float(_namespace_value(args, "probe_timeout", default=120)),
    )
    return {
        "prefix": str(instance.prefix),
        "descriptor": str(instance.descriptor_path),
        "reused": instance.reused,
    }


def cmd_run(args: Any) -> CommandResult:
    command = _namespace_value(args, "argv", "command", "passthrough", default=())
    return run_in_environment(
        args.paths,
        list(command),
        workspace=Path(_namespace_value(args, "workspace", default=Path.cwd())),
        environment_id=_namespace_value(args, "environment", "environment_id", "env_id", "env"),
        skill_id=_namespace_value(args, "skill", "skill_id"),
        logical_name=_namespace_value(args, "name", "logical_name"),
        executable=_namespace_value(args, "conda_executable", "executable"),
        chat_root=_namespace_value(args, "chat_root"),
        platform=_namespace_value(args, "platform"),
        ensure_timeout=float(_namespace_value(args, "timeout", default=1800)),
        probe_timeout=float(_namespace_value(args, "probe_timeout", default=120)),
        command_timeout=_namespace_value(args, "command_timeout"),
    )


def cmd_fork(args: Any) -> dict[str, Any] | list[dict[str, Any]]:
    source_workspace = Path(_namespace_value(args, "source_workspace"))
    target_workspace = Path(_namespace_value(args, "target_workspace"))
    all_environments = bool(_namespace_value(args, "all", "chat_fork", default=False))
    conda_additions = tuple(_namespace_value(args, "add_conda", "conda_additions", default=()))
    pip_additions = tuple(_namespace_value(args, "add_pip", "pip_additions", default=()))
    if all_environments and (conda_additions or pip_additions):
        raise ReviewError("env fork --all cannot be combined with --add-conda or --add-pip")
    common = {
        "source_workspace": source_workspace,
        "target_workspace": target_workspace,
        "mode": str(_namespace_value(args, "mode", default="clone")),
        "executable": _namespace_value(args, "conda_executable", "executable"),
        "chat_root": _namespace_value(args, "chat_root"),
        "platform": _namespace_value(args, "platform"),
        "timeout": float(_namespace_value(args, "timeout", default=1800)),
        "probe_timeout": float(_namespace_value(args, "probe_timeout", default=120)),
    }
    if all_environments:
        instances = rematerialize_chat_fork(args.paths, **common)
        return [
            {
                "prefix": str(instance.prefix),
                "descriptor": str(instance.descriptor_path),
                "reused": instance.reused,
            }
            for instance in instances
        ]
    environment_id = _namespace_value(args, "environment", "environment_id", "env_id", "env")
    name = _namespace_value(args, "name", "logical_name")
    if not environment_id or not name:
        raise ReviewError("env fork requires --environment and --name")
    instance = fork_environment(
        args.paths,
        environment_id=str(environment_id),
        name=str(name),
        source_logical_name=_namespace_value(args, "source_name", "source_logical_name"),
        conda_additions=conda_additions,
        pip_additions=pip_additions,
        **common,
    )
    return {
        "prefix": str(instance.prefix),
        "descriptor": str(instance.descriptor_path),
        "reused": instance.reused,
    }


def cmd_describe(args: Any) -> dict[str, Any]:
    logical_name = _namespace_value(args, "name", "logical_name", "environment")
    if not logical_name:
        raise ReviewError("env describe requires an environment logical name")
    return describe_environment(
        args.paths,
        Path(_namespace_value(args, "workspace", default=Path.cwd())),
        str(logical_name),
        chat_root=_namespace_value(args, "chat_root"),
    )


def cmd_verify(args: Any) -> dict[str, Any]:
    logical_name = _namespace_value(args, "name", "logical_name", "environment")
    if not logical_name:
        raise ReviewError("env verify requires an environment logical name")
    return verify_environment(
        args.paths,
        workspace=Path(_namespace_value(args, "workspace", default=Path.cwd())),
        logical_name=str(logical_name),
        executable=_namespace_value(args, "conda_executable", "executable"),
        chat_root=_namespace_value(args, "chat_root"),
        probe_timeout=float(_namespace_value(args, "probe_timeout", default=120)),
    )
