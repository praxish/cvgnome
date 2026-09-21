# SPDX-License-Identifier: MPL-2.0
"""Pure canonical-profile to baseline-resume projection.

The baseline normalization rules are adapted from earlier work by the same
author (see docs/PROVENANCE.md).  It intentionally projects onto a public JSON Resume-shaped allowlist:
private canonical-profile metadata and unknown fields never reach an export.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from ..legacy_compatibility import LEGACY_PROFILE_HOSTS, LEGACY_PROFILE_NETWORKS


JSON_RESUME_SCHEMA = (
    "https://raw.githubusercontent.com/jsonresume/resume-schema/v1.0.0/schema.json"
)

RENDERABLE_SECTIONS = (
    "work",
    "education",
    "skills",
    "projects",
    "volunteer",
    "certificates",
    "awards",
    "publications",
    "languages",
    "interests",
    "references",
)

_BASELINE_WORK_YEARS = 15
_MAX_WORK_ENTRIES = 8
_MAX_HIGHLIGHTS_PER_ROLE = 4
_MAX_HIGHLIGHT_CHARS = 360
_MAX_WORK_SUMMARY_CHARS = 420

_DATE_TOKEN_RE = re.compile(
    r"^(?P<year>(?:19|20)\d{2})"
    r"(?:[-/](?P<month>0?[1-9]|1[0-2])"
    r"(?:[-/](?P<day>0?[1-9]|[12]\d|3[01]))?)?$"
)
_DATE_ONLY_RE = re.compile(
    r"^(?:dates?\s*:\s*)?"
    r"(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2}(?:[-/]\d{1,2})?"
    r"\s*(?:-|--|to|through)\s*"
    r"(?:(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2}(?:[-/]\d{1,2})?"
    r"|present|current|now|ongoing)$",
    re.IGNORECASE,
)
_MARKDOWN_EDGE_RE = re.compile(r"^(?:\*\*|__|`)(.*?)(?:\*\*|__|`)$")
_PRESENT_VALUES = frozenset({"present", "current", "now", "ongoing"})
_INTERNAL_PROFILE_HOSTS = frozenset(
    {
        "cvgnome.com",
        "www.cvgnome.com",
        *LEGACY_PROFILE_HOSTS,
    }
)


def _text(value: Any, *, max_chars: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    cleaned = unicodedata.normalize("NFC", str(value))
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    edge_match = _MARKDOWN_EDGE_RE.fullmatch(cleaned)
    if edge_match:
        cleaned = edge_match.group(1).strip()
    return cleaned[:max_chars].rstrip()


def _compact_text(value: Any, *, max_chars: int) -> str:
    cleaned = _text(value, max_chars=max_chars * 2)
    if len(cleaned) <= max_chars:
        return cleaned
    candidate = cleaned[:max_chars].rstrip()
    sentence_break = max(
        candidate.rfind(". "),
        candidate.rfind("; "),
        candidate.rfind(" - "),
    )
    if sentence_break >= int(max_chars * 0.55):
        return candidate[: sentence_break + 1].rstrip()
    word_break = candidate.rfind(" ")
    if word_break >= int(max_chars * 0.70):
        candidate = candidate[:word_break].rstrip()
    return candidate.rstrip(" ,;:-") + "."


def _safe_public_url(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    raw = unicodedata.normalize("NFC", str(value)).strip()
    if not raw or any(character.isspace() or ord(character) < 32 for character in raw):
        return ""
    try:
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.hostname.casefold() in _INTERNAL_PROFILE_HOSTS
    ):
        return ""
    return raw[:500]


def _dedupe_text(values: Any, *, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _text(value, max_chars=max_chars)
        if not cleaned:
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(cleaned)
        if len(result) >= max_items:
            break
    return result


def _date_token(value: Any, *, present_as_empty: bool = False) -> str:
    cleaned = _text(value, max_chars=32)
    if not cleaned:
        return ""
    if cleaned.casefold() in _PRESENT_VALUES:
        return "" if present_as_empty else "Present"
    match = _DATE_TOKEN_RE.fullmatch(cleaned)
    if not match:
        return cleaned
    result = match.group("year")
    if match.group("month"):
        result += f"-{int(match.group('month')):02d}"
    if match.group("day"):
        result += f"-{int(match.group('day')):02d}"
    return result


def _year_from_date(value: Any) -> int | None:
    match = re.search(r"\b((?:19|20)\d{2})\b", _text(value, max_chars=64))
    return int(match.group(1)) if match else None


def _sort_date(value: Any) -> tuple[int, int, int]:
    cleaned = _text(value, max_chars=32)
    if not cleaned:
        return (0, 0, 0)
    if cleaned.casefold() in _PRESENT_VALUES:
        return (9999, 12, 31)
    match = _DATE_TOKEN_RE.fullmatch(cleaned)
    if match:
        return (
            int(match.group("year")),
            int(match.group("month") or 0),
            int(match.group("day") or 0),
        )
    year = _year_from_date(cleaned)
    return (year or 0, 0, 0)


def _clean_location(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, limit in (
        ("address", 240),
        ("postalCode", 40),
        ("city", 160),
        ("countryCode", 60),
        ("region", 160),
    ):
        cleaned = _text(value.get(key), max_chars=limit)
        if cleaned:
            result[key] = cleaned
    return result


def _clean_profiles(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        network = _text(raw.get("network"), max_chars=80)
        if network.casefold() in {"cvgnome", *LEGACY_PROFILE_NETWORKS}:
            continue
        entry: dict[str, str] = {}
        if network:
            entry["network"] = network
        username = _text(raw.get("username"), max_chars=160)
        if username:
            entry["username"] = username
        url = _safe_public_url(raw.get("url"))
        if url:
            entry["url"] = url
        marker = (
            entry.get("network", "").casefold(),
            entry.get("username", "").casefold(),
            entry.get("url", "").casefold(),
        )
        if not entry or marker in seen:
            continue
        seen.add(marker)
        result.append(entry)
        if len(result) >= 8:
            break
    return result


def _fallback_summary(profile: dict[str, Any], work: list[dict[str, Any]]) -> str:
    meta = profile.get("meta")
    context = meta.get("canonical_context") if isinstance(meta, dict) else None
    if isinstance(context, dict):
        narrative = _compact_text(context.get("career_narrative"), max_chars=700)
        if narrative:
            return narrative
        domain_context = context.get("domain_context")
        if isinstance(domain_context, list):
            for item in domain_context:
                candidate = _compact_text(item, max_chars=700)
                if candidate:
                    return candidate
    for item in work:
        summary = _compact_text(item.get("summary"), max_chars=700)
        if summary:
            return summary
    return ""


def _clean_basics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, limit in (
        ("name", 160),
        ("label", 200),
        ("email", 200),
        ("phone", 80),
        ("summary", 700),
    ):
        cleaned = _compact_text(value.get(key), max_chars=limit)
        if cleaned:
            result[key] = cleaned
    url = _safe_public_url(value.get("url"))
    if url:
        result["url"] = url
    location = _clean_location(value.get("location"))
    if location:
        result["location"] = location
    profiles = _clean_profiles(value.get("profiles"))
    if profiles:
        result["profiles"] = profiles
    return result


def _is_date_only(value: str) -> bool:
    cleaned = value.strip().strip("()[]{}")
    cleaned = re.sub(r"[\u2010-\u2015\u2212]", "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return bool(_DATE_ONLY_RE.fullmatch(cleaned))


def _clean_highlights(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        cleaned = _compact_text(raw, max_chars=_MAX_HIGHLIGHT_CHARS)
        if not cleaned or _is_date_only(cleaned):
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(cleaned)
        if len(result) >= _MAX_HIGHLIGHTS_PER_ROLE:
            break
    return result


def _clean_work_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    aliases = {
        "name": ("name", "organization", "company", "employer"),
        "position": ("position", "title", "role"),
        "location": ("location", "place"),
        "url": ("url",),
    }
    for key, keys in aliases.items():
        raw = next((value.get(candidate) for candidate in keys if value.get(candidate)), None)
        cleaned = _safe_public_url(raw) if key == "url" else _text(raw, max_chars=200)
        if cleaned:
            result[key] = cleaned

    start = _date_token(value.get("startDate") or value.get("start"))
    end_raw = value.get("endDate") or value.get("end")
    end = _date_token(end_raw, present_as_empty=True)
    if start:
        result["startDate"] = start
    if end:
        result["endDate"] = end

    summary = _compact_text(
        value.get("summary") or value.get("description"),
        max_chars=_MAX_WORK_SUMMARY_CHARS,
    )
    if summary and not _is_date_only(summary):
        result["summary"] = summary
    highlights = _clean_highlights(value.get("highlights"))
    if highlights:
        result["highlights"] = highlights

    # An empty object, or a fragment with neither identity nor publishable
    # context, is not a resume role.
    if not (result.get("name") or result.get("position")):
        return {}
    return result


def _work_marker(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("name") or "").casefold(),
        str(entry.get("position") or "").casefold(),
        str(entry.get("startDate") or "").casefold(),
    )


def _merge_work(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in candidate.items():
        if key == "highlights":
            merged["highlights"] = _dedupe_text(
                [*(merged.get("highlights") or []), *value],
                max_items=_MAX_HIGHLIGHTS_PER_ROLE,
                max_chars=_MAX_HIGHLIGHT_CHARS,
            )
        elif not merged.get(key) and value:
            merged[key] = value
    return merged


def _clean_work(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    merged: list[dict[str, Any]] = []
    marker_indexes: dict[tuple[str, str, str], int] = {}
    for raw in value:
        entry = _clean_work_entry(raw)
        if not entry:
            continue
        marker = _work_marker(entry)
        if marker in marker_indexes and any(marker):
            index = marker_indexes[marker]
            merged[index] = _merge_work(merged[index], entry)
        else:
            marker_indexes[marker] = len(merged)
            merged.append(entry)

    dated_years = [
        year
        for entry in merged
        for year in (
            _year_from_date(entry.get("startDate")),
            _year_from_date(entry.get("endDate")),
        )
        if year is not None
    ]
    # Anchor the rolling window to profile content, not wall-clock time, so a
    # persisted profile has stable derived bytes across New Year's Day.
    cutoff_year = (max(dated_years) - _BASELINE_WORK_YEARS) if dated_years else None
    recent: list[dict[str, Any]] = []
    for entry in merged:
        end_year = _year_from_date(entry.get("endDate"))
        start_year = _year_from_date(entry.get("startDate"))
        # No end date is rendered as Present when a start date exists. Undated
        # roles remain because silently dropping them is more surprising.
        if cutoff_year is not None and end_year is not None and end_year < cutoff_year:
            continue
        if end_year is None and start_year is None:
            recent.append(entry)
            continue
        recent.append(entry)

    def sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
        start = _sort_date(entry.get("startDate"))
        end = _sort_date(entry.get("endDate"))
        primary = end if end != (0, 0, 0) else ((9999, 12, 31) if start != (0, 0, 0) else start)
        detail = len(entry.get("highlights") or []) + bool(entry.get("summary"))
        return primary, start, detail, _work_marker(entry)

    return sorted(recent, key=sort_key, reverse=True)[:_MAX_WORK_ENTRIES]


def _clean_education(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {}
        for key, limit in (
            ("institution", 240),
            ("area", 200),
            ("studyType", 160),
            ("score", 80),
            ("summary", 420),
        ):
            cleaned = _compact_text(raw.get(key), max_chars=limit)
            if cleaned:
                entry[key] = cleaned
        url = _safe_public_url(raw.get("url"))
        if url:
            entry["url"] = url
        start = _date_token(raw.get("startDate") or raw.get("start"))
        end = _date_token(raw.get("endDate") or raw.get("end"), present_as_empty=True)
        if start:
            entry["startDate"] = start
        if end:
            entry["endDate"] = end
        courses = _dedupe_text(raw.get("courses"), max_items=12, max_chars=160)
        if courses:
            entry["courses"] = courses
        if not (entry.get("institution") or entry.get("studyType") or entry.get("area")):
            continue
        marker = (
            str(entry.get("institution") or "").casefold(),
            str(entry.get("studyType") or "").casefold(),
            str(entry.get("area") or "").casefold(),
        )
        if marker in seen:
            continue
        seen.add(marker)
        result.append(entry)
        if len(result) >= 8:
            break
    return result


def _clean_skills(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"), max_chars=120)
        level = _text(raw.get("level"), max_chars=80)
        keywords = _dedupe_text(raw.get("keywords"), max_items=24, max_chars=100)
        marker = name.casefold() or "\x00".join(item.casefold() for item in keywords)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        entry: dict[str, Any] = {}
        if name:
            entry["name"] = name
        if level:
            entry["level"] = level
        if keywords:
            entry["keywords"] = keywords
        result.append(entry)
        if len(result) >= 16:
            break
    return result


def _clean_named_entries(
    value: Any,
    *,
    allowed: Iterable[tuple[str, int]],
    required: tuple[str, ...],
    max_entries: int = 12,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {}
        for key, limit in allowed:
            if key in {"startDate", "endDate", "date"}:
                cleaned = _date_token(raw.get(key), present_as_empty=key == "endDate")
            elif key == "url":
                cleaned = _safe_public_url(raw.get(key))
            else:
                cleaned = _compact_text(raw.get(key), max_chars=limit)
            if cleaned:
                entry[key] = cleaned
        highlights = _clean_highlights(raw.get("highlights"))
        if highlights:
            entry["highlights"] = highlights
        keywords = _dedupe_text(raw.get("keywords"), max_items=16, max_chars=100)
        if keywords:
            entry["keywords"] = keywords
        if not any(entry.get(key) for key in required):
            continue
        marker = repr(sorted(entry.items())).casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(entry)
        if len(result) >= max_entries:
            break
    return result


def derive_baseline_resume(profile: dict[str, Any]) -> dict[str, Any]:
    """Project a canonical profile into a compact, public baseline resume.

    The input is never mutated. Unknown top-level and nested keys are dropped so
    internal evidence, notes, model metadata, and workflow state cannot leak into
    generated documents.
    """

    if not isinstance(profile, dict):
        raise TypeError("profile must be a dictionary")

    work = _clean_work(profile.get("work"))
    basics = _clean_basics(profile.get("basics"))
    if not basics.get("summary"):
        fallback = _fallback_summary(profile, work)
        if fallback:
            basics["summary"] = fallback

    resume: dict[str, Any] = {"$schema": JSON_RESUME_SCHEMA}
    if basics:
        resume["basics"] = basics
    if work:
        resume["work"] = work

    education = _clean_education(profile.get("education"))
    if education:
        resume["education"] = education
    skills = _clean_skills(profile.get("skills"))
    if skills:
        resume["skills"] = skills

    sections = (
        (
            "projects",
            (("name", 200), ("description", 600), ("url", 500), ("startDate", 32), ("endDate", 32)),
            ("name", "description"),
        ),
        (
            "volunteer",
            (
                ("organization", 200),
                ("position", 200),
                ("summary", 500),
                ("url", 500),
                ("startDate", 32),
                ("endDate", 32),
            ),
            ("organization", "position"),
        ),
        (
            "certificates",
            (("name", 200), ("issuer", 200), ("date", 32), ("url", 500)),
            ("name",),
        ),
        (
            "awards",
            (("title", 200), ("awarder", 200), ("date", 32), ("summary", 500)),
            ("title",),
        ),
        (
            "publications",
            (("name", 240), ("publisher", 200), ("releaseDate", 32), ("url", 500), ("summary", 500)),
            ("name",),
        ),
        (
            "languages",
            (("language", 120), ("fluency", 120)),
            ("language",),
        ),
        (
            "interests",
            (("name", 160),),
            ("name",),
        ),
        (
            "references",
            (("name", 200), ("reference", 700)),
            ("name", "reference"),
        ),
    )
    for section, allowed, required in sections:
        source = profile.get(section)
        if section == "certificates" and not source:
            source = profile.get("certifications")
        cleaned = _clean_named_entries(source, allowed=allowed, required=required)
        if cleaned:
            resume[section] = cleaned

    return resume


def projected_resume_is_renderable(resume: dict[str, Any]) -> bool:
    """Return whether an already projected resume meets the renderer contract."""

    basics = resume.get("basics") if isinstance(resume.get("basics"), dict) else {}
    if not _text(basics.get("name"), max_chars=160):
        return False
    if _text(basics.get("summary"), max_chars=700):
        return True
    return any(
        isinstance(resume.get(key), list) and bool(resume[key])
        for key in RENDERABLE_SECTIONS
    )


def is_baseline_resume_renderable(profile: dict[str, Any]) -> bool:
    """Project a canonical profile and apply the exact renderer content gate."""

    return projected_resume_is_renderable(derive_baseline_resume(profile))
