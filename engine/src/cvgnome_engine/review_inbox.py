# SPDX-License-Identifier: MPL-2.0
"""Durable, bounded review proposals produced by follow-up local imports.

The canonical profile remains the source of truth.  This module partitions a
small allowlist of deterministic source-synthesis changes before a follow-up
profile is saved, then exposes only human-readable cards and bounded evidence.
Schema 17 adds a version-pinned apply operation. Applying one current-generation
item appends one immutable profile version and one terminal review receipt in a
single SQLite transaction; the imported proposal and its evidence never change.
"""

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

from .profile_review import _education_cards, _named_cards, _skill_cards
from .profile_sections import (
    PROFILE_SECTION_ENTRY_LIMIT,
    _SPECS,
    _added_changed_fields,
    _canonical_entry,
    _changed_fields_between,
    _json_equivalent,
    _mark_education_curated,
    _mutable_entries,
    _normalize_full_entry,
    _public_entry,
    _require_named_profile,
    _validate_entry_identity,
)
from .profile.validation import validate_canonical_profile
from .profile_versions import _stored_version, _version_row
from .storage import (
    MAX_DRAFT_PROFILE_BYTES,
    MAX_REVIEW_CANDIDATES,
    MAX_REVIEW_CANDIDATE_BYTES,
    MAX_RPC_SAFE_COUNT,
    VaultError,
    _apply_migrations,
    _canonical_uuid,
    _connect,
    _json_text,
    _managed_file_matches,
    _managed_source_blob_path,
    _profile_review_state_counts,
    _utc_now_ms,
)


