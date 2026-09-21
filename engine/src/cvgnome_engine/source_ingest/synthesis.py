# SPDX-License-Identifier: MPL-2.0
"""Deterministic, offline synthesis of bounded career-profile sources."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Sequence
import unicodedata


_JSON_RESUME_SCHEMA = (
    "https://raw.githubusercontent.com/jsonresume/resume-schema/"
    "v1.0.0/schema.json"
)
_PROFILE_KEYS = frozenset(
    {
        "$schema",
        "basics",
        "work",
        "volunteer",
        "education",
        "awards",
        "certificates",
        "publications",
        "skills",
        "languages",
        "interests",
        "references",
        "projects",
    }
)
_PROFILE_CONTAINER_KEYS = (
    "resume_json",
    "baseline_resume_json",
    "canonical_profile",
    "profile",
    "payload",
)
_GENERIC_LIST_SECTIONS = (
    "volunteer",
    "awards",
    "certificates",
    "publications",
    "languages",
    "interests",
    "references",
    "projects",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
_KEY_VALUE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?"
    r"(?P<key>[A-Za-z][A-Za-z0-9 /&_.'-]{1,48})"
    r"(?:\*\*)?\s*(?::|\|)(?:\*\*)?\s*(?P<value>.+?)\s*$"
)
_HEADING_RE = re.compile(r"^\s*(?P<marks>#{1,6})\s+(?P<text>.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(?P<text>.+?)\s*$")
_IMPACT_VERB_RE = re.compile(
    r"\b(?:achieved|automated|built|cut|delivered|designed|generated|grew|"
    r"implemented|improved|increased|launched|led|managed|raised|reduced|"
    r"saved|shipped)\b",
    re.I,
)
_QUANTIFIED_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:%|percent|x|hours?|days?|weeks?|months?|"
    r"users?|customers?|clients?|people|million|billion|k)\b|[$£€]\s*\d)",
    re.I,
)
_DATE_RANGE_RE = re.compile(
    r"^\s*(?P<start>.+?)\s+(?:-|–|—|to)\s+(?P<end>.+?)\s*$",
    re.I,
)
_DATE_TOKEN = (
    r"(?:[12][0-9]{3}-(?:0[1-9]|1[0-2])|(?:0?[1-9]|1[0-2])/[12][0-9]{3}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+[12][0-9]{3}|[12][0-9]{3})"
)
_CONVENTIONAL_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE_TOKEN})\s*(?:-|–|—|\bto\b)\s*(?P<end>{_DATE_TOKEN}|present|current)",
    re.I,
)
_ROLE_WORDS = frozenset({
    "analyst", "architect", "consultant", "coordinator", "designer", "developer",
    "director", "editor", "engineer", "intern", "lead", "manager", "officer",
    "president", "producer", "professor", "researcher", "scientist", "specialist",
    "supervisor", "technician",
})
_EMPLOYER_WORDS = frozenset({
    "agency", "associates", "bank", "center", "centre", "co", "college", "company",
    "corp", "corporation", "engines", "foundation", "group", "hospital", "inc",
    "institute", "labs", "laboratories", "llc", "llp", "ltd", "partners", "plc",
    "school", "solutions", "studio", "studios", "systems", "technologies", "university",
})
_SECTION_TITLES = {
    "experience": "work",
    "work experience": "work",
    "professional experience": "work",
    "employment": "work",
    "education": "education",
    "academic background": "education",
    "skills": "skills",
    "technical skills": "skills",
    "core skills": "skills",
}
_NON_NAME_HEADINGS = frozenset(
    {
        "cv",
        "resume",
        "curriculum vitae",
        "summary",
        "professional summary",
        "profile",
        "contact",
        *_SECTION_TITLES.keys(),
    }
)
_SUMMARY_HEADINGS = frozenset({"summary", "professional summary", "executive summary", "profile"})
_HEADER_BODY_HEADINGS = frozenset({
    *_SECTION_TITLES, *_SUMMARY_HEADINGS, "projects", "selected projects",
    "certifications", "certificates", "awards", "publications", "references",
    "professional references", "achievements", "selected achievements",
})
_TITLE_OR_ORGANIZATION_WORDS = frozenset({
    "analyst", "architect", "consultant", "consulting", "designer", "developer",
    "director", "engineer", "engineering", "executive", "head", "lead", "leader",
    "manager", "officer", "president", "principal", "scientist", "specialist",
    "senior", "junior", "staff", "company", "corporation", "inc", "llc", "ltd",
    "university", "college",
})


@dataclass(frozen=True, slots=True)
class SynthesisLimits:
    """Deterministic source and provenance budgets for one synthesis pass."""

    max_sources: int = 100
    max_chars_per_source: int = 30_000
    max_total_chars: int = 120_000
    max_provenance_facts: int = 240
    max_context_items: int = 40


@dataclass(frozen=True, slots=True)
class _Source:
    source_id: str
    display_name: str
    media_type: str
    sha256: str
    text_sha256: str
    text: str
    original_chars: int
    truncated: bool

    def manifest(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "text_sha256": self.text_sha256,
            "characters_used": len(self.text),
            "truncated": self.truncated,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    encoded = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _clean_text(value: Any, *, max_chars: int = 500) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned[:max_chars].rstrip()


def _clean_markdown(value: Any, *, max_chars: int = 500) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned)
    cleaned = re.sub(r"^[-*•]\s+", "", cleaned)
    cleaned = cleaned.strip("`*_ ")
    return _clean_text(cleaned, max_chars=max_chars)


def _key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _is_non_empty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def _safe_display_name(value: Any, *, fallback: str) -> str:
    cleaned = "".join(
        character
        for character in str(value or fallback).replace("\\", "/")
        if character.isprintable()
        and not unicodedata.category(character).startswith("C")
    ).strip()
    cleaned = cleaned.lstrip("/")
    return cleaned[:512] or fallback


def _validate_limits(limits: SynthesisLimits) -> None:
    for name, value, maximum in (
        ("max_sources", limits.max_sources, 1_000),
        ("max_chars_per_source", limits.max_chars_per_source, 200_000),
        ("max_total_chars", limits.max_total_chars, 2_000_000),
        ("max_provenance_facts", limits.max_provenance_facts, 2_000),
        ("max_context_items", limits.max_context_items, 200),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} is outside the supported range")


def _normalize_sources(
    sources: Sequence[dict[str, Any]], limits: SynthesisLimits
) -> tuple[list[_Source], dict[str, Any]]:
    candidates: list[tuple[str, str, str, int, dict[str, Any], str]] = []
    invalid = 0
    for index, raw in enumerate(sources):
        if not isinstance(raw, dict):
            invalid += 1
            continue
        text_value = raw.get("text")
        if not isinstance(text_value, str):
            text_value = raw.get("content")
        if not isinstance(text_value, str) or not text_value.strip():
            invalid += 1
            continue
        display_name = _safe_display_name(
            raw.get("display_name") or raw.get("filename"),
            fallback=f"source-{index + 1}.txt",
        )
        supplied_sha = str(raw.get("sha256") or "").strip().casefold()
        text_sha = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
        file_sha = (
            supplied_sha
            if _SHA256_RE.fullmatch(supplied_sha)
            else _digest(
                {
                    "display_name": display_name,
                    "media_type": str(
                        raw.get("media_type") or raw.get("content_type") or "text/plain"
                    ),
                    "text_sha256": text_sha,
                }
            )
        )
        candidates.append(
            (
                display_name.casefold(),
                file_sha,
                text_sha,
                index,
                raw,
                text_value,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    selected = candidates[: limits.max_sources]
    skipped_for_count = max(0, len(candidates) - len(selected))
    normalized: list[_Source] = []
    duplicate_texts = 0
    seen_text_hashes: set[str] = set()
    remaining = limits.max_total_chars
    truncated_ids: list[str] = []
    for _display_marker, file_sha, _full_text_sha, index, raw, raw_text in selected:
        if remaining <= 0:
            skipped_for_count += 1
            continue
        original_chars = len(raw_text)
        per_source_clipped = raw_text[: limits.max_chars_per_source]
        duplicate_marker = hashlib.sha256(per_source_clipped.encode("utf-8")).hexdigest()
        if duplicate_marker in seen_text_hashes:
            duplicate_texts += 1
            continue
        seen_text_hashes.add(duplicate_marker)
        clipped = per_source_clipped[:remaining]
        text_sha = hashlib.sha256(clipped.encode("utf-8")).hexdigest()
        display_name = _safe_display_name(
            raw.get("display_name") or raw.get("filename"),
            fallback=f"source-{index + 1}.txt",
        )
        media_type = _clean_text(
            raw.get("media_type") or raw.get("content_type") or "text/plain",
            max_chars=255,
        ) or "text/plain"
        source_identity = {
            "sha256": file_sha,
            "text_sha256": text_sha,
            "display_name": display_name,
            "media_type": media_type,
        }
        source_id = f"src_{_digest(source_identity)[:24]}"
        truncated = len(clipped) < original_chars
        if truncated:
            truncated_ids.append(source_id)
        normalized.append(
            _Source(
                source_id=source_id,
                display_name=display_name,
                media_type=media_type,
                sha256=file_sha,
                text_sha256=text_sha,
                text=clipped,
                original_chars=original_chars,
                truncated=truncated,
            )
        )
        remaining -= len(clipped)

    input_digest = _digest([source.manifest() for source in normalized])
    report = {
        "source_count_received": len(sources),
        "source_count_used": len(normalized),
        "source_count_skipped": invalid + skipped_for_count + duplicate_texts,
        "duplicate_text_sources": duplicate_texts,
        "characters_used": sum(len(source.text) for source in normalized),
        "truncated_source_ids": sorted(truncated_ids),
        "input_digest_sha256": input_digest,
        "warnings": sorted(
            {
                *(["invalid_or_empty_source_skipped"] if invalid else []),
                *(["source_count_limit_reached"] if skipped_for_count else []),
                *(["duplicate_text_skipped"] if duplicate_texts else []),
                *(["source_text_truncated"] if truncated_ids else []),
            }
        ),
    }
    return normalized, report


class _Provenance:
    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self.facts: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self.omitted = 0
        self.basic_candidates: list[dict[str, Any]] = []

    def add(
        self,
        *,
        path: str,
        value: Any,
        source: _Source,
        locator: dict[str, Any],
        evidence: str,
        action: str,
        profile_section: str | None = None,
        profile_entry_index: int | None = None,
    ) -> bool:
        evidence_sha = _digest(evidence)
        value_sha = _digest(value)
        identity = {
            "path": path,
            "source_id": source.source_id,
            "locator": locator,
            "evidence_sha256": evidence_sha,
            "value_sha256": value_sha,
            "action": action,
        }
        fact_id = f"fact_{_digest(identity)[:24]}"
        if fact_id in self._ids:
            return True
        if len(self.facts) >= self.limit:
            self.omitted += 1
            return False
        self._ids.add(fact_id)
        fact = {
            "id": fact_id,
            **identity,
            "confidence": "high",
        }
        if profile_section is not None and profile_entry_index is not None:
            fact["profile_section"] = profile_section
            fact["profile_entry_index"] = profile_entry_index
        self.facts.append(fact)
        return True

    def sorted_facts(self) -> list[dict[str, Any]]:
        return sorted(
            self.facts,
            key=lambda item: (
                str(item["source_id"]),
                _canonical_json(item["locator"]),
                str(item["path"]),
                str(item["id"]),
            ),
        )


def _merge_missing(existing: Any, candidate: Any) -> Any:
    if isinstance(existing, dict) and isinstance(candidate, dict):
        merged = copy.deepcopy(existing)
        for key, value in candidate.items():
            if key in merged:
                merged[key] = _merge_missing(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    if isinstance(existing, list) and isinstance(candidate, list):
        merged = copy.deepcopy(existing)
        markers = {_canonical_json(item) for item in merged}
        for item in candidate:
            marker = _canonical_json(item)
            if marker not in markers:
                markers.add(marker)
                merged.append(copy.deepcopy(item))
        return merged
    return copy.deepcopy(existing if _is_non_empty(existing) else candidate)


def _has_conflict(existing: Any, candidate: Any) -> bool:
    if isinstance(existing, dict) and isinstance(candidate, dict):
        return any(
            key in existing and _has_conflict(existing[key], value)
            for key, value in candidate.items()
        )
    if isinstance(existing, list) and isinstance(candidate, list):
        return False
    return _is_non_empty(existing) and _is_non_empty(candidate) and existing != candidate


def _action(before: Any, candidate: Any, after: Any) -> str:
    if not _is_non_empty(before):
        return "added"
    if after != before:
        return "extended"
    if _has_conflict(before, candidate):
        return "conflict_preserved"
    return "matched"


def _recorded_basic(
    profile: dict[str, Any],
    *,
    key: str,
    value: Any,
    source: _Source,
    locator: dict[str, Any],
    evidence: str,
    provenance: _Provenance,
) -> None:
    if not _is_non_empty(value):
        return
    basics = profile.get("basics") if isinstance(profile.get("basics"), dict) else {}
    basics = copy.deepcopy(basics)
    before = basics.get(key)
    after = _merge_missing(before, value) if key in basics else copy.deepcopy(value)
    recorded = provenance.add(
        path=f"basics.{key}",
        value=value,
        source=source,
        locator=locator,
        evidence=evidence,
        action=_action(before, value, after),
    )
    if not recorded:
        return
    if key in {"name", "label", "summary", "email", "phone", "url"} and isinstance(value, str):
        provenance.basic_candidates.append({
            "key": key, "value": value, "sha256": source.sha256,
            "display_name": source.display_name, "locator": copy.deepcopy(locator),
        })
    basics[key] = after
    profile["basics"] = basics


def _entry_identity(section: str, entry: dict[str, Any]) -> str:
    if section == "work":
        fields = ("name", "position", "startDate", "endDate")
    elif section == "education":
        fields = ("institution", "studyType", "area", "startDate", "endDate")
    elif section == "skills":
        fields = ("name",)
    elif section == "projects":
        fields = ("name", "startDate", "endDate")
    else:
        fields = tuple(sorted(entry))
    return "|".join(_key(entry.get(field)) for field in fields)


def _entries_match(section: str, left: dict[str, Any], right: dict[str, Any]) -> bool:
    if section == "skills":
        return bool(_key(left.get("name"))) and _key(left.get("name")) == _key(right.get("name"))
    if section == "work":
        return (
            bool(_key(left.get("name")))
            and bool(_key(left.get("position")))
            and _key(left.get("name")) == _key(right.get("name"))
            and _key(left.get("position")) == _key(right.get("position"))
            and (
                not left.get("startDate")
                or not right.get("startDate")
                or _key(left.get("startDate")) == _key(right.get("startDate"))
            )
        )
    if section == "education":
        return (
            bool(_key(left.get("institution")))
            and _key(left.get("institution")) == _key(right.get("institution"))
            and (
                not left.get("studyType")
                or not right.get("studyType")
                or _key(left.get("studyType")) == _key(right.get("studyType"))
            )
        )
    if section == "projects":
        return (
            bool(_key(left.get("name")))
            and _key(left.get("name")) == _key(right.get("name"))
            and (
                not left.get("startDate")
                or not right.get("startDate")
                or _key(left.get("startDate")) == _key(right.get("startDate"))
            )
        )
    return _canonical_json(left) == _canonical_json(right)


def _recorded_entry(
    profile: dict[str, Any],
    *,
    section: str,
    entry: dict[str, Any],
    source: _Source,
    locator: dict[str, Any],
    evidence: str,
    provenance: _Provenance,
) -> None:
    if not entry:
        return
    values = profile.get(section) if isinstance(profile.get(section), list) else []
    values = copy.deepcopy(values)
    match_index = next(
        (
            index
            for index, existing in enumerate(values)
            if isinstance(existing, dict) and _entries_match(section, existing, entry)
        ),
        None,
    )
    before: Any = None
    if match_index is None:
        entry_index = len(values)
        values.append(copy.deepcopy(entry))
        after = entry
    else:
        entry_index = match_index
        before = values[match_index]
        after = _merge_missing(before, entry)
        values[match_index] = after
    identity = _digest({"section": section, "identity": _entry_identity(section, entry)})[:16]
    recorded = provenance.add(
        path=f"{section}[id={identity}]",
        value=entry,
        source=source,
        locator=locator,
        evidence=evidence,
        action=_action(before, entry, after),
        profile_section=section,
        profile_entry_index=entry_index,
    )
    if not recorded:
        return
    profile[section] = values


def _recorded_context(
    profile: dict[str, Any],
    *,
    key: str,
    value: str,
    source: _Source,
    locator: dict[str, Any],
    evidence: str,
    provenance: _Provenance,
    max_items: int,
) -> None:
    cleaned = _clean_text(value, max_chars=360)
    if not cleaned:
        return
    meta = profile.get("meta") if isinstance(profile.get("meta"), dict) else {}
    meta = copy.deepcopy(meta)
    context = (
        meta.get("canonical_context")
        if isinstance(meta.get("canonical_context"), dict)
        else {}
    )
    context = copy.deepcopy(context)
    before = context.get(key) if isinstance(context.get(key), list) else []
    values = list(before)
    markers = {_key(item) for item in values}
    if _key(cleaned) not in markers and len(values) < max_items:
        values.append(cleaned)
    recorded = provenance.add(
        path=f"meta.canonical_context.{key}",
        value=cleaned,
        source=source,
        locator=locator,
        evidence=evidence,
        action="added" if values != before else "matched",
    )
    if not recorded:
        return
    context[key] = values
    meta["canonical_context"] = context
    profile["meta"] = meta


def _looks_like_profile_json(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("basics"), dict):
        return True
    if isinstance(value.get("$schema"), str) and any(key in value for key in _PROFILE_KEYS):
        return True
    return any(isinstance(value.get(key), list) for key in _PROFILE_KEYS - {"$schema", "basics"})


def _find_profile_json(
    value: Any, *, pointer: str = "", depth: int = 4
) -> tuple[dict[str, Any], str] | None:
    if depth <= 0 or not isinstance(value, dict):
        return None
    if _looks_like_profile_json(value):
        return value, pointer or "/"
    for key in _PROFILE_CONTAINER_KEYS:
        nested = value.get(key)
        if isinstance(nested, dict):
            escaped = key.replace("~", "~0").replace("/", "~1")
            found = _find_profile_json(
                nested, pointer=f"{pointer}/{escaped}", depth=depth - 1
            )
            if found is not None:
                return found
    for key in sorted(value):
        if key in _PROFILE_CONTAINER_KEYS:
            continue
        nested = value.get(key)
        if isinstance(nested, dict):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            found = _find_profile_json(
                nested, pointer=f"{pointer}/{escaped}", depth=depth - 1
            )
            if found is not None:
                return found
    return None


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if _is_non_empty(mapping.get(key)):
            return mapping[key]
    return None


def _string_list(value: Any, *, max_items: int = 20, max_chars: int = 180) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[,;|\n]", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            continue
        cleaned = _clean_text(item, max_chars=max_chars).strip("-•* ")
        marker = _key(cleaned)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        output.append(cleaned)
        if len(output) >= max_items:
            break
    return output


def _normalize_work(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = _clean_text(
        _first(value, ("name", "company", "employer", "organization")),
        max_chars=180,
    )
    position = _clean_text(_first(value, ("position", "title", "role", "jobTitle")), max_chars=180)
    if not name or not position:
        return None
    result: dict[str, Any] = {"name": name, "position": position}
    for target, keys, maximum in (
        ("url", ("url",), 500),
        ("startDate", ("startDate", "start_date", "start"), 80),
        ("endDate", ("endDate", "end_date", "end"), 80),
        ("summary", ("summary", "description"), 1_000),
    ):
        cleaned = _clean_text(_first(value, keys), max_chars=maximum)
        if cleaned:
            result[target] = cleaned
    highlights = _string_list(value.get("highlights"), max_items=12, max_chars=500)
    if highlights:
        result["highlights"] = highlights
    return result


def _normalize_education(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    institution = _clean_text(
        _first(value, ("institution", "school", "university", "college")),
        max_chars=180,
    )
    study_type = _clean_text(
        _first(value, ("studyType", "study_type", "degree", "credential")),
        max_chars=140,
    )
    area = _clean_text(_first(value, ("area", "field", "major", "focus")), max_chars=140)
    if not institution or not (study_type or area):
        return None
    result: dict[str, Any] = {"institution": institution}
    if study_type:
        result["studyType"] = study_type
    if area:
        result["area"] = area
    for target, keys in (
        ("startDate", ("startDate", "start_date", "start")),
        ("endDate", ("endDate", "end_date", "end", "graduation")),
        ("score", ("score", "gpa")),
    ):
        cleaned = _clean_text(_first(value, keys), max_chars=80)
        if cleaned:
            result[target] = cleaned
    courses = _string_list(value.get("courses"), max_items=20, max_chars=180)
    if courses:
        result["courses"] = courses
    return result


def _normalize_skill(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        keywords = _string_list(value)
        return {"name": "Core", "keywords": keywords} if keywords else None
    if not isinstance(value, dict):
        return None
    name = _clean_text(value.get("name") or value.get("category") or "Core", max_chars=100)
    keywords = _string_list(
        value.get("keywords") or value.get("skills"),
        max_items=40,
        max_chars=100,
    )
    if not keywords:
        return None
    return {"name": name or "Core", "keywords": keywords}


def _normalize_basic_value(key: str, value: Any) -> Any | None:
    if key == "location":
        if not isinstance(value, dict):
            return None
        location: dict[str, str] = {}
        for field in ("address", "postalCode", "city", "countryCode", "region"):
            cleaned = _clean_text(value.get(field), max_chars=180)
            if cleaned:
                location[field] = cleaned
        return location or None
    if key == "profiles":
        if not isinstance(value, list):
            return None
        profiles: list[dict[str, str]] = []
        for item in value[:20]:
            if not isinstance(item, dict):
                continue
            profile: dict[str, str] = {}
            for field in ("network", "username", "url"):
                cleaned = _clean_text(item.get(field), max_chars=500)
                if cleaned:
                    profile[field] = cleaned
            if profile:
                profiles.append(profile)
        return profiles or None
    if not isinstance(value, str):
        return None
    maximum = 1_000 if key == "summary" else 500
    cleaned = _clean_text(value, max_chars=maximum)
    return cleaned or None


def _safe_generic_entry(value: Any) -> Any | None:
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(encoded) > 8_000:
        return None
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str) and value.strip():
        return _clean_text(value, max_chars=500)
    return None


def _merge_json_profile(
    profile: dict[str, Any],
    *,
    candidate: dict[str, Any],
    pointer: str,
    source: _Source,
    provenance: _Provenance,
) -> None:
    basics = candidate.get("basics")
    if isinstance(basics, dict):
        for key in (
            "name",
            "label",
            "image",
            "email",
            "phone",
            "url",
            "summary",
            "location",
            "profiles",
        ):
            value = _normalize_basic_value(key, basics.get(key))
            if _is_non_empty(value):
                _recorded_basic(
                    profile,
                    key=key,
                    value=copy.deepcopy(value),
                    source=source,
                    locator={
                        "kind": "json_pointer",
                        "value": f"{pointer.rstrip('/')}/basics/{key}",
                    },
                    evidence=_canonical_json(value),
                    provenance=provenance,
                )

    for section, normalizer in (
        ("work", _normalize_work),
        ("education", _normalize_education),
        ("skills", _normalize_skill),
    ):
        values = candidate.get(section)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values[:100]):
            normalized = normalizer(value)
            if normalized is None:
                continue
            _recorded_entry(
                profile,
                section=section,
                entry=normalized,
                source=source,
                locator={
                    "kind": "json_pointer",
                    "value": f"{pointer.rstrip('/')}/{section}/{index}",
                },
                evidence=_canonical_json(value),
                provenance=provenance,
            )

    for section in _GENERIC_LIST_SECTIONS:
        values = candidate.get(section)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values[:40]):
            normalized = _safe_generic_entry(value)
            if normalized is None:
                continue
            entry = normalized if isinstance(normalized, dict) else {"name": normalized}
            _recorded_entry(
                profile,
                section=section,
                entry=entry,
                source=source,
                locator={
                    "kind": "json_pointer",
                    "value": f"{pointer.rstrip('/')}/{section}/{index}",
                },
                evidence=_canonical_json(value),
                provenance=provenance,
            )


def _line_locator(start: int, end: int | None = None) -> dict[str, Any]:
    return {"kind": "line", "start": start, "end": end or start}


def _parse_location(value: str) -> dict[str, str]:
    parts = [_clean_text(part, max_chars=100) for part in value.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return {}
    location = {"city": parts[0]}
    if len(parts) >= 2:
        location["region"] = parts[1]
    if len(parts) >= 3:
        location["countryCode"] = parts[2]
    return location


def _parse_date_range(value: str) -> tuple[str, str]:
    match = _DATE_RANGE_RE.match(_clean_text(value, max_chars=180))
    if not match:
        return "", ""
    return (
        _clean_text(match.group("start"), max_chars=80),
        _clean_text(match.group("end"), max_chars=80),
    )


def _conventional_work_header(value: str, *, person_names: set[str]) -> tuple[str, str] | None:
    if len(value) > 360 or _has_header_contact(value) or re.search(r"https?://|www\.", value, re.I):
        return None
    # Split every delimiter: an extra date/location/contact column is not part
    # of an employer name. ASCII hyphens inside names/titles are not delimiters.
    parts = [part.strip() for part in re.split(r"\s*(?:\||—|–)\s*|\s+@\s+", value)]
    if len(parts) != 2 or any(not part or len(part) > 180 for part in parts):
        return None
    roles = [index for index, part in enumerate(parts) if _ROLE_WORDS.intersection(_key(part).split())]
    if len(roles) != 1:
        return None
    position, employer = parts[roles[0]], parts[1 - roles[0]]
    if any(_key(part) in person_names for part in parts) or any("@" in part for part in parts):
        return None
    # The @ form states a relationship. Pipe/dash columns need an organization
    # signal as well as a role signal; otherwise company, person and location
    # columns are too easy to confuse. Explicit labelled fields remain separate.
    if not re.search(r"\s+@\s+", value) and not _EMPLOYER_WORDS.intersection(_key(employer).split()):
        return None
    if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", employer) or employer.endswith(("!", "?")):
        return None
    return position, employer


def _blocks(lines: list[str]) -> list[list[tuple[int, str]]]:
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        if _HEADING_RE.match(line) and current:
            blocks.append(current)
            current = []
        current.append((number, line))
    if current:
        blocks.append(current)
    return blocks


def _block_fields(block: list[tuple[int, str]]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    bullets: list[str] = []
    for _line_number, raw_line in block:
        key_value = _KEY_VALUE_RE.match(raw_line)
        if key_value:
            values[_key(key_value.group("key"))] = _clean_markdown(
                key_value.group("value"), max_chars=1_000
            )
            continue
        bullet = _BULLET_RE.match(raw_line)
        if bullet:
            cleaned = _clean_markdown(bullet.group("text"), max_chars=500)
            if cleaned:
                bullets.append(cleaned)
    return values, bullets


def _field(values: dict[str, str], aliases: Iterable[str]) -> str:
    for alias in aliases:
        candidate = values.get(alias)
        if candidate:
            return candidate
    return ""


def _looks_like_name(value: str) -> bool:
    cleaned = _clean_markdown(value, max_chars=120)
    if (
        _key(cleaned) in _NON_NAME_HEADINGS
        or any(character.isdigit() for character in cleaned)
        or any(marker in cleaned for marker in (":", "@", "/"))
    ):
        return False
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.-]*", cleaned)
    if not 2 <= len(words) <= 5:
        return False
    return all(word[0].isupper() for word in words)


def _has_header_contact(value: str) -> bool:
    if _EMAIL_RE.search(value):
        return True
    # An unlabelled phone is common in resume headers. Requiring 10–15 digits
    # avoids treating ordinary year ranges or dates as contact evidence.
    return any(
        10 <= len(re.sub(r"\D", "", match.group())) <= 15
        for match in re.finditer(r"(?<!\w)\+?\d[\d () .-]{8,}\d(?!\w)", value)
    )


def _plain_heading(value: str) -> str:
    text = _clean_markdown(value)
    # Do not mistake a JSON property such as '"profile": {' for a heading.
    return _key(text) if re.fullmatch(r"[A-Za-z][A-Za-z ]*:? *", text) else ""


def _header_name(lines: list[str]) -> tuple[int, str, str] | None:
    header: list[tuple[int, str]] = []
    for number, raw in enumerate(lines[:80], start=1):
        text = _clean_markdown(raw, max_chars=500)
        if _plain_heading(text) in _HEADER_BODY_HEADINGS:
            break
        if text:
            header.append((number, raw))
        if len(header) >= 12:
            break
    contacts = [index for index, (_number, raw) in enumerate(header) if _has_header_contact(raw)]
    if not contacts:
        return None
    candidates: dict[str, tuple[int, str, str, bool]] = {}
    for index, (number, raw) in enumerate(header):
        if not any(abs(index - contact) <= 3 for contact in contacts):
            continue
        text = _clean_markdown(raw, max_chars=500)
        # A PDF footer may be emitted before its visual header. Permit a name
        # next to delimited contact information, but do not split arbitrary
        # title/company lines into asserted names.
        fragments = re.split(r"\s*[|•·]\s*", text) if _has_header_contact(text) else [text]
        for fragment in fragments:
            if (
                len(fragment) > 120
                or not _looks_like_name(fragment)
                or re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ\s'.-]*", fragment) is None
                or _TITLE_OR_ORGANIZATION_WORDS.intersection(_key(fragment).split())
            ):
                continue
            key = _key(fragment)
            standalone = len(fragments) == 1
            previous = candidates.get(key)
            # Case-only duplicates are the same identity. Prefer the standalone
            # header spelling and its evidence over an earlier PDF footer.
            if previous is None or (standalone and not previous[3]):
                candidates[key] = (number, fragment, raw, standalone)
    if len(candidates) != 1:
        return None  # Multiple people/locations require explicit review.
    number, name, evidence, _standalone = next(iter(candidates.values()))
    return number, name, evidence


def _leading_summary(lines: list[str]) -> tuple[int, int, str, str] | None:
    for offset, raw in enumerate(lines[:80]):
        title = _plain_heading(raw)
        if title not in _SUMMARY_HEADINGS:
            if title in _HEADER_BODY_HEADINGS:
                return None  # A role's later "Summary" is not the person's summary.
            continue
        pieces: list[str] = []
        last_line = offset + 1
        for following_offset in range(offset + 1, min(len(lines), offset + 9)):
            following = lines[following_offset]
            text = _clean_markdown(following, max_chars=1_000)
            if not text:
                if pieces:
                    break
                continue
            if (
                _HEADING_RE.match(following)
                or _plain_heading(text) in _HEADER_BODY_HEADINGS
                or _KEY_VALUE_RE.match(following)
                or _BULLET_RE.match(following)
                or (len(text) <= 60 and not text.endswith((".", "!", "?")) and (
                    text.isupper() or _looks_like_name(text)
                    or (pieces and pieces[-1].endswith((".", "!", "?")))
                ))
            ):
                break
            pieces.append(text)
            last_line = following_offset + 1
            if len(" ".join(pieces)) >= 1_000:
                break
        if pieces:
            return (offset + 1, last_line, _clean_text(" ".join(pieces), max_chars=1_000),
                    "\n".join(lines[offset:last_line]))
        return None
    return None


def _extract_text_profile(
    profile: dict[str, Any],
    *,
    source: _Source,
    provenance: _Provenance,
    limits: SynthesisLimits,
) -> None:
    lines = source.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    identity = _header_name(lines)
    if identity is not None:
        number, name, evidence = identity
        _recorded_basic(
            profile, key="name", value=name, source=source,
            locator=_line_locator(number), evidence=evidence, provenance=provenance,
        )
    summary = _leading_summary(lines)
    if summary is not None:
        start, end, text, evidence = summary
        _recorded_basic(
            profile, key="summary", value=text, source=source,
            locator=_line_locator(start, end), evidence=evidence, provenance=provenance,
        )

    for line_number, raw_line in enumerate(lines, start=1):
        key_value = _KEY_VALUE_RE.match(raw_line)
        normalized_key = _key(key_value.group("key")) if key_value else ""
        value = _clean_markdown(key_value.group("value"), max_chars=1_000) if key_value else ""
        locator = _line_locator(line_number)
        if normalized_key in {"name", "full name", "candidate name"} and _looks_like_name(value):
            _recorded_basic(
                profile,
                key="name",
                value=value,
                source=source,
                locator=locator,
                evidence=raw_line,
                provenance=provenance,
            )
        if normalized_key in {"email", "email address"} and _EMAIL_RE.search(value):
            email = _EMAIL_RE.search(value)
            assert email is not None
            _recorded_basic(
                profile,
                key="email",
                value=email.group(1),
                source=source,
                locator=locator,
                evidence=raw_line,
                provenance=provenance,
            )
        elif not normalized_key or (identity is not None and line_number == identity[0]):
            email = _EMAIL_RE.search(raw_line)
            if email:
                _recorded_basic(
                    profile,
                    key="email",
                    value=email.group(1),
                    source=source,
                    locator=locator,
                    evidence=raw_line,
                    provenance=provenance,
                )
        if normalized_key in {"phone", "phone number", "mobile", "telephone"}:
            phone = _clean_text(value, max_chars=80)
            if len(re.sub(r"\D", "", phone)) >= 7:
                _recorded_basic(
                    profile,
                    key="phone",
                    value=phone,
                    source=source,
                    locator=locator,
                    evidence=raw_line,
                    provenance=provenance,
                )
        if normalized_key in {"location", "city", "based in"}:
            location = _parse_location(value)
            if location:
                _recorded_basic(
                    profile,
                    key="location",
                    value=location,
                    source=source,
                    locator=locator,
                    evidence=raw_line,
                    provenance=provenance,
                )
        if normalized_key in {"headline", "professional title", "current title"}:
            _recorded_basic(
                profile,
                key="label",
                value=_clean_text(value, max_chars=180),
                source=source,
                locator=locator,
                evidence=raw_line,
                provenance=provenance,
            )
        if normalized_key in {"website", "portfolio", "url"} and re.match(
            r"https?://", value, re.I
        ):
            _recorded_basic(
                profile,
                key="url",
                value=value[:500],
                source=source,
                locator=locator,
                evidence=raw_line,
                provenance=provenance,
            )
        if normalized_key in {
            "skills",
            "technical skills",
            "core skills",
            "tools",
            "technologies",
        }:
            keywords = _string_list(value, max_items=40, max_chars=100)
            if keywords:
                _recorded_entry(
                    profile,
                    section="skills",
                    entry={"name": "Core", "keywords": keywords},
                    source=source,
                    locator=locator,
                    evidence=raw_line,
                    provenance=provenance,
                )
        if normalized_key in {
            "preferences",
            "work preferences",
            "location preference",
            "remote preference",
            "target role",
            "target roles",
            "employment type",
            "dealbreakers",
        }:
            preference = f"{_clean_text(key_value.group('key'), max_chars=80)}: {value}"
            _recorded_context(
                profile,
                key="work_preferences",
                value=preference,
                source=source,
                locator=locator,
                evidence=raw_line,
                provenance=provenance,
                max_items=limits.max_context_items,
            )

        bullet = _BULLET_RE.match(raw_line)
        impact_label = normalized_key in {
            "achievement",
            "achievements",
            "accomplishment",
            "accomplishments",
            "impact",
            "result",
            "highlight",
        }
        evidence_value = value if impact_label else (
            _clean_markdown(bullet.group("text"), max_chars=500) if bullet else ""
        )
        if evidence_value and (
            impact_label
            or (_IMPACT_VERB_RE.search(evidence_value) and _QUANTIFIED_RE.search(evidence_value))
        ):
            _recorded_context(
                profile,
                key="impact_evidence",
                value=evidence_value,
                source=source,
                locator=locator,
                evidence=raw_line,
                provenance=provenance,
                max_items=limits.max_context_items,
            )

    for block in _blocks(lines):
        values, bullets = _block_fields(block)
        start_line, end_line = block[0][0], block[-1][0]
        locator = _line_locator(start_line, end_line)
        evidence = "\n".join(line for _number, line in block)
        organization = _field(values, ("company", "employer", "organization"))
        position = _field(values, ("position", "role", "job title", "title"))
        if organization and position:
            work: dict[str, Any] = {
                "name": _clean_text(organization, max_chars=180),
                "position": _clean_text(position, max_chars=180),
            }
            start = _field(values, ("start date", "start", "from"))
            end = _field(values, ("end date", "end", "to"))
            if not (start or end):
                start, end = _parse_date_range(_field(values, ("dates", "date range")))
            if start:
                work["startDate"] = _clean_text(start, max_chars=80)
            if end:
                work["endDate"] = _clean_text(end, max_chars=80)
            summary = _field(values, ("summary", "description"))
            if summary:
                work["summary"] = _clean_text(summary, max_chars=1_000)
            if bullets:
                work["highlights"] = bullets[:12]
            _recorded_entry(
                profile,
                section="work",
                entry=work,
                source=source,
                locator=locator,
                evidence=evidence,
                provenance=provenance,
            )

        institution = _field(values, ("institution", "school", "university", "college"))
        study_type = _field(values, ("degree", "study type", "credential", "qualification"))
        area = _field(values, ("field", "area", "major", "focus"))
        if institution and (study_type or area):
            education: dict[str, Any] = {
                "institution": _clean_text(institution, max_chars=180)
            }
            if study_type:
                education["studyType"] = _clean_text(study_type, max_chars=140)
            if area:
                education["area"] = _clean_text(area, max_chars=140)
            start = _field(values, ("start date", "start", "from"))
            end = _field(values, ("end date", "end", "graduation", "graduated"))
            if start:
                education["startDate"] = _clean_text(start, max_chars=80)
            if end:
                education["endDate"] = _clean_text(end, max_chars=80)
            _recorded_entry(
                profile,
                section="education",
                entry=education,
                source=source,
                locator=locator,
                evidence=evidence,
                provenance=provenance,
            )

    current_section = ""
    for offset, raw_line in enumerate(lines):
        line_number = offset + 1
        heading = _HEADING_RE.match(raw_line)
        heading_text = (
            _clean_markdown(heading.group("text"), max_chars=240)
            if heading
            else _clean_markdown(raw_line, max_chars=240)
        )
        heading_level = len(heading.group("marks")) if heading else 0

        # Structured document extraction intentionally flattens visual layout.
        # Recognize only the same small, exact section vocabulary when it
        # arrives as a plain paragraph (for example, a Word Heading 1 whose
        # style metadata was unavailable or a PDF's all-caps section label).
        section = _SECTION_TITLES.get(_key(heading_text))
        if section and (heading is not None or len(heading_text) <= 48):
            current_section = section
            continue

        if current_section == "skills":
            if heading is not None and heading_level <= 2:
                current_section = ""
                continue
            bullet = _BULLET_RE.match(raw_line)
            skill_line = _clean_markdown(
                bullet.group("text") if bullet else heading_text,
                max_chars=240,
            )
            if (
                skill_line
                and not _KEY_VALUE_RE.match(raw_line)
                and (
                    heading_level >= 3
                    or bullet is not None
                    or any(mark in skill_line for mark in (",", ";", "|"))
                )
                and not skill_line.endswith(".")
            ):
                keywords = _string_list(skill_line, max_items=20, max_chars=100)
                if keywords:
                    _recorded_entry(
                        profile,
                        section="skills",
                        entry={"name": "Core", "keywords": keywords},
                        source=source,
                        locator=_line_locator(line_number),
                        evidence=raw_line,
                        provenance=provenance,
                    )
            continue

        if current_section != "work":
            if heading is not None and heading_level <= 2:
                current_section = ""
            continue
        if heading is not None and heading_level < 3:
            current_section = ""
            continue

        # This unlabelled heuristic requires exactly two identifiable columns
        # and a separate date-only range. Never reuse the more permissive parser
        # for explicit Dates fields: prose and PDF footers also contain dashes.
        basics = profile.get("basics") if isinstance(profile.get("basics"), dict) else {}
        person_names = {_key(basics.get("name")), _key(identity[1]) if identity else ""} - {""}
        work_header = _conventional_work_header(_clean_markdown(raw_line, max_chars=500), person_names=person_names)
        if work_header is None or offset + 1 >= len(lines):
            continue
        date_line = lines[offset + 1].strip()
        dates = _CONVENTIONAL_DATE_RANGE_RE.fullmatch(date_line)
        if dates is None:
            continue
        start, end = dates.group("start"), dates.group("end")
        highlights: list[str] = []
        last_line = line_number + 1
        for following_offset in range(offset + 2, len(lines)):
            following = lines[following_offset]
            following_heading = _HEADING_RE.match(following)
            following_text = _clean_markdown(
                following_heading.group("text") if following_heading else following,
                max_chars=500,
            )
            if following_heading is not None or _SECTION_TITLES.get(_key(following_text)):
                break
            bullet = _BULLET_RE.match(following)
            if bullet is None:
                if highlights or following.strip():
                    break
                continue
            highlight = _clean_markdown(bullet.group("text"), max_chars=500)
            if highlight:
                highlights.append(highlight)
                last_line = following_offset + 1
            if len(highlights) >= 12:
                break

        work: dict[str, Any] = {"position": work_header[0], "name": work_header[1]}
        if start:
            work["startDate"] = start
        if end:
            work["endDate"] = end
        if highlights:
            work["highlights"] = highlights
        _recorded_entry(
            profile,
            section="work",
            entry=work,
            source=source,
            locator=_line_locator(line_number, last_line),
            evidence="\n".join(lines[offset:last_line]),
            provenance=provenance,
        )


def _relock_existing_basics(
    profile: dict[str, Any], existing_profile: dict[str, Any]
) -> None:
    existing_basics = existing_profile.get("basics")
    if not isinstance(existing_basics, dict):
        return
    generated_basics = profile.get("basics") if isinstance(profile.get("basics"), dict) else {}
    profile["basics"] = _merge_missing(existing_basics, generated_basics)


def synthesize_canonical_profile(
    *,
    existing_profile: dict[str, Any] | None,
    sources: Sequence[dict[str, Any]],
    limits: SynthesisLimits | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a deterministic profile proposal and aggregate extraction report.

    Existing non-empty values always win. New structured entries and missing
    fields may be appended or filled, while conflicting source values are kept
    only in provenance as ``conflict_preserved`` facts.
    """

    if existing_profile is not None and not isinstance(existing_profile, dict):
        raise TypeError("existing_profile must be a dictionary or None")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
        raise TypeError("sources must be a sequence of dictionaries")
    active_limits = limits or SynthesisLimits()
    _validate_limits(active_limits)
    # Fail early if callers hand the deterministic kernel non-JSON state.
    existing = copy.deepcopy(existing_profile or {})
    _canonical_json(existing)
    normalized_sources, report = _normalize_sources(sources, active_limits)
    profile = copy.deepcopy(existing)
    provenance = _Provenance(limit=active_limits.max_provenance_facts)
    json_profile_sources = 0

    for source in normalized_sources:
        content = source.text.lstrip()
        looks_json = (
            source.display_name.casefold().endswith(".json")
            or "json" in source.media_type.casefold()
            or content.startswith("{")
        )
        if looks_json:
            try:
                parsed = json.loads(source.text)
            except json.JSONDecodeError:
                parsed = None
            found = _find_profile_json(parsed) if isinstance(parsed, dict) else None
            if found is not None:
                candidate, pointer = found
                json_profile_sources += 1
                _merge_json_profile(
                    profile,
                    candidate=candidate,
                    pointer=pointer,
                    source=source,
                    provenance=provenance,
                )
        _extract_text_profile(
            profile,
            source=source,
            provenance=provenance,
            limits=active_limits,
        )

    if normalized_sources and "$schema" not in profile:
        profile["$schema"] = _JSON_RESUME_SCHEMA
    _relock_existing_basics(profile, existing)

    facts = provenance.sorted_facts()
    work_source_ids = {fact["source_id"] for fact in facts if fact.get("profile_section") == "work"}
    if any(
        source.source_id not in work_source_ids
        and any(_SECTION_TITLES.get(_plain_heading(line)) == "work" for line in source.text.splitlines())
        for source in normalized_sources
    ):
        report["warnings"] = sorted({*report["warnings"], "work_section_not_imported"})
    action_counts: dict[str, int] = {}
    for fact in facts:
        action = str(fact.get("action") or "matched")
        action_counts[action] = action_counts.get(action, 0) + 1
    synthesis_material = {
        "existing_profile_sha256": _digest(existing),
        "input_digest_sha256": report["input_digest_sha256"],
        "facts": [fact["id"] for fact in facts],
    }
    synthesis_id = f"syn_{_digest(synthesis_material)[:24]}"
    meta = profile.get("meta") if isinstance(profile.get("meta"), dict) else {}
    meta = copy.deepcopy(meta)
    meta["source_ingest"] = {
        "version": 1,
        "synthesis_id": synthesis_id,
        "input_digest_sha256": report["input_digest_sha256"],
        "sources": [source.manifest() for source in normalized_sources],
        "facts": facts,
    }
    profile["meta"] = meta

    report.update(
        {
            "version": 1,
            "synthesis_id": synthesis_id,
            "facts_recorded": len(facts),
            "basic_candidates": provenance.basic_candidates,
            "facts_omitted": provenance.omitted,
            "fact_actions": dict(sorted(action_counts.items())),
            "json_profile_sources": json_profile_sources,
            "work_entries": len(profile.get("work", []))
            if isinstance(profile.get("work"), list)
            else 0,
            "education_entries": len(profile.get("education", []))
            if isinstance(profile.get("education"), list)
            else 0,
            "skill_groups": len(profile.get("skills", []))
            if isinstance(profile.get("skills"), list)
            else 0,
        }
    )
    if provenance.omitted:
        report["warnings"] = sorted(
            {*report["warnings"], "provenance_fact_limit_reached"}
        )
    return profile, report


__all__ = ["SynthesisLimits", "synthesize_canonical_profile"]
