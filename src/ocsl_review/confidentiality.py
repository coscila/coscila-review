"""Confidential-manuscript egress policy enforced before provider I/O."""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ConfidentialityError, ValidationError
from .util import is_within

MAX_EGRESS_PLAN_BYTES = 64 * 1024
# Bound of the legacy single-array ledger, which is read for verification but never written.
MAX_EGRESS_LEDGER_BYTES = 8 * 1024 * 1024
MAX_EGRESS_SEGMENT_BYTES = 8 * 1024 * 1024
MAX_EGRESS_RECORD_BYTES = 64 * 1024
# Appends reach the kernel before the request they authorize; fdatasync runs at most this
# often per ledger and process, at every segment seal and at interpreter exit.
EGRESS_SYNC_SECONDS = 1.0
EGRESS_GENESIS = "0" * 64
_LEGACY_LEDGER = "egress-events.json"
_ACTIVE_SEGMENT = "egress-events.jsonl"
_SEALED_SEGMENT = re.compile(r"^egress-events\.(\d{6})\.jsonl$")
_LEDGER_LOCK = "egress-events.lock"
_SYNC_GUARD = threading.Lock()
_LAST_SYNC: dict[str, float] = {}
_UNSYNCED: set[str] = set()
# Per process: the active segment's identity and size after this process's last append, with
# the chain head written then. A size change by another writer forces a tail read.
_HEADS: dict[str, tuple[int, int, int, int, str]] = {}

IDENTIFIER_FIELDS = {
    "doi",
    "pmid",
    "pmcid",
    "issn",
    "isbn",
    "orcid",
    "datasetDoi",
    "softwareDoi",
}
QUERY_FIELDS = {"query", "sanitizedQuery"}
SAFE_CONTROL_FIELDS = {
    "cursor",
    "format",
    "fromYear",
    "kind",
    "limit",
    "page",
    "provider",
    "resultType",
    "rows",
    "sort",
    "source",
    "toYear",
}
SENSITIVE_KEY_FRAGMENTS = {
    "abstract",
    "author",
    "caption",
    "figure",
    "manuscript",
    "note",
    "paragraph",
    "path",
    "review",
    "sentence",
    "table",
    "title",
    "upload",
}
IDENTIFIER_PATTERNS = {
    "doi": re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE),
    "datasetDoi": re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE),
    "softwareDoi": re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE),
    "pmid": re.compile(r"^\d{1,10}$"),
    "pmcid": re.compile(r"^PMC\d+$", re.IGNORECASE),
    "orcid": re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.IGNORECASE),
}


