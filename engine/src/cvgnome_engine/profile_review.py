# SPDX-License-Identifier: MPL-2.0
"""Bounded, human-readable projections of immutable profile versions.

The desktop renderer never receives canonical JSON through this module.  It
gets a small overview and one page of allowlisted public profile cards at a
time.  Private ``meta`` content, dossier claims, source paths, hashes, and raw
imports remain inside the engine.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from .legacy_compatibility import LEGACY_PROFILE_HOSTS, LEGACY_PROFILE_NETWORKS

from .resume.derive import (
    _safe_public_url,
    is_baseline_resume_renderable,
)


PROFILE_REVIEW_PAGE_LIMIT = 10
PROFILE_REVIEW_OFFSET_LIMIT = 10_000
PROFILE_REVIEW_SECTION_ITEM_LIMIT = 5_000
PROFILE_REVIEW_SUMMARY_LIMIT_BYTES = 30 * 1024
PROFILE_REVIEW_RESPONSE_LIMIT_BYTES = 112 * 1024

SECTION_LABELS: tuple[tuple[str, str], ...] = (
    ("work", "Work history"),
    ("education", "Education"),
    ("skills", "Skills"),
    ("projects", "Projects & experiences"),
    ("volunteer", "Volunteer work"),
    ("certificates", "Certificates"),
    ("awards", "Awards"),
    ("publications", "Publications"),
    ("languages", "Languages"),
    ("interests", "Interests"),
    ("references", "References"),
)

_SECTION_LABEL_BY_KEY = dict(SECTION_LABELS)

_INTERNAL_PROFILE_HOSTS = frozenset(
    {
        "cvgnome.com",
        "www.cvgnome.com",
        *LEGACY_PROFILE_HOSTS,
    }
)
_INTERNAL_PROFILE_NETWORKS = frozenset({"cvgnome", *LEGACY_PROFILE_NETWORKS})
_NAMED_SECTIONS = (
    "projects",
    "volunteer",
    "certificates",
    "awards",
    "publications",
    "languages",
    "interests",
    "references",
)


def _optional(value: Any, *, max_chars: int) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = unicodedata.normalize("NFC", str(value)).replace("\u00a0", " ")
    cleaned = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in normalized
        if unicodedata.category(character) == "Cc"
        or not unicodedata.category(character).startswith("C")
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:max_chars].rstrip()
    return cleaned or None


def _first(entry: dict[str, Any], keys: tuple[str, ...], *, max_chars: int) -> str | None:
    for key in keys:
        cleaned = _optional(entry.get(key), max_chars=max_chars)
        if cleaned:
            return cleaned
    return None


def _date_range(
    entry: dict[str, Any],
    *,
    start_keys: tuple[str, ...] = ("startDate", "start"),
    end_keys: tuple[str, ...] = ("endDate", "end"),
) -> str | None:
    start_text = _first(entry, start_keys, max_chars=32)
    end_text = _first(entry, end_keys, max_chars=32)
    if start_text and end_text:
        return f"{start_text} – {end_text}"
    if start_text:
        return start_text
    return end_text


def _text_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        cleaned = _optional(raw, max_chars=max_chars)
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


def _safe_review_public_url(value: Any) -> str | None:
    cleaned = _optional(value, max_chars=500)
    if not cleaned:
        return None
    safe = _safe_public_url(cleaned)
    if not safe:
        return None
    try:
        candidate = safe if "://" in safe else f"https://{safe}"
        hostname = (urlsplit(candidate).hostname or "").rstrip(".")
        ascii_hostname = hostname.encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        return None
    if ascii_hostname in _INTERNAL_PROFILE_HOSTS:
        return None
    return _optional(safe, max_chars=500)


def _network_marker(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _detail(label: str, value: Any, *, max_chars: int = 240) -> dict[str, str] | None:
    cleaned = _optional(value, max_chars=max_chars)
    return {"label": label, "value": cleaned} if cleaned else None


def _card(
    *,
    title: Any,
    subtitle: Any = None,
    date_range: Any = None,
    location: Any = None,
    summary: Any = None,
    highlights: Any = None,
    tags: Any = None,
    details: list[dict[str, str] | None] | None = None,
) -> dict[str, Any] | None:
    normalized_title = _optional(title, max_chars=240)
    if not normalized_title:
        return None
    return {
        "title": normalized_title,
        "subtitle": _optional(subtitle, max_chars=240),
        "date_range": _optional(date_range, max_chars=80),
        "location": _optional(location, max_chars=200),
        "summary": _optional(summary, max_chars=700),
        "highlights": _text_list(highlights, max_items=8, max_chars=360),
        "tags": _text_list(tags, max_items=18, max_chars=100),
        "details": [item for item in (details or []) if item is not None][:8],
    }


def _work_cards(profile: dict[str, Any]) -> list[dict[str, Any]]:
    raw_entries = profile.get("work")
    if not isinstance(raw_entries, list):
        return []
    cards: list[dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        position = _first(entry, ("position", "title", "role"), max_chars=240)
        organization = _first(
            entry,
            ("name", "organization", "company", "employer"),
            max_chars=240,
        )
        card = _card(
            title=position or organization,
            subtitle=organization if position else None,
            date_range=_date_range(entry),
            location=_first(entry, ("location", "place"), max_chars=200),
            summary=_first(entry, ("summary", "description"), max_chars=700),
            highlights=entry.get("highlights"),
        )
        if card:
            cards.append(card)
    return cards


def _education_cards(profile: dict[str, Any]) -> list[dict[str, Any]]:
    raw_entries = profile.get("education")
    if not isinstance(raw_entries, list):
        return []
    cards: list[dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        credential = " · ".join(
            value
            for value in (
                _optional(entry.get("studyType"), max_chars=160),
                _optional(entry.get("area"), max_chars=200),
            )
            if value
        )
        institution = _optional(entry.get("institution"), max_chars=240)
        card = _card(
            title=credential or institution,
            subtitle=institution if credential else None,
            date_range=_date_range(entry),
            location=entry.get("location"),
            summary=_optional(entry.get("summary"), max_chars=700),
            tags=entry.get("courses"),
            details=[_detail("Score", entry.get("score"), max_chars=80)],
        )
        if card:
            cards.append(card)
    return cards


def _skill_cards(profile: dict[str, Any]) -> list[dict[str, Any]]:
    raw_entries = profile.get("skills")
    if not isinstance(raw_entries, list):
        return []
    cards: list[dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        name = _optional(entry.get("name"), max_chars=120)
        keywords = _text_list(entry.get("keywords"), max_items=18, max_chars=100)
        if not name and not keywords:
            continue
        card = _card(
            title=name or "Skills",
            subtitle=entry.get("level"),
            tags=keywords,
        )
        if card:
            cards.append(card)
    return cards


def _named_cards(profile: dict[str, Any], section: str) -> list[dict[str, Any]]:
    source = profile.get(section)
    if section == "certificates" and not source:
        source = profile.get("certifications")
    if not isinstance(source, list):
        return []
    cards: list[dict[str, Any]] = []
    for entry in source:
        if not isinstance(entry, dict):
            continue
        if section == "projects":
            name = _optional(entry.get("name"), max_chars=200)
            description = _optional(entry.get("description"), max_chars=700)
            if not name and not description:
                continue
            card = _card(
                title=name or "Project",
                date_range=_date_range(entry),
                summary=description,
                highlights=entry.get("highlights"),
                tags=entry.get("keywords"),
            )
        elif section == "volunteer":
            position = _optional(entry.get("position"), max_chars=200)
            organization = _optional(entry.get("organization"), max_chars=200)
            card = _card(
                title=position or organization,
                subtitle=organization if position else None,
                date_range=_date_range(entry),
                location=entry.get("location"),
                summary=entry.get("summary"),
                highlights=entry.get("highlights"),
                tags=entry.get("keywords"),
            )
        elif section == "certificates":
            card = _card(
                title=entry.get("name"),
                subtitle=entry.get("issuer"),
                date_range=_date_range(entry, start_keys=(), end_keys=("date",)),
            )
        elif section == "awards":
            card = _card(
                title=entry.get("title"),
                subtitle=entry.get("awarder"),
                date_range=_date_range(entry, start_keys=(), end_keys=("date",)),
                summary=entry.get("summary"),
            )
        elif section == "publications":
            card = _card(
                title=entry.get("name"),
                subtitle=entry.get("publisher"),
                date_range=_date_range(
                    entry,
                    start_keys=(),
                    end_keys=("releaseDate",),
                ),
                summary=entry.get("summary"),
            )
        elif section == "languages":
            card = _card(title=entry.get("language"), subtitle=entry.get("fluency"))
        elif section == "interests":
            card = _card(
                title=entry.get("name"),
                tags=entry.get("keywords"),
            )
        else:
            reference_name = _optional(entry.get("name"), max_chars=240)
            reference_text = _optional(entry.get("reference"), max_chars=700)
            if not reference_name and not reference_text:
                continue
            card = _card(
                title=reference_name or "Reference",
                summary=reference_text,
            )
        if card:
            cards.append(card)
    return cards


_SECTION_PROJECTORS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "work": _work_cards,
    "education": _education_cards,
    "skills": _skill_cards,
    **{
        section: (lambda profile, section=section: _named_cards(profile, section))
        for section in _NAMED_SECTIONS
    },
}


def profile_source_kind(source: Any) -> str:
    normalized = str(source or "")
    if normalized.startswith("source_scan:"):
        return "source_documents"
    if normalized.startswith("file_import:"):
        return "structured_file"
    if normalized == "local_edit":
        return "local_edit"
    if normalized == "local_restore":
        return "local_restore"
    if normalized == "local_start":
        return "manual_start"
    return "other"


def build_profile_review_section_changes(
    before_profile: dict[str, Any],
    after_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare only the bounded public projections of all review sections."""

    changes: list[dict[str, Any]] = []
    for key, label in SECTION_LABELS:
        before_cards = _SECTION_PROJECTORS[key](before_profile)[
            :PROFILE_REVIEW_SECTION_ITEM_LIMIT
        ]
        after_cards = _SECTION_PROJECTORS[key](after_profile)[
            :PROFILE_REVIEW_SECTION_ITEM_LIMIT
        ]
        changes.append(
            {
                "key": key,
                "label": label,
                "before_count": len(before_cards),
                "after_count": len(after_cards),
                "content_changed": before_cards != after_cards,
            }
        )
    return changes


