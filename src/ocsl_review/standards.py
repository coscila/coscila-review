"""Immutable snapshots of official standards, stored outside source control."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ProviderError, ReviewError, ValidationError
from .util import atomic_json, load_toml, sha256_bytes, sha256_file

MAX_STANDARD_BYTES = 24 * 1024 * 1024


def catalog(paths) -> dict[str, dict[str, Any]]:
    records = load_toml(paths.standards_catalog).get("standard", [])
    result = {record["id"]: record for record in records}
    if len(result) != len(records):
        raise ValidationError("standards catalog contains duplicate identifiers")
    return result


def _extension(media_type: str) -> str:
    return ".pdf" if media_type == "application/pdf" else ".html"


def _current_path(paths, identifier: str) -> Path:
    return paths.standards / identifier / "current.json"


def sync_standard(paths, identifier: str, *, timeout: float = 30) -> dict[str, Any]:
    records = catalog(paths)
    if identifier not in records:
        raise ReviewError(f"unknown standard: {identifier}")
    record = records[identifier]
    request = urllib.request.Request(
        record["url"], headers={"User-Agent": "ocsl-review/0.1 standards snapshot"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=min(max(timeout, 1), 60)) as response:
            body = response.read(MAX_STANDARD_BYTES + 1)
            final_url = response.geturl()
            received_type = response.headers.get_content_type()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"could not retrieve standard {identifier}: {exc}") from exc
    if len(body) > MAX_STANDARD_BYTES:
        raise ProviderError(f"standard {identifier} exceeds the snapshot size limit")
    digest = sha256_bytes(body)
    root = paths.standards / identifier / digest
    document = root / f"document{_extension(record['media_type'])}"
    metadata = root / "snapshot.json"
    if not document.exists():
        root.mkdir(parents=True, exist_ok=False)
        descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, document)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        atomic_json(
            metadata,
            {
                "schemaVersion": 1,
                "id": identifier,
                "title": record["title"],
                "requestedUrl": record["url"],
                "finalUrl": final_url,
                "declaredMediaType": record["media_type"],
                "receivedMediaType": received_type,
                "sha256": digest,
                "bytes": len(body),
                "retrievedAt": datetime.now(UTC).isoformat(),
                "document": str(document),
            },
            mode=0o444,
        )
        root.chmod(0o555)
    atomic_json(
        _current_path(paths, identifier),
        {"id": identifier, "sha256": digest, "snapshot": str(metadata)},
        mode=0o444,
    )
    return {"id": identifier, "sha256": digest, "path": str(document), "status": "snapshotted"}


def verify_standard(paths, identifier: str) -> dict[str, Any]:
    record = catalog(paths).get(identifier)
    if not record:
        raise ReviewError(f"unknown standard: {identifier}")
    current = _current_path(paths, identifier)
    problems = []
    if not current.is_file():
        return {"id": identifier, "ok": False, "problems": ["no current snapshot"], "path": None}
    value = json.loads(current.read_text(encoding="utf-8"))
    digest = value.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        problems.append("current pointer has invalid digest")
    root = paths.standards / identifier / digest
    document = root / f"document{_extension(record['media_type'])}"
    if not document.is_file():
        problems.append("snapshot document is missing")
    elif sha256_file(document) != digest:
        problems.append("snapshot content digest differs")
    if document.exists() and document.stat().st_mode & 0o222:
        problems.append("snapshot document is writable")
    return {
        "id": identifier,
        "ok": not problems,
        "problems": problems,
        "path": str(document),
        "sha256": digest,
    }


def standards_path(paths, identifier: str) -> Path:
    report = verify_standard(paths, identifier)
    if not report["ok"]:
        raise ValidationError(
            f"standard snapshot is not verified: {identifier}: {report['problems']}"
        )
    return Path(report["path"])


def cmd_list(args):
    return list(catalog(args.paths).values())


def cmd_sync(args):
    identifiers = (
        [args.standard] if getattr(args, "standard", None) else sorted(catalog(args.paths))
    )
    return {"standards": [sync_standard(args.paths, item) for item in identifiers]}


def cmd_verify(args):
    identifiers = (
        [args.standard] if getattr(args, "standard", None) else sorted(catalog(args.paths))
    )
    reports = [verify_standard(args.paths, item) for item in identifiers]
    result = {"ok": all(report["ok"] for report in reports), "standards": reports}
    if getattr(args, "strict", False) and not result["ok"]:
        raise ValidationError("one or more standards snapshots failed verification")
    return result


def cmd_path(args):
    return str(standards_path(args.paths, args.standard))
