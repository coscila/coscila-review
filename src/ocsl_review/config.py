"""Resolve immutable source roots and mutable deployment roots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ReviewError


def review_home(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("OCSL_REVIEW_HOME")
    if value:
        root = Path(value).expanduser().resolve()
        if not (root / "pyproject.toml").is_file():
            raise ReviewError(f"OCSL_REVIEW_HOME is not a companion checkout: {root}")
        return root
    for parent in Path(__file__).resolve().parents:
        if (parent / "bin" / "ocsl-review").is_file():
            return parent
    raise ReviewError("cannot locate the review companion; set OCSL_REVIEW_HOME")


def state_root(explicit: str | None = None, *, required: bool = True) -> Path | None:
    value = explicit or os.environ.get("OCSL_REVIEW_STATE")
    if value:
        return Path(value).expanduser().resolve()
    if required:
        raise ReviewError("OCSL_REVIEW_STATE is required for this command")
    return None


def science_lab_repo(explicit: str | None, home: Path) -> Path | None:
    value = explicit or os.environ.get("OCSL_REPO")
    if value:
        return Path(value).expanduser().resolve()
    sibling = home.parent / "oc-science-lab"
    return sibling if sibling.is_dir() else None


@dataclass(frozen=True)
class Paths:
    home: Path
    state: Path | None
    lab_repo: Path | None

    def need_state(self) -> Path:
        if self.state is None:
            raise ReviewError("this operation requires OCSL_REVIEW_STATE")
        return self.state

    @property
    def environment_catalog(self) -> Path:
        return self.home / "environments" / "catalog.toml"

    @property
    def skill_catalog(self) -> Path:
        return self.home / "skills" / "catalog.toml"

    @property
    def mcp_catalog(self) -> Path:
        return self.home / "mcp" / "catalog.toml"

    @property
    def repository_catalog(self) -> Path:
        return self.home / "repos" / "catalog.toml"

    @property
    def repository_lock(self) -> Path:
        return self.home / "repos" / "repositories.lock.json"

    @property
    def standards_catalog(self) -> Path:
        return self.home / "standards" / "catalog.toml"

    @property
    def templates(self) -> Path:
        return self.need_state() / "environment-templates"

    @property
    def environment_locks(self) -> Path:
        return self.need_state() / "environment-locks"

    @property
    def repositories(self) -> Path:
        return self.need_state() / "repository-store"

    @property
    def standards(self) -> Path:
        return self.need_state() / "standards-store"

    @property
    def integration(self) -> Path:
        return self.need_state() / "integration"

    @property
    def cache(self) -> Path:
        return self.need_state() / "cache"


def protected_source_roots(paths: Paths) -> tuple[tuple[str, Path], ...]:
    """Return every source checkout that review-time state must not overlap."""

    candidates: list[tuple[str, Path]] = [("companion source", paths.home)]
    if paths.lab_repo is not None:
        candidates.append(("OC Science Lab source", paths.lab_repo))
    bio = paths.home.parent / "oc-science-lab-bio"
    if bio.is_dir():
        candidates.append(("OC Science Lab Bio source", bio))

    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for label, candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append((label, resolved))
    return tuple(result)


def protected_workspace_roots(paths: Paths) -> tuple[tuple[str, Path], ...]:
    """Return source and operator-state domains forbidden to review workspaces."""

    candidates = list(protected_source_roots(paths))
    if paths.state is not None:
        candidates.append(("operator state", paths.state.resolve()))

    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for label, candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append((label, candidate))
    return tuple(result)


def resolve_paths(
    *,
    home: str | None = None,
    state: str | None = None,
    lab_repo: str | None = None,
    state_required: bool = True,
) -> Paths:
    source = review_home(home)
    return Paths(
        home=source,
        state=state_root(state, required=state_required),
        lab_repo=science_lab_repo(lab_repo, source),
    )


def conda_install_root() -> Path | None:
    value = os.environ.get("LAB_CONDA_INSTALL_ROOT")
    return Path(value).resolve() if value else None


def chat_conda_root() -> Path | None:
    value = os.environ.get("LAB_CONDA_ROOT")
    return Path(value).resolve() if value else None


def conda_executable() -> Path | None:
    root = conda_install_root()
    if root:
        for name in ("micromamba", "mamba", "conda"):
            candidate = root / "bin" / name
            if candidate.is_file():
                return candidate
    return None