def _location_text(location: Any) -> str:
    if not isinstance(location, dict):
        return ""
    return ", ".join(
        value
        for value in (
            _optional(location.get("city"), max_chars=160),
            _optional(location.get("region"), max_chars=160),
            _optional(location.get("countryCode"), max_chars=60),
        )
        if value
    )[:300]


def _profile_contacts(basics: dict[str, Any]) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    for kind, label, value in (
        ("email", "Email", basics.get("email")),
        ("phone", "Phone", basics.get("phone")),
        ("location", "Location", _location_text(basics.get("location"))),
        ("website", "Website", _safe_review_public_url(basics.get("url"))),
    ):
        cleaned = _optional(value, max_chars=500)
        if cleaned:
            contacts.append({"kind": kind, "label": label, "value": cleaned})
    profiles = basics.get("profiles")
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, dict):
            continue
        label = _optional(profile.get("network"), max_chars=80) or "Profile"
        if _network_marker(label) in _INTERNAL_PROFILE_NETWORKS:
            continue
        raw_url = _optional(profile.get("url"), max_chars=500)
        url = _safe_review_public_url(raw_url)
        if raw_url and not url:
            continue
        username = _optional(profile.get("username"), max_chars=160)
        value = url or username
        if value:
            contacts.append({"kind": "profile", "label": label, "value": value})
        if len(contacts) >= 12:
            break
    return contacts


