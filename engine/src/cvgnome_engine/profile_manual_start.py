# SPDX-License-Identifier: MPL-2.0
"""Atomic first-profile creation from user-entered Basic Details."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from .profile_versions import (
    _CHANGED_FIELD_ORDER,
    _apply_basic_patch,
    _normalize_patch,
    _public_basics,
    _stored_version,
    _version_row,
)
from .storage import (
    MAX_DRAFT_PROFILE_BYTES,
    VaultError,
    _apply_migrations,
    _canonical_uuid,
    _connect,
    _json_text,
    _utc_now_ms,
)


MAX_MANUAL_START_REQUEST_BYTES = 64 * 1024


def _request_fingerprint(
    *,
    request_id: str,
    patch: dict[str, Any],
) -> tuple[str, str]:
    fingerprint_input = {
        "expected_parent_profile_version_id": None,
        "request_id": request_id,
        "patch": patch,
    }
    encoded = json.dumps(
        fingerprint_input,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_MANUAL_START_REQUEST_BYTES:
        raise ValueError("The manual profile request exceeds its local size limit")
    patch_json = json.dumps(
        patch,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded).hexdigest(), patch_json


def _manual_start_receipt(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    created: bool,
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(receipt["request_id"], label="stored request id")
        output_id = _canonical_uuid(
            receipt["output_profile_version_id"],
            label="stored output profile version id",
        )
        request_fingerprint = str(receipt["request_fingerprint"])
        patch = json.loads(str(receipt["patch_json"]))
        changed_fields = json.loads(str(receipt["changed_fields_json"]))
        created_at_ms = int(receipt["created_at_ms"])
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt failed its integrity check.",
        ) from exc
    if (
        len(request_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in request_fingerprint)
        or not isinstance(patch, dict)
        or not isinstance(changed_fields, list)
        or not 1 <= len(changed_fields) <= len(_CHANGED_FIELD_ORDER)
        or any(field not in _CHANGED_FIELD_ORDER for field in changed_fields)
        or changed_fields
        != [field for field in _CHANGED_FIELD_ORDER if field in changed_fields]
        or len(set(changed_fields)) != len(changed_fields)
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt failed its integrity check.",
        )
    try:
        normalized_patch = _normalize_patch(patch)
    except ValueError as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt failed its integrity check.",
        ) from exc
    if normalized_patch != patch or "name" not in normalized_patch:
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt failed its integrity check.",
        )
    expected_fingerprint, expected_patch_json = _request_fingerprint(
        request_id=request_id,
        patch=normalized_patch,
    )
    if (
        expected_fingerprint != request_fingerprint
        or expected_patch_json != str(receipt["patch_json"])
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt failed its integrity check.",
        )

    version = _stored_version(_version_row(connection, output_id))
    expected_profile, expected_changed_fields = _apply_basic_patch({}, normalized_patch)
    expected_canonical_json = _json_text(
        expected_profile,
        label="Canonical profile",
        expected_type=dict,
        max_bytes=MAX_DRAFT_PROFILE_BYTES,
    )
    competing_claims = int(
        connection.execute(
            """
            SELECT
                EXISTS(
                    SELECT 1 FROM profile_source_imports
                    WHERE output_profile_version_id = ?
                )
              + EXISTS(
                    SELECT 1 FROM profile_version_sources
                    WHERE profile_version_id = ?
                )
              + EXISTS(
                    SELECT 1 FROM profile_basic_update_receipts
                    WHERE output_profile_version_id = ?
                )
              + EXISTS(
                    SELECT 1 FROM profile_work_update_receipts
                    WHERE output_profile_version_id = ?
                )
              + EXISTS(
                    SELECT 1 FROM profile_section_update_receipts
                    WHERE output_profile_version_id = ?
                )
              + EXISTS(
                    SELECT 1 FROM profile_change_receipts
                    WHERE output_profile_version_id = ?
                )
              + EXISTS(
                    SELECT 1 FROM profile_restore_receipts
                    WHERE output_profile_version_id = ?
                )
            """,
            (output_id,) * 7,
        ).fetchone()[0]
    )
    if (
        version["parent_version_id"] is not None
        or version["version_number"] != 1
        or version["source"] != "local_start"
        or version["created_at_ms"] != created_at_ms
        or version["canonical_json"] != expected_canonical_json
        or version["checksum_sha256"]
        != hashlib.sha256(expected_canonical_json.encode("utf-8")).hexdigest()
        or changed_fields != expected_changed_fields
        or competing_claims != 0
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt has invalid lineage.",
        )
    basics = _public_basics(version)
    if not basics["name"]:
        raise VaultError(
            "vault_integrity_error",
            "A stored manual profile receipt points to a nameless profile.",
        )
    return {
        "request_id": request_id,
        "profile_version_id": output_id,
        "parent_profile_version_id": None,
        "version_number": 1,
        "created_at_ms": version["created_at_ms"],
        "created": created,
        "changed_fields": changed_fields,
        "profile_name": basics["name"],
        "headline": basics["headline"],
        "renderable": basics["renderable"],
    }


def start_manual_profile(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "expected_parent_profile_version_id",
        "request_id",
        "patch",
    }
    if set(params) != expected_keys:
        raise ValueError(
            "params must contain exactly expected_parent_profile_version_id, "
            "request_id, and patch"
        )
    if params["expected_parent_profile_version_id"] is not None:
        raise ValueError("expected_parent_profile_version_id must be null")
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    patch = _normalize_patch(params["patch"])
    if "name" not in patch:
        raise ValueError("patch must include a non-empty name")
    request_fingerprint, patch_json = _request_fingerprint(
        request_id=request_id,
        patch=patch,
    )

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")

        # Resolve idempotency before inspecting mutable workspace state. A caller
        # can therefore safely recover the first result after later edits exist.
        existing = connection.execute(
            "SELECT * FROM profile_manual_start_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise VaultError(
                    "profile_start_request_conflict",
                    "That manual profile request was already used with different inputs.",
                )
            result = _manual_start_receipt(connection, existing, created=False)
            connection.commit()
            return result

        if connection.execute("SELECT 1 FROM profile_versions LIMIT 1").fetchone():
            raise VaultError(
                "profile_start_conflict",
                "A career profile already exists in this local workspace.",
            )
        if connection.execute("SELECT 1 FROM profile_source_scans LIMIT 1").fetchone():
            raise VaultError(
                "profile_start_preview_conflict",
                "Discard the unfinished source preview before starting manually.",
            )

        profile, changed_fields = _apply_basic_patch({}, patch)
        canonical_json = _json_text(
            profile,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
        checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        output_id = str(uuid.uuid4())
        created_at_ms = _utc_now_ms()
        connection.execute(
            """
            INSERT INTO profile_versions(
                id, version_number, parent_version_id, canonical_json,
                checksum_sha256, source, created_at_ms
            ) VALUES (?, 1, NULL, ?, ?, 'local_start', ?)
            """,
            (output_id, canonical_json, checksum, created_at_ms),
        )
        connection.execute(
            """
            INSERT INTO profile_manual_start_receipts(
                request_id, request_fingerprint, output_profile_version_id,
                patch_json, changed_fields_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_fingerprint,
                output_id,
                patch_json,
                json.dumps(changed_fields, separators=(",", ":")),
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_manual_start_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        result = _manual_start_receipt(connection, receipt, created=True)
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            "The local vault rejected invalid manual profile lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = ["MAX_MANUAL_START_REQUEST_BYTES", "start_manual_profile"]
