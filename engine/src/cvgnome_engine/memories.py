# SPDX-License-Identifier: MPL-2.0
"""Customer-authored, immutable local stories and explicit project proposals.

Saving a memory never rewrites the public profile. A separate, deliberate
proposal carries only the customer's short project draft into the existing
review inbox. Dates, metrics, skills and work-role attachments are not inferred.
"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .profile_sections import _SPECS, _canonical_entry, _normalize_full_entry, _require_named_profile
from .profile_versions import _stored_version
from .storage import (
    MAX_RPC_SAFE_COUNT, VaultError, _apply_migrations, _canonical_uuid,
    _connect, _json_text, _utc_now_ms,
)

MEMORY_NARRATIVE_CHARS = 12_000
MEMORY_NARRATIVE_BYTES = 48_000
MEMORY_PAGE_LIMIT = 10
MEMORY_OFFSET_LIMIT = 10_000


def _json(value: Any) -> str:
    return _json_text(value, label="Memory material", expected_type=type(value), max_bytes=64 * 1024)


def _checksum(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, label: str, maximum: int, multiline: bool, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{label} contains unsupported characters") from exc
    if len(value) > maximum or byte_length > maximum * 4:
        raise ValueError(f"{label} exceeds its text limit")
    allowed = "\r\n\t" if multiline else ""
    if any(unicodedata.category(char) in {"Cc", "Cf"} and char not in allowed for char in value):
        raise ValueError(f"{label} contains unsupported formatting characters")
    result = value if multiline else value.strip()
    if not any(not char.isspace() and not unicodedata.category(char).startswith("C") for char in result):
        if nullable:
            return None
        raise ValueError(f"{label} must contain visible text")
    return result


def _generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_RPC_SAFE_COUNT:
        raise ValueError("expected_retention_generation must be a positive safe integer")
    return value


def _keys(params: dict[str, Any], expected: set[str]) -> None:
    if not isinstance(params, dict) or set(params) != expected:
        raise ValueError("Memory request contains missing or unsupported fields")


def _context(connection: sqlite3.Connection) -> tuple[int, dict[str, Any] | None]:
    retention = connection.execute(
        "SELECT current_generation FROM profile_source_retention_state WHERE singleton_id = 1"
    ).fetchone()
    if retention is None:
        raise VaultError("vault_integrity_error", "The local source-retention state is missing.")
    row = connection.execute(
        "SELECT * FROM profile_versions ORDER BY version_number DESC LIMIT 1"
    ).fetchone()
    return int(retention[0]), _stored_version(row) if row is not None else None


def _check_context(connection: sqlite3.Connection, parent_id: str, generation: int) -> None:
    current_generation, current = _context(connection)
    if current_generation != generation:
        raise VaultError("memory_stale_generation", "The import set changed. Refresh Memories and try again.")
    if current is None:
        raise VaultError("profile_missing", "Create a profile before saving career memories.")
    if current["id"] != parent_id:
        raise VaultError("memory_profile_conflict", "The profile changed. Refresh Memories and try again.")
    _require_named_profile(current["profile"])


def _memory_row(connection: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise VaultError("memory_not_found", "That saved memory is no longer available.")
    try:
        title = _text(row["title"], label="title", maximum=160, multiline=False, nullable=True)
        narrative = _text(row["narrative"], label="narrative", maximum=MEMORY_NARRATIVE_CHARS, multiline=True)
        valid = (
            _canonical_uuid(row["id"], label="memory_id") == memory_id
            and title == row["title"] and narrative == row["narrative"]
            and _checksum({"title": title, "narrative": narrative}) == row["content_checksum_sha256"]
            and _checksum({
                "request_id": memory_id,
                "expected_parent_profile_version_id": row["profile_version_id"],
                "expected_retention_generation": row["retention_generation"],
                "title": title, "narrative": narrative,
            }) == row["request_fingerprint"]
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise VaultError("vault_integrity_error", "A saved memory failed its integrity check.") from exc
    if not valid:
        raise VaultError("vault_integrity_error", "A saved memory failed its integrity check.")
    return row


def _summary(connection: sqlite3.Connection, row: sqlite3.Row, generation: int) -> dict[str, Any]:
    from .review_inbox import _clean_excerpt

    suggestion = connection.execute(
        "SELECT review_item_id FROM memory_project_receipts WHERE memory_id = ? "
        "ORDER BY created_at_ms DESC, request_id DESC LIMIT 1", (row["id"],)
    ).fetchone()
    return {
        "id": str(row["id"]), "title": row["title"],
        "excerpt": _clean_excerpt(row["narrative"])[:240].rstrip(),
        "character_count": len(row["narrative"]), "created_at_ms": int(row["created_at_ms"]),
        "is_previous_import_set": int(row["retention_generation"]) != generation,
        "review_item_id": str(suggestion[0]) if suggestion is not None else None,
    }


def list_memories(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    _keys(params, {"scope", "limit", "offset"})
    scope, limit, offset = params["scope"], params["limit"], params["offset"]
    if scope not in {"current", "history"}:
        raise ValueError("scope must be current or history")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MEMORY_PAGE_LIMIT:
        raise ValueError("limit must be between 1 and 10")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= MEMORY_OFFSET_LIMIT:
        raise ValueError("offset must be between 0 and 10000")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        generation, profile = _context(connection)
        operator = "=" if scope == "current" else "!="
        total = int(connection.execute(
            f"SELECT count(*) FROM memories WHERE retention_generation {operator} ?", (generation,)
        ).fetchone()[0])
        if total > MAX_RPC_SAFE_COUNT:
            raise VaultError("vault_integrity_error", "The saved memory list is too large.")
        ids = connection.execute(
            f"SELECT id FROM memories WHERE retention_generation {operator} ? "
            "ORDER BY created_at_ms DESC, id DESC LIMIT ? OFFSET ?", (generation, limit, offset)
        ).fetchall()
        items = [_summary(connection, _memory_row(connection, row[0]), generation) for row in ids]
        end = offset + len(items)
        return {
            "scope": scope, "offset": offset, "limit": limit, "total_items": total,
            "next_offset": end if end < total and end <= MEMORY_OFFSET_LIMIT else None,
            "current_profile_version_id": profile["id"] if profile is not None else None,
            "retention_generation": generation, "items": items,
        }
    finally:
        connection.close()


def get_memory(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    _keys(params, {"memory_id"})
    memory_id = _canonical_uuid(params["memory_id"], label="memory_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        generation, _profile = _context(connection)
        row = _memory_row(connection, memory_id)
        return {**_summary(connection, row, generation), "narrative": row["narrative"],
                "profile_version_id": row["profile_version_id"],
                "retention_generation": int(row["retention_generation"])}
    finally:
        connection.close()


def _save_result(row: sqlite3.Row, created: bool) -> dict[str, Any]:
    return {"request_id": row["id"], "memory_id": row["id"], "created": created,
            "created_at_ms": int(row["created_at_ms"]), "profile_version_id": row["profile_version_id"],
            "retention_generation": int(row["retention_generation"])}


def save_memory(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    _keys(params, {"request_id", "expected_parent_profile_version_id", "expected_retention_generation", "title", "narrative"})
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    parent_id = _canonical_uuid(params["expected_parent_profile_version_id"], label="expected_parent_profile_version_id")
    generation = _generation(params["expected_retention_generation"])
    title = _text(params["title"], label="title", maximum=160, multiline=False, nullable=True)
    narrative = _text(params["narrative"], label="narrative", maximum=MEMORY_NARRATIVE_CHARS, multiline=True)
    fingerprint = _checksum({**params, "title": title, "narrative": narrative})
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT request_fingerprint FROM memories WHERE id = ?", (request_id,)).fetchone()
        if existing is not None:
            if existing[0] != fingerprint:
                raise VaultError("memory_request_conflict", "This save request was already used for a different memory.")
            result = _save_result(_memory_row(connection, request_id), False)
        else:
            _check_context(connection, parent_id, generation)
            connection.execute(
                "INSERT INTO memories(id, request_fingerprint, profile_version_id, retention_generation, "
                "title, narrative, content_checksum_sha256, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, fingerprint, parent_id, generation, title, narrative,
                 _checksum({"title": title, "narrative": narrative}), _utc_now_ms()),
            )
            result = _save_result(_memory_row(connection, request_id), True)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _project(name: Any, description: Any) -> dict[str, Any]:
    return _normalize_full_entry(_SPECS["projects"], {
        "name": name, "description": description, "url": None,
        "start_date": None, "end_date": None, "highlights": [], "keywords": [],
    })


def _memory_evidence(row: sqlite3.Row) -> list[dict[str, Any]]:
    from .review_inbox import _clean_excerpt
    return [{"memory_id": str(row["id"]), "locator": {"kind": "memory"},
             "excerpt": _clean_excerpt(row["narrative"])}]


def verify_memory_review_item(connection: sqlite3.Connection, item: sqlite3.Row,
                              proposed: dict[str, Any], evidence: list[dict[str, Any]]) -> sqlite3.Row:
    """Bind a user-authored draft and exact original story to its immutable receipt."""
    row = _memory_row(connection, str(item["memory_id"]))
    receipt = connection.execute("SELECT * FROM memory_project_receipts WHERE review_item_id = ?", (item["id"],)).fetchone()
    try:
        candidate = _project(proposed.get("name"), proposed.get("description"))
        fingerprint = _checksum({
            "request_id": receipt["request_id"], "memory_id": row["id"],
            "expected_parent_profile_version_id": item["profile_version_id"],
            "expected_retention_generation": item["retention_generation"],
            "name": candidate["name"], "description": candidate["description"],
        }) if receipt is not None else None
        valid = (
            item["generator_contract"] == "user-memory-project-v1"
            and item["candidate_kind"] == "projects" and item["operation_kind"] == "add"
            and item["import_id"] is None and item["primary_source_ordinal"] is None
            and int(item["retention_generation"]) == int(row["retention_generation"])
            and proposed == _canonical_entry(_SPECS["projects"], candidate)
            and evidence == _memory_evidence(row)
            and receipt is not None and receipt["memory_id"] == row["id"]
            and receipt["request_fingerprint"] == fingerprint
            and receipt["created_at_ms"] == item["created_at_ms"]
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise VaultError("vault_integrity_error", "A memory suggestion failed its evidence check.") from exc
    if not valid:
        raise VaultError("vault_integrity_error", "A memory suggestion failed its evidence check.")
    return row


def _proposal_result(connection: sqlite3.Connection, data_dir: Path, receipt: sqlite3.Row, created: bool) -> dict[str, Any]:
    from .review_inbox import _review_material
    item = connection.execute("SELECT * FROM profile_review_items WHERE id = ?", (receipt["review_item_id"],)).fetchone()
    if item is None:
        raise VaultError("vault_integrity_error", "The saved memory suggestion lost its review item.")
    _review_material(connection, data_dir, item, verify_source_files=False)
    return {"request_id": receipt["request_id"], "memory_id": receipt["memory_id"],
            "review_item_id": receipt["review_item_id"], "created": created,
            "created_at_ms": int(receipt["created_at_ms"])}


def propose_memory_project(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    _keys(params, {"request_id", "memory_id", "expected_parent_profile_version_id", "expected_retention_generation", "name", "description"})
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    memory_id = _canonical_uuid(params["memory_id"], label="memory_id")
    parent_id = _canonical_uuid(params["expected_parent_profile_version_id"], label="expected_parent_profile_version_id")
    generation = _generation(params["expected_retention_generation"])
    candidate = _project(params["name"], params["description"])
    fingerprint = _checksum({**params, "name": candidate["name"], "description": candidate["description"]})
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM memory_project_receipts WHERE request_id = ?", (request_id,)).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise VaultError("memory_request_conflict", "This request was already used for a different project suggestion.")
            result = _proposal_result(connection, data_dir, existing, False)
        else:
            _check_context(connection, parent_id, generation)
            memory = _memory_row(connection, memory_id)
            if int(memory["retention_generation"]) != generation:
                raise VaultError("memory_stale_generation", "Memories from a previous import set are read-only.")
            if connection.execute("SELECT 1 FROM profile_review_items WHERE memory_id = ?", (memory_id,)).fetchone():
                raise VaultError("memory_project_exists", "This memory already has a project suggestion. Continue it in Review.")
            proposed = _canonical_entry(_SPECS["projects"], candidate)
            evidence = _memory_evidence(memory)
            material = {"candidate_kind": "projects", "operation_kind": "add",
                        "proposed": proposed, "previous": None, "evidence": evidence}
            item_id, now = str(uuid.uuid4()), _utc_now_ms()
            connection.execute(
                "INSERT INTO profile_review_items(id, import_id, memory_id, candidate_ordinal, "
                "primary_source_ordinal, profile_version_id, retention_generation, candidate_kind, "
                "operation_kind, proposed_json, previous_json, candidate_checksum_sha256, "
                "previous_checksum_sha256, evidence_json, generator_contract, created_at_ms) "
                "VALUES (?, NULL, ?, 0, NULL, ?, ?, 'projects', 'add', ?, NULL, ?, NULL, ?, 'user-memory-project-v1', ?)",
                (item_id, memory_id, parent_id, generation, _json(proposed), _checksum(material), _json(evidence), now),
            )
            connection.execute(
                "INSERT INTO memory_project_receipts(request_id, request_fingerprint, memory_id, "
                "review_item_id, created_at_ms) VALUES (?, ?, ?, ?, ?)",
                (request_id, fingerprint, memory_id, item_id, now),
            )
            receipt = connection.execute("SELECT * FROM memory_project_receipts WHERE request_id = ?", (request_id,)).fetchone()
            result = _proposal_result(connection, data_dir, receipt, True)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