@dataclass(frozen=True)
class EgressPlan:
    confidential: bool = True
    allow_sanitized_queries: bool = True
    max_query_terms: int = 12
    approved_operations: frozenset[str] = field(default_factory=frozenset)
    approved_fields: frozenset[str] = field(default_factory=frozenset)
    shared_cache: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EgressPlan:
        known = {
            "confidential",
            "allowSanitizedQueries",
            "maxQueryTerms",
            "approvedOperations",
            "approvedFields",
            "sharedCache",
        }
        extra = set(value) - known
        if extra:
            raise ValidationError(f"unknown egress-plan fields: {sorted(extra)}")
        for field_name in ("confidential", "allowSanitizedQueries", "sharedCache"):
            if field_name in value and not isinstance(value[field_name], bool):
                raise ValidationError(f"egress-plan {field_name} must be boolean")
        maximum = value.get("maxQueryTerms", 12)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 30:
            raise ValidationError("egress-plan maxQueryTerms must be an integer from 1 to 30")
        approved: dict[str, frozenset[str]] = {}
        for source_name, target_name in (
            ("approvedOperations", "operations"),
            ("approvedFields", "fields"),
        ):
            items = value.get(source_name, [])
            if (
                not isinstance(items, list)
                or not all(isinstance(item, str) and item for item in items)
                or len(items) != len(set(items))
            ):
                raise ValidationError(f"egress-plan {source_name} must contain unique strings")
            approved[target_name] = frozenset(items)
        confidential = value.get("confidential", True)
        shared = value.get("sharedCache", False)
        if confidential and shared:
            raise ValidationError("confidential reviews cannot use a shared provider cache")
        return cls(
            confidential=confidential,
            allow_sanitized_queries=value.get("allowSanitizedQueries", True),
            max_query_terms=maximum,
            approved_operations=approved["operations"],
            approved_fields=approved["fields"],
            shared_cache=shared,
        )

    @classmethod
    def load(cls, path: Path | None) -> EgressPlan:
        plan, _ = cls.load_with_digest(path)
        return plan

    @classmethod
    def load_with_digest(cls, path: Path | None) -> tuple[EgressPlan, str | None]:
        """Read, hash, and parse one bounded plan from the same file descriptor."""

        if path is None:
            return cls(), None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise ValidationError(f"cannot open egress plan: {path}: {exc}") from exc
        try:
            with os.fdopen(descriptor, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValidationError(f"egress plan is not a regular file: {path}")
                body = handle.read(MAX_EGRESS_PLAN_BYTES + 1)
        except Exception:
            # os.fdopen owns and closes the descriptor once constructed.  If
            # construction itself failed, close the raw descriptor here.
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if len(body) > MAX_EGRESS_PLAN_BYTES:
            raise ValidationError(f"egress plan exceeds {MAX_EGRESS_PLAN_BYTES} bytes: {path}")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"egress plan is not valid UTF-8 JSON: {path}") from exc
        if not isinstance(value, Mapping):
            raise ValidationError("egress plan must be one JSON object")
        return cls.from_mapping(value), hashlib.sha256(body).hexdigest()


