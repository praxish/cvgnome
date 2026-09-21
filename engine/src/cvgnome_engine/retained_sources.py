# SPDX-License-Identifier: MPL-2.0
"""Safe, paginated views of immutable profile-source lineage.

The renderer may review retained material metadata, but it must never receive
managed paths, content hashes, extracted text, or internal source identifiers.
This module keeps that boundary explicit and intentionally small.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import VaultError, _apply_migrations, _connect


MATERIAL_SCOPES = frozenset({"active", "historical", "all"})
MAX_MATERIAL_LIST_LIMIT = 25
MAX_MATERIAL_LIST_OFFSET = 100_000
MAX_MATERIAL_LIST_RESPONSE_BYTES = 64 * 1024
_LIST_PARAM_KEYS = frozenset({"scope", "limit", "offset"})


def _strict_integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _validated_params(params: dict[str, Any]) -> tuple[str, int, int]:
    if set(params) != _LIST_PARAM_KEYS:
        raise ValueError("params must contain exactly scope, limit, and offset")
    scope = params["scope"]
    if not isinstance(scope, str) or scope not in MATERIAL_SCOPES:
        raise ValueError("scope must be active, historical, or all")
    limit = _strict_integer(
        params["limit"],
        label="limit",
        minimum=1,
        maximum=MAX_MATERIAL_LIST_LIMIT,
    )
    offset = _strict_integer(
        params["offset"],
        label="offset",
        minimum=0,
        maximum=MAX_MATERIAL_LIST_OFFSET,
    )
    return scope, limit, offset


def list_retained_sources(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Return renderer-safe retained-material metadata for one retention scope."""

    scope, limit, offset = _validated_params(params)
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        state = connection.execute(
            """
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if state is None:
            raise VaultError(
                "vault_integrity_error",
                "The local source-retention state is missing.",
            )
        current_generation = int(state["current_generation"])

        if scope == "active":
            scope_where = "source_import.retention_generation = ?"
            scope_bindings: tuple[Any, ...] = (current_generation,)
        elif scope == "historical":
            scope_where = "source_import.retention_generation < ?"
            scope_bindings = (current_generation,)
        else:
            scope_where = "1 = 1"
            scope_bindings = ()

        totals = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_entries,
                COUNT(DISTINCT profile_source.source_id) AS total_unique_files,
                COUNT(DISTINCT source_import.id) AS total_imports
            FROM profile_version_sources AS profile_source
            JOIN profile_source_imports AS source_import
              ON source_import.id = profile_source.import_id
            WHERE {scope_where}
            """,
            scope_bindings,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT
                source_import.id AS import_id,
                profile_source.ordinal,
                profile_source.display_name,
                profile_source.source_format,
                profile_source.source_kind,
                source.byte_size,
                profile_source.extraction_status,
                profile_source.issue_code,
                source_import.retention_generation,
                source_import.committed_at_ms,
                profile.version_number AS profile_version_number,
                source_import.synthesis_contract,
                (
                    SELECT COUNT(*)
                    FROM profile_version_sources AS import_source
                    WHERE import_source.import_id = source_import.id
                ) AS import_file_count,
                (
                    SELECT COUNT(*)
                    FROM profile_version_sources AS content_reference
                    WHERE content_reference.source_id = profile_source.source_id
                ) AS content_reference_count
            FROM profile_version_sources AS profile_source
            JOIN profile_source_imports AS source_import
              ON source_import.id = profile_source.import_id
            JOIN sources AS source ON source.id = profile_source.source_id
            JOIN profile_versions AS profile ON profile.id = profile_source.profile_version_id
            WHERE {scope_where}
            ORDER BY
                source_import.committed_at_ms DESC,
                source_import.id DESC,
                profile_source.ordinal ASC
            LIMIT ? OFFSET ?
            """,
            (*scope_bindings, limit, offset),
        ).fetchall()

        items = [
            {
                "import_id": str(row["import_id"]),
                "ordinal": int(row["ordinal"]),
                "display_name": str(row["display_name"]),
                "source_format": str(row["source_format"]),
                "source_kind": str(row["source_kind"]),
                "byte_size": int(row["byte_size"]),
                "extraction_status": str(row["extraction_status"]),
                "issue_code": (
                    str(row["issue_code"]) if row["issue_code"] is not None else None
                ),
                "retention_generation": int(row["retention_generation"]),
                "retention_status": (
                    "active"
                    if int(row["retention_generation"]) == current_generation
                    else "historical"
                ),
                "committed_at_ms": int(row["committed_at_ms"]),
                "profile_version_number": int(row["profile_version_number"]),
                "import_mode": (
                    "canonical"
                    if str(row["synthesis_contract"]) == "canonical-file-v1"
                    else "source_set"
                ),
                "import_file_count": int(row["import_file_count"]),
                "content_reference_count": int(row["content_reference_count"]),
            }
            for row in rows
        ]
        result = {
            "scope": scope,
            "current_generation": current_generation,
            "offset": offset,
            "limit": limit,
            "total_entries": int(totals["total_entries"]),
            "total_unique_files": int(totals["total_unique_files"]),
            "total_imports": int(totals["total_imports"]),
            "items": items,
        }
        response_size = len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if response_size > MAX_MATERIAL_LIST_RESPONSE_BYTES:
            raise VaultError(
                "profile_sources_response_too_large",
                "The retained-materials list exceeded its safe response limit.",
            )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
