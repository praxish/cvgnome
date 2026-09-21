# SPDX-License-Identifier: MPL-2.0
"""Bounded Projects, Education, and Skills projections and immutable local edits."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import unicodedata
import uuid

from .profile.validation import validate_canonical_profile
from .profile_versions import _public_basics, _public_url, _stored_version, _version_row
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


PROFILE_SECTION_LIST_LIMIT = 20
PROFILE_SECTION_OFFSET_LIMIT = 10_000
PROFILE_SECTION_RESPONSE_LIMIT_BYTES = 128 * 1024
PROFILE_SECTION_ENTRY_LIMIT = 5_000


@dataclass(frozen=True)
class _ListLimit:
    max_items: int
    max_item_chars: int
    max_total_chars: int


@dataclass(frozen=True)
class _SectionSpec:
    section: str
    label: str
    entry_name: str
    field_order: tuple[str, ...]
    canonical_fields: dict[str, str]
    required_text_fields: frozenset[str]
    optional_text_fields: frozenset[str]
    text_limits: dict[str, tuple[int, int, bool]]
    list_limits: dict[str, _ListLimit]
    add_fallback_fields: tuple[str, ...]


_SPECS = {
    "projects": _SectionSpec(
        section="projects",
        label="Projects",
        entry_name="project",
        field_order=(
            "name",
            "description",
            "url",
            "start_date",
            "end_date",
            "highlights",
            "keywords",
        ),
        canonical_fields={
            "name": "name",
            "description": "description",
            "url": "url",
            "start_date": "startDate",
            "end_date": "endDate",
            "highlights": "highlights",
            "keywords": "keywords",
        },
        required_text_fields=frozenset({"name"}),
        optional_text_fields=frozenset(
            {"description", "url", "start_date", "end_date"}
        ),
        text_limits={
            "name": (200, 800, False),
            "description": (600, 2_400, True),
            "url": (2_048, 8_192, False),
            "start_date": (80, 320, False),
            "end_date": (80, 320, False),
        },
        list_limits={
            "highlights": _ListLimit(8, 360, 1_800),
            "keywords": _ListLimit(16, 100, 1_200),
        },
        add_fallback_fields=("name",),
    ),
    "education": _SectionSpec(
        section="education",
        label="Education",
        entry_name="education entry",
        field_order=(
            "institution",
            "study_type",
            "area",
            "url",
            "start_date",
            "end_date",
            "score",
            "courses",
        ),
        canonical_fields={
            "institution": "institution",
            "study_type": "studyType",
            "area": "area",
            "url": "url",
            "start_date": "startDate",
            "end_date": "endDate",
            "score": "score",
            "courses": "courses",
        },
        required_text_fields=frozenset({"institution"}),
        optional_text_fields=frozenset(
            {"study_type", "area", "url", "start_date", "end_date", "score"}
        ),
        text_limits={
            "institution": (240, 960, False),
            "study_type": (160, 640, False),
            "area": (200, 800, False),
            "url": (2_048, 8_192, False),
            "start_date": (80, 320, False),
            "end_date": (80, 320, False),
            "score": (80, 320, False),
        },
        list_limits={"courses": _ListLimit(12, 160, 1_200)},
        add_fallback_fields=("institution",),
    ),
    "skills": _SectionSpec(
        section="skills",
        label="Skills",
        entry_name="skill group",
        field_order=("name", "level", "keywords"),
        canonical_fields={"name": "name", "level": "level", "keywords": "keywords"},
        required_text_fields=frozenset({"name"}),
        optional_text_fields=frozenset({"level"}),
        text_limits={
            "name": (120, 480, False),
            "level": (80, 320, False),
        },
        list_limits={"keywords": _ListLimit(18, 80, 1_200)},
        add_fallback_fields=("name", "keywords"),
    ),
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _clean_text(value: str, *, limits: tuple[int, int, bool], truncate: bool) -> str | None:
    max_chars, max_bytes, multiline = limits
    normalized = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    if multiline:
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    cleaned_characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("C"):
            if multiline and character == "\n":
                cleaned_characters.append(character)
            elif truncate and category == "Cc":
                cleaned_characters.append(" ")
            elif not truncate:
                raise ValueError("contains unsupported control characters")
            continue
        cleaned_characters.append(character)
    cleaned = "".join(cleaned_characters)
    if not multiline:
        cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if not truncate:
        if len(cleaned) > max_chars or len(cleaned.encode("utf-8")) > max_bytes:
            raise ValueError("exceeds its local size limit")
        return cleaned
    cleaned = cleaned[:max_chars]
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_bytes:
        cleaned = encoded[:max_bytes].decode("utf-8", "ignore")
    return cleaned.rstrip() or None


def _normalize_text(value: Any, *, spec: _SectionSpec, field: str, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be text")
    try:
        cleaned = _clean_text(value, limits=spec.text_limits[field], truncate=False)
    except ValueError as exc:
        raise ValueError(f"{path} {exc}") from exc
    if cleaned is None:
        raise ValueError(f"{path} must not be blank")
    return cleaned


def _public_text(value: Any, *, spec: _SectionSpec, field: str) -> str | None:
    if not isinstance(value, str):
        return None
    return _clean_text(value, limits=spec.text_limits[field], truncate=True)


def _normalize_url(value: Any, *, spec: _SectionSpec, path: str) -> str:
    cleaned = _normalize_text(value, spec=spec, field="url", path=path)
    if _public_url(cleaned) != cleaned:
        raise ValueError(f"{path} must be a public credential-free HTTP or HTTPS URL")
    return cleaned


def _normalize_list(value: Any, *, spec: _SectionSpec, field: str, path: str) -> list[str]:
    limits = spec.list_limits[field]
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list of text values")
    if len(value) > limits.max_items:
        raise ValueError(f"{path} may contain at most {limits.max_items} values")
    result: list[str] = []
    total_chars = 0
    item_limits = (limits.max_item_chars, limits.max_item_chars * 4, False)
    for index, raw in enumerate(value):
        if not isinstance(raw, str):
            raise ValueError(f"{path}[{index}] must be text")
        try:
            cleaned = _clean_text(raw, limits=item_limits, truncate=False)
        except ValueError as exc:
            raise ValueError(f"{path}[{index}] {exc}") from exc
        if cleaned is None:
            raise ValueError(f"{path}[{index}] must not be blank")
        total_chars += len(cleaned)
        if total_chars > limits.max_total_chars:
            raise ValueError(
                f"{path} may contain at most {limits.max_total_chars} total characters"
            )
        result.append(cleaned)
    return result


def _public_list(value: Any, *, spec: _SectionSpec, field: str) -> list[str]:
    if not isinstance(value, list):
        return []
    limits = spec.list_limits[field]
    item_limits = (limits.max_item_chars, limits.max_item_chars * 4, False)
    result: list[str] = []
    total_chars = 0
    for raw in value:
        if not isinstance(raw, str):
            continue
        cleaned = _clean_text(raw, limits=item_limits, truncate=True)
        if cleaned is None:
            continue
        remaining = limits.max_total_chars - total_chars
        if remaining <= 0:
            break
        cleaned = cleaned[:remaining].rstrip()
        if not cleaned:
            break
        result.append(cleaned)
        total_chars += len(cleaned)
        if len(result) >= limits.max_items:
            break
    return result


def _validate_entry_identity(spec: _SectionSpec, entry: dict[str, Any], *, path: str) -> None:
    for field in spec.required_text_fields:
        _normalize_text(
            entry.get(spec.canonical_fields[field]),
            spec=spec,
            field=field,
            path=f"{path}.{field}",
        )
    if spec.section == "education" and not any(
        _public_text(entry.get(spec.canonical_fields[field]), spec=spec, field=field)
        for field in ("study_type", "area")
    ):
        raise ValueError(f"{path} must retain a study type or area")
    if spec.section == "skills":
        keywords = entry.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            raise ValueError(f"{path}.keywords must retain at least one keyword")
        _normalize_list(keywords, spec=spec, field="keywords", path=f"{path}.keywords")


def _public_entry(spec: _SectionSpec, entry_index: int, entry: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"entry_index": entry_index}
    for field in spec.field_order:
        canonical_field = spec.canonical_fields[field]
        if field in spec.list_limits:
            result[field] = _public_list(entry.get(canonical_field), spec=spec, field=field)
        elif field == "url":
            result[field] = _public_url(entry.get(canonical_field))
        else:
            value = _public_text(entry.get(canonical_field), spec=spec, field=field)
            result[field] = (value or "") if field in spec.required_text_fields else value
    return result


def _list_profile_section(
    data_dir: Path,
    *,
    section: str,
    profile_version_id: Any,
    offset: Any,
) -> dict[str, Any]:
    spec = _SPECS[section]
    normalized_id = _canonical_uuid(profile_version_id, label="profile_version_id")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > PROFILE_SECTION_OFFSET_LIMIT
    ):
        raise ValueError("offset must be an integer between 0 and 10000")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        version = _stored_version(_version_row(connection, normalized_id))
        raw_entries = version["profile"].get(section)
        if isinstance(raw_entries, list) and len(raw_entries) > PROFILE_SECTION_ENTRY_LIMIT:
            raise VaultError(
                "vault_integrity_error",
                f"The stored {spec.label} section exceeds the supported size.",
            )
        public_entries = (
            [
                _public_entry(spec, entry_index, entry)
                for entry_index, entry in enumerate(raw_entries)
                if isinstance(entry, dict)
            ]
            if isinstance(raw_entries, list)
            else []
        )
        total_items = len(public_entries)
        page = public_entries[offset : offset + PROFILE_SECTION_LIST_LIMIT]
        connection.commit()
    finally:
        connection.close()
    if not 0 <= total_items <= PROFILE_SECTION_ENTRY_LIMIT:
        raise VaultError(
            "vault_integrity_error",
            f"The stored {spec.label} section exceeds the supported size.",
        )
    items: list[dict[str, Any]] = []
    for item in page:
        prospective = {
            "profile_version_id": version["id"],
            "version_number": version["version_number"],
            "section": section,
            "total_items": total_items,
            "offset": offset,
            "limit": PROFILE_SECTION_LIST_LIMIT,
            "next_offset": offset + len(items) + 1,
            "items": [*items, item],
        }
        if len(
            json.dumps(
                prospective,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ) > PROFILE_SECTION_RESPONSE_LIMIT_BYTES:
            break
        items.append(item)
    if page and not items:
        raise VaultError(
            "vault_integrity_error",
            f"A public {spec.entry_name} exceeds the supported response size.",
        )
    page_end = offset + len(items)
    result = {
        "profile_version_id": version["id"],
        "version_number": version["version_number"],
        "section": section,
        "total_items": total_items,
        "offset": offset,
        "limit": PROFILE_SECTION_LIST_LIMIT,
        "next_offset": (
            page_end
            if page_end < total_items and page_end <= PROFILE_SECTION_OFFSET_LIMIT
            else None
        ),
        "items": items,
    }
    if len(
        json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ) > PROFILE_SECTION_RESPONSE_LIMIT_BYTES:
        raise VaultError(
            "vault_integrity_error",
            f"The public {spec.label} page exceeds the supported response size.",
        )
    return result


def list_profile_projects(data_dir: Path, profile_version_id: Any, offset: Any) -> dict[str, Any]:
    return _list_profile_section(
        data_dir, section="projects", profile_version_id=profile_version_id, offset=offset
    )


def list_profile_education(data_dir: Path, profile_version_id: Any, offset: Any) -> dict[str, Any]:
    return _list_profile_section(
        data_dir, section="education", profile_version_id=profile_version_id, offset=offset
    )


def list_profile_skills(data_dir: Path, profile_version_id: Any, offset: Any) -> dict[str, Any]:
    return _list_profile_section(
        data_dir, section="skills", profile_version_id=profile_version_id, offset=offset
    )


def _normalize_full_entry(spec: _SectionSpec, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != set(spec.field_order):
        raise ValueError(
            f"operation.entry must contain exactly {', '.join(spec.field_order)}"
        )
    normalized: dict[str, Any] = {}
    for field in spec.field_order:
        value = entry[field]
        path = f"operation.entry.{field}"
        if field in spec.required_text_fields:
            normalized[field] = _normalize_text(value, spec=spec, field=field, path=path)
        elif field == "url":
            normalized[field] = (
                None
                if value is None
                else _normalize_url(value, spec=spec, path=path)
            )
        elif field in spec.list_limits:
            if value is None:
                raise ValueError(f"{path} must be a list of text values")
            normalized[field] = _normalize_list(value, spec=spec, field=field, path=path)
        else:
            normalized[field] = (
                None
                if value is None
                else _normalize_text(value, spec=spec, field=field, path=path)
            )
    canonical = _canonical_entry(spec, normalized)
    _validate_entry_identity(spec, canonical, path="operation.entry")
    return normalized


def _normalize_patch(spec: _SectionSpec, patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("operation.patch must be a non-empty object")
    if any(not isinstance(key, str) or key not in spec.field_order for key in patch):
        raise ValueError("operation.patch contains unsupported fields")
    normalized: dict[str, Any] = {}
    for field in spec.field_order:
        if field not in patch:
            continue
        value = patch[field]
        path = f"operation.patch.{field}"
        if field in spec.required_text_fields:
            if value is None:
                raise ValueError(f"{path} must be non-empty text")
            normalized[field] = _normalize_text(value, spec=spec, field=field, path=path)
        elif field == "url":
            normalized[field] = (
                None
                if value is None
                else _normalize_url(value, spec=spec, path=path)
            )
        elif field in spec.list_limits:
            if value is None:
                if spec.section == "skills" and field == "keywords":
                    raise ValueError(f"{path} must retain at least one keyword")
                normalized[field] = None
            else:
                normalized[field] = _normalize_list(
                    value, spec=spec, field=field, path=path
                )
                if (
                    spec.section == "skills"
                    and field == "keywords"
                    and not normalized[field]
                ):
                    raise ValueError(f"{path} must retain at least one keyword")
        else:
            normalized[field] = (
                None
                if value is None
                else _normalize_text(value, spec=spec, field=field, path=path)
            )
    return normalized


def _normalize_entry_index(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= PROFILE_SECTION_ENTRY_LIMIT
    ):
        raise ValueError("operation.entry_index must be an integer between 0 and 4999")
    return value


def _normalize_operation(spec: _SectionSpec, operation: Any) -> dict[str, Any]:
    if not isinstance(operation, dict):
        raise ValueError("operation must be a tagged object")
    kind = operation.get("kind")
    if kind == "add":
        if set(operation) != {"kind", "entry"}:
            raise ValueError("add operation must contain exactly kind and entry")
        return {"kind": "add", "entry": _normalize_full_entry(spec, operation["entry"])}
    if kind == "update":
        if set(operation) != {"kind", "entry_index", "patch"}:
            raise ValueError("update operation must contain exactly kind, entry_index, and patch")
        return {
            "kind": "update",
            "entry_index": _normalize_entry_index(operation["entry_index"]),
            "patch": _normalize_patch(spec, operation["patch"]),
        }
    if kind == "remove":
        if set(operation) != {"kind", "entry_index"}:
            raise ValueError("remove operation must contain exactly kind and entry_index")
        return {"kind": "remove", "entry_index": _normalize_entry_index(operation["entry_index"])}
    raise ValueError("operation.kind must be add, update, or remove")


def _canonical_entry(spec: _SectionSpec, entry: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in spec.field_order:
        value = entry[field]
        if value is not None:
            result[spec.canonical_fields[field]] = deepcopy(value)
    return result


def _changed_fields_between(
    spec: _SectionSpec, before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    return [
        field
        for field in spec.field_order
        if (
            (spec.canonical_fields[field] in before)
            != (spec.canonical_fields[field] in after)
            or not _json_equivalent(
                before.get(spec.canonical_fields[field]),
                after.get(spec.canonical_fields[field]),
            )
        )
    ]


def _added_changed_fields(spec: _SectionSpec, entry: dict[str, Any]) -> list[str]:
    return [
        field
        for field in spec.field_order
        if spec.canonical_fields[field] in entry
        and not (
            field in spec.list_limits and not entry.get(spec.canonical_fields[field])
        )
    ]


def _removed_changed_fields(spec: _SectionSpec, entry: dict[str, Any]) -> list[str]:
    present = [field for field in spec.field_order if spec.canonical_fields[field] in entry]
    return present or list(spec.add_fallback_fields)


def _mutable_entries(profile: dict[str, Any], spec: _SectionSpec) -> list[Any]:
    if spec.section not in profile or profile.get(spec.section) is None:
        return []
    entries = profile.get(spec.section)
    if not isinstance(entries, list):
        raise ValueError(f"The current profile {spec.label} section is malformed")
    if len(entries) > PROFILE_SECTION_ENTRY_LIMIT:
        raise ValueError(f"The current profile {spec.label} section is too large to edit")
    return deepcopy(entries)


def _require_named_profile(profile: dict[str, Any]) -> str:
    name = _public_basics(
        {
            "id": str(uuid.UUID(int=0)),
            "version_number": 1,
            "created_at_ms": 1,
            "profile": profile,
        }
    )["name"]
    if not name:
        raise ValueError("The edited profile must retain a non-empty name")
    return name


def _mark_education_curated(profile: dict[str, Any]) -> None:
    raw_meta = profile.get("meta")
    if raw_meta is not None and not isinstance(raw_meta, dict):
        raise ValueError("The current profile metadata is malformed")
    meta = deepcopy(raw_meta) if isinstance(raw_meta, dict) else {}
    meta["education_curated"] = True
    profile["meta"] = meta


def _apply_operation(
    profile: dict[str, Any], spec: _SectionSpec, operation: dict[str, Any]
) -> tuple[dict[str, Any], int, list[str]]:
    result = deepcopy(profile)
    entries = _mutable_entries(profile, spec)
    kind = operation["kind"]
    if kind == "add":
        if len(entries) >= PROFILE_SECTION_ENTRY_LIMIT:
            raise ValueError(f"The {spec.label} section cannot contain more than 5000 entries")
        entry = _canonical_entry(spec, operation["entry"])
        entry_index = len(entries)
        entries.append(entry)
        changed_fields = _added_changed_fields(spec, entry)
    else:
        entry_index = operation["entry_index"]
        if entry_index >= len(entries):
            raise ValueError(f"operation.entry_index does not identify a {spec.entry_name}")
        target = entries[entry_index]
        if not isinstance(target, dict):
            raise ValueError(f"operation.entry_index does not identify a valid {spec.entry_name}")
        if kind == "remove":
            changed_fields = _removed_changed_fields(spec, target)
            del entries[entry_index]
        else:
            edited = deepcopy(target)
            for field, value in operation["patch"].items():
                canonical_field = spec.canonical_fields[field]
                if value is None:
                    edited.pop(canonical_field, None)
                else:
                    edited[canonical_field] = deepcopy(value)
            _validate_entry_identity(spec, edited, path=f"resulting {spec.entry_name}")
            changed_fields = _changed_fields_between(spec, target, edited)
            if not changed_fields:
                raise ValueError(
                    f"operation.patch does not make a semantic change to the {spec.entry_name}"
                )
            entries[entry_index] = edited
    result[spec.section] = entries
    if spec.section == "education":
        _mark_education_curated(result)
    _require_named_profile(result)
    validate_canonical_profile(result)
    return result, entry_index, changed_fields


def _receipt_entries(profile: dict[str, Any], spec: _SectionSpec) -> list[Any]:
    if spec.section not in profile or profile.get(spec.section) is None:
        return []
    entries = profile.get(spec.section)
    if not isinstance(entries, list) or len(entries) > PROFILE_SECTION_ENTRY_LIMIT:
        raise VaultError(
            "vault_integrity_error",
            f"A stored {spec.label} update receipt has invalid profile data.",
        )
    return entries


def _unknown_fields(spec: _SectionSpec, entry: dict[str, Any]) -> dict[str, Any]:
    public_keys = set(spec.canonical_fields.values())
    return {key: value for key, value in entry.items() if key not in public_keys}


def _json_equivalent(left: Any, right: Any) -> bool:
    """Compare parsed JSON without Python's bool/int equality aliasing."""
    try:
        return json.dumps(
            left,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) == json.dumps(
            right,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        return False


def _stored_field_is_normalized(spec: _SectionSpec, entry: dict[str, Any], field: str) -> bool:
    canonical_field = spec.canonical_fields[field]
    if canonical_field not in entry:
        return field not in spec.required_text_fields and field not in spec.list_limits
    value = entry[canonical_field]
    try:
        if field == "url":
            return _normalize_url(value, spec=spec, path="stored section URL") == value
        if field in spec.list_limits:
            return (
                _normalize_list(value, spec=spec, field=field, path="stored section list")
                == value
            )
        return (
            _normalize_text(value, spec=spec, field=field, path="stored section field")
            == value
        )
    except ValueError:
        return False


def _strict_added_entry(spec: _SectionSpec, entry: dict[str, Any]) -> bool:
    if _unknown_fields(spec, entry) or not all(
        _stored_field_is_normalized(spec, entry, field) for field in spec.field_order
    ):
        return False
    try:
        _validate_entry_identity(spec, entry, path=f"stored {spec.entry_name}")
    except ValueError:
        return False
    return True


def _expected_receipt_profile(
    parent_profile: dict[str, Any], spec: _SectionSpec, output_entries: list[Any]
) -> dict[str, Any]:
    expected = deepcopy(parent_profile)
    expected[spec.section] = deepcopy(output_entries)
    if spec.section == "education":
        _mark_education_curated(expected)
    return expected


def _section_receipt(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    created: bool,
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(receipt["request_id"], label="stored request id")
        parent_id = _canonical_uuid(
            receipt["parent_profile_version_id"], label="stored parent profile version id"
        )
        output_id = _canonical_uuid(
            receipt["output_profile_version_id"], label="stored output profile version id"
        )
        request_fingerprint = str(receipt["request_fingerprint"])
        section = str(receipt["section"])
        operation = str(receipt["operation"])
        entry_index = int(receipt["entry_index"])
        created_at_ms = int(receipt["created_at_ms"])
        changed_fields = json.loads(str(receipt["changed_fields_json"]))
        spec = _SPECS[section]
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
            "A stored profile section update receipt failed its integrity check.",
        ) from exc
    if (
        _SHA256_RE.fullmatch(request_fingerprint) is None
        or operation not in {"add", "update", "remove"}
        or not 0 <= entry_index < PROFILE_SECTION_ENTRY_LIMIT
        or not 1 <= created_at_ms <= MAX_RPC_SAFE_COUNT
        or not isinstance(changed_fields, list)
        or not 1 <= len(changed_fields) <= len(spec.field_order)
        or any(field not in spec.field_order for field in changed_fields)
        or changed_fields != [field for field in spec.field_order if field in changed_fields]
        or len(set(changed_fields)) != len(changed_fields)
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile section update receipt failed its integrity check.",
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
            "vault_integrity_error",
            "A stored profile section update receipt has invalid lineage.",
        )
    competing_receipts = connection.execute(
        """
        SELECT (
            EXISTS(
                SELECT 1 FROM profile_basic_update_receipts
                WHERE output_profile_version_id = ?
            )
            + EXISTS(
                SELECT 1 FROM profile_work_update_receipts
                WHERE output_profile_version_id = ?
            )
            + EXISTS(
                SELECT 1 FROM profile_restore_receipts
                WHERE output_profile_version_id = ?
            )
        )
        """,
        (output_id, output_id, output_id),
    ).fetchone()[0]
    if competing_receipts:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile section update receipt conflicts with another edit receipt.",
        )
    parent_entries = _receipt_entries(parent["profile"], spec)
    output_entries = _receipt_entries(output["profile"], spec)
    if operation == "add":
        valid = (
            entry_index == len(parent_entries)
            and len(output_entries) == len(parent_entries) + 1
            and _json_equivalent(output_entries[:entry_index], parent_entries)
            and isinstance(output_entries[entry_index], dict)
            and _strict_added_entry(spec, output_entries[entry_index])
        )
        expected_changes = (
            _added_changed_fields(spec, output_entries[entry_index]) if valid else []
        )
    elif operation == "update":
        valid = (
            entry_index < len(parent_entries)
            and len(output_entries) == len(parent_entries)
            and isinstance(parent_entries[entry_index], dict)
            and isinstance(output_entries[entry_index], dict)
            and _json_equivalent(
                _unknown_fields(spec, parent_entries[entry_index]),
                _unknown_fields(spec, output_entries[entry_index]),
            )
            and _json_equivalent(
                output_entries[:entry_index], parent_entries[:entry_index]
            )
            and _json_equivalent(
                output_entries[entry_index + 1 :], parent_entries[entry_index + 1 :]
            )
            and not _json_equivalent(
                output_entries[entry_index], parent_entries[entry_index]
            )
        )
        if valid:
            try:
                _validate_entry_identity(
                    spec,
                    output_entries[entry_index],
                    path=f"stored {spec.entry_name}",
                )
            except ValueError:
                valid = False
        expected_changes = (
            _changed_fields_between(
                spec, parent_entries[entry_index], output_entries[entry_index]
            )
            if valid
            else []
        )
        if valid:
            valid = all(
                spec.canonical_fields[field] not in output_entries[entry_index]
                or _stored_field_is_normalized(spec, output_entries[entry_index], field)
                for field in expected_changes
            )
    else:
        valid = (
            entry_index < len(parent_entries)
            and isinstance(parent_entries[entry_index], dict)
            and _json_equivalent(
                output_entries,
                parent_entries[:entry_index] + parent_entries[entry_index + 1 :],
            )
        )
        expected_changes = (
            _removed_changed_fields(spec, parent_entries[entry_index]) if valid else []
        )
    try:
        expected_json = _json_text(
            _expected_receipt_profile(parent["profile"], spec, output_entries),
            label="Expected profile section update",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
    except (TypeError, ValueError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile section update receipt has invalid mutation data.",
        ) from exc
    if (
        not valid
        or output["canonical_json"] != expected_json
        or changed_fields != expected_changes
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile section update receipt has invalid mutation data.",
        )
    try:
        profile_name = _require_named_profile(output["profile"])
    except ValueError as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile section update receipt points to a nameless profile.",
        ) from exc
    return {
        "request_id": request_id,
        "profile_version_id": output_id,
        "parent_profile_version_id": parent_id,
        "version_number": output["version_number"],
        "created_at_ms": output["created_at_ms"],
        "created": created,
        "section": section,
        "operation": operation,
        "entry_index": entry_index,
        "changed_fields": changed_fields,
        "profile_name": profile_name,
        "section_entries": sum(isinstance(entry, dict) for entry in output_entries),
        "renderable": is_baseline_resume_renderable(output["profile"]),
    }


