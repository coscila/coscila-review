"""Dependency-light utilities shared by operator and review-time commands."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ReviewError


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: Path, *, ignored: Iterable[str] = (".git", "__pycache__")) -> str:
    ignored_names = set(ignored)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ignored_names for part in relative.parts) or not path.is_file():
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def atomic_write(path: Path, data: str | bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = isinstance(data, bytes)
    descriptor, temporary = tempfile.mkstemp(prefix=".ocsl-review-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb" if binary else "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    atomic_write(
        path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", mode=mode
    )


def is_within(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def require_relative_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReviewError(f"path must remain relative to its declared root: {value}")
    return candidate


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture: bool = True,
) -> CommandResult:
    if not argv:
        raise ReviewError("refusing to run an empty command")
    process = subprocess.run(
        [os.fspath(part) for part in argv],
        cwd=os.fspath(cwd) if cwd else None,
        env=dict(env) if env else None,
        timeout=timeout,
        text=True,
        capture_output=capture,
        check=False,
    )
    result = CommandResult(process.returncode, process.stdout or "", process.stderr or "")
    if check and process.returncode:
        rendered = " ".join(os.fspath(part) for part in argv)
        raise ReviewError(
            f"command failed ({process.returncode}): {rendered}\n{result.stderr.strip()}"
        )
    return result