def build_profile_review_summary(profile_version: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded overview for one stored immutable profile version."""

    profile = profile_version.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("Profile version must contain canonical profile data")
    basics = profile.get("basics")
    if not isinstance(basics, dict):
        basics = {}
    section_summaries = [
        {
            "key": key,
            "label": label,
            "count": min(
                len(_SECTION_PROJECTORS[key](profile)),
                PROFILE_REVIEW_SECTION_ITEM_LIMIT,
            ),
        }
        for key, label in SECTION_LABELS
    ]
    name = _optional(basics.get("name"), max_chars=160) or "Unnamed profile"
    result = {
        "profile_version_id": str(profile_version.get("id") or ""),
        "version_number": int(profile_version.get("version_number") or 0),
        "created_at_ms": int(profile_version.get("created_at_ms") or 0),
        "source_kind": profile_source_kind(profile_version.get("source")),
        "renderable": is_baseline_resume_renderable(profile),
        "name": name,
        "headline": _optional(basics.get("label"), max_chars=200),
        "summary": _optional(basics.get("summary"), max_chars=700),
        "contacts": _profile_contacts(basics),
        "sections": section_summaries,
    }
    while result["contacts"]:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= PROFILE_REVIEW_SUMMARY_LIMIT_BYTES:
            break
        result["contacts"].pop()
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > PROFILE_REVIEW_SUMMARY_LIMIT_BYTES:
        raise ValueError("The profile review summary exceeds the response limit")
    return result


def build_profile_review_section(
    profile_version: dict[str, Any],
    *,
    section: Any,
    offset: Any,
) -> dict[str, Any]:
    """Return one deterministic page from a public profile section."""

    if not isinstance(section, str) or section not in _SECTION_PROJECTORS:
        raise ValueError("section must name a supported profile review section")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > PROFILE_REVIEW_OFFSET_LIMIT
    ):
        raise ValueError("offset must be a bounded non-negative integer")
    profile = profile_version.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("Profile version must contain canonical profile data")
    cards = _SECTION_PROJECTORS[section](profile)[:PROFILE_REVIEW_SECTION_ITEM_LIMIT]
    page = cards[offset : offset + PROFILE_REVIEW_PAGE_LIMIT]
    items: list[dict[str, Any]] = []
    for index, card in enumerate(page):
        candidate = {"ordinal": offset + index, **card}
        prospective = {
            "profile_version_id": str(profile_version.get("id") or ""),
            "key": section,
            "label": _SECTION_LABEL_BY_KEY[section],
            "total_items": len(cards),
            "offset": offset,
            "limit": PROFILE_REVIEW_PAGE_LIMIT,
            "next_offset": offset + len(items) + 1,
            "items": [*items, candidate],
        }
        encoded = json.dumps(
            prospective,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > PROFILE_REVIEW_RESPONSE_LIMIT_BYTES:
            break
        items.append(candidate)
    if page and not items:
        raise ValueError("A profile review item exceeds the response limit")
    page_end = offset + len(items)
    return {
        "profile_version_id": str(profile_version.get("id") or ""),
        "key": section,
        "label": _SECTION_LABEL_BY_KEY[section],
        "total_items": len(cards),
        "offset": offset,
        "limit": PROFILE_REVIEW_PAGE_LIMIT,
        "next_offset": page_end if page_end < len(cards) else None,
        "items": items,
    }


__all__ = [
    "PROFILE_REVIEW_OFFSET_LIMIT",
    "PROFILE_REVIEW_PAGE_LIMIT",
    "PROFILE_REVIEW_RESPONSE_LIMIT_BYTES",
    "PROFILE_REVIEW_SECTION_ITEM_LIMIT",
    "PROFILE_REVIEW_SUMMARY_LIMIT_BYTES",
    "SECTION_LABELS",
    "build_profile_review_section_changes",
    "build_profile_review_section",
    "build_profile_review_summary",
    "profile_source_kind",
]