def _lexical_absolute(value: str | os.PathLike[str], *, label: str) -> Path:
    candidate = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    if not Path(value).expanduser().is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    return candidate


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    for candidate in reversed((path, *path.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(f"{label} contains a symlink component: {candidate}")


def _validated_scholarly_workspace(
    paths: Any,
    workspace_value: str | os.PathLike[str],
    root_value: str | os.PathLike[str],
) -> tuple[Path, Path]:
    workspace_lexical = _lexical_absolute(workspace_value, label="scholarly workspace")
    root_lexical = _lexical_absolute(root_value, label="LAB_WORKSPACE_ROOT")
    _assert_no_symlink_components(workspace_lexical, label="scholarly workspace")
    _assert_no_symlink_components(root_lexical, label="LAB_WORKSPACE_ROOT")
    if not workspace_lexical.is_dir():
        raise ValidationError(f"scholarly workspace does not exist: {workspace_lexical}")
    if not root_lexical.is_dir():
        raise ValidationError(f"LAB_WORKSPACE_ROOT does not exist: {root_lexical}")
    workspace = workspace_lexical.resolve()
    workspace_root = root_lexical.resolve()
    if workspace == workspace_root:
        raise ValidationError("LAB_WORKSPACE_ROOT itself is not a scholarly chat workspace")
    if workspace == Path(workspace.anchor):
        raise ValidationError("the filesystem root cannot be a scholarly workspace")
    if not is_within(workspace, workspace_root):
        raise ValidationError("scholarly workspace is outside LAB_WORKSPACE_ROOT")

    from .config import protected_workspace_roots

    for label, protected in protected_workspace_roots(paths):
        if is_within(workspace, protected) or is_within(protected, workspace):
            raise ValidationError(f"scholarly workspace overlaps protected {label}: {protected}")
    return workspace, workspace_root


def _fixed_workspace_plan(workspace: Path) -> Path | None:
    plan = workspace / ".ocsl-review" / "egress-plan.json"
    _assert_no_symlink_components(plan, label="fixed workspace egress plan")
    audit_root = plan.parent
    if audit_root.exists() and not audit_root.is_dir():
        raise ValidationError(f"workspace review state is not a directory: {audit_root}")
    if not plan.exists():
        return None
    if not plan.is_file():
        raise ValidationError(f"fixed workspace egress plan is not a regular file: {plan}")
    return plan


def _is_narrowing_only(plan: EgressPlan) -> bool:
    default = EgressPlan()
    sanitized_queries_are_not_broader = (
        plan.allow_sanitized_queries is False
        or plan.allow_sanitized_queries == default.allow_sanitized_queries
    )
    # The empty default operation set means that all declared public operations
    # are eligible.  A non-empty plan set is therefore an allowlist restriction,
    # while another empty set preserves the default.
    return (
        plan.confidential is True
        and plan.shared_cache is False
        and not plan.approved_fields
        and plan.max_query_terms <= default.max_query_terms
        and sanitized_queries_are_not_broader
    )


def _load_context_plan(
    path: Path | None,
    *,
    expected_sha256: str | None,
    auto_loaded: bool,
) -> EgressPlan:
    plan, actual_sha256 = EgressPlan.load_with_digest(path)
    if path is None:
        return plan
    narrowing_only = _is_narrowing_only(plan)
    if auto_loaded and not narrowing_only:
        raise ValidationError(
            "the fixed workspace egress plan may only narrow the confidential default"
        )
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValidationError("OCSL_REVIEW_EGRESS_PLAN_SHA256 must be lowercase SHA-256")
        assert actual_sha256 is not None
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValidationError("egress plan SHA-256 does not match operator configuration")
    elif not narrowing_only:
        raise ValidationError(
            "a broader explicit egress plan requires OCSL_REVIEW_EGRESS_PLAN_SHA256"
        )
    return plan


def load_scholarly_context(
    paths: Any,
    environment: Mapping[str, str],
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> tuple[EgressPlan, Path | None]:
    """Validate the deployment context before constructing a scholarly client.

    When the process runs from a strict descendant of LAB_WORKSPACE_ROOT, that
    exact working directory is the implicit chat workspace.  An outside CWD
    retains the documented confidential diagnostic default.  An explicit or
    derived workspace cannot use symlinks or broader paths to reach another
    filesystem domain.
    """

    plan_value = environment.get("OCSL_REVIEW_EGRESS_PLAN")
    plan_sha256 = environment.get("OCSL_REVIEW_EGRESS_PLAN_SHA256")
    workspace_value = environment.get("OCSL_REVIEW_WORKSPACE")
    if plan_value and not workspace_value:
        raise ValidationError("an explicit egress plan requires OCSL_REVIEW_WORKSPACE")
    if plan_sha256 and not plan_value:
        raise ValidationError(
            "OCSL_REVIEW_EGRESS_PLAN_SHA256 requires an explicit OCSL_REVIEW_EGRESS_PLAN"
        )
    root_value = environment.get("LAB_WORKSPACE_ROOT")
    if workspace_value:
        if not root_value:
            raise ValidationError(
                "OCSL_REVIEW_WORKSPACE requires the deployment LAB_WORKSPACE_ROOT boundary"
            )
        workspace, _ = _validated_scholarly_workspace(paths, workspace_value, root_value)
    elif root_value:
        cwd_value = Path.cwd() if cwd is None else cwd
        cwd_lexical = _lexical_absolute(cwd_value, label="scholarly process cwd")
        root_lexical = _lexical_absolute(root_value, label="LAB_WORKSPACE_ROOT")
        _assert_no_symlink_components(cwd_lexical, label="scholarly process cwd")
        _assert_no_symlink_components(root_lexical, label="LAB_WORKSPACE_ROOT")
        if not root_lexical.is_dir():
            raise ValidationError(f"LAB_WORKSPACE_ROOT does not exist: {root_lexical}")
        cwd_resolved = cwd_lexical.resolve()
        root_resolved = root_lexical.resolve()
        if cwd_resolved == root_resolved:
            raise ValidationError("LAB_WORKSPACE_ROOT itself is not a scholarly chat workspace")
        if not is_within(cwd_resolved, root_resolved):
            return EgressPlan(), None
        workspace, _ = _validated_scholarly_workspace(paths, cwd_lexical, root_lexical)
    else:
        return EgressPlan(), None

    plan_path: Path | None = None
    if plan_value:
        plan_lexical = _lexical_absolute(plan_value, label="egress plan")
        _assert_no_symlink_components(plan_lexical, label="egress plan")
        if not plan_lexical.is_file():
            raise ValidationError(f"egress plan does not exist: {plan_lexical}")
        plan_path = plan_lexical.resolve()
        if not is_within(plan_path, workspace):
            raise ValidationError("egress plan must be inside the exact scholarly workspace")
    else:
        plan_path = _fixed_workspace_plan(workspace)
    plan = _load_context_plan(
        plan_path,
        expected_sha256=plan_sha256,
        auto_loaded=not bool(plan_value),
    )
    return plan, workspace


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    flattened: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        if not value and prefix:
            flattened.append((prefix, value))
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten(child, name))
    elif isinstance(value, list):
        if not value and prefix:
            flattened.append((prefix, value))
        for index, child in enumerate(value):
            flattened.extend(_flatten(child, f"{prefix}[{index}]"))
    else:
        flattened.append((prefix, value))
    return flattened


def _query_terms(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", value)


def _validate_sanitized_query(value: str, maximum: int) -> None:
    if "\n" in value or "\r" in value or len(value) > 240:
        raise ConfidentialityError("search query resembles manuscript prose")
    terms = _query_terms(value)
    if not terms or len(terms) > maximum:
        raise ConfidentialityError(f"search query must contain 1-{maximum} minimized terms")
    if any(len(term) > 50 for term in terms):
        raise ConfidentialityError("search query contains an unusually identifying token")
    if re.search(r"[.!?]\s+[A-Z]", value) or re.search(r"[\"“”].{20,}[\"“”]", value):
        raise ConfidentialityError(
            "quoted or sentence-like manuscript text is not a sanitized query"
        )


def authorize_egress(
    *,
    provider: str,
    operation: str,
    arguments: Mapping[str, Any],
    plan: EgressPlan,
) -> dict[str, Any]:
    """Reject confidential content before any network client is constructed."""

    if plan.approved_operations and operation not in plan.approved_operations:
        raise ConfidentialityError(f"operation is outside the approved egress plan: {operation}")
    if not plan.confidential:
        return _audit_event(provider, operation, arguments, decision="allowed-nonconfidential")

    allowed = IDENTIFIER_FIELDS | SAFE_CONTROL_FIELDS | plan.approved_fields
    if plan.allow_sanitized_queries:
        allowed |= QUERY_FIELDS
    for path, value in _flatten(arguments):
        leaf = re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1])
        lowered_path = path.lower()
        if any(fragment in lowered_path for fragment in SENSITIVE_KEY_FRAGMENTS):
            raise ConfidentialityError(f"confidential field cannot leave the workspace: {path}")
        if leaf not in allowed and value not in (None, "", False):
            raise ConfidentialityError(f"field is not approved for confidential egress: {path}")
        if value in (None, "", False):
            continue
        if leaf in QUERY_FIELDS:
            if not isinstance(value, str):
                raise ConfidentialityError(f"query field must be text: {path}")
            _validate_sanitized_query(value, plan.max_query_terms)
        if leaf in IDENTIFIER_PATTERNS and (
            not isinstance(value, str) or not IDENTIFIER_PATTERNS[leaf].match(value.strip())
        ):
            raise ConfidentialityError(f"invalid {leaf} identifier")
    return _audit_event(provider, operation, arguments, decision="allowed-minimized")


def _audit_event(
    provider: str, operation: str, arguments: Mapping[str, Any], *, decision: str
) -> dict[str, Any]:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "provider": provider,
        "operation": operation,
        "decision": decision,
        "argumentNames": sorted(arguments),
        "payloadSha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _assert_no_symlink_prefixes(text: str, *, label: str) -> None:
    """String form of _assert_no_symlink_components for the per-event ledger path."""

    index = 0
    while True:
        index = text.find(os.sep, index + 1)
        prefix = text if index < 0 else text[:index]
        try:
            if stat.S_ISLNK(os.lstat(prefix).st_mode):
                raise ValidationError(f"{label} contains a symlink component: {prefix}")
        except FileNotFoundError:
            pass
        if index < 0:
            return


def _ledger_directory(workspace: Path, *, create: bool) -> int | None:
    """Open the non-symlinked, owner-only ledger directory of one workspace."""

    root = os.fspath(_lexical_absolute(workspace, label="egress-ledger workspace"))
    _assert_no_symlink_prefixes(root, label="egress-ledger workspace")
    if not os.path.isdir(root):
        raise ValidationError(f"egress-ledger workspace does not exist: {root}")
    audit_root = os.path.join(root, ".ocsl-review")
    try:
        existing = os.lstat(audit_root)
    except FileNotFoundError:
        existing = None
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise ValidationError(f"egress-ledger directory contains a symlink component: {audit_root}")
    if existing is None:
        if not create:
            return None
        try:
            os.mkdir(audit_root, 0o700)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(audit_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ValidationError(f"egress-ledger directory contains a symlink component: {audit_root}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(f"egress-ledger directory escapes its workspace: {audit_root}")
        if create and stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _ledger_canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_digest(event: Mapping[str, Any], index: int, previous: str) -> str:
    return hashlib.sha256(_ledger_canonical({"event": event, "index": index, "prev": previous}).encode()).hexdigest()


def _encode_record(event: Mapping[str, Any], index: int, previous: str) -> tuple[bytes, str]:
    """The canonical record line and its digest; equal to _ledger_canonical of the record."""

    body = '{"event":' + _ledger_canonical(event) + ',"index":' + str(index) + ',"prev":"' + previous + '"'
    digest = hashlib.sha256((body + "}").encode("utf-8")).hexdigest()
    line = (body + ',"sha256":"' + digest + '"}\n').encode("utf-8")
    if len(line) > MAX_EGRESS_RECORD_BYTES:
        raise ValidationError("egress event exceeds the ledger record bound")
    return line, digest


def _decode_record(line: bytes, where: str) -> dict[str, Any]:
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"egress ledger record is not valid UTF-8 JSON: {where}") from exc
    if (
        not isinstance(record, dict)
        or set(record) != {"event", "index", "prev", "sha256"}
        or not isinstance(record["event"], dict)
        or not isinstance(record["index"], int)
        or isinstance(record["index"], bool)
        or record["index"] < 0
        or not isinstance(record["prev"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["prev"])
        or record["sha256"] != _record_digest(record["event"], record["index"], record["prev"])
        or line != _ledger_canonical(record).encode("utf-8")
    ):
        raise ValidationError(f"egress ledger record fails its hash chain: {where}")
    return record


def _last_record(descriptor: int, size: int, where: str) -> dict[str, Any] | None:
    """The final record of a segment, read from its tail only."""

    if size == 0:
        return None
    window = min(size, 4096)
    while True:
        data = os.pread(descriptor, window, size - window)
        if not data.endswith(b"\n"):
            raise ValidationError(
                f"egress ledger ends with an incomplete record and is never rewritten automatically: {where}"
            )
        start = data.rfind(b"\n", 0, len(data) - 1)
        if start >= 0 or window == size:
            return _decode_record(data[start + 1 : -1], where)
        if window > MAX_EGRESS_RECORD_BYTES:
            raise ValidationError(f"egress ledger record exceeds its bound: {where}")
        window = min(size, window * 8)


def _sealed_segments(directory: int) -> list[tuple[int, str]]:
    found = []
    for name in os.listdir(directory):
        match = _SEALED_SEGMENT.match(name)
        if match:
            found.append((int(match.group(1)), name))
    return sorted(found)


def _read_file(directory: int, name: str, bound: int) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValidationError(f"egress ledger is not a bounded regular file: {name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > bound:
            raise ValidationError(f"egress ledger is not a bounded regular file: {name}")
        chunks, remaining = [], metadata.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > bound:
            raise ValidationError(f"egress ledger is not a bounded regular file: {name}")
        return body
    finally:
        os.close(descriptor)


def _legacy_events(directory: int) -> tuple[list[dict[str, Any]], str]:
    """Events of a legacy JSON array ledger and the digest the hash chain starts from."""

    body = _read_file(directory, _LEGACY_LEDGER, MAX_EGRESS_LEDGER_BYTES)
    if body is None:
        return [], EGRESS_GENESIS
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"egress ledger is not valid UTF-8 JSON: {_LEGACY_LEDGER}") from exc
    if not isinstance(loaded, list) or not all(isinstance(item, dict) for item in loaded):
        raise ValidationError(f"egress ledger must contain one JSON event array: {_LEGACY_LEDGER}")
    return loaded, hashlib.sha256(body).hexdigest()


def _chain_head(directory: int) -> tuple[int, str]:
    """Index and digest the next record follows when the active segment is empty."""

    sealed = _sealed_segments(directory)
    if not sealed:
        return -1, _legacy_events(directory)[1]
    name = sealed[-1][1]
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        record = _last_record(descriptor, os.fstat(descriptor).st_size, name)
    finally:
        os.close(descriptor)
    if record is None:
        raise ValidationError(f"sealed egress ledger segment is empty: {name}")
    return record["index"], record["sha256"]


def _open_active(directory: int) -> int:
    for _ in range(2):
        descriptor = os.open(
            _ACTIVE_SEGMENT,
            os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            return descriptor
        os.close(descriptor)
        sealed = _sealed_segments(directory)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 2 and sealed:
            latest = os.stat(sealed[-1][1], dir_fd=directory, follow_symlinks=False)
            if (latest.st_dev, latest.st_ino) == (metadata.st_dev, metadata.st_ino):
                # Finish a seal that stopped between linking and unlinking the active name.
                os.unlink(_ACTIVE_SEGMENT, dir_fd=directory)
                continue
        break
    raise ValidationError("egress ledger segment is not a private regular file")


def _seal(directory: int, active: int) -> None:
    """Close the active segment under the next sequence number; it is never written again."""

    os.fsync(active)
    sealed = _sealed_segments(directory)
    name = f"egress-events.{(sealed[-1][0] + 1) if sealed else 1:06d}.jsonl"
    os.link(_ACTIVE_SEGMENT, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    os.fchmod(active, 0o400)
    os.unlink(_ACTIVE_SEGMENT, dir_fd=directory)
    os.fsync(directory)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _datasync(descriptor: int) -> None:
    getattr(os, "fdatasync", os.fsync)(descriptor)


def flush_egress_ledgers() -> None:
    """Flush segments that received events since their last sync in this process."""

    with _SYNC_GUARD:
        pending = sorted(_UNSYNCED)
        _UNSYNCED.clear()
    for path in pending:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            _datasync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


atexit.register(flush_egress_ledgers)


def append_egress_event(workspace: Path, event: Mapping[str, Any]) -> None:
    """Persist only hashes and field names; never persist confidential payloads.

    Each event becomes one line of an append-only JSONL segment. A line carries the
    event, its index and the SHA-256 of the previous line, so editing, reordering or
    removing an earlier event breaks the chain. A full segment is sealed read-only
    and the chain continues in a new one; a legacy JSON array is never rewritten and
    anchors the chain by its digest.
    """

    event = dict(event)
    key = os.path.join(_lexical_absolute(workspace, label="egress-ledger workspace"), ".ocsl-review", _ACTIVE_SEGMENT)
    directory = _ledger_directory(workspace, create=True)
    try:
        lock = os.open(_LEDGER_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            active = _open_active(directory)
            try:
                metadata = os.fstat(active)
                cached = _HEADS.get(key)
                if cached and cached[:3] == (metadata.st_dev, metadata.st_ino, metadata.st_size):
                    index, previous = cached[3], cached[4]
                else:
                    last = _last_record(active, metadata.st_size, _ACTIVE_SEGMENT)
                    index, previous = (last["index"], last["sha256"]) if last else _chain_head(directory)
                line, digest = _encode_record(event, index + 1, previous)
                if metadata.st_size and metadata.st_size + len(line) > MAX_EGRESS_SEGMENT_BYTES:
                    _seal(directory, active)
                    os.close(active)
                    active = _open_active(directory)
                _write_all(active, line)
                metadata = os.fstat(active)
                _HEADS[key] = (metadata.st_dev, metadata.st_ino, metadata.st_size, index + 1, digest)
                now = time.monotonic()
                with _SYNC_GUARD:
                    due = now - _LAST_SYNC.get(key, float("-inf")) >= EGRESS_SYNC_SECONDS
                    if due:
                        _LAST_SYNC[key] = now
                        _UNSYNCED.discard(key)
                    else:
                        _UNSYNCED.add(key)
                if due:
                    _datasync(active)
            finally:
                os.close(active)
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def _load_egress_ledger(workspace: Path) -> dict[str, Any]:
    directory = _ledger_directory(workspace, create=False)
    if directory is None:
        return {"events": [], "legacyEvents": 0, "chainedEvents": 0, "segments": 0, "headSha256": EGRESS_GENESIS}
    try:
        try:
            lock = os.open(_LEDGER_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            lock = None
        try:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_SH)
            legacy, previous = _legacy_events(directory)
            sealed = _sealed_segments(directory)
            if [number for number, _ in sealed] != list(range(1, len(sealed) + 1)):
                raise ValidationError("egress ledger segments are not numbered contiguously")
            names = [name for _, name in sealed]
            if _ACTIVE_SEGMENT in os.listdir(directory):
                names.append(_ACTIVE_SEGMENT)
            events, index = list(legacy), 0
            for name in names:
                body = _read_file(directory, name, MAX_EGRESS_SEGMENT_BYTES + MAX_EGRESS_RECORD_BYTES) or b""
                if body and not body.endswith(b"\n"):
                    raise ValidationError(f"egress ledger ends with an incomplete record: {name}")
                if not body and name != _ACTIVE_SEGMENT:
                    raise ValidationError(f"sealed egress ledger segment is empty: {name}")
                for number, line in enumerate(body.split(b"\n")[:-1], start=1):
                    record = _decode_record(line, f"{name}:{number}")
                    if record["index"] != index or record["prev"] != previous:
                        raise ValidationError(f"egress ledger hash chain is broken at {name}:{number}")
                    events.append(record["event"])
                    previous, index = record["sha256"], index + 1
            return {"events": events, "legacyEvents": len(legacy), "chainedEvents": index,
                    "segments": len(names), "headSha256": previous}
        finally:
            if lock is not None:
                os.close(lock)
    finally:
        os.close(directory)


def read_egress_events(workspace: Path) -> list[dict[str, Any]]:
    """All recorded events in order, legacy array first, after verifying the hash chain."""

    return _load_egress_ledger(workspace)["events"]


def verify_egress_ledger(workspace: Path) -> dict[str, Any]:
    """Verify every segment and link; return counts and the head digest."""

    ledger = _load_egress_ledger(workspace)
    return {key: value for key, value in ledger.items() if key != "events"}
