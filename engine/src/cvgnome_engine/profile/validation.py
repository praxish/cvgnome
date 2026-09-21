# SPDX-License-Identifier: MPL-2.0
"""Bound and validate canonical-profile JSON before local persistence.

Adapted from earlier private work by the original author; only the local
profile behavior is included. See docs/PROVENANCE.md for publication scope.
"""

from __future__ import annotations

import json
import math
from typing import Any


MAX_PROFILE_BYTES = 2 * 1024 * 1024
MAX_PROFILE_DEPTH = 30
MAX_PROFILE_NODES = 50_000
MAX_CONTAINER_ITEMS = 5_000
MAX_STRING_CHARS = 100_000
MAX_KEY_CHARS = 256


class CanonicalProfileValidationError(ValueError):
    """A validation failure with a stable code for RPC consumers."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _invalid(code: str, message: str) -> CanonicalProfileValidationError:
    return CanonicalProfileValidationError(code, message)


def _validate_object_keys(profile_json: dict[Any, Any]) -> None:
    """Reject invalid mapping keys before JSON's permissive key coercion runs."""

    pending: list[object] = [profile_json]
    visited_containers: set[int] = set()
    while pending:
        item = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        identity = id(item)
        if identity in visited_containers:
            continue
        visited_containers.add(identity)
        if isinstance(item, dict):
            for key, value in item.items():
                if not isinstance(key, str):
                    raise _invalid(
                        "canonical_profile.key_not_string",
                        "Canonical profile object keys must be strings.",
                    )
                if len(key) > MAX_KEY_CHARS:
                    raise _invalid(
                        "canonical_profile.key_too_long",
                        f"Canonical profile object keys may not exceed {MAX_KEY_CHARS} characters.",
                    )
                pending.append(value)
        else:
            pending.extend(item)


def validate_canonical_profile(profile_json: object) -> None:
    """Validate a finite, bounded JSON object or raise a stable domain error.

    The function returns ``None`` on success and never mutates its input.
    """

    if not isinstance(profile_json, dict):
        raise _invalid(
            "canonical_profile.not_object",
            "Canonical profile JSON payload must be an object.",
        )

    _validate_object_keys(profile_json)
    try:
        encoded = json.dumps(
            profile_json,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _invalid(
            "canonical_profile.not_finite_json",
            "Canonical profile must contain only finite, acyclic JSON values.",
        ) from exc

    if len(encoded) > MAX_PROFILE_BYTES:
        raise _invalid(
            "canonical_profile.too_large",
            f"Canonical profile JSON may not exceed {MAX_PROFILE_BYTES // (1024 * 1024)} MiB.",
        )

    node_count = 0
    pending: list[tuple[Any, int]] = [(profile_json, 0)]
    while pending:
        item, depth = pending.pop()
        node_count += 1
        if node_count > MAX_PROFILE_NODES:
            raise _invalid(
                "canonical_profile.too_many_values",
                "Canonical profile JSON contains too many values "
                f"(maximum {MAX_PROFILE_NODES:,}).",
            )
        if depth > MAX_PROFILE_DEPTH:
            raise _invalid(
                "canonical_profile.too_deep",
                "Canonical profile JSON is nested too deeply "
                f"(maximum {MAX_PROFILE_DEPTH} levels).",
            )
        if isinstance(item, dict):
            if len(item) > MAX_CONTAINER_ITEMS:
                raise _invalid(
                    "canonical_profile.object_too_large",
                    "A canonical profile object contains too many fields.",
                )
            for key, value in item.items():
                if not isinstance(key, str):
                    raise _invalid(
                        "canonical_profile.key_not_string",
                        "Canonical profile object keys must be strings.",
                    )
                if len(key) > MAX_KEY_CHARS:
                    raise _invalid(
                        "canonical_profile.key_too_long",
                        f"Canonical profile object keys may not exceed {MAX_KEY_CHARS} characters.",
                    )
                pending.append((value, depth + 1))
            continue
        if isinstance(item, list):
            if len(item) > MAX_CONTAINER_ITEMS:
                raise _invalid(
                    "canonical_profile.list_too_large",
                    "A canonical profile list contains too many items.",
                )
            pending.extend((value, depth + 1) for value in item)
            continue
        if isinstance(item, str):
            if len(item) > MAX_STRING_CHARS:
                raise _invalid(
                    "canonical_profile.string_too_long",
                    "Individual canonical profile fields may not exceed "
                    f"{MAX_STRING_CHARS:,} characters.",
                )
            continue
        if item is None or isinstance(item, (bool, int)):
            continue
        if isinstance(item, float) and math.isfinite(item):
            continue
        raise _invalid(
            "canonical_profile.invalid_value",
            "Canonical profile must contain only valid JSON values.",
        )


def prepare_canonical_profile(
    profile_json: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate, compact, and revalidate a canonical profile.

    This is the integration entry point for an import or save workflow. The
    returned profile is a deep copy; callers can safely retain their input.
    """

    validate_canonical_profile(profile_json)
    from .compaction import compact_canonical_profile

    compacted, report = compact_canonical_profile(profile_json)
    validate_canonical_profile(compacted)
    return compacted, report
