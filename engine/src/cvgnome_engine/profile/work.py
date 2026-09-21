# SPDX-License-Identifier: MPL-2.0
"""Conservative work-history identity matching.

Adapted from earlier private work by the original author; only the local
profile behavior is included. See docs/PROVENANCE.md for publication scope.
"""

from __future__ import annotations

import re
from typing import Any


_CONNECTOR_TOKENS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
}
_CORPORATE_SUFFIX_TOKENS = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "llp",
    "lp",
    "ltd",
    "plc",
}
_DOMAIN_SUFFIX_TOKENS = {
    "ai",
    "app",
    "biz",
    "co",
    "com",
    "dev",
    "io",
    "net",
    "org",
    "tech",
}
_GENERIC_ORGANIZATION_MARKERS = {
    "business",
    "client",
    "company",
    "employer",
    "firm",
    "organization",
}
_MONTH_NUMBERS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}
_PRESENT_DATE_MARKERS = {"current", "now", "present"}
_WORKPLACE_QUALIFIER_RE = re.compile(
    r"\b(?:fully\s+remote|remote|hybrid|on[\s-]?site)\b",
    re.IGNORECASE,
)
_PARENTHETICAL_RE = re.compile(r"\(([^()]*)\)")
_ALIAS_SEPARATOR_RE = re.compile(
    r"\s+(?:d/?b/?a|doing\s+business\s+as|formerly|aka)\s+",
    re.IGNORECASE,
)


def _text_marker(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9+#]+", " ", str(value or "").casefold()),
    ).strip()


def _organization_segment_marker(value: Any) -> str:
    cleaned = str(value or "").strip().casefold()
    cleaned = re.sub(r"^[a-z][a-z0-9+.-]*://", "", cleaned)
    cleaned = re.sub(r"^www\.", "", cleaned)
    cleaned = re.split(r"[/?#]", cleaned, maxsplit=1)[0]
    tokens = re.findall(r"[a-z0-9]+", cleaned)
    while tokens and tokens[-1] in _CORPORATE_SUFFIX_TOKENS:
        tokens.pop()
    if len(tokens) > 1 and tokens[-1] in _DOMAIN_SUFFIX_TOKENS:
        tokens.pop()
    while tokens and tokens[-1] in _CORPORATE_SUFFIX_TOKENS:
        tokens.pop()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens.pop(0)
    marker = " ".join(tokens)
    if marker in _GENERIC_ORGANIZATION_MARKERS:
        return ""
    return marker


def organization_alias_markers(value: Any) -> set[str]:
    """Return conservative brand/legal/domain aliases for an organization label."""

    raw = str(value or "").strip()
    if not raw:
        return set()

    segments: list[str] = []
    parenthetical_values = _PARENTHETICAL_RE.findall(raw)
    without_parentheticals = _PARENTHETICAL_RE.sub(" ", raw)
    segments.extend(_ALIAS_SEPARATOR_RE.split(without_parentheticals))
    segments.extend(parenthetical_values)

    markers = {
        marker
        for segment in segments
        if (marker := _organization_segment_marker(segment))
    }
    whole_marker = _organization_segment_marker(raw)
    if whole_marker:
        markers.add(whole_marker)
    return markers


