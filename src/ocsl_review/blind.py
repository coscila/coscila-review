# SPDX-License-Identifier: Apache-2.0
"""Blind studies: a study that must not see judgments of other runs or their libraries.

The lab marks a blind study with ``OCSL_REVIEW_BLIND=1`` in the environment or with the marker file
``.ocsl/blind-study.json`` it writes into a blind chat's workspace before every turn (found from the current directory
upwards). Then
every way of bringing judgments of other runs into a study refuses: ``corpus import-legacy`` (historical scores, legacy
census ledgers, legacy evidence), ``corpus import-snapshot`` (a whole library), and ``store attach`` of any
store that is not a label-free ``literature-evidence@2`` projection. The marker is a refusal, never a permission:
without it the commands behave as documented.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import ValidationError

BLIND_ENV = "OCSL_REVIEW_BLIND"
PROJECTION_FORMAT = "literature-evidence@2"


MARKER = Path(".ocsl") / "blind-study.json"


def _marker_above(start: Path) -> bool:
    for directory in (start, *start.parents):
        candidate = directory / MARKER
        try:
            if candidate.is_file() and not candidate.is_symlink():
                value = json.loads(candidate.read_text(encoding="utf-8"))
                return isinstance(value, dict) and value.get("blind") is True
        except (OSError, ValueError):
            return True  # an unreadable marker is no permission: treat the study as blind
    return False


def blind_study(env: dict[str, str] | None = None, *, cwd: Path | None = None) -> bool:
    value = (env if env is not None else os.environ).get(BLIND_ENV, "")
    if str(value).strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        start = (cwd or Path.cwd()).resolve()
    except OSError:
        return False
    return _marker_above(start)


def refuse_in_blind_study(action: str, env: dict[str, str] | None = None) -> None:
    if blind_study(env):
        raise ValidationError(
            f"{action} is refused in a blind study ({BLIND_ENV} is set): a blind study never imports judgments "
            "of other runs or their libraries; it reads a label-free literature-evidence@2 projection as a byte cache"
        )


def require_projection_in_blind_study(store: Path, env: dict[str, str] | None = None) -> None:
    """A blind study attaches only label-free projections: the store's manifest must declare literature-evidence@2."""
    if not blind_study(env):
        return
    manifest = Path(store) / "manifest.json"
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8")).get("store")
    except (OSError, ValueError, AttributeError):
        declared = None
    if not isinstance(declared, dict) or declared.get("id") != PROJECTION_FORMAT:
        raise ValidationError(
            f"a blind study ({BLIND_ENV} is set) attaches only a label-free {PROJECTION_FORMAT} projection; "
            f"{store} is not one"
        )