REVIEW_INBOX_PAGE_LIMIT = 10
REVIEW_INBOX_OFFSET_LIMIT = 10_000
REVIEW_INBOX_RESPONSE_LIMIT_BYTES = 112 * 1024
REVIEW_TRANSITION_RESPONSE_LIMIT_BYTES = 8 * 1024
REVIEW_APPLY_RESPONSE_LIMIT_BYTES = 16 * 1024
REVIEW_TRANSITION_STATES = frozenset({"inbox", "deferred", "rejected"})
REVIEW_SCOPES = frozenset({"inbox", "deferred", "history"})
REVIEW_ACTIONS = frozenset({"defer", "reject", "reopen"})
_TARGET_SECTIONS = ("education", "projects", "skills")
_WHITESPACE_RE = re.compile(r"\s+")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _clean_excerpt(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    visible = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    return _WHITESPACE_RE.sub(" ", visible).strip()[:360].rstrip()


def _json_pointer_value(document: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return document
    current = document
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


_STRUCTURED_EVIDENCE_FIELDS = {
    "education": frozenset(
        {
            "institution",
            "school",
            "university",
            "college",
            "studyType",
            "study_type",
            "degree",
            "credential",
            "qualification",
            "area",
            "major",
            "field",
            "focus",
            "discipline",
            "url",
            "startDate",
            "start_date",
            "start",
            "endDate",
            "end_date",
            "end",
            "graduation",
            "score",
            "gpa",
            "courses",
        }
    ),
    "projects": frozenset(
        {
            "name",
            "description",
            "url",
            "startDate",
            "start_date",
            "endDate",
            "end_date",
            "highlights",
            "keywords",
        }
    ),
    "skills": frozenset({"name", "category", "level", "keywords", "skills"}),
}


def _evidence_excerpt(
    text: str,
    locator: dict[str, Any],
    candidate_kind: str,
) -> str:
    if locator.get("kind") == "line":
        start = locator.get("start")
        end = locator.get("end")
        if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            excerpt = _clean_excerpt("\n".join(lines[start - 1 : min(end, start + 5)]))
            if excerpt:
                return excerpt
    elif locator.get("kind") == "json_pointer":
        pointer = locator.get("value")
        if isinstance(pointer, str):
            try:
                value = _json_pointer_value(json.loads(text), pointer)
                if isinstance(value, dict):
                    value = {
                        key: child
                        for key, child in value.items()
                        if key in _STRUCTURED_EVIDENCE_FIELDS[candidate_kind]
                    }
                if not isinstance(value, (dict, list, str, int, float, bool)):
                    return ""
                excerpt = _clean_excerpt(_canonical_json(value))
                if excerpt:
                    return excerpt
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
    return ""


def _candidate_evidence(
    *,
    facts: list[dict[str, Any]],
    source_manifest: list[dict[str, Any]],
    synthesis_sources: list[dict[str, Any]],
    candidate_kind: str,
) -> list[dict[str, Any]]:
    source_by_id = {
        source.get("source_id"): source
        for source in source_manifest
        if isinstance(source, dict) and isinstance(source.get("source_id"), str)
    }
    candidates: list[tuple[str, str, int, str, dict[str, Any]]] = []
    for fact in facts:
        manifest = source_by_id.get(fact.get("source_id"))
        locator = fact.get("locator")
        if not isinstance(manifest, dict) or not isinstance(locator, dict):
            continue
        characters_used = manifest.get("characters_used")
        matching_inputs = [
            source
            for source in synthesis_sources
            if isinstance(source, dict)
            and source.get("sha256") == manifest.get("sha256")
            and source.get("display_name") == manifest.get("display_name")
            and source.get("media_type") == manifest.get("media_type")
            and isinstance(source.get("text"), str)
            and isinstance(characters_used, int)
            and not isinstance(characters_used, bool)
            and hashlib.sha256(
                source["text"][:characters_used].encode("utf-8")
            ).hexdigest()
            == manifest.get("text_sha256")
            and isinstance(source.get("origin_ordinal"), int)
            and not isinstance(source.get("origin_ordinal"), bool)
        ]
        source_input = (
            min(matching_inputs, key=lambda source: int(source["origin_ordinal"]))
            if matching_inputs
            else None
        )
        source_ordinal = (
            source_input.get("origin_ordinal") if isinstance(source_input, dict) else None
        )
        text = source_input.get("text") if isinstance(source_input, dict) else None
        if (
            not isinstance(source_ordinal, int)
            or isinstance(source_ordinal, bool)
            or not isinstance(text, str)
        ):
            continue
        if locator.get("kind") == "line":
            normalized_locator = {
                "kind": "line",
                "start": locator.get("start"),
                "end": locator.get("end"),
            }
        elif locator.get("kind") == "json_pointer":
            normalized_locator = {
                "kind": "json_pointer",
                "value": locator.get("value"),
            }
        else:
            continue
        excerpt = _evidence_excerpt(text, normalized_locator, candidate_kind)
        if not excerpt:
            continue
        marker = _canonical_json(normalized_locator)
        candidates.append(
            (
                str(manifest["source_id"]),
                marker,
                source_ordinal,
                str(fact.get("id") or ""),
                {
                    "source_ordinal": source_ordinal,
                    "locator": normalized_locator,
                    "excerpt": excerpt,
                },
            )
        )
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for _source_id, marker, source_ordinal, _fact_id, value in sorted(candidates):
        identity = (source_ordinal, marker)
        if identity in seen:
            continue
        seen.add(identity)
        evidence.append(value)
        if len(evidence) == 3:
            break
    return evidence


def partition_review_candidates(
    *,
    existing_profile: dict[str, Any],
    draft_profile: dict[str, Any],
    synthesis_sources: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Quarantine genuine Education, Project, and Skill additions/extensions.

    The caller invokes this only when the base profile is already renderable.
    A first import or bootstrap-completion import must bypass this function.
    """

    if not isinstance(existing_profile, dict) or not isinstance(draft_profile, dict):
        raise TypeError("Review candidate partitioning requires profile objects")
    result = deepcopy(draft_profile)
    meta = draft_profile.get("meta")
    ingest = meta.get("source_ingest") if isinstance(meta, dict) else None
    facts = ingest.get("facts") if isinstance(ingest, dict) else None
    source_manifest = ingest.get("sources") if isinstance(ingest, dict) else None
    if not isinstance(facts, list) or not isinstance(source_manifest, list):
        return result, []

    grouped_facts: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for raw_fact in facts:
        if not isinstance(raw_fact, dict) or raw_fact.get("action") not in {
            "added",
            "extended",
        }:
            continue
        section = raw_fact.get("profile_section")
        entry_index = raw_fact.get("profile_entry_index")
        if (
            section not in _TARGET_SECTIONS
            or not isinstance(entry_index, int)
            or isinstance(entry_index, bool)
            or entry_index < 0
        ):
            continue
        grouped_facts.setdefault((section, entry_index), []).append(raw_fact)

    candidates: list[dict[str, Any]] = []
    replacements: dict[str, dict[int, dict[str, Any]]] = {}
    removals: dict[str, set[int]] = {}
    for section_order, section in enumerate(_TARGET_SECTIONS):
        base_entries = (
            existing_profile.get(section)
            if isinstance(existing_profile.get(section), list)
            else []
        )
        draft_entries = (
            draft_profile.get(section) if isinstance(draft_profile.get(section), list) else []
        )
        for entry_index, proposed in enumerate(draft_entries):
            if not isinstance(proposed, dict):
                continue
            relevant_facts = grouped_facts.get((section, entry_index), [])
            if not relevant_facts:
                continue
            previous = (
                base_entries[entry_index]
                if entry_index < len(base_entries)
                and isinstance(base_entries[entry_index], dict)
                else None
            )
            if previous is None:
                if not any(fact.get("action") == "added" for fact in relevant_facts):
                    continue
                operation_kind = "add"
            else:
                if proposed == previous or not any(
                    fact.get("action") == "extended" for fact in relevant_facts
                ):
                    continue
                operation_kind = "update"
            evidence = _candidate_evidence(
                facts=relevant_facts,
                source_manifest=source_manifest,
                synthesis_sources=synthesis_sources,
                candidate_kind=section,
            )
            if not evidence:
                raise VaultError(
                    "profile_source_review_evidence_invalid",
                    "A source suggestion could not be grounded in reviewable evidence.",
                )
            try:
                _review_card(section, proposed)
                changed_fields = _changed_fields(
                    section,
                    operation_kind,
                    previous,
                    proposed,
                )
            except VaultError as exc:
                raise VaultError(
                    "profile_source_review_candidate_invalid",
                    "A source suggestion could not be prepared for safe review.",
                ) from exc
            if not changed_fields:
                raise VaultError(
                    "profile_source_review_candidate_invalid",
                    "A source suggestion does not contain a reviewable profile change.",
                )
            candidates.append(
                {
                    "candidate_kind": section,
                    "operation_kind": operation_kind,
                    "proposed": deepcopy(proposed),
                    "previous": deepcopy(previous),
                    "evidence": evidence,
                    "_sort": (section_order, entry_index),
                }
            )
            if operation_kind == "add":
                removals.setdefault(section, set()).add(entry_index)
            else:
                replacements.setdefault(section, {})[entry_index] = deepcopy(previous)

    candidates.sort(key=lambda candidate: candidate.pop("_sort"))
    if len(candidates) > MAX_REVIEW_CANDIDATES:
        raise VaultError(
            "profile_source_review_candidates_limit",
            "This import contains too many review suggestions. Import a smaller source set.",
        )
    if len(_canonical_json(candidates).encode("utf-8")) > MAX_REVIEW_CANDIDATE_BYTES:
        raise VaultError(
            "profile_source_review_candidates_limit",
            "This import contains too much review material. Import a smaller source set.",
        )
    for section in _TARGET_SECTIONS:
        values = result.get(section)
        if not isinstance(values, list):
            continue
        section_removals = removals.get(section, set())
        section_replacements = replacements.get(section, {})
        result[section] = [
            deepcopy(section_replacements.get(index, value))
            for index, value in enumerate(values)
            if index not in section_removals
        ]
    return result, candidates


def _scope_clause(scope: str) -> str:
    if scope == "inbox":
        return "item.retention_generation = ? AND item_state = 'inbox'"
    if scope == "deferred":
        return "item.retention_generation = ? AND item_state = 'deferred'"
    return "(item.retention_generation != ? OR item_state IN ('rejected', 'applied'))"


def _public_candidate(kind: str, proposed: dict[str, Any]) -> dict[str, Any]:
    """Project stored canonical material into the exact bounded editor shape."""

    spec = _SPECS[kind]
    public = _public_entry(spec, 0, proposed)
    public.pop("entry_index", None)
    try:
        return _normalize_full_entry(spec, public)
    except ValueError as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion cannot be edited safely.",
        ) from exc


def _location_label(locator: dict[str, Any]) -> str | None:
    if locator.get("kind") == "json_pointer":
        return "Structured profile field"
    if locator.get("kind") != "line":
        return None
    start = locator.get("start")
    end = locator.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return f"Line {start}" if start == end else f"Lines {start}–{end}"


def _review_card(kind: str, proposed: dict[str, Any]) -> dict[str, Any]:
    profile = {kind: [proposed]}
    if kind == "education":
        cards = _education_cards(profile)
    elif kind == "skills":
        cards = _skill_cards(profile)
    else:
        cards = _named_cards(profile, "projects")
    if not cards:
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion cannot be displayed safely.",
        )
    card = cards[0]
    return {
        "title": card["title"],
        "subtitle": card["subtitle"],
        "date_range": card["date_range"],
        "summary": card["summary"],
        "highlights": card["highlights"],
        "tags": card["tags"],
        "details": [
            {
                "label": str(detail["label"])[:32],
                "value": str(detail["value"])[:240],
            }
            for detail in card["details"][:8]
            if isinstance(detail, dict)
            and isinstance(detail.get("label"), str)
            and isinstance(detail.get("value"), str)
        ],
    }


def _changed_fields(
    kind: str,
    operation_kind: str,
    previous: dict[str, Any] | None,
    proposed: dict[str, Any],
) -> list[str]:
    spec = _SPECS[kind]
    if operation_kind == "add":
        return _added_changed_fields(spec, proposed)
    if previous is None:
        raise VaultError("vault_integrity_error", "A review update lost its base entry.")
    return _changed_fields_between(spec, previous, proposed)


def _review_material(
    connection: sqlite3.Connection,
    data_dir: Path,
    item: sqlite3.Row,
    *,
    verify_source_files: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """Verify the immutable proposal, checksums, and every evidence link."""

    try:
        proposed = json.loads(str(item["proposed_json"]))
        previous = (
            json.loads(str(item["previous_json"]))
            if item["previous_json"] is not None
            else None
        )
        evidence = json.loads(str(item["evidence_json"]))
        kind = str(item["candidate_kind"])
        operation_kind = str(item["operation_kind"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion failed its integrity check.",
        ) from exc
    if (
        kind not in _TARGET_SECTIONS
        or operation_kind not in {"add", "update"}
        or not isinstance(proposed, dict)
        or not isinstance(evidence, list)
        or not 1 <= len(evidence) <= 3
        or (operation_kind == "add" and previous is not None)
        or (operation_kind == "update" and not isinstance(previous, dict))
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion failed its integrity check.",
        )
    material = {
        "candidate_kind": kind,
        "operation_kind": operation_kind,
        "proposed": proposed,
        "previous": previous,
        "evidence": evidence,
    }
    try:
        material_json = _json_text(
            material,
            label="stored review candidate material",
            expected_type=dict,
            max_bytes=64 * 1024,
        )
    except (TypeError, ValueError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion failed its integrity check.",
        ) from exc
    if hashlib.sha256(material_json.encode("utf-8")).hexdigest() != str(
        item["candidate_checksum_sha256"]
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion failed its integrity check.",
        )
    previous_checksum = (
        hashlib.sha256(str(item["previous_json"]).encode("utf-8")).hexdigest()
        if item["previous_json"] is not None
        else None
    )
    if previous_checksum != item["previous_checksum_sha256"]:
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion failed its integrity check.",
        )
    if item["memory_id"] is not None:
        from .memories import verify_memory_review_item
        verify_memory_review_item(connection, item, proposed, evidence)
        return proposed, previous, evidence
    if str(item["generator_contract"]) != "deterministic-review-candidate-v1":
        raise VaultError(
            "vault_integrity_error",
            "A stored review suggestion uses an unsupported generator contract.",
        )

    seen: set[tuple[int, str]] = set()
    for raw_evidence in evidence:
        if not isinstance(raw_evidence, dict) or set(raw_evidence) != {
            "source_ordinal",
            "locator",
            "excerpt",
        }:
            raise VaultError(
                "review_item_evidence_invalid",
                "The review suggestion no longer has complete source evidence.",
            )
        source_ordinal = raw_evidence.get("source_ordinal")
        locator = raw_evidence.get("locator")
        excerpt = raw_evidence.get("excerpt")
        if (
            not isinstance(source_ordinal, int)
            or isinstance(source_ordinal, bool)
            or not 0 <= source_ordinal < 64
            or not isinstance(locator, dict)
            or not isinstance(excerpt, str)
            or not _clean_excerpt(excerpt)
        ):
            raise VaultError(
                "review_item_evidence_invalid",
                "The review suggestion no longer has complete source evidence.",
            )
        if locator.get("kind") == "line" and set(locator) == {"kind", "start", "end"}:
            start, end = locator.get("start"), locator.get("end")
            valid_locator = (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and 1 <= start <= end <= 2_000_000
            )
        elif locator.get("kind") == "json_pointer" and set(locator) == {"kind", "value"}:
            pointer = locator.get("value")
            valid_locator = (
                isinstance(pointer, str)
                and pointer.startswith("/")
                and len(pointer) <= 512
                and all(character.isprintable() for character in pointer)
            )
        else:
            valid_locator = False
        marker = (source_ordinal, _canonical_json(locator))
        if not valid_locator or marker in seen:
            raise VaultError(
                "review_item_evidence_invalid",
                "The review suggestion no longer has complete source evidence.",
            )
        seen.add(marker)
        source = connection.execute(
            """
            SELECT
                link.profile_version_id,
                link.extraction_status,
                link.extraction_id,
                source.id AS source_id,
                source.relative_path,
                source.byte_size,
                source.checksum_sha256,
                source.content_addressed,
                extraction.source_id AS extraction_source_id
            FROM profile_version_sources AS link
            JOIN sources AS source ON source.id = link.source_id
            LEFT JOIN source_extractions AS extraction
              ON extraction.id = link.extraction_id
            WHERE link.import_id = ? AND link.ordinal = ?
            """,
            (str(item["import_id"]), source_ordinal),
        ).fetchone()
        if (
            source is None
            or str(source["profile_version_id"]) != str(item["profile_version_id"])
            or str(source["extraction_status"]) != "parsed"
            or source["extraction_id"] is None
            or str(source["source_id"]) != str(source["extraction_source_id"])
            or int(source["content_addressed"]) != 1
        ):
            raise VaultError(
                "review_item_evidence_invalid",
                "The review suggestion no longer has complete source evidence.",
            )
        if verify_source_files:
            try:
                relative_path = str(source["relative_path"])
                byte_size = int(source["byte_size"])
                checksum = str(source["checksum_sha256"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise VaultError(
                    "review_item_evidence_invalid",
                    "The retained source file for this suggestion is unavailable.",
                ) from exc
            source_path = _managed_source_blob_path(data_dir, relative_path)
            if source_path is None or not _managed_file_matches(
                source_path, byte_size, checksum
            ):
                raise VaultError(
                    "review_item_evidence_invalid",
                    "The retained source file for this suggestion is unavailable.",
                )
    return proposed, previous, evidence


def _current_review_state(
    connection: sqlite3.Connection, review_item_id: str
) -> tuple[str, int]:
    applied = connection.execute(
        """
        SELECT result_state_revision
        FROM profile_review_apply_receipts
        WHERE review_item_id = ?
        """,
        (review_item_id,),
    ).fetchone()
    if applied is not None:
        return "applied", int(applied["result_state_revision"])
    latest = connection.execute(
        """
        SELECT result_state, result_state_revision
        FROM profile_review_item_events
        WHERE review_item_id = ?
        ORDER BY result_state_revision DESC
        LIMIT 1
        """,
        (review_item_id,),
    ).fetchone()
    return (
        (str(latest["result_state"]), int(latest["result_state_revision"]))
        if latest is not None
        else ("inbox", 0)
    )


def _entry_has_candidate_shape(
    kind: str, entry: Any, candidate: dict[str, Any]
) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        return _json_equivalent(_public_candidate(kind, entry), candidate)
    except VaultError:
        return False


def list_profile_review_items(
    data_dir: Path,
    *,
    scope: Any,
    offset: Any,
) -> dict[str, Any]:
    if scope not in REVIEW_SCOPES:
        raise ValueError("scope must be inbox, deferred, or history")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 0 <= offset <= REVIEW_INBOX_OFFSET_LIMIT
    ):
        raise ValueError("offset must be an integer between 0 and 10000")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        retention = connection.execute(
            """
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if retention is None:
            raise VaultError(
                "vault_integrity_error",
                "The local source-retention state is missing.",
            )
        current_generation = int(retention["current_generation"])
        current_row = connection.execute(
            "SELECT id FROM profile_versions ORDER BY version_number DESC LIMIT 1"
        ).fetchone()
        current_profile = (
            _canonical_uuid(current_row["id"], label="current profile version id")
            if current_row is not None
            else None
        )
        state_cte = """
            WITH item_states AS (
                SELECT
                    item.*,
                    CASE
                      WHEN apply_receipt.review_item_id IS NOT NULL THEN 'applied'
                      ELSE COALESCE(
                        (
                            SELECT event.result_state
                            FROM profile_review_item_events AS event
                            WHERE event.review_item_id = item.id
                            ORDER BY event.result_state_revision DESC
                            LIMIT 1
                        ),
                        'inbox'
                      )
                    END AS item_state,
                    CASE
                      WHEN apply_receipt.review_item_id IS NOT NULL
                        THEN apply_receipt.result_state_revision
                      ELSE COALESCE(
                        (
                            SELECT event.result_state_revision
                            FROM profile_review_item_events AS event
                            WHERE event.review_item_id = item.id
                            ORDER BY event.result_state_revision DESC
                            LIMIT 1
                        ),
                        0
                      )
                    END AS item_state_revision
                FROM profile_review_items AS item
                LEFT JOIN profile_review_apply_receipts AS apply_receipt
                  ON apply_receipt.review_item_id = item.id
            )
        """
        clause = _scope_clause(scope)
        total_row = connection.execute(
            state_cte + f" SELECT COUNT(*) AS total FROM item_states AS item WHERE {clause}",
            (current_generation,),
        ).fetchone()
        total_items = int(total_row["total"] if total_row is not None else 0)
        if not 0 <= total_items <= MAX_RPC_SAFE_COUNT:
            raise VaultError("vault_integrity_error", "The local review inbox is too large.")
        rows = connection.execute(
            state_cte
            + f"""
                SELECT
                    item.*,
                    apply_receipt.candidate_json AS applied_candidate_json,
                    apply_receipt.changed_fields_json AS applied_changed_fields_json
                FROM item_states AS item
                LEFT JOIN profile_review_apply_receipts AS apply_receipt
                  ON apply_receipt.review_item_id = item.id
                WHERE {clause}
                ORDER BY
                    item.created_at_ms DESC,
                    item.import_id DESC,
                    item.candidate_ordinal ASC,
                    item.id DESC
                LIMIT ? OFFSET ?
            """,
            (current_generation, REVIEW_INBOX_PAGE_LIMIT, offset),
        ).fetchall()
        raw_counts = _profile_review_state_counts(connection)
        counts = {
            "inbox": raw_counts["review_inbox_items"],
            "deferred": raw_counts["review_deferred_items"],
            "history": raw_counts["review_history_items"],
        }
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                proposed = json.loads(str(row["proposed_json"]))
                previous = (
                    json.loads(str(row["previous_json"]))
                    if row["previous_json"] is not None
                    else None
                )
                evidence_values = json.loads(str(row["evidence_json"]))
            except (json.JSONDecodeError, TypeError) as exc:
                raise VaultError(
                    "vault_integrity_error",
                    "A stored review suggestion is invalid.",
                ) from exc
            kind = str(row["candidate_kind"])
            operation_kind = str(row["operation_kind"])
            candidate = _public_candidate(kind, proposed)
            card_entry = proposed
            changed_fields = _changed_fields(
                kind,
                operation_kind,
                previous,
                proposed,
            )
            if str(row["item_state"]) == "applied":
                try:
                    applied_candidate = json.loads(str(row["applied_candidate_json"]))
                    applied_changed_fields = json.loads(
                        str(row["applied_changed_fields_json"])
                    )
                    candidate = _normalize_full_entry(_SPECS[kind], applied_candidate)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise VaultError(
                        "vault_integrity_error",
                        "A stored applied suggestion cannot be displayed safely.",
                    ) from exc
                if (
                    candidate != applied_candidate
                    or not isinstance(applied_changed_fields, list)
                    or not applied_changed_fields
                    or any(field not in _SPECS[kind].field_order for field in applied_changed_fields)
                    or applied_changed_fields
                    != [
                        field
                        for field in _SPECS[kind].field_order
                        if field in applied_changed_fields
                    ]
                    or len(set(applied_changed_fields)) != len(applied_changed_fields)
                ):
                    raise VaultError(
                        "vault_integrity_error",
                        "A stored applied suggestion cannot be displayed safely.",
                    )
                card_entry = _canonical_entry(_SPECS[kind], candidate)
                changed_fields = applied_changed_fields
            evidence: list[dict[str, Any]] = []
            if row["memory_id"] is not None:
                from .memories import verify_memory_review_item
                _review_material(connection, data_dir, row, verify_source_files=False)
                memory = verify_memory_review_item(connection, row, proposed, evidence_values)
                evidence.append({
                    "display_name": memory["title"] or "Career memory",
                    "source_format": "txt", "source_kind": "evidence",
                    "location_label": "Original memory", "excerpt": _clean_excerpt(memory["narrative"]),
                })
            for raw_evidence in evidence_values:
                if row["memory_id"] is not None:
                    break
                source_ordinal = int(raw_evidence["source_ordinal"])
                source = connection.execute(
                    """
                    SELECT display_name, source_format, source_kind
                    FROM profile_version_sources
                    WHERE import_id = ? AND ordinal = ?
                    """,
                    (str(row["import_id"]), source_ordinal),
                ).fetchone()
                if source is None:
                    raise VaultError(
                        "vault_integrity_error",
                        "A review suggestion lost its source evidence.",
                    )
                locator = raw_evidence["locator"]
                evidence.append(
                    {
                        "display_name": str(source["display_name"])[:240],
                        "source_format": str(source["source_format"]),
                        "source_kind": str(source["source_kind"]),
                        "location_label": _location_label(locator),
                        "excerpt": _clean_excerpt(raw_evidence["excerpt"]),
                    }
                )
            item = {
                "id": str(row["id"]),
                "state": str(row["item_state"]),
                "state_revision": int(row["item_state_revision"]),
                "candidate_kind": kind,
                "operation_kind": operation_kind,
                "candidate": candidate,
                **_review_card(kind, card_entry),
                "changed_fields": changed_fields,
                "evidence": evidence,
                "is_previous_import_set": (
                    int(row["retention_generation"]) != current_generation
                ),
                "created_at_ms": int(row["created_at_ms"]),
            }
            prospective = {
                "scope": scope,
                "current_profile_version_id": current_profile,
                "offset": offset,
                "limit": REVIEW_INBOX_PAGE_LIMIT,
                "total_items": total_items,
                "next_offset": offset + len(items) + 1,
                "counts": counts,
                "items": [*items, item],
            }
            if len(_canonical_json(prospective).encode("utf-8")) > REVIEW_INBOX_RESPONSE_LIMIT_BYTES:
                break
            items.append(item)
        if rows and not items:
            raise VaultError(
                "vault_integrity_error",
                "A review suggestion exceeds the safe response size.",
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    page_end = offset + len(items)
    result = {
        "scope": scope,
        "current_profile_version_id": current_profile,
        "offset": offset,
        "limit": REVIEW_INBOX_PAGE_LIMIT,
        "total_items": total_items,
        "next_offset": (
            page_end
            if page_end < total_items and page_end <= REVIEW_INBOX_OFFSET_LIMIT
            else None
        ),
        "counts": counts,
        "items": items,
    }
    if len(_canonical_json(result).encode("utf-8")) > REVIEW_INBOX_RESPONSE_LIMIT_BYTES:
        raise VaultError(
            "vault_integrity_error",
            "The local review inbox response exceeds the safe size.",
        )
    return result


def _transition_result(row: Any, *, created: bool) -> dict[str, Any]:
    return {
        "request_id": str(row["request_id"]),
        "review_item_id": str(row["review_item_id"]),
        "action": str(row["action"]),
        "previous_state": str(row["expected_state"]),
        "state": str(row["result_state"]),
        "state_revision": int(row["result_state_revision"]),
        "created_at_ms": int(row["created_at_ms"]),
        "created": created,
    }


def transition_profile_review_item(
    data_dir: Path,
    *,
    review_item_id: Any,
    request_id: Any,
    action: Any,
    expected_state: Any,
    expected_state_revision: Any,
) -> dict[str, Any]:
    normalized_item_id = _canonical_uuid(review_item_id, label="review_item_id")
    normalized_request_id = _canonical_uuid(request_id, label="request_id")
    if action not in REVIEW_ACTIONS:
        raise ValueError("action must be defer, reject, or reopen")
    if expected_state not in REVIEW_TRANSITION_STATES:
        raise ValueError("expected_state must be inbox, deferred, or rejected")
    if (
        not isinstance(expected_state_revision, int)
        or isinstance(expected_state_revision, bool)
        or not 0 <= expected_state_revision < MAX_RPC_SAFE_COUNT
    ):
        raise ValueError("expected_state_revision must be a non-negative safe integer")
    fingerprint_material = {
        "action": action,
        "expected_state": expected_state,
        "expected_state_revision": expected_state_revision,
        "request_id": normalized_request_id,
        "review_item_id": normalized_item_id,
    }
    request_fingerprint = hashlib.sha256(
        _json_text(
            fingerprint_material,
            label="review transition request",
            expected_type=dict,
            max_bytes=8 * 1024,
        ).encode("utf-8")
    ).hexdigest()
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT * FROM profile_review_item_events WHERE request_id = ?",
            (normalized_request_id,),
        ).fetchone()
        if prior is not None:
            if str(prior["request_fingerprint"]) != request_fingerprint:
                raise VaultError(
                    "review_item_request_conflict",
                    "This request identifier was already used for a different review change.",
                )
            result = _transition_result(prior, created=False)
            connection.commit()
            return result
        reused_apply = connection.execute(
            "SELECT 1 FROM profile_review_apply_receipts WHERE request_id = ?",
            (normalized_request_id,),
        ).fetchone()
        if reused_apply is not None:
            raise VaultError(
                "review_item_request_conflict",
                "This request identifier was already used for a different review change.",
            )

        retention = connection.execute(
            """
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        item = connection.execute(
            "SELECT retention_generation FROM profile_review_items WHERE id = ?",
            (normalized_item_id,),
        ).fetchone()
        if item is None:
            raise VaultError(
                "review_item_not_found",
                "The review suggestion no longer exists.",
            )
        if retention is None:
            raise VaultError(
                "vault_integrity_error",
                "The local source-retention state is missing.",
            )
        if int(item["retention_generation"]) != int(retention["current_generation"]):
            raise VaultError(
                "review_item_stale_generation",
                "Suggestions from a previous import set cannot be changed.",
            )
        current_state, current_revision = _current_review_state(
            connection, normalized_item_id
        )
        if current_state != expected_state or current_revision != expected_state_revision:
            raise VaultError(
                "review_item_state_conflict",
                "The review suggestion changed. Refresh it and try again.",
            )
        transitions = {
            ("inbox", "defer"): "deferred",
            ("inbox", "reject"): "rejected",
            ("deferred", "reject"): "rejected",
            ("deferred", "reopen"): "inbox",
            ("rejected", "reopen"): "inbox",
        }
        result_state = transitions.get((current_state, action))
        if result_state is None:
            raise VaultError(
                "review_item_invalid_transition",
                "That review action is not available from the current state.",
            )
        created_at_ms = _utc_now_ms()
        connection.execute(
            """
            INSERT INTO profile_review_item_events(
                request_id, request_fingerprint, review_item_id, action,
                expected_state, expected_state_revision, result_state,
                result_state_revision, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_request_id,
                request_fingerprint,
                normalized_item_id,
                action,
                current_state,
                current_revision,
                result_state,
                current_revision + 1,
                created_at_ms,
            ),
        )
        row = connection.execute(
            "SELECT * FROM profile_review_item_events WHERE request_id = ?",
            (normalized_request_id,),
        ).fetchone()
        if row is None:
            raise VaultError("vault_integrity_error", "The review action was not recorded.")
        result = _transition_result(row, created=True)
        if len(_canonical_json(result).encode("utf-8")) > REVIEW_TRANSITION_RESPONSE_LIMIT_BYTES:
            raise VaultError("vault_integrity_error", "The review action receipt is invalid.")
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _apply_candidate_to_profile(
    profile: dict[str, Any],
    *,
    kind: str,
    operation_kind: str,
    proposed: dict[str, Any],
    previous: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], int, list[str]]:
    spec = _SPECS[kind]
    candidate_entry = deepcopy(proposed) if operation_kind == "add" else {}
    for field in spec.field_order:
        canonical_field = spec.canonical_fields[field]
        value = candidate[field]
        if value is None or (field in spec.list_limits and not value):
            candidate_entry.pop(canonical_field, None)
        else:
            candidate_entry[canonical_field] = deepcopy(value)
    result = deepcopy(profile)
    entries = _mutable_entries(profile, spec)
    if operation_kind == "add":
        if len(entries) >= PROFILE_SECTION_ENTRY_LIMIT:
            raise VaultError(
                "review_item_apply_conflict",
                f"The {spec.label} section cannot contain another entry.",
            )
        if any(_entry_has_candidate_shape(kind, entry, candidate) for entry in entries):
            raise VaultError(
                "review_item_apply_noop",
                "That suggestion is already present in the current profile.",
            )
        entry_index = len(entries)
        try:
            _validate_entry_identity(
                spec, candidate_entry, path=f"resulting {spec.entry_name}"
            )
        except ValueError as exc:
            raise ValueError(f"candidate would create an invalid {spec.entry_name}") from exc
        entries.append(candidate_entry)
        changed_fields = _added_changed_fields(spec, candidate_entry)
    else:
        if previous is None:
            raise VaultError(
                "vault_integrity_error",
                "A stored review update lost its original profile entry.",
            )
        matches = [
            index
            for index, entry in enumerate(entries)
            if isinstance(entry, dict) and _json_equivalent(entry, previous)
        ]
        if len(matches) != 1:
            raise VaultError(
                "review_item_apply_conflict",
                "The original profile entry changed or is ambiguous. Review the current profile and try again.",
            )
        entry_index = matches[0]
        target = entries[entry_index]
        edited = deepcopy(target)
        public_keys = set(spec.canonical_fields.values())
        for key, value in proposed.items():
            if key not in public_keys and key not in edited:
                edited[key] = deepcopy(value)
        for field in spec.field_order:
            canonical_field = spec.canonical_fields[field]
            value = candidate[field]
            if value is None or (field in spec.list_limits and not value):
                edited.pop(canonical_field, None)
            else:
                edited[canonical_field] = deepcopy(value)
        try:
            _validate_entry_identity(spec, edited, path=f"resulting {spec.entry_name}")
        except ValueError as exc:
            raise ValueError(f"candidate would create an invalid {spec.entry_name}") from exc
        changed_fields = _changed_fields_between(spec, target, edited)
        if not changed_fields:
            raise VaultError(
                "review_item_apply_noop",
                "That suggestion does not change the current profile.",
            )
        if any(
            index != entry_index and _entry_has_candidate_shape(kind, entry, candidate)
            for index, entry in enumerate(entries)
        ):
            raise VaultError(
                "review_item_apply_noop",
                "That suggestion duplicates another current profile entry.",
            )
        entries[entry_index] = edited
    if not changed_fields:
        raise VaultError(
            "review_item_apply_noop",
            "That suggestion does not change the current profile.",
        )
    result[kind] = entries
    if kind == "education":
        _mark_education_curated(result)
    _require_named_profile(result)
    validate_canonical_profile(result)
    return result, entry_index, changed_fields


def _review_apply_result(
    connection: sqlite3.Connection,
    data_dir: Path,
    receipt: sqlite3.Row,
    *,
    created: bool,
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(receipt["request_id"], label="stored request id")
        review_item_id = _canonical_uuid(
            receipt["review_item_id"], label="stored review item id"
        )
        parent_id = _canonical_uuid(
            receipt["parent_profile_version_id"],
            label="stored parent profile version id",
        )
        output_id = _canonical_uuid(
            receipt["output_profile_version_id"],
            label="stored output profile version id",
        )
        candidate_kind = str(receipt["candidate_kind"])
        operation_kind = str(receipt["operation_kind"])
        candidate = json.loads(str(receipt["candidate_json"]))
        changed_fields = json.loads(str(receipt["changed_fields_json"]))
        entry_index = int(receipt["entry_index"])
        state_revision = int(receipt["result_state_revision"])
        expected_revision = int(receipt["expected_state_revision"])
        created_at_ms = int(receipt["created_at_ms"])
        request_fingerprint = str(receipt["request_fingerprint"])
        retention_generation = int(receipt["retention_generation"])
        expected_state = str(receipt["expected_state"])
        spec = _SPECS[candidate_kind]
        normalized_candidate = _normalize_full_entry(spec, candidate)
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
            "A stored review apply receipt failed its integrity check.",
        ) from exc
    if (
        len(request_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in request_fingerprint)
        or expected_state != "inbox"
        or state_revision != expected_revision + 1
        or not 0 <= expected_revision < MAX_RPC_SAFE_COUNT
        or not 0 <= entry_index < PROFILE_SECTION_ENTRY_LIMIT
        or not 1 <= created_at_ms <= MAX_RPC_SAFE_COUNT
        or not isinstance(changed_fields, list)
        or not 1 <= len(changed_fields) <= len(spec.field_order)
        or any(field not in spec.field_order for field in changed_fields)
        or changed_fields != [field for field in spec.field_order if field in changed_fields]
        or len(set(changed_fields)) != len(changed_fields)
        or candidate != normalized_candidate
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored review apply receipt failed its integrity check.",
        )
    item = connection.execute(
        "SELECT * FROM profile_review_items WHERE id = ?", (review_item_id,)
    ).fetchone()
    if (
        item is None
        or str(item["candidate_kind"]) != candidate_kind
        or str(item["operation_kind"]) != operation_kind
        or int(item["retention_generation"]) != retention_generation
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored review apply receipt lost its suggestion lineage.",
        )
    proposed, previous, _evidence = _review_material(
        connection, data_dir, item, verify_source_files=False
    )
    parent = _stored_version(_version_row(connection, parent_id))
    output = _stored_version(_version_row(connection, output_id))
    try:
        expected_profile, expected_index, expected_changes = _apply_candidate_to_profile(
            parent["profile"],
            kind=candidate_kind,
            operation_kind=operation_kind,
            proposed=proposed,
            previous=previous,
            candidate=normalized_candidate,
        )
        expected_json = _json_text(
            expected_profile,
            label="expected review apply profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
    except VaultError as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored review apply receipt has invalid mutation data.",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored review apply receipt has invalid mutation data.",
        ) from exc
    competing = connection.execute(
        """
        SELECT (
            EXISTS(SELECT 1 FROM profile_basic_update_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_work_update_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_section_update_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_change_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_restore_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_manual_start_receipts WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_source_imports WHERE output_profile_version_id = ?)
          + EXISTS(SELECT 1 FROM profile_version_sources WHERE profile_version_id = ?)
        )
        """,
        (output_id,) * 8,
    ).fetchone()[0]
    if (
        output["parent_version_id"] != parent_id
        or output["version_number"] != parent["version_number"] + 1
        or output["source"] != "local_edit"
        or output["created_at_ms"] != created_at_ms
        or output["canonical_json"] != expected_json
        or expected_index != entry_index
        or expected_changes != changed_fields
        or competing
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored review apply receipt has invalid profile lineage.",
        )
    return {
        "request_id": request_id,
        "review_item_id": review_item_id,
        "previous_state": "inbox",
        "state": "applied",
        "state_revision": state_revision,
        "profile_version_id": output_id,
        "parent_profile_version_id": parent_id,
        "version_number": output["version_number"],
        "candidate_kind": candidate_kind,
        "operation_kind": operation_kind,
        "entry_index": entry_index,
        "changed_fields": changed_fields,
        "created_at_ms": created_at_ms,
        "created": created,
    }


def apply_profile_review_item(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "review_item_id",
        "request_id",
        "expected_state",
        "expected_state_revision",
        "expected_parent_profile_version_id",
        "candidate",
    }
    if set(params) != expected_keys:
        raise ValueError(
            "params must contain exactly review_item_id, request_id, expected_state, "
            "expected_state_revision, expected_parent_profile_version_id, and candidate"
        )
    review_item_id = _canonical_uuid(params["review_item_id"], label="review_item_id")
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    parent_id = _canonical_uuid(
        params["expected_parent_profile_version_id"],
        label="expected_parent_profile_version_id",
    )
    expected_state = params["expected_state"]
    if expected_state != "inbox":
        raise ValueError("expected_state must be inbox")
    expected_revision = params["expected_state_revision"]
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or not 0 <= expected_revision < MAX_RPC_SAFE_COUNT
    ):
        raise ValueError("expected_state_revision must be a non-negative safe integer")

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        item_kind_row = connection.execute(
            "SELECT candidate_kind FROM profile_review_items WHERE id = ?",
            (review_item_id,),
        ).fetchone()
        if item_kind_row is None:
            raise VaultError(
                "review_item_not_found", "The review suggestion no longer exists."
            )
        candidate_kind = str(item_kind_row["candidate_kind"])
        if candidate_kind not in _SPECS:
            raise VaultError(
                "vault_integrity_error",
                "The stored review suggestion has an invalid kind.",
            )
        candidate = _normalize_full_entry(_SPECS[candidate_kind], params["candidate"])
        fingerprint_material = {
            "review_item_id": review_item_id,
            "request_id": request_id,
            "expected_state": expected_state,
            "expected_state_revision": expected_revision,
            "expected_parent_profile_version_id": parent_id,
            "candidate": candidate,
        }
        request_fingerprint = hashlib.sha256(
            _json_text(
                fingerprint_material,
                label="review apply request",
                expected_type=dict,
                max_bytes=64 * 1024,
            ).encode("utf-8")
        ).hexdigest()

        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM profile_review_apply_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise VaultError(
                    "review_item_apply_request_conflict",
                    "This request identifier was already used with different apply inputs.",
                )
            result = _review_apply_result(
                connection, data_dir, existing, created=False
            )
            connection.commit()
            return result
        reused_transition = connection.execute(
            "SELECT 1 FROM profile_review_item_events WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if reused_transition is not None:
            raise VaultError(
                "review_item_apply_request_conflict",
                "This request identifier was already used for a different review change.",
            )
        item = connection.execute(
            "SELECT * FROM profile_review_items WHERE id = ?", (review_item_id,)
        ).fetchone()
        if item is None:
            raise VaultError(
                "review_item_not_found", "The review suggestion no longer exists."
            )
        if str(item["candidate_kind"]) != candidate_kind:
            raise VaultError(
                "vault_integrity_error",
                "The stored review suggestion changed unexpectedly.",
            )
        retention = connection.execute(
            """
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if retention is None:
            raise VaultError(
                "vault_integrity_error", "The local source-retention state is missing."
            )
        if int(item["retention_generation"]) != int(retention["current_generation"]):
            raise VaultError(
                "review_item_stale_generation",
                "Suggestions from a previous import set cannot be applied.",
            )
        current_state, current_revision = _current_review_state(
            connection, review_item_id
        )
        if current_state != expected_state or current_revision != expected_revision:
            raise VaultError(
                "review_item_state_conflict",
                "The review suggestion changed. Refresh it and try again.",
            )
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
                "Build or import a career profile before applying suggestions.",
            )
        current = _stored_version(current_row)
        if current["id"] != parent_id:
            raise VaultError(
                "review_item_profile_conflict",
                "The career profile changed. Refresh the review inbox and try again.",
            )
        if current["version_number"] >= MAX_RPC_SAFE_COUNT:
            raise VaultError(
                "profile_version_limit", "The local profile history cannot be advanced."
            )
        proposed, previous, _evidence = _review_material(
            connection, data_dir, item, verify_source_files=True
        )
        edited_profile, entry_index, changed_fields = _apply_candidate_to_profile(
            current["profile"],
            kind=candidate_kind,
            operation_kind=str(item["operation_kind"]),
            proposed=proposed,
            previous=previous,
            candidate=candidate,
        )
        canonical_json = _json_text(
            edited_profile,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
        checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if checksum == current["checksum_sha256"]:
            raise VaultError(
                "review_item_apply_noop",
                "That suggestion does not change the current profile.",
            )
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
            INSERT INTO profile_review_apply_receipts(
                request_id, request_fingerprint, review_item_id,
                expected_state, expected_state_revision, result_state_revision,
                retention_generation, candidate_kind, operation_kind,
                candidate_json, output_profile_version_id,
                parent_profile_version_id, entry_index, changed_fields_json,
                created_at_ms
            ) VALUES (?, ?, ?, 'inbox', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_fingerprint,
                review_item_id,
                expected_revision,
                expected_revision + 1,
                int(item["retention_generation"]),
                candidate_kind,
                str(item["operation_kind"]),
                _json_text(
                    candidate,
                    label="review apply candidate",
                    expected_type=dict,
                    max_bytes=32 * 1024,
                ),
                output_id,
                parent_id,
                entry_index,
                _canonical_json(changed_fields),
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_review_apply_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if receipt is None:
            raise VaultError(
                "vault_integrity_error", "The review apply receipt was not recorded."
            )
        result = _review_apply_result(connection, data_dir, receipt, created=True)
        if len(_canonical_json(result).encode("utf-8")) > REVIEW_APPLY_RESPONSE_LIMIT_BYTES:
            raise VaultError(
                "vault_integrity_error", "The review apply receipt is invalid."
            )
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            "The local vault rejected invalid review apply lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "REVIEW_INBOX_OFFSET_LIMIT",
    "REVIEW_INBOX_PAGE_LIMIT",
    "REVIEW_INBOX_RESPONSE_LIMIT_BYTES",
    "apply_profile_review_item",
    "list_profile_review_items",
    "partition_review_candidates",
    "transition_profile_review_item",
]
