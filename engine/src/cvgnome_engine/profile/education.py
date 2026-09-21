# SPDX-License-Identifier: MPL-2.0
"""Deterministic cleanup and conservative identity checks for education rows.

Adapted from earlier private work by the original author; only the local
profile behavior is included. See docs/PROVENANCE.md for publication scope.
"""

from __future__ import annotations

import re
from typing import Any


_WHITESPACE_RE = re.compile(r"\s+")
_EDUCATION_EDITORIAL_MARKER_RE = re.compile(
    r"(?:"
    r"dates?\s+(?:were\s+)?not\s+provided\s+(?:in|from)\s+(?:the\s+)?source\s+materials?"
    r"|(?:candidate|applicant)\s+"
    r"(?:reports?|states?|notes?|describes?|indicates?|confirms?)\b"
    r"|as\s+(?:described|reported|stated|noted|confirmed)\s+by\s+"
    r"(?:the\s+)?(?:candidate|applicant)\b"
    r")",
    re.IGNORECASE,
)
_SOURCE_ATTRIBUTION_SUFFIX_RE = re.compile(
    r"\s*\(\s*source\s*\.?\s*\)\s*[.;,:-]*\s*$",
    re.IGNORECASE,
)
_EDUCATION_ANNOTATION_RE = re.compile(
    r"\b(?:qualifying\s+exams?|source\s+materials?|left\s+(?:as\s+)?ABD|"
    r"dates?\s+not\s+provided)\b",
    re.IGNORECASE,
)
_EDUCATION_INSTITUTION_RE = re.compile(
    r"\b(?:university|college|institute|academy|polytechnic|school|conservatory)\b",
    re.IGNORECASE,
)
_EDUCATION_CREDENTIAL_RE = re.compile(
    r"^(?:"
    r"B\.?A\.?|B\.?S\.?|M\.?A\.?|M\.?S\.?|M\.?B\.?A\.?|"
    r"MFA|MEng|J\.?D\.?|M\.?D\.?|Ph\.?D\.?|"
    r"Doctoral(?:\s+Candidate(?:\s+\(ABD\))?)?|Doctorate|Doctor\s+of\b.*|"
    r"Master(?:'?s|\s+of\b.*)?|Bachelor(?:'?s|\s+of\b.*)?|"
    r"Certificate(?:\s+of\b.*)?|Diploma(?:\s+in\b.*)?"
    r")$",
    re.IGNORECASE,
)
_EDUCATION_WORD_STOPWORDS = {"a", "an", "and", "in", "of", "the"}
_EDUCATION_CREDENTIAL_WORDS = {
    "abd",
    "associate",
    "bachelor",
    "doctoral",
    "doctorate",
    "master",
    "phd",
}


def strip_education_editorial_commentary(value: object) -> str:
    """Remove source/candidate commentary while preserving the preceding fact."""

    cleaned = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    if not cleaned:
        return ""
    cleaned = _SOURCE_ATTRIBUTION_SUFFIX_RE.sub("", cleaned).strip()
    marker = _EDUCATION_EDITORIAL_MARKER_RE.search(cleaned)
    if marker is not None:
        cleaned = cleaned[: marker.start()]
    return cleaned.rstrip(" \t,;:.-–—([{").strip()


def looks_like_education_annotation(value: object) -> bool:
    return bool(_EDUCATION_ANNOTATION_RE.search(str(value or "").strip()))


def _education_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _education_words(value: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if token not in _EDUCATION_WORD_STOPWORDS
    }


def _education_institution_token(value: object) -> str:
    without_parenthetical = re.sub(r"\([^)]*\)", "", str(value or ""))
    return _education_token(without_parenthetical)


def education_values_compatible(
    existing: object,
    candidate: object,
    *,
    field: str,
) -> bool:
    """Return whether two degree/field labels can represent one education row."""

    existing_text = str(existing or "").strip()
    candidate_text = str(candidate or "").strip()
    existing_token = _education_token(existing_text)
    candidate_token = _education_token(candidate_text)
    if not existing_token or not candidate_token or existing_token == candidate_token:
        return True

    existing_words = _education_words(existing_text)
    candidate_words = _education_words(candidate_text)
    shared_words = existing_words & candidate_words
    if field == "area":
        shorter_words = min(
            (existing_words, candidate_words),
            key=lambda words: (len(words), sorted(words)),
        )
        return bool(shorter_words) and shorter_words <= shared_words
    if field == "studyType":
        return len(shared_words) >= 2 and bool(
            shared_words & _EDUCATION_CREDENTIAL_WORDS
        )
    return False


def education_entries_can_merge(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Conservatively match semantically duplicate structured education rows."""

    existing_institution = _education_institution_token(
        existing.get("institution")
        or existing.get("school")
        or existing.get("university")
        or existing.get("college")
    )
    candidate_institution = _education_institution_token(
        candidate.get("institution")
        or candidate.get("school")
        or candidate.get("university")
        or candidate.get("college")
    )
    if (
        not existing_institution
        or not candidate_institution
        or existing_institution != candidate_institution
    ):
        return False

    for key in ("startDate", "endDate"):
        existing_date = _education_token(existing.get(key))
        candidate_date = _education_token(candidate.get(key))
        if existing_date and candidate_date and existing_date != candidate_date:
            return False
    return education_values_compatible(
        existing.get("studyType") or existing.get("degree"),
        candidate.get("studyType") or candidate.get("degree"),
        field="studyType",
    ) and education_values_compatible(
        existing.get("area") or existing.get("field") or existing.get("major"),
        candidate.get("area") or candidate.get("field") or candidate.get("major"),
        field="area",
    )


def parse_legacy_education_summary(value: object) -> dict[str, str]:
    """Parse only an unmistakable ``Institution: Credential, Area`` row."""

    cleaned = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    if not cleaned or ":" not in cleaned:
        return {}
    institution_raw, credential_area_raw = cleaned.split(":", 1)
    institution = strip_education_editorial_commentary(institution_raw)
    if (
        not institution
        or len(institution) > 160
        or not _EDUCATION_INSTITUTION_RE.search(institution)
    ):
        return {}
    credential_parts = credential_area_raw.split(",", 1)
    credential = strip_education_editorial_commentary(credential_parts[0])
    if (
        not credential
        or len(credential) > 120
        or not _EDUCATION_CREDENTIAL_RE.fullmatch(credential)
    ):
        return {}
    parsed = {"institution": institution, "studyType": credential}
    if len(credential_parts) == 2:
        area = strip_education_editorial_commentary(credential_parts[1])
        if area:
            parsed["area"] = area[:160]
    return parsed
