# SPDX-License-Identifier: MPL-2.0
"""Deterministically compact a canonical profile into bounded public data.

Adapted from earlier private work by the original author; only the local
profile behavior is included. See docs/PROVENANCE.md for publication scope.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from .education import (
    education_entries_can_merge,
    looks_like_education_annotation,
    parse_legacy_education_summary,
    strip_education_editorial_commentary,
)
from .provenance import strip_public_profile_provenance
from .work import work_entries_can_merge


_WHITESPACE_RE = re.compile(r"\s+")
_MARKER_RE = re.compile(r"[^a-z0-9]+")

_SKILL_KEYWORD_LIMIT = 18
_SKILL_GROUP_LIMIT = 24
_WORK_HIGHLIGHT_LIMIT = 8
_WORK_HIGHLIGHT_TOTAL_CHARS = 1_800
_WORK_SUMMARY_CHARS = 1_000
_GENERIC_LIST_LIMIT = 40

_CONTEXT_LIST_LIMITS = {
    "impact_evidence": 35,
    "domain_context": 25,
    "work_preferences": 12,
    "open_questions": 8,
}
_CONTEXT_LIST_CHAR_BUDGETS = {
    "impact_evidence": 9_000,
    "domain_context": 7_000,
    "work_preferences": 3_000,
    "open_questions": 2_000,
}
_CONTEXT_LIST_ITEM_CHARS = {
    "impact_evidence": 360,
    "domain_context": 320,
    "work_preferences": 300,
    "open_questions": 260,
}
_CONTEXT_STRING_LIMITS = {"career_narrative": 3_500}
_CONTEXT_ONLY_SKILL_TERMS = {"aws", "amazon web services"}
_CONTEXT_ONLY_SKILL_RE = re.compile(
    r"\b(?:aws|amazon\s+web\s+services)\b.{0,80}\b(?:account|accounts|client|clients|customer|customers|"
    r"familiarity|concepts?)\b|\b(?:account|accounts|client|clients|customer|customers|b2b|cloud|technology)"
    r"\b.{0,80}\b(?:aws|amazon\s+web\s+services)\b",
    re.IGNORECASE,
)
_CONTEXT_SKILL_LINE_RE = re.compile(
    r"^\s*(?:core|skills?|technical\s+skills?|skills?\s+and\s+tools|tools?(?:\s+and\s+technologies)?|"
    r"data\s+tools(?:\s*&\s*tech)?|research\s*&\s*analysis|advertising\s*&\s*brand\s+measurement|"
    r"program\s*&\s*project\s+management|storytelling\s*&\s*communication)\s*(?:[:|]|\s+[-–—]\s+)",
    re.IGNORECASE,
)
_SKILL_EDITORIAL_PROVENANCE_RE = re.compile(
    r"\b(?:referenced|mentioned|listed|included|shown|appearing)\s+"
    r"(?:in|on)\s+(?:the\s+)?(?:(?:latest|current|uploaded|provided|source|prior|previous)\s+)?"
    r"(?:draft|resume|cv|document|profile)\b(?:\s+only)?[\s\])}]*$",
    re.IGNORECASE,
)
_DOSSIER_CLAIM_LIMIT = 44
_DOSSIER_CLAIM_TEXT_CHARS = 300
_EDUCATION_NARRATIVE_FIELDS = {
    "note",
    "notes",
    "comment",
    "comments",
    "summary",
    "description",
    "detail",
    "details",
    "annotation",
}
_EDUCATION_STRUCTURED_TEXT_FIELDS = {
    "institution",
    "school",
    "university",
    "college",
    "studytype",
    "study type",
    "degree",
    "credential",
    "qualification",
    "area",
    "major",
    "field",
    "focus",
    "discipline",
}
_EDUCATION_ANNOTATION_TEXT_FIELDS = {
    "studytype",
    "study type",
    "degree",
    "credential",
    "qualification",
    "area",
    "major",
    "field",
    "focus",
    "discipline",
}


def _clean_text(value: object, *, max_chars: int | None = None) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


def _marker(value: object) -> str:
    cleaned = _clean_text(value).casefold()
    return _MARKER_RE.sub(" ", cleaned).strip()


def _dict_metric(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except TypeError:
        return len(str(value))


def _text_score(value: str, *, index: int, total: int) -> float:
    lowered = value.casefold()
    score = 1.0
    if any(
        token in lowered
        for token in ("led ", "built ", "launched ", "measured ", "improved ", "reduced ")
    ):
        score += 2.0
    if any(
        token in lowered
        for token in ("%", "$", " revenue", " budget", " team", " client", " account")
    ):
        score += 1.25
    if any(
        token in lowered
        for token in ("prefer", "target", "wants", "looking for", "remote", "hybrid")
    ):
        score += 1.5
    if len(value) <= 220:
        score += 0.5
    if total > 1:
        score += index / (total - 1) * 0.75
    return score


def _dedupe_budgeted_strings(
    values: list[Any],
    *,
    max_items: int,
    max_total_chars: int | None,
    max_item_chars: int,
) -> list[str]:
    candidates: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        cleaned = _clean_text(raw, max_chars=max_item_chars)
        if not cleaned:
            continue
        marker = _marker(cleaned)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        candidates.append((index, cleaned, marker))

    if not candidates:
        return []

    selected: list[tuple[int, str]] = []
    total_chars = 0
    ranked = sorted(
        candidates,
        key=lambda item: (
            _text_score(item[1], index=item[0], total=len(candidates)),
            item[0],
        ),
        reverse=True,
    )
    for index, cleaned, _item_marker in ranked:
        if len(selected) >= max_items:
            break
        next_total = total_chars + len(cleaned)
        if max_total_chars is not None and selected and next_total > max_total_chars:
            continue
        selected.append((index, cleaned))
        total_chars = next_total

    return [text for _index, text in sorted(selected, key=lambda item: item[0])]


def _merge_text(existing: object, candidate: object, *, max_chars: int) -> str:
    existing_clean = _clean_text(existing, max_chars=max_chars)
    candidate_clean = _clean_text(candidate, max_chars=max_chars)
    if not existing_clean:
        return candidate_clean
    if not candidate_clean:
        return existing_clean
    existing_marker = _marker(existing_clean)
    candidate_marker = _marker(candidate_clean)
    if existing_marker == candidate_marker or candidate_marker in existing_marker:
        return existing_clean
    if existing_marker in candidate_marker:
        return candidate_clean
    if len(candidate_clean) > len(existing_clean):
        return candidate_clean[:max_chars].rstrip()
    return existing_clean


def _merge_string_lists(
    values: list[Any],
    *,
    max_items: int,
    max_total_chars: int,
    max_item_chars: int,
) -> list[str]:
    return _dedupe_budgeted_strings(
        values,
        max_items=max_items,
        max_total_chars=max_total_chars,
        max_item_chars=max_item_chars,
    )


def _iter_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            strings.extend(_iter_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_iter_strings(child))
    elif isinstance(value, str) and value.strip():
        strings.append(value)
    return strings


def _context_only_skill_terms(profile: dict[str, Any]) -> set[str]:
    text = "\n".join(
        _iter_strings(profile.get("work")) + _iter_strings(profile.get("meta"))
    )
    if not _CONTEXT_ONLY_SKILL_RE.search(text):
        return set()
    return set(_CONTEXT_ONLY_SKILL_TERMS)


def _clean_skill_text(value: object, *, max_chars: int, report: dict[str, Any]) -> str:
    cleaned = _clean_text(value, max_chars=max_chars)
    if not cleaned:
        return ""
    match = _SKILL_EDITORIAL_PROVENANCE_RE.search(cleaned)
    if not match:
        return cleaned
    prefix = cleaned[: match.start()].rstrip(" \t-–—,:;([{/")
    report["skill_editorial_notes_removed"] += 1
    return prefix


def _compact_skills(profile: dict[str, Any], report: dict[str, Any]) -> None:
    raw_skills = profile.get("skills")
    if not isinstance(raw_skills, list):
        return

    context_only_terms = _context_only_skill_terms(profile)
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in raw_skills:
        if isinstance(entry, str):
            name = _clean_skill_text(entry, max_chars=120, report=report)
            level = ""
            keywords: list[Any] = []
        elif isinstance(entry, dict):
            name = _clean_skill_text(entry.get("name"), max_chars=120, report=report)
            raw_level = entry.get("level")
            level = (
                _clean_skill_text(raw_level, max_chars=80, report=report)
                if isinstance(raw_level, str)
                else ""
            )
            raw_keywords = entry.get("keywords")
            keywords = list(raw_keywords) if isinstance(raw_keywords, list) else []
        else:
            continue
        if not name and not keywords:
            continue
        group_key = _marker(name or "Core") or "core"
        if group_key not in groups:
            groups[group_key] = {
                "name": name or "Core",
                "level": level,
                "keywords": [],
            }
            order.append(group_key)
        else:
            report["skill_groups_merged"] += 1
            if level and not groups[group_key].get("level"):
                groups[group_key]["level"] = level
        for keyword in keywords:
            cleaned_keyword = _clean_skill_text(keyword, max_chars=80, report=report)
            if not cleaned_keyword:
                continue
            if _marker(cleaned_keyword) in context_only_terms:
                report["contextual_skill_terms_removed"] += 1
                continue
            groups[group_key]["keywords"].append(cleaned_keyword)

    compacted: list[dict[str, Any]] = []
    for group_key in order[:_SKILL_GROUP_LIMIT]:
        group = groups[group_key]
        if _marker(group.get("name")) in context_only_terms:
            report["contextual_skill_terms_removed"] += 1
            continue
        keywords = _dedupe_budgeted_strings(
            group.get("keywords") or [],
            max_items=_SKILL_KEYWORD_LIMIT,
            max_total_chars=1_200,
            max_item_chars=80,
        )
        item = {"name": group["name"]}
        if group.get("level"):
            item["level"] = group["level"]
        if keywords:
            item["keywords"] = keywords
        compacted.append(item)
    report["skill_groups_removed"] += max(0, len(order) - len(compacted))
    profile["skills"] = compacted


def _strip_education_narrative_fields(
    profile: dict[str, Any],
    report: dict[str, Any],
) -> None:
    education = profile.get("education")
    if not isinstance(education, list):
        return
    cleaned_entries: list[Any] = []
    for raw_entry in education:
        if not isinstance(raw_entry, dict):
            cleaned_entries.append(raw_entry)
            continue
        source_entry = copy.deepcopy(raw_entry)
        legacy_fields = parse_legacy_education_summary(raw_entry.get("summary"))
        for key, value in legacy_fields.items():
            if not str(source_entry.get(key) or "").strip():
                source_entry[key] = value
        cleaned_entry: dict[str, Any] = {}
        narrative_fields_removed = 0
        for key, value in source_entry.items():
            normalized_key = str(key).casefold()
            if normalized_key in _EDUCATION_NARRATIVE_FIELDS:
                narrative_fields_removed += 1
                continue
            cleaned_value = copy.deepcopy(value)
            if normalized_key in _EDUCATION_STRUCTURED_TEXT_FIELDS and isinstance(
                value, str
            ):
                cleaned_value = strip_education_editorial_commentary(value)
                editorial_fragment_removed = cleaned_value != value.strip()
                if (
                    normalized_key in _EDUCATION_ANNOTATION_TEXT_FIELDS
                    and looks_like_education_annotation(cleaned_value)
                ):
                    cleaned_value = ""
                    editorial_fragment_removed = True
                if editorial_fragment_removed:
                    report["education_editorial_fragments_removed"] += 1
                if not cleaned_value:
                    continue
            cleaned_entry[key] = cleaned_value
        report["education_narrative_fields_removed"] += narrative_fields_removed
        if cleaned_entry:
            cleaned_entries.append(cleaned_entry)
        else:
            report["education_entries_removed"] += 1
    profile["education"] = cleaned_entries


def _entry_identity(entry: dict[str, Any], *, keys: tuple[str, ...]) -> str:
    parts = [_marker(entry.get(key)) for key in keys]
    return "|".join(parts).strip("|")


def _merge_entry(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in candidate.items():
        if key == "highlights":
            existing_values = (
                merged.get("highlights")
                if isinstance(merged.get("highlights"), list)
                else []
            )
            candidate_values = value if isinstance(value, list) else []
            merged["highlights"] = _merge_string_lists(
                [*existing_values, *candidate_values],
                max_items=_WORK_HIGHLIGHT_LIMIT,
                max_total_chars=_WORK_HIGHLIGHT_TOTAL_CHARS,
                max_item_chars=360,
            )
            continue
        if key == "summary":
            merged["summary"] = _merge_text(
                merged.get("summary"), value, max_chars=_WORK_SUMMARY_CHARS
            )
            continue
        if key not in merged or merged.get(key) in (None, "", [], {}):
            merged[key] = copy.deepcopy(value)
    return merged


def _compact_entry_list(
    profile: dict[str, Any],
    *,
    field: str,
    identity_keys: tuple[str, ...],
    max_items: int,
    report_key: str,
    report: dict[str, Any],
    removed_report_key: str | None = None,
) -> None:
    raw_values = profile.get(field)
    if not isinstance(raw_values, list):
        return

    by_identity: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    passthrough: list[Any] = []
    for index, raw_entry in enumerate(raw_values):
        if not isinstance(raw_entry, dict):
            passthrough.append(raw_entry)
            continue
        identity = _entry_identity(raw_entry, keys=identity_keys)
        if not identity:
            identity = f"{field}:{index}"
        if field in {"work", "education"}:
            merged_identity = None
            for existing_identity in order:
                existing_entry = by_identity[existing_identity]
                can_merge = (
                    work_entries_can_merge(existing_entry, raw_entry)
                    if field == "work"
                    else education_entries_can_merge(existing_entry, raw_entry)
                )
                if can_merge:
                    merged_identity = existing_identity
                    break
            if merged_identity is not None:
                by_identity[merged_identity] = _merge_entry(
                    by_identity[merged_identity], raw_entry
                )
                report[report_key] += 1
                continue
        if identity in by_identity:
            by_identity[identity] = _merge_entry(by_identity[identity], raw_entry)
            report[report_key] += 1
        else:
            by_identity[identity] = copy.deepcopy(raw_entry)
            order.append(identity)

    compacted = [by_identity[key] for key in order[:max_items]]
    removed_key = removed_report_key or f"{field}_entries_removed"
    report[removed_key] += max(0, len(order) - len(compacted))
    profile[field] = [
        *compacted,
        *passthrough[: max(0, max_items - len(compacted))],
    ]


def _compact_generic_lists(profile: dict[str, Any], report: dict[str, Any]) -> None:
    for field in (
        "awards",
        "certificates",
        "publications",
        "languages",
        "interests",
        "references",
    ):
        values = profile.get(field)
        if not isinstance(values, list):
            continue
        compacted: list[Any] = []
        seen: set[str] = set()
        for item in values:
            marker = _marker(
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            )
            if not marker or marker in seen:
                report["generic_entries_merged"] += 1
                continue
            seen.add(marker)
            compacted.append(item)
            if len(compacted) >= _GENERIC_LIST_LIMIT:
                break
        report["generic_entries_removed"] += max(0, len(values) - len(compacted))
        profile[field] = compacted


def _split_context_string(value: str) -> list[str]:
    parts: list[str] = []
    for raw_line in str(value or "").replace("\r\n", "\n").split("\n"):
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", raw_line.strip()):
            cleaned = _clean_text(sentence)
            if cleaned:
                parts.append(cleaned)
    return parts


def _looks_like_context_skill_line(value: object) -> bool:
    cleaned = _clean_text(value, max_chars=600)
    if not cleaned or len(cleaned) > 520:
        return False
    if not _CONTEXT_SKILL_LINE_RE.search(cleaned):
        return False
    delimiter_count = cleaned.count("|") + cleaned.count(",") + cleaned.count(";")
    return delimiter_count >= 1


def _compact_canonical_context(
    profile: dict[str, Any], report: dict[str, Any]
) -> None:
    meta = profile.get("meta")
    if not isinstance(meta, dict):
        return
    context = meta.get("canonical_context")
    if not isinstance(context, dict):
        return

    compacted_context: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, list):
            retained_values = []
            for item in value:
                if _looks_like_context_skill_line(item):
                    report["context_skill_lines_removed"] += 1
                    continue
                retained_values.append(item)
            limit = _CONTEXT_LIST_LIMITS.get(key, 30)
            total_chars = _CONTEXT_LIST_CHAR_BUDGETS.get(key, 6_000)
            item_chars = _CONTEXT_LIST_ITEM_CHARS.get(key, 300)
            compacted_values = _dedupe_budgeted_strings(
                retained_values,
                max_items=limit,
                max_total_chars=total_chars,
                max_item_chars=item_chars,
            )
            report["context_items_removed"] += max(
                0, len(value) - len(compacted_values)
            )
            if compacted_values:
                compacted_context[key] = compacted_values
            continue
        if isinstance(value, str):
            max_chars = _CONTEXT_STRING_LIMITS.get(key, 2_000)
            lines = []
            for line in _split_context_string(value):
                if _looks_like_context_skill_line(line):
                    report["context_skill_lines_removed"] += 1
                    continue
                lines.append(line)
            if len(lines) > 1:
                compacted_value = "\n".join(
                    _dedupe_budgeted_strings(
                        lines,
                        max_items=24,
                        max_total_chars=max_chars,
                        max_item_chars=320,
                    )
                ).strip()
            else:
                compacted_value = _clean_text(value, max_chars=max_chars)
            if compacted_value:
                compacted_context[key] = compacted_value
            if len(str(value)) > len(compacted_value):
                report["context_chars_removed"] += len(str(value)) - len(
                    compacted_value
                )
            continue
        compacted_context[key] = value

    if compacted_context:
        meta["canonical_context"] = compacted_context
    else:
        meta.pop("canonical_context", None)
    profile["meta"] = meta


def _compact_dossier(profile: dict[str, Any], report: dict[str, Any]) -> None:
    meta = profile.get("meta")
    if not isinstance(meta, dict):
        return
    dossier = meta.get("dossier")
    if not isinstance(dossier, dict):
        return

    compacted = dict(dossier)
    claims = dossier.get("claims")
    if isinstance(claims, list):
        candidates: list[tuple[int, dict[str, Any], str]] = []
        seen: set[str] = set()
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            text = _clean_text(
                claim.get("text"), max_chars=_DOSSIER_CLAIM_TEXT_CHARS
            )
            marker = _marker(text)
            if not marker or marker in seen:
                report["dossier_claims_removed"] += 1
                continue
            seen.add(marker)
            compact_claim = {
                key: copy.deepcopy(value)
                for key, value in claim.items()
                if key
                in {
                    "id",
                    "kind",
                    "text",
                    "source_file",
                    "source_content_type",
                    "line_start",
                    "line_end",
                    "confidence",
                }
            }
            compact_claim["text"] = text
            candidates.append((index, compact_claim, text))
        ranked = sorted(
            candidates,
            key=lambda item: (
                _text_score(item[2], index=item[0], total=len(candidates)),
                item[0],
            ),
            reverse=True,
        )[:_DOSSIER_CLAIM_LIMIT]
        selected_claims = [
            claim
            for _index, claim, _text in sorted(ranked, key=lambda item: item[0])
        ]
        report["dossier_claims_removed"] += max(
            0, len(candidates) - len(selected_claims)
        )
        compacted["claims"] = selected_claims
        compacted["claim_count"] = len(selected_claims)
        counts: dict[str, int] = {}
        for claim in selected_claims:
            kind = _clean_text(claim.get("kind"), max_chars=64) or "experience"
            counts[kind] = counts.get(kind, 0) + 1
        compacted["claim_counts_by_kind"] = counts

    source_manifest = dossier.get("source_manifest")
    if isinstance(source_manifest, list):
        compacted["source_manifest"] = [
            {
                key: value
                for key, value in item.items()
                if isinstance(item, dict)
                and key
                in {
                    "filename",
                    "content_type",
                    "char_count",
                    "structured_extraction_skip_reason",
                }
            }
            for item in source_manifest[:40]
            if isinstance(item, dict)
        ]

    for field, max_items, max_total_chars, max_item_chars in (
        ("skill_signals", 40, 2_400, 80),
        ("open_questions", 24, 4_000, 240),
    ):
        values = dossier.get(field)
        if isinstance(values, list):
            compacted_values = _dedupe_budgeted_strings(
                values,
                max_items=max_items,
                max_total_chars=max_total_chars,
                max_item_chars=max_item_chars,
            )
            if field == "skill_signals":
                context_only_terms = _context_only_skill_terms(profile)
                compacted_values = [
                    value
                    for value in compacted_values
                    if _marker(value) not in context_only_terms
                ]
            compacted[field] = compacted_values

    for field, identity_keys, max_items, report_key in (
        (
            "work_signals",
            ("name", "position", "startDate", "endDate"),
            16,
            "dossier_work_signals_merged",
        ),
        (
            "education_signals",
            ("institution", "studyType", "area", "startDate", "endDate"),
            12,
            "dossier_education_signals_merged",
        ),
    ):
        values = dossier.get(field)
        if isinstance(values, list):
            holder = {field: values}
            _compact_entry_list(
                holder,
                field=field,
                identity_keys=identity_keys,
                max_items=max_items,
                report_key=report_key,
                report=report,
                removed_report_key=f"dossier_{field}_entries_removed",
            )
            compacted[field] = holder[field]

    meta["dossier"] = compacted
    profile["meta"] = meta


def canonical_profile_metrics(profile: dict[str, Any]) -> dict[str, int]:
    """Return stable size/count metrics used in compaction reports."""

    skills = profile.get("skills") if isinstance(profile.get("skills"), list) else []
    work = profile.get("work") if isinstance(profile.get("work"), list) else []
    meta = profile.get("meta") if isinstance(profile.get("meta"), dict) else {}
    context = (
        meta.get("canonical_context")
        if isinstance(meta.get("canonical_context"), dict)
        else {}
    )
    context_items = 0
    context_chars = 0
    if isinstance(context, dict):
        for value in context.values():
            if isinstance(value, list):
                context_items += len(value)
                context_chars += sum(len(str(item)) for item in value)
            elif isinstance(value, str):
                context_chars += len(value)
    return {
        "profile_json_chars": _dict_metric(profile),
        "skill_groups": len(skills),
        "work_entries": len(work),
        "canonical_context_items": context_items,
        "canonical_context_chars": context_chars,
    }


def compact_canonical_profile(
    profile_json: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a compacted deep copy and a plain-dict audit report."""

    if not isinstance(profile_json, dict):
        raise ValueError("Canonical profile JSON payload must be an object.")

    profile = copy.deepcopy(profile_json)
    before = canonical_profile_metrics(profile)
    report: dict[str, Any] = {
        "version": 1,
        "skill_groups_merged": 0,
        "skill_groups_removed": 0,
        "work_entries_merged": 0,
        "work_entries_removed": 0,
        "education_entries_merged": 0,
        "education_entries_removed": 0,
        "education_narrative_fields_removed": 0,
        "education_editorial_fragments_removed": 0,
        "project_entries_merged": 0,
        "project_entries_removed": 0,
        "volunteer_entries_merged": 0,
        "volunteer_entries_removed": 0,
        "generic_entries_merged": 0,
        "generic_entries_removed": 0,
        "context_items_removed": 0,
        "context_chars_removed": 0,
        "context_skill_lines_removed": 0,
        "contextual_skill_terms_removed": 0,
        "skill_editorial_notes_removed": 0,
        "public_provenance_annotations_removed": 0,
        "dossier_claims_removed": 0,
        "dossier_work_signals_merged": 0,
        "dossier_work_signals_entries_removed": 0,
        "dossier_education_signals_merged": 0,
        "dossier_education_signals_entries_removed": 0,
    }

    profile, report["public_provenance_annotations_removed"] = (
        strip_public_profile_provenance(profile)
    )
    _compact_dossier(profile, report)
    _compact_skills(profile, report)
    _strip_education_narrative_fields(profile, report)
    _compact_entry_list(
        profile,
        field="work",
        identity_keys=("name", "position", "startDate", "endDate"),
        max_items=40,
        report_key="work_entries_merged",
        report=report,
    )
    _compact_entry_list(
        profile,
        field="education",
        identity_keys=("institution", "studyType", "area", "startDate", "endDate"),
        max_items=16,
        report_key="education_entries_merged",
        report=report,
    )
    _compact_entry_list(
        profile,
        field="projects",
        identity_keys=("name", "startDate", "endDate"),
        max_items=32,
        report_key="project_entries_merged",
        report=report,
        removed_report_key="project_entries_removed",
    )
    _compact_entry_list(
        profile,
        field="volunteer",
        identity_keys=("organization", "position", "startDate", "endDate"),
        max_items=24,
        report_key="volunteer_entries_merged",
        report=report,
    )
    _compact_generic_lists(profile, report)
    _compact_canonical_context(profile, report)

    after = canonical_profile_metrics(profile)
    report["metrics_before"] = before
    report["metrics_after"] = after
    report["changed"] = profile != profile_json
    return profile, report
