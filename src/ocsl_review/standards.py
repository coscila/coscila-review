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


def _retrieve(url: str, *, timeout: float) -> tuple[bytes, str, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "ocsl-review/0.1 standards snapshot"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=min(max(timeout, 1), 60)) as response:
        body = response.read(MAX_STANDARD_BYTES + 1)
        return body, response.geturl(), response.headers.get_content_type()


def _validate_retrieval(
    record: dict[str, Any], body: bytes, received_type: str
) -> str:
    identifier = record["id"]
    if len(body) > MAX_STANDARD_BYTES:
        raise ProviderError(f"standard {identifier} exceeds the snapshot size limit")
    if received_type != record["media_type"]:
        raise ProviderError(
            f"standard {identifier} returned {received_type}, expected {record['media_type']}"
        )
    if record["media_type"] == "application/pdf" and not body.startswith(b"%PDF-"):
        raise ProviderError(f"standard {identifier} did not return a PDF document")
    digest = sha256_bytes(body)
    expected_digest = record.get("sha256")
    if expected_digest and digest != expected_digest:
        raise ProviderError(
            f"standard {identifier} differs from its catalogued SHA-256: "
            f"expected {expected_digest}, received {digest}"
        )
    return digest


def _fetch_validated(
    record: dict[str, Any], url: str, *, timeout: float
) -> tuple[bytes, str, str, str]:
    body, final_url, received_type = _retrieve(url, timeout=timeout)
    digest = _validate_retrieval(record, body, received_type)
    return body, final_url, received_type, digest


def sync_standard(paths, identifier: str, *, timeout: float = 30) -> dict[str, Any]:
    records = catalog(paths)
    if identifier not in records:
        raise ReviewError(f"unknown standard: {identifier}")
    record = records[identifier]
    retrieval_url = record["url"]
    used_archive = False
    primary_failure = None
    try:
        body, final_url, received_type, digest = _fetch_validated(
            record, retrieval_url, timeout=timeout
        )
    except (urllib.error.URLError, TimeoutError, ProviderError) as primary_error:
        retrieval_url = record.get("archive_url", "")
        if not retrieval_url:
            raise ProviderError(
                f"could not retrieve standard {identifier}: {primary_error}"
            ) from primary_error
        used_archive = True
        primary_failure = f"{type(primary_error).__name__}: {primary_error}"[:500]
        try:
            body, final_url, received_type, digest = _fetch_validated(
                record, retrieval_url, timeout=timeout
            )
        except (urllib.error.URLError, TimeoutError, ProviderError) as archive_error:
            raise ProviderError(
                f"could not retrieve standard {identifier} from its official or "
                f"digest-pinned archive source: official={primary_error}; archive={archive_error}"
            ) from archive_error
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
                "retrievalUrl": retrieval_url,
                "finalUrl": final_url,
                "archiveFallback": used_archive,
                "source": "archive" if used_archive else "official",
                "expectedSha256": record.get("sha256"),
                "primaryFailure": primary_failure,
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
    return {
        "id": identifier,
        "sha256": digest,
        "path": str(document),
        "source": "archive" if used_archive else "official",
        "status": "snapshotted",
    }


def verify_standard(paths, identifier: str) -> dict[str, Any]:
    record = catalog(paths).get(identifier)
    if not record:
        raise ReviewError(f"unknown standard: {identifier}")
    current = _current_path(paths, identifier)
    problems = []
    if current.is_symlink() or not current.is_file():
        return {"id": identifier, "ok": False, "problems": ["no current snapshot"], "path": None}
    try:
        value = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "id": identifier,
            "ok": False,
            "problems": [f"current pointer is invalid: {exc}"],
            "path": None,
        }
    digest = value.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        problems.append("current pointer has invalid digest")
    expected_digest = record.get("sha256")
    if expected_digest and digest != expected_digest:
        problems.append("current snapshot differs from the catalogued SHA-256")
    root = paths.standards / identifier / digest
    document = root / f"document{_extension(record['media_type'])}"
    metadata = root / "snapshot.json"
    if document.is_symlink() or not document.is_file():
        problems.append("snapshot document is missing")
    elif sha256_file(document) != digest:
        problems.append("snapshot content digest differs")
    else:
        if document.stat().st_mode & 0o222:
            problems.append("snapshot document is writable")
        if record["media_type"] == "application/pdf":
            with document.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    problems.append("snapshot document is not a PDF")
    if metadata.is_symlink() or not metadata.is_file():
        problems.append("snapshot metadata is missing")
    else:
        try:
            snapshot = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            problems.append("snapshot metadata is invalid")
        else:
            if snapshot.get("id") != identifier or snapshot.get("sha256") != digest:
                problems.append("snapshot metadata identity differs")
            if snapshot.get("requestedUrl") != record["url"]:
                problems.append("snapshot metadata official URL differs")
            if snapshot.get("declaredMediaType") != record["media_type"]:
                problems.append("snapshot metadata media type differs")
        if metadata.stat().st_mode & 0o222:
            problems.append("snapshot metadata is writable")
    if root.exists() and root.stat().st_mode & 0o222:
        problems.append("snapshot directory is writable")
    if current.stat().st_mode & 0o222:
        problems.append("current pointer is writable")
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
