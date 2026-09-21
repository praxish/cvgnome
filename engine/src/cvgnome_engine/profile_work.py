# SPDX-License-Identifier: MPL-2.0
"""Bounded public Work History projection and immutable local edits."""

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


WORK_LIST_LIMIT = 20
WORK_LIST_OFFSET_LIMIT = 10_000
WORK_LIST_RESPONSE_LIMIT_BYTES = 128 * 1024
WORK_ENTRY_LIMIT = 5_000
WORK_HIGHLIGHT_LIMIT = 8
WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS = 1_800

_FIELD_ORDER = (
    "name",
    "position",
    "url",
    "start_date",
    "end_date",
    "summary",
    "highlights",
    "location",
)
_CANONICAL_FIELDS = {
    "name": "name",
    "position": "position",
    "url": "url",
    "start_date": "startDate",
    "end_date": "endDate",
    "summary": "summary",
    "highlights": "highlights",
    "location": "location",
}
_TEXT_LIMITS: dict[str, tuple[int, int, bool]] = {
    "name": (240, 960, False),
    "position": (240, 960, False),
    "url": (2_048, 8_192, False),
    "start_date": (80, 320, False),
    "end_date": (80, 320, False),
    "summary": (1_000, 4_000, True),
    "highlight": (360, 1_440, False),
    "location": (200, 800, False),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _clean_text(value: str, *, field: str, truncate: bool) -> str | None:
    max_chars, max_bytes, multiline = _TEXT_LIMITS[field]
    normalized = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    if multiline:
        normalized = (
            normalized.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\t", " ")
        )
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


def _normalize_text(value: Any, *, field: str, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be text")
    try:
        cleaned = _clean_text(value, field=field, truncate=False)
    except ValueError as exc:
        raise ValueError(f"{path} {exc}") from exc
    if cleaned is None:
        raise ValueError(f"{path} must not be blank")
    return cleaned


def _public_text(value: Any, *, field: str) -> str | None:
    if not isinstance(value, str):
        return None
    return _clean_text(value, field=field, truncate=True)


def _normalize_url(value: Any, *, path: str) -> str:
    cleaned = _normalize_text(value, field="url", path=path)
    if _public_url(cleaned) != cleaned:
        raise ValueError(
            f"{path} must be a public credential-free HTTP or HTTPS URL"
        )
    return cleaned


def _normalize_highlights(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list of text values or null")
    if len(value) > WORK_HIGHLIGHT_LIMIT:
        raise ValueError(
            f"{path} may contain at most {WORK_HIGHLIGHT_LIMIT} highlights"
        )
    result: list[str] = []
    total_chars = 0
    for index, raw in enumerate(value):
        highlight = _normalize_text(
            raw,
            field="highlight",
            path=f"{path}[{index}]",
        )
        total_chars += len(highlight)
        if total_chars > WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS:
            raise ValueError(
                f"{path} may contain at most "
                f"{WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS} highlight characters"
            )
        result.append(highlight)
    return result


def _public_highlights(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    total_chars = 0
    for raw in value:
        cleaned = _public_text(raw, field="highlight")
        if cleaned is not None:
            remaining = WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS - total_chars
            if remaining <= 0:
                break
            cleaned = cleaned[:remaining].rstrip()
            if not cleaned:
                break
            result.append(cleaned)
            total_chars += len(cleaned)
        if len(result) >= WORK_HIGHLIGHT_LIMIT:
            break
    return result


def _public_work_entry(entry_index: int, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_index": entry_index,
        "name": _public_text(entry.get("name"), field="name") or "",
        "position": _public_text(entry.get("position"), field="position") or "",
        "url": _public_url(entry.get("url")),
        "start_date": _public_text(entry.get("startDate"), field="start_date"),
        "end_date": _public_text(entry.get("endDate"), field="end_date"),
        "summary": _public_text(entry.get("summary"), field="summary"),
        "highlights": _public_highlights(entry.get("highlights")),
        "location": _public_text(entry.get("location"), field="location"),
    }


def _listable_work_entry(
    entry_index: int,
    entry: Any,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    return _public_work_entry(entry_index, entry)


def _work_entry_has_required_identity(entry: dict[str, Any]) -> bool:
    public = _public_work_entry(0, entry)
    return bool(public["name"] and public["position"])


def list_profile_work(
    data_dir: Path,
    profile_version_id: Any,
    offset: Any,
) -> dict[str, Any]:
    normalized_id = _canonical_uuid(profile_version_id, label="profile_version_id")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > WORK_LIST_OFFSET_LIMIT
    ):
        raise ValueError("offset must be an integer between 0 and 10000")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        version = _stored_version(_version_row(connection, normalized_id))
        raw_work = version["profile"].get("work")
        public_entries = (
            [
                public
                for entry_index, entry in enumerate(raw_work)
                if (public := _listable_work_entry(entry_index, entry)) is not None
            ]
            if isinstance(raw_work, list)
            else []
        )
        total_items = len(public_entries)
        page = public_entries[offset : offset + WORK_LIST_LIMIT]
        connection.commit()
    finally:
        connection.close()
    if not 0 <= total_items <= WORK_ENTRY_LIMIT:
        raise VaultError(
            "vault_integrity_error",
            "The stored work history exceeds the supported size.",
        )
    items: list[dict[str, Any]] = []
    for item in page:
        prospective = {
            "profile_version_id": version["id"],
            "version_number": version["version_number"],
            "total_items": total_items,
            "offset": offset,
            "limit": WORK_LIST_LIMIT,
            "next_offset": offset + len(items) + 1,
            "items": [*items, item],
        }
        prospective_bytes = json.dumps(
            prospective,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(prospective_bytes) > WORK_LIST_RESPONSE_LIMIT_BYTES:
            break
        items.append(item)
    if page and not items:
        raise VaultError(
            "vault_integrity_error",
            "A public work-history entry exceeds the supported response size.",
        )
    page_end = offset + len(items)
    result = {
        "profile_version_id": version["id"],
        "version_number": version["version_number"],
        "total_items": total_items,
        "offset": offset,
        "limit": WORK_LIST_LIMIT,
        "next_offset": (
            page_end
            if page_end < total_items and page_end <= WORK_LIST_OFFSET_LIMIT
            else None
        ),
        "items": items,
    }
    encoded = json.dumps(
        result,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > WORK_LIST_RESPONSE_LIMIT_BYTES:
        raise VaultError(
            "vault_integrity_error",
            "The public work-history page exceeds the supported response size.",
        )
    return result


def _normalize_optional_text(
    value: Any,
    *,
    field: str,
    path: str,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, field=field, path=path)


def _normalize_full_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != set(_FIELD_ORDER):
        raise ValueError(
            "operation.entry must contain exactly name, position, url, start_date, "
            "end_date, summary, highlights, and location"
        )
    normalized: dict[str, Any] = {
        "name": _normalize_text(
            entry["name"], field="name", path="operation.entry.name"
        ),
        "position": _normalize_text(
            entry["position"],
            field="position",
            path="operation.entry.position",
        ),
    }
    for field in ("start_date", "end_date", "summary", "location"):
        normalized[field] = _normalize_optional_text(
            entry[field], field=field, path=f"operation.entry.{field}"
        )
    normalized["url"] = (
        None
        if entry["url"] is None
        else _normalize_url(entry["url"], path="operation.entry.url")
    )
    if entry["highlights"] is None:
        raise ValueError("operation.entry.highlights must be a list of text values")
    normalized["highlights"] = _normalize_highlights(
        entry["highlights"], path="operation.entry.highlights"
    )
    return {field: normalized[field] for field in _FIELD_ORDER}


def _normalize_patch(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("operation.patch must be a non-empty object")
    if any(not isinstance(key, str) or key not in _FIELD_ORDER for key in patch):
        raise ValueError("operation.patch contains unsupported fields")
    normalized: dict[str, Any] = {}
    for field in _FIELD_ORDER:
        if field not in patch:
            continue
        value = patch[field]
        path = f"operation.patch.{field}"
        if field in {"name", "position"}:
            if value is None:
                raise ValueError(f"{path} must be non-empty text")
            normalized[field] = _normalize_text(value, field=field, path=path)
        elif field == "url":
            normalized[field] = (
                None if value is None else _normalize_url(value, path=path)
            )
        elif field == "highlights":
            normalized[field] = (
                None if value is None else _normalize_highlights(value, path=path)
            )
        else:
            normalized[field] = _normalize_optional_text(
                value,
                field=field,
                path=path,
            )
    return normalized


def _normalize_entry_index(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= WORK_ENTRY_LIMIT
    ):
        raise ValueError("operation.entry_index must be an integer between 0 and 4999")
    return value


def _normalize_operation(operation: Any) -> dict[str, Any]:
    if not isinstance(operation, dict):
        raise ValueError("operation must be a tagged object")
    kind = operation.get("kind")
    if kind == "add":
        if set(operation) != {"kind", "entry"}:
            raise ValueError("add operation must contain exactly kind and entry")
        return {"kind": "add", "entry": _normalize_full_entry(operation["entry"])}
    if kind == "update":
        if set(operation) != {"kind", "entry_index", "patch"}:
            raise ValueError(
                "update operation must contain exactly kind, entry_index, and patch"
            )
        return {
            "kind": "update",
            "entry_index": _normalize_entry_index(operation["entry_index"]),
            "patch": _normalize_patch(operation["patch"]),
        }
    if kind == "remove":
        if set(operation) != {"kind", "entry_index"}:
            raise ValueError(
                "remove operation must contain exactly kind and entry_index"
            )
        return {
            "kind": "remove",
            "entry_index": _normalize_entry_index(operation["entry_index"]),
        }
    raise ValueError("operation.kind must be add, update, or remove")


def _canonical_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in _FIELD_ORDER:
        value = entry[field]
        if value is not None:
            result[_CANONICAL_FIELDS[field]] = deepcopy(value)
    return result


def _changed_fields_between(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[str]:
    return [
        field
        for field in _FIELD_ORDER
        if (
            (_CANONICAL_FIELDS[field] in before)
            != (_CANONICAL_FIELDS[field] in after)
            or before.get(_CANONICAL_FIELDS[field])
            != after.get(_CANONICAL_FIELDS[field])
        )
    ]


def _added_changed_fields(entry: dict[str, Any]) -> list[str]:
    return [
        field
        for field in _FIELD_ORDER
        if _CANONICAL_FIELDS[field] in entry
        and not (field == "highlights" and not entry["highlights"])
    ]


def _removed_changed_fields(entry: dict[str, Any]) -> list[str]:
    present = [
        field for field in _FIELD_ORDER if _CANONICAL_FIELDS[field] in entry
    ]
    return present or ["name", "position"]


def _unknown_work_fields(entry: dict[str, Any]) -> dict[str, Any]:
    public_keys = set(_CANONICAL_FIELDS.values())
    return {key: value for key, value in entry.items() if key not in public_keys}


def _stored_field_is_normalized(entry: dict[str, Any], field: str) -> bool:
    canonical_field = _CANONICAL_FIELDS[field]
    if canonical_field not in entry:
        return field not in {"name", "position", "highlights"}
    value = entry[canonical_field]
    try:
        if field == "url":
            return _normalize_url(value, path="stored work URL") == value
        if field == "highlights":
            return _normalize_highlights(
                value,
                path="stored work highlights",
            ) == value
        return _normalize_text(value, field=field, path="stored work field") == value
    except ValueError:
        return False


def _strict_added_entry(entry: dict[str, Any]) -> bool:
    return not _unknown_work_fields(entry) and all(
        _stored_field_is_normalized(entry, field) for field in _FIELD_ORDER
    )


def _mutable_work(profile: dict[str, Any]) -> list[Any]:
    if "work" not in profile or profile.get("work") is None:
        return []
    work = profile.get("work")
    if not isinstance(work, list):
        raise ValueError("The current profile work history is malformed")
    return deepcopy(work)


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


def _apply_work_operation(
    profile: dict[str, Any],
    operation: dict[str, Any],
) -> tuple[dict[str, Any], int, list[str]]:
    result = deepcopy(profile)
    work = _mutable_work(profile)
    kind = operation["kind"]
    if kind == "add":
        if len(work) >= WORK_ENTRY_LIMIT:
            raise ValueError("The work history cannot contain more than 5000 entries")
        entry = _canonical_entry(operation["entry"])
        entry_index = len(work)
        work.append(entry)
        changed_fields = _added_changed_fields(entry)
    else:
        entry_index = operation["entry_index"]
        if entry_index >= len(work):
            raise ValueError("operation.entry_index does not identify a work entry")
        target = work[entry_index]
        if not isinstance(target, dict):
            raise ValueError("operation.entry_index does not identify a valid work entry")
        if kind == "remove":
            changed_fields = _removed_changed_fields(target)
            del work[entry_index]
        else:
            edited = deepcopy(target)
            for field, value in operation["patch"].items():
                canonical_field = _CANONICAL_FIELDS[field]
                if value is None:
                    edited.pop(canonical_field, None)
                else:
                    edited[canonical_field] = deepcopy(value)
            for required_field in ("name", "position"):
                _normalize_text(
                    edited.get(_CANONICAL_FIELDS[required_field]),
                    field=required_field,
                    path=f"resulting work entry {required_field}",
                )
            changed_fields = _changed_fields_between(target, edited)
            if not changed_fields:
                raise ValueError(
                    "operation.patch does not make a semantic change to the work entry"
                )
            work[entry_index] = edited
    result["work"] = work
    _require_named_profile(result)
    validate_canonical_profile(result)
    return result, entry_index, changed_fields


def _receipt_work(profile: dict[str, Any]) -> list[Any]:
    if "work" not in profile or profile.get("work") is None:
        return []
    work = profile.get("work")
    if not isinstance(work, list) or len(work) > WORK_ENTRY_LIMIT:
        raise VaultError(
            "vault_integrity_error",
            "A stored work update receipt has invalid profile data.",
        )
    return work


def _work_receipt(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    created: bool,
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(receipt["request_id"], label="stored request id")
        parent_id = _canonical_uuid(
            receipt["parent_profile_version_id"],
            label="stored parent profile version id",
        )
        output_id = _canonical_uuid(
            receipt["output_profile_version_id"],
            label="stored output profile version id",
        )
        request_fingerprint = str(receipt["request_fingerprint"])
        operation = str(receipt["operation"])
        entry_index = int(receipt["entry_index"])
        created_at_ms = int(receipt["created_at_ms"])
        changed_fields = json.loads(str(receipt["changed_fields_json"]))
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError, RecursionError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored work update receipt failed its integrity check.",
        ) from exc
    if (
        _SHA256_RE.fullmatch(request_fingerprint) is None
        or operation not in {"add", "update", "remove"}
        or not 0 <= entry_index < WORK_ENTRY_LIMIT
        or not 1 <= created_at_ms <= MAX_RPC_SAFE_COUNT
        or not isinstance(changed_fields, list)
        or len(changed_fields) > len(_FIELD_ORDER)
        or any(field not in _FIELD_ORDER for field in changed_fields)
        or changed_fields != [field for field in _FIELD_ORDER if field in changed_fields]
        or len(set(changed_fields)) != len(changed_fields)
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored work update receipt failed its integrity check.",
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
            "A stored work update receipt has invalid lineage.",
        )
    parent_work = _receipt_work(parent["profile"])
    output_work = _receipt_work(output["profile"])
    expected_profile = deepcopy(parent["profile"])
    if operation == "add":
        valid = (
            entry_index == len(parent_work)
            and len(output_work) == len(parent_work) + 1
            and output_work[:entry_index] == parent_work
            and isinstance(output_work[entry_index], dict)
            and _listable_work_entry(entry_index, output_work[entry_index]) is not None
            and _strict_added_entry(output_work[entry_index])
        )
        expected_changes = (
            _added_changed_fields(output_work[entry_index])
            if valid
            else []
        )
        if valid:
            valid = all(
                _CANONICAL_FIELDS[field] not in output_work[entry_index]
                or _stored_field_is_normalized(output_work[entry_index], field)
                for field in expected_changes
            )
    elif operation == "update":
        valid = (
            entry_index < len(parent_work)
            and len(output_work) == len(parent_work)
            and isinstance(parent_work[entry_index], dict)
            and isinstance(output_work[entry_index], dict)
            and _work_entry_has_required_identity(output_work[entry_index])
            and _unknown_work_fields(parent_work[entry_index])
            == _unknown_work_fields(output_work[entry_index])
            and output_work[:entry_index] == parent_work[:entry_index]
            and output_work[entry_index + 1 :] == parent_work[entry_index + 1 :]
            and output_work[entry_index] != parent_work[entry_index]
        )
        expected_changes = (
            _changed_fields_between(
                parent_work[entry_index],
                output_work[entry_index],
            )
            if valid
            else []
        )
        if valid:
            valid = all(
                _CANONICAL_FIELDS[field] not in output_work[entry_index]
                or _stored_field_is_normalized(output_work[entry_index], field)
                for field in expected_changes
            )
    else:
        valid = (
            entry_index < len(parent_work)
            and isinstance(parent_work[entry_index], dict)
            and output_work
            == parent_work[:entry_index] + parent_work[entry_index + 1 :]
        )
        expected_changes = (
            _removed_changed_fields(parent_work[entry_index])
            if valid
            else []
        )
    expected_profile["work"] = output_work
    if (
        not valid
        or output["profile"] != expected_profile
        or changed_fields != expected_changes
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored work update receipt has invalid mutation data.",
        )
    try:
        profile_name = _require_named_profile(output["profile"])
    except ValueError as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored work update receipt points to a nameless profile.",
        ) from exc
    return {
        "request_id": request_id,
        "profile_version_id": output_id,
        "parent_profile_version_id": parent_id,
        "version_number": output["version_number"],
        "created_at_ms": output["created_at_ms"],
        "created": created,
        "operation": operation,
        "entry_index": entry_index,
        "changed_fields": changed_fields,
        "profile_name": profile_name,
        "work_entries": sum(isinstance(entry, dict) for entry in output_work),
        "renderable": is_baseline_resume_renderable(output["profile"]),
    }


def update_profile_work(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "expected_parent_profile_version_id",
        "request_id",
        "operation",
    }
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
    operation = _normalize_operation(params["operation"])
    fingerprint_input = {
        "expected_parent_profile_version_id": parent_id,
        "request_id": request_id,
        "operation": operation,
    }
    fingerprint_json = json.dumps(
        fingerprint_input,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_fingerprint = hashlib.sha256(
        fingerprint_json.encode("utf-8")
    ).hexdigest()

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM profile_work_update_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise VaultError(
                    "profile_update_request_conflict",
                    "That Work History request was already used with different inputs.",
                )
            result = _work_receipt(connection, existing, created=False)
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
                "Build or import a career profile before editing Work History.",
            )
        current = _stored_version(current_row)
        if current["id"] != parent_id:
            raise VaultError(
                "profile_update_conflict",
                "The career profile changed before this Work History edit could be saved.",
            )
        if current["version_number"] >= MAX_RPC_SAFE_COUNT:
            raise VaultError(
                "profile_version_limit",
                "The local profile history cannot be advanced.",
            )
        edited_profile, entry_index, changed_fields = _apply_work_operation(
            current["profile"], operation
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
            INSERT INTO profile_work_update_receipts(
                request_id, request_fingerprint, output_profile_version_id,
                parent_profile_version_id, operation, entry_index,
                changed_fields_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_fingerprint,
                output_id,
                parent_id,
                operation["kind"],
                entry_index,
                json.dumps(changed_fields, separators=(",", ":")),
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_work_update_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        result = _work_receipt(connection, receipt, created=True)
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            "The local vault rejected invalid Work History update lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "WORK_ENTRY_LIMIT",
    "WORK_HIGHLIGHT_LIMIT",
    "WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS",
    "WORK_LIST_LIMIT",
    "WORK_LIST_OFFSET_LIMIT",
    "WORK_LIST_RESPONSE_LIMIT_BYTES",
    "list_profile_work",
    "update_profile_work",
]
