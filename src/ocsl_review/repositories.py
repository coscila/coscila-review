"""Pinned, operator-populated source archives with state-only verification data."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ReviewError, ValidationError
from .util import atomic_json, load_toml, run_command, tree_digest


def _catalog(paths) -> dict[str, dict[str, Any]]:
    records = load_toml(paths.repository_catalog).get("repository", [])
    result = {record["id"]: record for record in records}
    if len(result) != len(records):
        raise ValidationError("repository catalog contains duplicate identifiers")
    return result


def _lock(paths) -> dict[str, dict[str, Any]]:
    value = json.loads(paths.repository_lock.read_text(encoding="utf-8"))
    records = value.get("repositories", [])
    result = {record["id"]: record for record in records}
    if len(result) != len(records):
        raise ValidationError("repository lock contains duplicate identifiers")
    return result


def validate_sources(paths) -> dict[str, Any]:
    catalog = _catalog(paths)
    lock = _lock(paths)
    problems = []
    if set(catalog) != set(lock):
        problems.append("catalog and lock repository sets differ")
    for identifier in sorted(set(catalog) & set(lock)):
        declared = catalog[identifier]
        pinned = lock[identifier]
        if declared["url"] != pinned["url"]:
            problems.append(f"{identifier}: URL differs between catalog and lock")
        commit = pinned.get("commit", "")
        if not isinstance(commit, str) or not __import__("re").fullmatch(r"[0-9a-f]{40}", commit):
            problems.append(f"{identifier}: commit is not an exact lowercase SHA")
        if declared.get("executed") != pinned.get("executed"):
            problems.append(f"{identifier}: execution policy differs")
        if declared.get("executed") and pinned.get("license") in {"NOASSERTION", "NONE", "UNKNOWN"}:
            problems.append(f"{identifier}: code with an unestablished license cannot execute")
    if problems:
        raise ValidationError("repository source validation failed:\n- " + "\n- ".join(problems))
    return {"ok": True, "repositories": len(catalog)}


def _mirror(paths, identifier: str) -> Path:
    return paths.repositories / "mirrors" / f"{identifier}.git"


def _checkout(paths, record: dict[str, Any]) -> Path:
    return paths.repositories / "checkouts" / record["id"] / record["commit"]


def _manifest(paths, record: dict[str, Any]) -> Path:
    return paths.repositories / "manifests" / f"{record['id']}-{record['commit']}.json"


def _extract_git_archive(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValidationError(f"unsafe path in repository archive: {member.name}")
            if member.isdev() or member.isfifo():
                raise ValidationError(
                    f"unsupported special file in repository archive: {member.name}"
                )
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                combined = name.parent / target
                if target.is_absolute() or ".." in combined.parts:
                    raise ValidationError(f"unsafe link in repository archive: {member.name}")
        archive.extractall(destination, filter="data")


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            path.chmod(0o555 if executable else 0o444)
    root.chmod(0o555)


def sync_repository(paths, identifier: str, *, refresh_mirror: bool = False) -> dict[str, Any]:
    validate_sources(paths)
    locked = _lock(paths)
    if identifier not in locked:
        raise ReviewError(f"unknown repository: {identifier}")
    record = locked[identifier]
    mirror = _mirror(paths, identifier)
    destination = _checkout(paths, record)
    manifest_path = _manifest(paths, record)
    if destination.is_dir() and manifest_path.is_file():
        verified = verify_repository(paths, identifier)
        if verified["ok"]:
            return {
                "id": identifier,
                "status": "present",
                "path": str(destination),
                "commit": record["commit"],
            }
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not mirror.exists():
        run_command(
            ["git", "clone", "--mirror", "--", record["url"], str(mirror)], check=True, timeout=600
        )
    elif refresh_mirror:
        run_command(
            ["git", "--git-dir", str(mirror), "fetch", "--prune", "origin"], check=True, timeout=600
        )
    exists = run_command(
        ["git", "--git-dir", str(mirror), "cat-file", "-e", f"{record['commit']}^{{commit}}"],
        timeout=30,
    )
    if exists.returncode:
        raise ReviewError(f"pinned commit is absent from mirror {identifier}: {record['commit']}")
    archived = subprocess.run(
        ["git", "--git-dir", str(mirror), "archive", "--format=tar", record["commit"]],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if archived.returncode:
        raise ReviewError(
            f"could not archive {identifier}: {archived.stderr.decode(errors='replace')[:500]}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{identifier}-", dir=destination.parent))
    try:
        _extract_git_archive(archived.stdout, temporary)
        digest = tree_digest(temporary)
        _make_read_only(temporary)
        if destination.exists():
            raise ReviewError(f"refusing to replace an existing immutable checkout: {destination}")
        os.replace(temporary, destination)
        atomic_json(
            manifest_path,
            {
                "schemaVersion": 1,
                "id": identifier,
                "url": record["url"],
                "commit": record["commit"],
                "treeSha256": digest,
                "materializedAt": datetime.now(UTC).isoformat(),
                "path": str(destination),
            },
            mode=0o444,
        )
        return {
            "id": identifier,
            "status": "synced",
            "path": str(destination),
            "commit": record["commit"],
            "treeSha256": digest,
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def sync_all(paths, *, refresh_mirrors: bool = False) -> dict[str, Any]:
    results = []
    for identifier in sorted(_lock(paths)):
        results.append(sync_repository(paths, identifier, refresh_mirror=refresh_mirrors))
    return {"repositories": results}


def verify_repository(paths, identifier: str) -> dict[str, Any]:
    record = _lock(paths).get(identifier)
    if not record:
        raise ReviewError(f"unknown repository: {identifier}")
    destination = _checkout(paths, record)
    manifest_path = _manifest(paths, record)
    problems = []
    if not destination.is_dir():
        problems.append("checkout missing")
    if not manifest_path.is_file():
        problems.append("state manifest missing")
        manifest = {}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("commit") not in (None, record["commit"]):
        problems.append("manifest commit differs from source lock")
    actual = tree_digest(destination) if destination.is_dir() else None
    if actual and manifest.get("treeSha256") != actual:
        problems.append("checkout content digest differs from materialization manifest")
    if destination.is_dir():
        writable = [
            str(path.relative_to(destination))
            for path in destination.rglob("*")
            if not path.is_symlink() and path.stat().st_mode & 0o222
        ]
        if writable:
            problems.append(f"checkout contains writable entries: {writable[:3]}")
    return {
        "id": identifier,
        "ok": not problems,
        "path": str(destination),
        "commit": record["commit"],
        "treeSha256": actual,
        "problems": problems,
    }


def repository_path(paths, identifier: str) -> Path:
    report = verify_repository(paths, identifier)
    if not report["ok"]:
        raise ValidationError(
            f"repository archive is not verified: {identifier}: {report['problems']}"
        )
    return Path(report["path"])


def resolve_update_candidates(paths) -> dict[str, Any]:
    candidates = []
    for identifier, record in sorted(_catalog(paths).items()):
        result = run_command(["git", "ls-remote", record["url"], "HEAD"], check=True, timeout=120)
        commit = result.stdout.split()[0] if result.stdout.split() else ""
        if not __import__("re").fullmatch(r"[0-9a-f]{40}", commit):
            raise ReviewError(f"could not resolve exact remote HEAD for {identifier}")
        candidates.append({"id": identifier, "url": record["url"], "commit": commit})
    output = (
        paths.repositories
        / "update-candidates"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    value = {
        "schemaVersion": 1,
        "resolvedAt": datetime.now(UTC).isoformat(),
        "repositories": candidates,
    }
    atomic_json(output, value, mode=0o600)
    return {"candidateFile": str(output), **value}


def cmd_list(args):
    locked = _lock(args.paths)
    return [
        {
            "id": record["id"],
            "commit": record["commit"],
            "license": record["license"],
            "executed": record["executed"],
        }
        for record in locked.values()
    ]


def cmd_path(args):
    return str(repository_path(args.paths, args.repository))


def cmd_verify(args):
    identifiers = (
        [args.repository] if getattr(args, "repository", None) else sorted(_lock(args.paths))
    )
    reports = [verify_repository(args.paths, item) for item in identifiers]
    result = {"ok": all(item["ok"] for item in reports), "repositories": reports}
    if getattr(args, "strict", False) and not result["ok"]:
        raise ValidationError("one or more archived repositories failed verification")
    return result


def cmd_sync(args):
    return sync_all(args.paths, refresh_mirrors=bool(getattr(args, "refresh", False)))


def cmd_update(args):
    return resolve_update_candidates(args.paths)