def _position_core(value: Any) -> str:
    cleaned = _text_marker(value)
    cleaned = re.sub(
        r"\b(?:contract|contractor|consulting contract|temporary|temp)\b",
        " ",
        cleaned,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _position_tokens(value: Any) -> set[str]:
    return {
        token
        for token in _position_core(value).split()
        if token and token not in _CONNECTOR_TOKENS
    }


def positions_compatible(existing: Any, candidate: Any) -> bool:
    existing_core = _position_core(existing)
    candidate_core = _position_core(candidate)
    if not existing_core or not candidate_core:
        return not existing_core or not candidate_core
    if existing_core == candidate_core:
        return True
    if existing_core in candidate_core or candidate_core in existing_core:
        shorter = min(len(existing_core), len(candidate_core))
        longer = max(len(existing_core), len(candidate_core))
        if shorter and shorter / longer >= 0.72:
            return True
    existing_tokens = _position_tokens(existing)
    candidate_tokens = _position_tokens(candidate)
    if not existing_tokens or not candidate_tokens:
        return False
    overlap = len(existing_tokens & candidate_tokens)
    return overlap / min(len(existing_tokens), len(candidate_tokens)) >= 0.8


def _date_marker(value: Any) -> str:
    cleaned = str(value or "").strip().casefold().rstrip(".")
    if not cleaned:
        return ""
    if cleaned in _PRESENT_DATE_MARKERS:
        return "present"
    numeric_match = re.fullmatch(
        r"((?:19|20)\d{2})[-/](\d{1,2})(?:[-/]\d{1,2})?", cleaned
    )
    if numeric_match:
        return f"{numeric_match.group(1)}-{int(numeric_match.group(2)):02d}"
    month_match = re.fullmatch(r"([a-z]+)\.?\s+((?:19|20)\d{2})", cleaned)
    if month_match and month_match.group(1) in _MONTH_NUMBERS:
        return f"{month_match.group(2)}-{_MONTH_NUMBERS[month_match.group(1)]}"
    return re.sub(r"\s+", " ", cleaned)


def _values_compatible(existing: Any, candidate: Any) -> bool:
    existing_marker = _date_marker(existing)
    candidate_marker = _date_marker(candidate)
    if not existing_marker or not candidate_marker:
        return True
    if existing_marker == candidate_marker:
        return True
    if re.fullmatch(r"(?:19|20)\d{2}", existing_marker):
        return candidate_marker.startswith(f"{existing_marker}-")
    if re.fullmatch(r"(?:19|20)\d{2}", candidate_marker):
        return existing_marker.startswith(f"{candidate_marker}-")
    return False


def _share_date_anchor(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    existing_start = _date_marker(existing.get("startDate"))
    candidate_start = _date_marker(candidate.get("startDate"))
    existing_end = _date_marker(existing.get("endDate"))
    candidate_end = _date_marker(candidate.get("endDate"))

    start_matches = bool(
        existing_start
        and candidate_start
        and _values_compatible(existing_start, candidate_start)
    )
    end_matches = bool(
        existing_end
        and candidate_end
        and _values_compatible(existing_end, candidate_end)
    )
    if start_matches and _values_compatible(existing_end, candidate_end):
        return True
    if end_matches and _values_compatible(existing_start, candidate_start):
        return True
    return False


def _location_marker(value: Any) -> str:
    cleaned = _PARENTHETICAL_RE.sub(" ", str(value or ""))
    cleaned = _WORKPLACE_QUALIFIER_RE.sub(" ", cleaned)
    return _text_marker(cleaned)


def locations_compatible(existing: Any, candidate: Any) -> bool:
    existing_marker = _location_marker(existing)
    candidate_marker = _location_marker(candidate)
    return not existing_marker or not candidate_marker or existing_marker == candidate_marker


def work_entries_can_merge(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Resolve duplicate work entries without merging distinct roles or stints."""

    existing_name = str(existing.get("name") or "").strip()
    candidate_name = str(candidate.get("name") or "").strip()
    existing_aliases = organization_alias_markers(existing_name)
    candidate_aliases = organization_alias_markers(candidate_name)
    if existing_aliases and candidate_aliases and not (existing_aliases & candidate_aliases):
        return False
    if not positions_compatible(existing.get("position"), candidate.get("position")):
        return False

    existing_position = _position_core(existing.get("position"))
    candidate_position = _position_core(candidate.get("position"))
    needs_date_anchor = (
        not existing_aliases
        or not candidate_aliases
        or _text_marker(existing_name) != _text_marker(candidate_name)
        or not existing_position
        or not candidate_position
        or existing_position != candidate_position
    )
    if needs_date_anchor and not _share_date_anchor(existing, candidate):
        return False

    return (
        locations_compatible(existing.get("location"), candidate.get("location"))
        and _values_compatible(existing.get("startDate"), candidate.get("startDate"))
        and _values_compatible(existing.get("endDate"), candidate.get("endDate"))
    )


def work_entries_have_mergeable_duplicates(values: Any) -> bool:
    """Return whether a work list contains entries the canonical guard would merge."""

    if not isinstance(values, list):
        return False
    entries = [value for value in values if isinstance(value, dict)]
    for index, existing in enumerate(entries):
        for candidate in entries[index + 1 :]:
            if work_entries_can_merge(existing, candidate):
                return True
    return False
