# SPDX-License-Identifier: MPL-2.0
"""Bounded duplicate previews and atomic immutable profile-review changes."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import unicodedata
import uuid

from .profile.education import education_entries_can_merge
from .profile.validation import validate_canonical_profile
from .profile.work import work_entries_can_merge
from .profile_sections import (
    PROFILE_SECTION_ENTRY_LIMIT,
    _SPECS,
    _changed_fields_between as _section_changed_fields_between,
    _json_equivalent,
    _mark_education_curated,
    _normalize_patch as _normalize_section_patch,
    _public_entry as _public_section_entry,
    _stored_field_is_normalized as _section_field_is_normalized,
    _validate_entry_identity as _validate_section_entry_identity,
)
from .profile_versions import _public_basics, _stored_version, _version_row
from .profile_work import (
    WORK_ENTRY_LIMIT,
    _CANONICAL_FIELDS as _WORK_CANONICAL_FIELDS,
    _FIELD_ORDER as _WORK_FIELD_ORDER,
    _changed_fields_between as _work_changed_fields_between,
    _normalize_patch as _normalize_work_patch,
    _public_work_entry,
    _stored_field_is_normalized as _work_field_is_normalized,
    _work_entry_has_required_identity,
)
from .resume import is_baseline_resume_renderable
from .storage import (
    MAX_DRAFT_PROFILE_BYTES,
    MAX_RPC_SAFE_COUNT,
    VaultError,
    _apply_migrations,
    _canonical_uuid,
    _connect,
    _json_text,
    _utc_now_ms,
)


# Durable receipts replay this algorithm to verify their exact output. Preserve
# the v1 implementation when introducing later matching/merge behavior; route
# by the stored algorithm version instead of changing v1 in place.
PROFILE_CHANGES_ALGORITHM_VERSION = 1
PROFILE_CHANGE_LIMIT = 64
PROFILE_CHANGE_OPERATIONS_LIMIT_BYTES = 256 * 1024
PROFILE_CHANGE_PREVIEW_COMPARISON_LIMIT = 20_000
PROFILE_CHANGE_PREVIEW_RESPONSE_LIMIT_BYTES = 128 * 1024
PROFILE_CHANGE_SECTIONS = ("work", "projects", "education", "skills")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODES = {
    "work": "matching_employer_role_and_dates",
    "projects": "matching_project_name_and_dates",
    "education": "matching_institution_program_and_dates",
    "skills": "matching_skill_group",
}


class _MergeConflict(ValueError):
    """Internal signal that a pair cannot be merged without choosing facts."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text_marker(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value).casefold()
    return " ".join(normalized.split())


def _identity_marker(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text_marker(value)).strip()


