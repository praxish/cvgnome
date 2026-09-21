# SPDX-License-Identifier: MPL-2.0
"""Remove public-facing attribution prose without touching internal metadata.

Adapted from earlier private work by the original author; only the local
profile behavior is included. See docs/PROVENANCE.md for publication scope.
"""

from __future__ import annotations

import copy
import re
from typing import Any


_TRAILING_PARENTHETICAL_RE = re.compile(
    r"(?P<leading>\s*)\((?P<annotation>[^()]*)\)(?P<punctuation>[.!?]?)\s*$"
)
_PUBLIC_PROVENANCE_ANNOTATION_RE = re.compile(
    r"(?:"
    r"\b(?:as\s+)?(?:described|reported|stated|noted|confirmed|indicated)\s+by\s+"
    r"(?:the\s+)?(?:candidate|applicant)\s*$"
    r"|^\s*(?:candidate|applicant)[\s-]+reported\s+(?:from|based\s+on|per)\b"
    r"|^\s*(?:candidate|applicant)[\s-]+reported\s*$"
    r"|^\s*(?:candidate|applicant)\s+"
    r"(?:reports?|reported|states?|stated|notes?|noted|describes?|described|"
    r"indicates?|indicated|confirms?|confirmed)\b"
    r"|\b(?:according\s+to|per)\s+(?:the\s+)?(?:candidate|applicant)\s*$"
    r"|\b(?:referenced|mentioned|listed|included|shown|reported|documented)\s+"
    r"(?:in|on)\s+(?:the\s+)?"
    r"(?:(?:source|provided|uploaded|supporting|resume|cv|draft)\s+)?"
    r"(?:materials?|documents?|resume|cv|draft|profile)\s*$"
    r"|\b(?:from|in)\s+(?:the\s+)?source\s+(?:materials?|documents?)\s*$"
    r"|^\s*source\s*\.?\s*$"
    r")",
    re.IGNORECASE,
)
_TERMINAL_PUBLIC_PROVENANCE_RE = re.compile(
    r"\s*(?:[,;:–—-]\s*)?(?:"
    r"(?:(?:was|were)\s+)?(?:as\s+)?"
    r"\b(?:described|reported|stated|noted|confirmed|indicated)\s+by\s+"
    r"(?:the\s+)?(?:candidate|applicant)"
    r"|\b(?:according\s+to|per)\b\s+(?:the\s+)?(?:candidate|applicant)"
    r"|(?:(?:was|were)\s+)?"
    r"\b(?:referenced|mentioned|listed|included|shown|reported|documented)\s+"
    r"(?:in|on)\s+(?:the\s+)?"
    r"(?:(?:source|provided|uploaded|supporting|resume|cv|draft)\s+)?"
    r"(?:materials?|documents?|resume|cv|draft|profile)"
    r")(?P<punctuation>[.!?]?)\s*$",
    re.IGNORECASE,
)
_FULL_PUBLIC_PROVENANCE_COMMENTARY_RE = re.compile(
    r"^\s*(?:"
    r"(?:the\s+)?(?:candidate|applicant)\s+"
    r"(?:reports?|reported|states?|stated|notes?|noted|describes?|described|"
    r"indicates?|indicated|confirms?|confirmed)\b"
    r"|(?:candidate|applicant)[\s-]+reported\s+(?:from|based\s+on|per)\b"
    r")",
    re.IGNORECASE,
)
_PUBLIC_PROSE_FIELDS = {
    "description",
    "fluency",
    "highlights",
    "reference",
    "summary",
}


def strip_public_provenance_suffix(value: object) -> str:
    """Remove only an explicit terminal provenance aside or attribution clause."""

    cleaned = str(value or "").strip()
    if _FULL_PUBLIC_PROVENANCE_COMMENTARY_RE.match(cleaned):
        return ""
    while True:
        match = _TRAILING_PARENTHETICAL_RE.search(cleaned)
        if match is None or not _PUBLIC_PROVENANCE_ANNOTATION_RE.search(
            match.group("annotation")
        ):
            break
        prefix = cleaned[: match.start()].rstrip(" \t,;:–—-")
        punctuation = match.group("punctuation")
        if punctuation and prefix and prefix[-1] not in ".!?":
            prefix = f"{prefix}{punctuation}"
        cleaned = prefix

    match = _TERMINAL_PUBLIC_PROVENANCE_RE.search(cleaned)
    if match is None:
        return cleaned
    prefix = cleaned[: match.start()].rstrip(" \t,;:–—-")
    punctuation = match.group("punctuation")
    if punctuation and prefix and prefix[-1] not in ".!?":
        prefix = f"{prefix}{punctuation}"
    return prefix


def strip_public_profile_provenance(
    profile_json: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Sanitize public profile strings while preserving internal ``meta`` context."""

    annotations_removed = 0

    def _sanitize(value: Any, *, field_name: str | None = None) -> Any:
        nonlocal annotations_removed
        if isinstance(value, str):
            if str(field_name or "").casefold() not in _PUBLIC_PROSE_FIELDS:
                return value
            cleaned = strip_public_provenance_suffix(value)
            if cleaned != value.strip():
                annotations_removed += 1
            return cleaned
        if isinstance(value, list):
            sanitized_items: list[Any] = []
            for item in value:
                sanitized = _sanitize(item, field_name=field_name)
                if isinstance(item, str) and item.strip() and not sanitized:
                    continue
                sanitized_items.append(sanitized)
            return sanitized_items
        if isinstance(value, dict):
            sanitized_mapping: dict[str, Any] = {}
            for key, item in value.items():
                sanitized = _sanitize(item, field_name=str(key))
                if isinstance(item, str) and item.strip() and not sanitized:
                    continue
                sanitized_mapping[key] = sanitized
            return sanitized_mapping
        return copy.deepcopy(value)

    sanitized_profile: dict[str, Any] = {}
    for key, value in profile_json.items():
        if str(key).casefold() == "meta":
            sanitized_profile[key] = copy.deepcopy(value)
        else:
            sanitized_profile[key] = _sanitize(value, field_name=str(key))
    return sanitized_profile, annotations_removed