def _update_profile_section(
    data_dir: Path, *, section: str, params: dict[str, Any]
) -> dict[str, Any]:
    spec = _SPECS[section]
    expected_keys = {"expected_parent_profile_version_id", "request_id", "operation"}
    if set(params) != expected_keys:
        raise ValueError(
            "params must contain exactly expected_parent_profile_version_id, "
            "request_id, and operation"
        )
    parent_id = _canonical_uuid(
        params["expected_parent_profile_version_id"],
        label="expected_parent_profile_version_id",
    )
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    operation = _normalize_operation(spec, params["operation"])
    fingerprint_json = json.dumps(
        {
            "section": section,
            "expected_parent_profile_version_id": parent_id,
            "request_id": request_id,
            "operation": operation,
        },
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_fingerprint = hashlib.sha256(fingerprint_json.encode("utf-8")).hexdigest()
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM profile_section_update_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["request_fingerprint"]) != request_fingerprint
                or str(existing["section"]) != section
            ):
                raise VaultError(
                    "profile_update_request_conflict",
                    f"That {spec.label} request was already used with different inputs.",
                )
            result = _section_receipt(connection, existing, created=False)
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
            raise VaultError(
                "profile_missing",
                f"Build or import a career profile before editing {spec.label}.",
            )
        current = _stored_version(current_row)
        if current["id"] != parent_id:
            raise VaultError(
                "profile_update_conflict",
                f"The career profile changed before this {spec.label} edit could be saved.",
            )
        if current["version_number"] >= MAX_RPC_SAFE_COUNT:
            raise VaultError(
                "profile_version_limit", "The local profile history cannot be advanced."
            )
        edited_profile, entry_index, changed_fields = _apply_operation(
            current["profile"], spec, operation
        )
        canonical_json = _json_text(
            edited_profile,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
        checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if checksum == current["checksum_sha256"]:
            raise ValueError("operation does not change the current profile")
        output_id = str(uuid.uuid4())
        version_number = current["version_number"] + 1
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
                version_number,
                parent_id,
                canonical_json,
                checksum,
                created_at_ms,
            ),
        )
        connection.execute(
            """
            INSERT INTO profile_section_update_receipts(
                request_id, request_fingerprint, section, output_profile_version_id,
                parent_profile_version_id, operation, entry_index,
                changed_fields_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_fingerprint,
                section,
                output_id,
                parent_id,
                operation["kind"],
                entry_index,
                json.dumps(changed_fields, separators=(",", ":")),
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_section_update_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        result = _section_receipt(connection, receipt, created=True)
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            f"The local vault rejected invalid {spec.label} update lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_profile_projects(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    return _update_profile_section(data_dir, section="projects", params=params)


def update_profile_education(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    return _update_profile_section(data_dir, section="education", params=params)


def update_profile_skills(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    return _update_profile_section(data_dir, section="skills", params=params)


__all__ = [
    "PROFILE_SECTION_ENTRY_LIMIT",
    "PROFILE_SECTION_LIST_LIMIT",
    "PROFILE_SECTION_OFFSET_LIMIT",
    "PROFILE_SECTION_RESPONSE_LIMIT_BYTES",
    "list_profile_education",
    "list_profile_projects",
    "list_profile_skills",
    "update_profile_education",
    "update_profile_projects",
    "update_profile_skills",
]
