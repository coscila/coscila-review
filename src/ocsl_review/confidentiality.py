"""Confidential-manuscript egress policy enforced before provider I/O."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ConfidentialityError, ValidationError
from .util import atomic_json, is_within

MAX_EGRESS_PLAN_BYTES = 64 * 1024
MAX_EGRESS_LEDGER_BYTES = 8 * 1024 * 1024

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
        if path is None:
            return cls()
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise ValidationError(f"cannot open egress plan: {path}: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValidationError(f"egress plan is not a regular file: {path}")
            if metadata.st_size > MAX_EGRESS_PLAN_BYTES:
                raise ValidationError(
                    f"egress plan exceeds {MAX_EGRESS_PLAN_BYTES} bytes: {path}"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                try:
                    value = json.load(handle)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(f"egress plan is not valid UTF-8 JSON: {path}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, Mapping):
            raise ValidationError("egress plan must be one JSON object")
        return cls.from_mapping(value)


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
    workspace_value = environment.get("OCSL_REVIEW_WORKSPACE")
    if plan_value and not workspace_value:
        raise ValidationError("an explicit egress plan requires OCSL_REVIEW_WORKSPACE")
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
    return EgressPlan.load(plan_path), workspace


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    flattened: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten(child, name))
    elif isinstance(value, list):
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
        lowered = leaf.lower()
        if (
            any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)
            and leaf not in plan.approved_fields
        ):
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


def append_egress_event(workspace: Path, event: Mapping[str, Any]) -> None:
    """Persist only hashes and field names; never persist confidential payloads."""

    workspace = _lexical_absolute(workspace, label="egress-ledger workspace")
    _assert_no_symlink_components(workspace, label="egress-ledger workspace")
    if not workspace.is_dir():
        raise ValidationError(f"egress-ledger workspace does not exist: {workspace}")
    audit_root = workspace / ".ocsl-review"
    _assert_no_symlink_components(audit_root, label="egress-ledger directory")
    audit_root.mkdir(mode=0o700, exist_ok=True)
    _assert_no_symlink_components(audit_root, label="egress-ledger directory")
    if not audit_root.is_dir() or not is_within(audit_root, workspace):
        raise ValidationError(f"egress-ledger directory escapes its workspace: {audit_root}")
    os.chmod(audit_root, 0o700)
    path = audit_root / "egress-events.json"
    _assert_no_symlink_components(path, label="egress ledger")
    events: list[dict[str, Any]] = []
    if path.exists():
        if not path.is_file() or path.stat().st_size > MAX_EGRESS_LEDGER_BYTES:
            raise ValidationError(f"egress ledger is not a bounded regular file: {path}")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"egress ledger is not valid UTF-8 JSON: {path}") from exc
        if not isinstance(loaded, list) or not all(isinstance(item, dict) for item in loaded):
            raise ValidationError(f"egress ledger must contain one JSON event array: {path}")
        events = loaded
    events.append(dict(event))
    atomic_json(path, events, mode=0o600)