def _substantive_text_marker(value: Any) -> str:
    marker = _text_marker(value)
    words = re.findall(r"[^\W_]+", marker, flags=re.UNICODE)
    if len(words) < 3 or sum(len(word) for word in words) < 20:
        return ""
    return marker


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_string_lists(existing: list[Any], candidate: list[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for value in [*existing, *candidate]:
        if not isinstance(value, str):
            raise _MergeConflict
        marker = _text_marker(value)
        if not marker:
            raise _MergeConflict
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(deepcopy(value))
    return merged


def _merge_unknown(existing: Any, candidate: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        raise _MergeConflict
    if _json_equivalent(existing, candidate):
        return deepcopy(existing)
    if _is_empty(existing):
        return deepcopy(candidate)
    if _is_empty(candidate):
        return deepcopy(existing)
    if isinstance(existing, dict) and isinstance(candidate, dict):
        merged = deepcopy(existing)
        for key, value in candidate.items():
            if key in merged:
                merged[key] = _merge_unknown(merged[key], value, depth=depth + 1)
            else:
                merged[key] = deepcopy(value)
        return merged
    if isinstance(existing, list) and isinstance(candidate, list):
        merged = deepcopy(existing)
        seen = {_canonical_json(value) for value in merged}
        for value in candidate:
            marker = _canonical_json(value)
            if marker not in seen:
                seen.add(marker)
                merged.append(deepcopy(value))
        return merged
    raise _MergeConflict


def _merge_scalar(existing: Any, candidate: Any, *, narrative: bool) -> Any:
    if _json_equivalent(existing, candidate):
        return deepcopy(existing)
    if _is_empty(existing):
        return deepcopy(candidate)
    if _is_empty(candidate):
        return deepcopy(existing)
    if isinstance(existing, str) and isinstance(candidate, str):
        existing_marker = _text_marker(existing)
        candidate_marker = _text_marker(candidate)
        if existing_marker == candidate_marker:
            return deepcopy(existing)
        if narrative and existing_marker and candidate_marker:
            if existing_marker in candidate_marker:
                return deepcopy(candidate)
            if candidate_marker in existing_marker:
                return deepcopy(existing)
    raise _MergeConflict


def _section_metadata(section: str) -> tuple[tuple[str, ...], dict[str, str], set[str]]:
    if section == "work":
        return (
            _WORK_FIELD_ORDER,
            _WORK_CANONICAL_FIELDS,
            {"highlights"},
        )
    spec = _SPECS[section]
    return spec.field_order, spec.canonical_fields, set(spec.list_limits)


def _merge_entries(
    section: str, existing: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    fields, canonical_fields, list_fields = _section_metadata(section)
    reverse_fields = {canonical: field for field, canonical in canonical_fields.items()}
    merged = deepcopy(existing)
    for key, candidate_value in candidate.items():
        if key not in merged:
            merged[key] = deepcopy(candidate_value)
            continue
        field = reverse_fields.get(key)
        if field in list_fields:
            existing_value = merged[key]
            if not isinstance(existing_value, list) or not isinstance(candidate_value, list):
                raise _MergeConflict
            merged[key] = _merge_string_lists(existing_value, candidate_value)
        elif field is not None:
            merged[key] = _merge_scalar(
                merged[key],
                candidate_value,
                narrative=field in {"summary", "description"},
            )
        else:
            merged[key] = _merge_unknown(merged[key], candidate_value)

    if section == "work":
        if not _work_entry_has_required_identity(merged):
            raise _MergeConflict
        if not all(
            _WORK_CANONICAL_FIELDS[field] not in merged
            or _work_field_is_normalized(merged, field)
            for field in _WORK_FIELD_ORDER
        ):
            raise _MergeConflict
    else:
        spec = _SPECS[section]
        try:
            _validate_section_entry_identity(spec, merged, path="merged entry")
        except ValueError as exc:
            raise _MergeConflict from exc
        if not all(
            spec.canonical_fields[field] not in merged
            or _section_field_is_normalized(spec, merged, field)
            for field in spec.field_order
        ):
            raise _MergeConflict
    return merged


def _entries_can_merge(section: str, existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if section == "work":
        return work_entries_can_merge(existing, candidate)
    if section == "education":
        return education_entries_can_merge(existing, candidate)
    if section == "skills":
        marker = _identity_marker(existing.get("name"))
        return bool(marker and marker == _identity_marker(candidate.get("name")))
    if section == "projects":
        existing_name = _identity_marker(existing.get("name"))
        candidate_name = _identity_marker(candidate.get("name"))
        if not existing_name or existing_name != candidate_name:
            return False
        shared_date_anchor = any(
            bool(existing_marker)
            and existing_marker == _identity_marker(candidate.get(field))
            for field in ("startDate", "endDate")
            if (existing_marker := _identity_marker(existing.get(field)))
        )
        existing_url = _text_marker(existing.get("url"))
        shared_url_anchor = bool(
            existing_url and existing_url == _text_marker(candidate.get("url"))
        )
        existing_description = _substantive_text_marker(existing.get("description"))
        shared_description_anchor = bool(
            existing_description
            and existing_description
            == _substantive_text_marker(candidate.get("description"))
        )
        return shared_date_anchor or shared_url_anchor or shared_description_anchor
    return False


def _public_merged_entry(section: str, entry_index: int, entry: dict[str, Any]) -> dict[str, Any]:
    if section == "work":
        public = _public_work_entry(entry_index, entry)
    else:
        public = _public_section_entry(_SPECS[section], entry_index, entry)
    public.pop("entry_index", None)
    return public


def _changed_fields(section: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    if section == "work":
        return _work_changed_fields_between(before, after)
    return _section_changed_fields_between(_SPECS[section], before, after)


def _suggestion_id(
    profile_version_id: str,
    section: str,
    keep_entry_index: int,
    remove_entry_index: int,
    merged_entry: dict[str, Any],
) -> str:
    payload = {
        "algorithm_version": PROFILE_CHANGES_ALGORITHM_VERSION,
        "profile_version_id": profile_version_id,
        "section": section,
        "keep_entry_index": keep_entry_index,
        "remove_entry_index": remove_entry_index,
        "merged_entry": merged_entry,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _build_suggestion(
    profile_version_id: str,
    section: str,
    keep_entry_index: int,
    remove_entry_index: int,
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not _entries_can_merge(section, existing, candidate):
        return None
    try:
        merged = _merge_entries(section, existing, candidate)
        validate_canonical_profile(
            {"basics": {"name": "Duplicate preview"}, section: [merged]}
        )
    except (TypeError, ValueError, RecursionError):
        return None
    public = _public_merged_entry(section, keep_entry_index, merged)
    suggestion = {
        "suggestion_id": _suggestion_id(
            profile_version_id,
            section,
            keep_entry_index,
            remove_entry_index,
            merged,
        ),
        "section": section,
        "keep_entry_index": keep_entry_index,
        "remove_entry_index": remove_entry_index,
        "reason_codes": [_REASON_CODES[section]],
        "merged_entry": public,
        "changed_fields": _changed_fields(section, existing, merged),
    }
    return suggestion, merged


def preview_profile_changes(data_dir: Path, profile_version_id: Any) -> dict[str, Any]:
    normalized_id = _canonical_uuid(profile_version_id, label="profile_version_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        version = _stored_version(_version_row(connection, normalized_id))
        connection.commit()
    finally:
        connection.close()

    suggestions: list[dict[str, Any]] = []
    comparisons = 0
    truncated = False
    for section in PROFILE_CHANGE_SECTIONS:
        raw_entries = version["profile"].get(section)
        if not isinstance(raw_entries, list):
            continue
        limit = WORK_ENTRY_LIMIT if section == "work" else PROFILE_SECTION_ENTRY_LIMIT
        if len(raw_entries) > limit:
            raise VaultError(
                "vault_integrity_error",
                f"The stored {section} section exceeds the supported size.",
            )
        claimed: set[int] = set()
        for keep_index, existing in enumerate(raw_entries):
            if keep_index in claimed or not isinstance(existing, dict):
                continue
            for remove_index in range(keep_index + 1, len(raw_entries)):
                if comparisons >= PROFILE_CHANGE_PREVIEW_COMPARISON_LIMIT:
                    truncated = True
                    break
                comparisons += 1
                if remove_index in claimed:
                    continue
                candidate = raw_entries[remove_index]
                if not isinstance(candidate, dict):
                    continue
                built = _build_suggestion(
                    normalized_id,
                    section,
                    keep_index,
                    remove_index,
                    existing,
                    candidate,
                )
                if built is None:
                    continue
                suggestion, _merged = built
                prospective = {
                    "profile_version_id": normalized_id,
                    "version_number": version["version_number"],
                    "algorithm_version": PROFILE_CHANGES_ALGORITHM_VERSION,
                    "suggestions": [*suggestions, suggestion],
                    "truncated": False,
                }
                if (
                    len(suggestions) >= PROFILE_CHANGE_LIMIT
                    or len(_canonical_json(prospective).encode("utf-8"))
                    > PROFILE_CHANGE_PREVIEW_RESPONSE_LIMIT_BYTES
                ):
                    truncated = True
                    break
                suggestions.append(suggestion)
                claimed.update((keep_index, remove_index))
                break
            if truncated:
                break
        if truncated:
            break
    return {
        "profile_version_id": normalized_id,
        "version_number": version["version_number"],
        "algorithm_version": PROFILE_CHANGES_ALGORITHM_VERSION,
        "suggestions": suggestions,
        "truncated": truncated,
    }


def _normalize_index(value: Any, *, path: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= PROFILE_SECTION_ENTRY_LIMIT
    ):
        raise ValueError(f"{path} must be an integer between 0 and 4999")
    return value


def _normalize_operation(operation: Any) -> dict[str, Any]:
    if not isinstance(operation, dict):
        raise ValueError("operations must contain tagged objects")
    section = operation.get("section")
    if section not in PROFILE_CHANGE_SECTIONS:
        raise ValueError("operation.section must be work, projects, education, or skills")
    kind = operation.get("kind")
    if kind == "remove":
        if set(operation) != {"kind", "section", "entry_index"}:
            raise ValueError(
                "remove operation must contain exactly kind, section, and entry_index"
            )
        return {
            "kind": kind,
            "section": section,
            "entry_index": _normalize_index(
                operation["entry_index"], path="operation.entry_index"
            ),
        }
    if kind == "update":
        if set(operation) != {"kind", "section", "entry_index", "patch"}:
            raise ValueError(
                "update operation must contain exactly kind, section, entry_index, and patch"
            )
        patch = (
            _normalize_work_patch(operation["patch"])
            if section == "work"
            else _normalize_section_patch(_SPECS[section], operation["patch"])
        )
        return {
            "kind": kind,
            "section": section,
            "entry_index": _normalize_index(
                operation["entry_index"], path="operation.entry_index"
            ),
            "patch": patch,
        }
    if kind == "merge":
        if set(operation) != {
            "kind",
            "section",
            "keep_entry_index",
            "remove_entry_index",
            "suggestion_id",
        }:
            raise ValueError(
                "merge operation must contain exactly kind, section, keep_entry_index, "
                "remove_entry_index, and suggestion_id"
            )
        keep_index = _normalize_index(
            operation["keep_entry_index"], path="operation.keep_entry_index"
        )
        remove_index = _normalize_index(
            operation["remove_entry_index"], path="operation.remove_entry_index"
        )
        suggestion_id = operation["suggestion_id"]
        if not isinstance(suggestion_id, str) or _SHA256_RE.fullmatch(suggestion_id) is None:
            raise ValueError("operation.suggestion_id must be a lowercase SHA-256 value")
        if keep_index >= remove_index:
            raise ValueError("merge operation must keep the earlier suggested entry")
        return {
            "kind": kind,
            "section": section,
            "keep_entry_index": keep_index,
            "remove_entry_index": remove_index,
            "suggestion_id": suggestion_id,
        }
    raise ValueError("operation.kind must be update, remove, or merge")


def _normalize_operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= PROFILE_CHANGE_LIMIT:
        raise ValueError(f"operations must contain between 1 and {PROFILE_CHANGE_LIMIT} changes")
    operations = [_normalize_operation(operation) for operation in value]
    if (
        len(_canonical_json(operations).encode("utf-8"))
        > PROFILE_CHANGE_OPERATIONS_LIMIT_BYTES
    ):
        raise ValueError("operations exceed the 256 KiB request limit")
    targets: set[tuple[str, int]] = set()
    for operation in operations:
        section = operation["section"]
        indexes = (
            (operation["keep_entry_index"], operation["remove_entry_index"])
            if operation["kind"] == "merge"
            else (operation["entry_index"],)
        )
        for entry_index in indexes:
            target = (section, entry_index)
            if target in targets:
                raise ValueError("each profile entry may be targeted only once per apply request")
            targets.add(target)
    return operations


def _entries(profile: dict[str, Any], section: str) -> list[Any]:
    raw = profile.get(section)
    if raw is None or section not in profile:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"The current profile {section} section is malformed")
    if len(raw) > PROFILE_SECTION_ENTRY_LIMIT:
        raise ValueError(f"The current profile {section} section is too large to edit")
    return deepcopy(raw)


def _patched_entry(
    section: str, entry: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    _fields, canonical_fields, _list_fields = _section_metadata(section)
    edited = deepcopy(entry)
    for field, value in patch.items():
        canonical_field = canonical_fields[field]
        if value is None:
            edited.pop(canonical_field, None)
        else:
            edited[canonical_field] = deepcopy(value)
    if section == "work":
        if not _work_entry_has_required_identity(edited):
            raise ValueError("resulting work entry must retain an employer and position")
        changed = _work_changed_fields_between(entry, edited)
    else:
        spec = _SPECS[section]
        _validate_section_entry_identity(spec, edited, path=f"resulting {spec.entry_name}")
        changed = _section_changed_fields_between(spec, entry, edited)
    if not changed:
        raise ValueError("operation.patch does not make a semantic change")
    return edited


def _apply_operations(
    profile: dict[str, Any],
    profile_version_id: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(profile)
    for section in PROFILE_CHANGE_SECTIONS:
        section_operations = [item for item in operations if item["section"] == section]
        if not section_operations:
            continue
        entries = _entries(profile, section)
        replacements: dict[int, dict[str, Any]] = {}
        removals: set[int] = set()
        for operation in section_operations:
            if operation["kind"] == "merge":
                keep_index = operation["keep_entry_index"]
                remove_index = operation["remove_entry_index"]
                if keep_index >= len(entries) or remove_index >= len(entries):
                    raise ValueError("merge operation does not identify two profile entries")
                existing = entries[keep_index]
                candidate = entries[remove_index]
                if not isinstance(existing, dict) or not isinstance(candidate, dict):
                    raise ValueError("merge operation does not identify two valid profile entries")
                built = _build_suggestion(
                    profile_version_id,
                    section,
                    keep_index,
                    remove_index,
                    existing,
                    candidate,
                )
                if built is None or built[0]["suggestion_id"] != operation["suggestion_id"]:
                    raise ValueError(
                        "merge operation no longer matches a safe duplicate suggestion"
                    )
                replacements[keep_index] = built[1]
                removals.add(remove_index)
                continue
            entry_index = operation["entry_index"]
            if entry_index >= len(entries) or not isinstance(entries[entry_index], dict):
                raise ValueError("operation.entry_index does not identify a valid profile entry")
            if operation["kind"] == "remove":
                removals.add(entry_index)
            else:
                replacements[entry_index] = _patched_entry(
                    section, entries[entry_index], operation["patch"]
                )
        result[section] = [
            deepcopy(replacements.get(index, entry))
            for index, entry in enumerate(entries)
            if index not in removals
        ]
        if section == "education":
            _mark_education_curated(result)
    basics = _public_basics(
        {
            "id": str(uuid.UUID(int=0)),
            "version_number": 1,
            "created_at_ms": 1,
            "profile": result,
        }
    )
    if not basics["name"]:
        raise ValueError("The edited profile must retain a non-empty name")
    validate_canonical_profile(result)
    return result


def _section_counts(profile: dict[str, Any]) -> dict[str, int]:
    return {
        section: sum(isinstance(entry, dict) for entry in profile.get(section, []))
        if isinstance(profile.get(section), list)
        else 0
        for section in PROFILE_CHANGE_SECTIONS
    }


def _receipt(
    connection: sqlite3.Connection, row: sqlite3.Row, *, created: bool
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(row["request_id"], label="stored request id")
        parent_id = _canonical_uuid(
            row["parent_profile_version_id"], label="stored parent profile version id"
        )
        output_id = _canonical_uuid(
            row["output_profile_version_id"], label="stored output profile version id"
        )
        fingerprint = str(row["request_fingerprint"])
        algorithm_version = int(row["algorithm_version"])
        operations = _normalize_operations(json.loads(str(row["operations_json"])))
        changed_sections = json.loads(str(row["changed_sections_json"]))
        created_at_ms = int(row["created_at_ms"])
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile changes receipt failed its integrity check.",
        ) from exc
    expected_sections = [
        section for section in PROFILE_CHANGE_SECTIONS if any(
            operation["section"] == section for operation in operations
        )
    ]
    fingerprint_input = {
        "expected_parent_profile_version_id": parent_id,
        "request_id": request_id,
        "operations": operations,
    }
    expected_fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_input).encode("utf-8")
    ).hexdigest()
    if (
        algorithm_version != PROFILE_CHANGES_ALGORITHM_VERSION
        or _SHA256_RE.fullmatch(fingerprint) is None
        or fingerprint != expected_fingerprint
        or changed_sections != expected_sections
        or not 1 <= created_at_ms <= MAX_RPC_SAFE_COUNT
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile changes receipt failed its integrity check.",
        )
    parent = _stored_version(_version_row(connection, parent_id))
    output = _stored_version(_version_row(connection, output_id))
    if (
        output["parent_version_id"] != parent_id
        or output["version_number"] != parent["version_number"] + 1
        or output["source"] != "local_edit"
        or output["created_at_ms"] != created_at_ms
    ):
        raise VaultError(
            "vault_integrity_error", "A stored profile changes receipt has invalid lineage."
        )
    try:
        expected_profile = _apply_operations(parent["profile"], parent_id, operations)
        expected_json = _json_text(
            expected_profile,
            label="Expected profile changes",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile changes receipt has invalid mutation data.",
        ) from exc
    if output["canonical_json"] != expected_json:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile changes receipt has invalid mutation data.",
        )
    competing = connection.execute(
        """
        SELECT (
            EXISTS(SELECT 1 FROM profile_basic_update_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_work_update_receipts WHERE output_profile_version_id = ?)
          + EXISTS(
                SELECT 1 FROM profile_section_update_receipts
                WHERE output_profile_version_id = ?
            )
          + EXISTS(SELECT 1 FROM profile_restore_receipts WHERE output_profile_version_id = ?)
        )
        """,
        (output_id, output_id, output_id, output_id),
    ).fetchone()[0]
    if competing:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile changes receipt conflicts with another edit receipt.",
        )
    basics = _public_basics(output)
    if not basics["name"]:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile changes receipt points to a nameless profile.",
        )
    return {
        "request_id": request_id,
        "profile_version_id": output_id,
        "parent_profile_version_id": parent_id,
        "version_number": output["version_number"],
        "created_at_ms": created_at_ms,
        "created": created,
        "operation_count": len(operations),
        "changed_sections": expected_sections,
        "section_counts": _section_counts(output["profile"]),
        "profile_name": basics["name"],
        "renderable": is_baseline_resume_renderable(output["profile"]),
    }


def apply_profile_changes(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    if set(params) != {
        "expected_parent_profile_version_id",
        "request_id",
        "operations",
    }:
        raise ValueError(
            "params must contain exactly expected_parent_profile_version_id, request_id, "
            "and operations"
        )
    parent_id = _canonical_uuid(
        params["expected_parent_profile_version_id"],
        label="expected_parent_profile_version_id",
    )
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    operations = _normalize_operations(params["operations"])
    fingerprint_input = {
        "expected_parent_profile_version_id": parent_id,
        "request_id": request_id,
        "operations": operations,
    }
    fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_input).encode("utf-8")
    ).hexdigest()
    changed_sections = [
        section for section in PROFILE_CHANGE_SECTIONS if any(
            operation["section"] == section for operation in operations
        )
    ]
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM profile_change_receipts WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != fingerprint:
                raise VaultError(
                    "profile_changes_request_conflict",
                    "That profile changes request was already used with different inputs.",
                )
            result = _receipt(connection, existing, created=False)
            connection.commit()
            return result
        current_row = connection.execute(
            """
            SELECT id, version_number, parent_version_id, canonical_json,
                   checksum_sha256, source, created_at_ms
            FROM profile_versions
            ORDER BY version_number DESC
            LIMIT 1
            """
        ).fetchone()
        if current_row is None:
            raise VaultError("profile_missing", "Build or import a career profile first.")
        current = _stored_version(current_row)
        if current["id"] != parent_id:
            raise VaultError(
                "profile_changes_conflict",
                "The career profile changed before these review changes could be saved.",
            )
        if current["version_number"] >= MAX_RPC_SAFE_COUNT:
            raise VaultError(
                "profile_version_limit", "The local profile history cannot be advanced."
            )
        edited = _apply_operations(current["profile"], parent_id, operations)
        canonical_json = _json_text(
            edited,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
        checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if checksum == current["checksum_sha256"]:
            raise ValueError("operations do not change the current profile")
        output_id = str(uuid.uuid4())
        created_at_ms = max(_utc_now_ms(), current["created_at_ms"])
        connection.execute(
            """
            INSERT INTO profile_versions(
                id, version_number, parent_version_id, canonical_json,
                checksum_sha256, source, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, 'local_edit', ?)
            """,
            (
                output_id,
                current["version_number"] + 1,
                parent_id,
                canonical_json,
                checksum,
                created_at_ms,
            ),
        )
        connection.execute(
            """
            INSERT INTO profile_change_receipts(
                request_id, request_fingerprint, algorithm_version,
                output_profile_version_id, parent_profile_version_id,
                operations_json, changed_sections_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                fingerprint,
                PROFILE_CHANGES_ALGORITHM_VERSION,
                output_id,
                parent_id,
                _canonical_json(operations),
                _canonical_json(changed_sections),
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_change_receipts WHERE request_id = ?", (request_id,)
        ).fetchone()
        result = _receipt(connection, receipt, created=True)
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            "The local vault rejected invalid profile changes lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "PROFILE_CHANGE_LIMIT",
    "PROFILE_CHANGE_OPERATIONS_LIMIT_BYTES",
    "PROFILE_CHANGES_ALGORITHM_VERSION",
    "apply_profile_changes",
    "preview_profile_changes",
]
