# SPDX-License-Identifier: Apache-2.0
"""Compare pinned scientific worker settings independently of live capacity."""
from __future__ import annotations

from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


def is_positive_safe_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_SAFE_INTEGER


def matches_pinned_worker_configuration(spec: dict[str, Any], current: dict[str, Any]) -> bool:
    from .literature import digest

    pinned = spec.get("workerConfig")
    # Keep validating the original snapshot, including the capacity recorded at
    # preparation. Never rewrite an old run or accept an invalid stored hash.
    if not isinstance(pinned, dict) or digest(pinned) != spec.get("workerConfigSha256"):
        return False
    for configuration in (pinned, current):
        if ("bulkConcurrencyLimit" in configuration
                and not is_positive_safe_integer(configuration["bulkConcurrencyLimit"])):
            return False
    # This one field is operator capacity, not the scientific instrument or a
    # disclosure permission. Every other public setting remains exact.
    return digest({key: value for key, value in pinned.items() if key != "bulkConcurrencyLimit"}) == digest(
        {key: value for key, value in current.items() if key != "bulkConcurrencyLimit"})
