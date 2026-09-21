# SPDX-License-Identifier: MPL-2.0
"""Bounded source checks and atomic, queued human decisions."""

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

from .profile_versions import (
    _apply_basic_patch, _normalize_patch, _stored_version,
)
from .review_inbox import _clean_excerpt, _location_label
from .storage import (
    MAX_DRAFT_PROFILE_BYTES, MAX_RPC_SAFE_COUNT, SOURCE_FORMATS, SOURCE_KINDS,
    VaultError, _apply_migrations, _canonical_uuid, _connect, _json_text,
    _managed_file_matches, _managed_source_blob_path, _utc_now_ms,
)

FIELDS = {"name": "name", "headline": "label", "summary": "summary",
          "email": "email", "phone": "phone", "url": "url"}
LABELS = {"name": "Name", "headline": "Headline", "summary": "Summary",
          "email": "Email", "phone": "Phone", "url": "Website"}
MAX_CHECKS = 240
MAX_CHECK_BYTES = 2 * 1024 * 1024
SCOPES = {"inbox", "deferred", "history"}
ACTIONS = {"keep_profile", "use_source", "acknowledge_duplicate", "defer", "reopen"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _field_value(profile: dict[str, Any], field: str) -> Any:
    basics = profile.get("basics")
    return basics.get(FIELDS[field]) if isinstance(basics, dict) else None


def normalize_source_checks(checks: Any, *, source_count: int) -> list[dict[str, Any]]:
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ValueError("source checks must be a bounded array")
    result = []
    for raw in checks:
        if not isinstance(raw, dict) or set(raw) != {
            "kind", "field", "previous_value", "proposed_value", "evidence"
        }:
            raise ValueError("source check has invalid fields")
        kind, field = raw["kind"], raw["field"]
        if kind == "basic_conflict":
            if not isinstance(field, str) or field not in FIELDS:
                raise ValueError("source check field is unsupported")
            for key in ("previous_value", "proposed_value"):
                value = raw[key]
                if not isinstance(value, str) or _normalize_patch({field: value})[field] != value:
                    raise ValueError("source check value is invalid")
            if raw["previous_value"] == raw["proposed_value"]:
                raise ValueError("source check does not contain a conflict")
            expected_evidence = 1
        elif kind == "duplicate_document":
            if any(raw[key] is not None for key in ("field", "previous_value", "proposed_value")):
                raise ValueError("duplicate check contains profile fields")
            expected_evidence = 2
        else:
            raise ValueError("source check kind is unsupported")
        evidence = raw["evidence"]
        if not isinstance(evidence, list) or len(evidence) != expected_evidence:
            raise ValueError("source check evidence is invalid")
        seen = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"source_ordinal", "locator", "excerpt"}:
                raise ValueError("source check evidence fields are invalid")
            ordinal = item["source_ordinal"]
            if not _integer(ordinal, 0, min(source_count, 64) - 1) or ordinal in seen:
                raise ValueError("source check source ordinal is invalid")
            seen.add(ordinal)
            locator = item["locator"]
            excerpt = item["excerpt"]
            if not isinstance(excerpt, str) or len(excerpt) > 360 or _clean_excerpt(excerpt) != excerpt:
                raise ValueError("source check excerpt is invalid")
            if kind == "duplicate_document":
                if locator is not None or excerpt:
                    raise ValueError("duplicate check must not contain extracted text")
            elif not isinstance(locator, dict):
                raise ValueError("source check locator is invalid")
            elif locator.get("kind") == "line":
                if set(locator) != {"kind", "start", "end"} or not (
                    _integer(locator["start"], 1, 250_001)
                    and _integer(locator["end"], locator["start"], 250_001)
                ):
                    raise ValueError("source check line locator is invalid")
            elif locator.get("kind") == "json_pointer":
                if (set(locator) != {"kind", "value"} or not isinstance(locator["value"], str)
                    or not locator["value"].startswith("/") or len(locator["value"]) > 1000):
                    raise ValueError("source check structured locator is invalid")
            else:
                raise ValueError("source check locator kind is invalid")
        if len(_json(raw).encode()) > 32 * 1024:
            raise ValueError("source check is too large")
        result.append(deepcopy(raw))
    if len(_json(result).encode()) > MAX_CHECK_BYTES:
        raise ValueError("source checks are too large")
    return result


def build_source_checks(*, profile: dict[str, Any], basic_candidates: list[dict[str, Any]],
                        sources: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare incoming public facts to the final baseline and pair exact bytes."""
    checks: list[dict[str, Any]] = []
    seen = set()
    for candidate in basic_candidates:
        field = next((field for field, key in FIELDS.items() if key == candidate.get("key")), None)
        if field is None:
            continue
        previous, proposed = _field_value(profile, field), candidate.get("value")
        try:
            if not isinstance(previous, str) or not isinstance(proposed, str):
                continue
            normalized_previous = _normalize_patch({field: previous})[field]
            normalized_proposed = _normalize_patch({field: proposed})[field]
            if normalized_previous != previous or normalized_previous == normalized_proposed:
                continue
        except ValueError:
            continue
        source = next((source for source in sources
                       if source.get("sha256") == candidate.get("sha256")
                       and source.get("display_name") == candidate.get("display_name")), None)
        if source is None:
            continue
        ordinal = source.get("origin_ordinal")
        identity = (field, normalized_proposed)
        if identity in seen:
            continue
        check = {"kind": "basic_conflict", "field": field,
                 "previous_value": previous, "proposed_value": normalized_proposed,
                 "evidence": [{"source_ordinal": ordinal,
                               "locator": candidate.get("locator"),
                               "excerpt": _clean_excerpt(normalized_proposed)}]}
        try:
            normalize_source_checks([check], source_count=len(items))
        except ValueError:
            continue
        seen.add(identity)
        checks.append(check)
    primaries: dict[str, int] = {}
    for item in items:
        checksum = item["checksum_sha256"]
        ordinal = int(item["ordinal"])
        if checksum in primaries:
            checks.append({"kind": "duplicate_document", "field": None,
                           "previous_value": None, "proposed_value": None,
                           "evidence": [{"source_ordinal": index, "locator": None, "excerpt": ""}
                                        for index in (primaries[checksum], ordinal)]})
        else:
            primaries[checksum] = ordinal
    try:
        return normalize_source_checks(checks, source_count=len(items))
    except ValueError as exc:
        raise VaultError("source_review_limit", "This import contains too many source checks. Import fewer files.") from exc


def persist_source_checks(connection: sqlite3.Connection, *, scan: sqlite3.Row,
                          profile_version_id: str, retention_generation: int,
                          items: list[dict[str, Any]], now_ms: int) -> int:
    checks = normalize_source_checks(json.loads(str(scan["source_review_json"])), source_count=len(items))
    profile_row = connection.execute("SELECT * FROM profile_versions WHERE id = ?", (profile_version_id,)).fetchone()
    profile = _stored_version(profile_row)["profile"]
    for ordinal, check in enumerate(checks):
        _validate_scan_check(check, items, profile)
        material_json = _json(check)
        checksum = hashlib.sha256(material_json.encode()).hexdigest()
        connection.execute("""INSERT INTO source_review_items
          (id, import_id, ordinal, profile_version_id, retention_generation, kind,
           material_json, checksum_sha256, created_at_ms) VALUES (?,?,?,?,?,?,?,?,?)""",
          (str(uuid.uuid5(uuid.UUID(scan["id"]), f"source-review-v1:{ordinal}:{checksum}")),
           scan["id"], ordinal, profile_version_id, retention_generation, check["kind"],
           material_json, checksum, now_ms))
    return len(checks)


def _validate_scan_check(check: dict[str, Any], items: list[dict[str, Any]], profile: dict[str, Any]) -> None:
    selected = [items[e["source_ordinal"]] for e in check["evidence"]]
    if check["kind"] == "duplicate_document":
        if (selected[0]["checksum_sha256"] != selected[1]["checksum_sha256"]
            or selected[0]["byte_size"] != selected[1]["byte_size"]):
            raise VaultError("vault_integrity_error", "The duplicate source evidence does not match.")
    elif (selected[0]["extraction_status"] != "parsed"
          or _field_value(profile, check["field"]) != check["previous_value"]):
        raise VaultError("vault_integrity_error", "The source conflict no longer matches its imported profile.")
    if check["kind"] == "basic_conflict":
        _verify_basic_fact(check, selected[0])


def _verify_basic_fact(check: dict[str, Any], source: Any) -> None:
    """Derive the fact again from its exact, checksummed extraction."""
    from .source_ingest.synthesis import SynthesisLimits, synthesize_canonical_profile
    from .profile_sources import MAX_EXTRACTED_CHARS, MAX_EXTRACTED_TOTAL_CHARS, _parser_contract

    text = source["extracted_text"]
    if (not isinstance(text, str) or len(text) > MAX_EXTRACTED_CHARS
        or hashlib.sha256(text.encode()).hexdigest() != source["extracted_text_sha256"]
        or source["parser_contract"] != _parser_contract(source["source_format"])):
        raise VaultError("vault_integrity_error", "A source check extraction failed its integrity check.")
    _profile, report = synthesize_canonical_profile(
        existing_profile=None,
        sources=[{"display_name": source["display_name"], "sha256": source["checksum_sha256"],
                  "media_type": source["media_type"], "text": text}],
        limits=SynthesisLimits(max_chars_per_source=MAX_EXTRACTED_CHARS,
                               max_total_chars=MAX_EXTRACTED_TOTAL_CHARS),
    )
    for candidate in report.get("basic_candidates", []):
        if candidate["key"] != FIELDS[check["field"]] or candidate["locator"] != check["evidence"][0]["locator"]:
            continue
        try:
            value = _normalize_patch({check["field"]: candidate["value"]})[check["field"]]
        except ValueError:
            continue
        if value == check["proposed_value"] and _clean_excerpt(value) == check["evidence"][0]["excerpt"]:
            return
    if _matches_legacy_header_name_proof(check, text):
        return
    raise VaultError("vault_integrity_error", "A source check is not supported by its retained extraction.")


def _matches_legacy_header_name_proof(check: dict[str, Any], text: str) -> bool:
    """Verify an existing 0.5.0 proof, never infer a new profile fact/check.

    Freeze the former first-line rule here so later extraction improvements do
    not make saved name-conflict decisions unusable. The caller has already
    verified the retained text checksum and parser contract. Every location,
    proposed value and excerpt must still match what that old rule produced.
    """
    if check["field"] != "name":
        return False

    def clean(value: str) -> str:
        value = re.sub(r"^#{1,6}\s+", "", value.strip())
        value = re.sub(r"^[-*•]\s+", "", value).strip("`*_ ")
        return re.sub(r"\s+", " ", value).strip()[:120].rstrip()

    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", value).casefold()).strip()

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header = [(number, line) for number, line in enumerate(lines[:10], start=1) if clean(line)]
    email = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
    if not header or not any(email.search(line) or key(line).startswith(("phone", "email"))
                             for _number, line in header):
        return False
    number, first = header[0]
    name = clean(first)
    old_non_names = {
        "resume", "curriculum vitae", "summary", "professional summary", "profile", "contact",
        "experience", "work experience", "professional experience", "employment", "education",
        "academic background", "skills", "technical skills", "core skills",
    }
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.-]*", name)
    if (key(name) in old_non_names or any(character.isdigit() for character in name)
        or any(marker in name for marker in (":", "@", "/"))
        or not 2 <= len(words) <= 5 or not all(word[0].isupper() for word in words)):
        return False
    evidence = check["evidence"][0]
    if evidence["locator"] != {"kind": "line", "start": number, "end": number}:
        return False
    try:
        value = _normalize_patch({"name": name})["name"]
    except ValueError:
        return False
    return value == check["proposed_value"] and _clean_excerpt(value) == evidence["excerpt"]


STATE_CTE = """WITH states AS (SELECT i.*,
  COALESCE((SELECT state FROM source_review_events e WHERE e.item_id = i.id ORDER BY revision DESC LIMIT 1), 'inbox') AS state,
  COALESCE((SELECT revision FROM source_review_events e WHERE e.item_id = i.id ORDER BY revision DESC LIMIT 1), 0) AS state_revision,
  (SELECT action FROM source_review_events e WHERE e.item_id = i.id AND state = 'resolved' ORDER BY revision DESC LIMIT 1) AS resolution
  FROM source_review_items i) """


def source_review_counts(connection: sqlite3.Connection) -> dict[str, int]:
    generation = connection.execute("SELECT current_generation FROM profile_source_retention_state WHERE singleton_id=1").fetchone()[0]
    row = connection.execute(STATE_CTE + """SELECT
      COALESCE(SUM(retention_generation = ? AND state = 'inbox'),0),
      COALESCE(SUM(retention_generation = ? AND state = 'deferred'),0),
      COALESCE(SUM(retention_generation != ? OR state = 'resolved'),0) FROM states""",
      (generation, generation, generation)).fetchone()
    values = dict(zip(("inbox", "deferred", "history"), (int(value) for value in row), strict=True))
    if any(value > MAX_RPC_SAFE_COUNT for value in values.values()):
        raise VaultError("source_review_limit", "The local source review history is too large.")
    return values


def _material(connection: sqlite3.Connection, data_dir: Path, item: sqlite3.Row,
              *, verify_files: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        encoded = str(item["material_json"])
        if hashlib.sha256(encoded.encode()).hexdigest() != item["checksum_sha256"]:
            raise ValueError
        material = normalize_source_checks([json.loads(encoded)], source_count=64)[0]
        if material["kind"] != item["kind"]:
            raise ValueError
        imported = connection.execute("SELECT * FROM profile_source_imports WHERE id=?", (item["import_id"],)).fetchone()
        if (imported is None or imported["output_profile_version_id"] != item["profile_version_id"]
            or imported["retention_generation"] != item["retention_generation"]):
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise VaultError("vault_integrity_error", "A source check failed its integrity check.") from exc
    evidence = []
    source_rows = []
    for entry in material["evidence"]:
        source = connection.execute("""SELECT l.*, s.relative_path, s.byte_size,
          s.checksum_sha256, s.content_addressed, s.media_type,
          x.source_id AS extraction_source_id, x.extracted_text, x.extracted_text_sha256,
          x.parser_contract
          FROM profile_version_sources l JOIN sources s ON s.id=l.source_id
          LEFT JOIN source_extractions x ON x.id=l.extraction_id
          WHERE l.import_id=? AND l.ordinal=?""", (item["import_id"], entry["source_ordinal"])).fetchone()
        if (source is None or source["profile_version_id"] != item["profile_version_id"]
            or source["content_addressed"] != 1 or source["source_format"] not in SOURCE_FORMATS
            or source["source_kind"] not in SOURCE_KINDS
            or (item["kind"] == "basic_conflict" and (source["extraction_status"] != "parsed"
                or source["extraction_source_id"] != source["source_id"]))):
            raise VaultError("vault_integrity_error", "A source check lost its retained evidence.")
        if verify_files:
            path = _managed_source_blob_path(data_dir, str(source["relative_path"]))
            if path is None or not _managed_file_matches(path, int(source["byte_size"]), str(source["checksum_sha256"])):
                raise VaultError("source_review_evidence_missing", "A retained file for this source check is unavailable or changed.")
        source_rows.append(source)
        evidence.append({"display_name": str(source["display_name"])[:240],
                         "source_format": source["source_format"], "source_kind": source["source_kind"],
                         "location_label": _location_label(entry["locator"]) if entry["locator"] else None,
                         "excerpt": entry["excerpt"]})
    if item["kind"] == "duplicate_document" and (
        source_rows[0]["checksum_sha256"] != source_rows[1]["checksum_sha256"]
        or source_rows[0]["byte_size"] != source_rows[1]["byte_size"]
    ):
        raise VaultError("vault_integrity_error", "The duplicate file contents do not match.")
    if item["kind"] == "basic_conflict":
        _verify_basic_fact(material, source_rows[0])
        profile_row = connection.execute("SELECT * FROM profile_versions WHERE id=?", (item["profile_version_id"],)).fetchone()
        if _field_value(_stored_version(profile_row)["profile"], material["field"]) != material["previous_value"]:
            raise VaultError("vault_integrity_error", "A source check no longer matches its imported baseline.")
    return material, evidence


def list_source_review_items(data_dir: Path, *, scope: Any, limit: Any, offset: Any) -> dict[str, Any]:
    if not isinstance(scope, str) or scope not in SCOPES or not _integer(limit, 1, 10) or not _integer(offset, 0, 10_000):
        raise ValueError("source review scope, limit, or offset is invalid")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        generation = connection.execute("SELECT current_generation FROM profile_source_retention_state WHERE singleton_id=1").fetchone()[0]
        current = connection.execute("SELECT id FROM profile_versions ORDER BY version_number DESC LIMIT 1").fetchone()
        counts = source_review_counts(connection)
        clause = "(retention_generation != ? OR state = 'resolved')" if scope == "history" else f"retention_generation = ? AND state = '{scope}'"
        rows = connection.execute(STATE_CTE + f"SELECT * FROM states WHERE {clause} ORDER BY created_at_ms DESC, import_id DESC, ordinal ASC LIMIT ? OFFSET ?",
                                  (generation, limit, offset)).fetchall()
        items = []
        for row in rows:
            material, evidence = _material(connection, data_dir, row, verify_files=False)
            field = material["field"]
            items.append({"id": row["id"], "kind": row["kind"], "state": row["state"],
                          "state_revision": row["state_revision"],
                          "title": f"{LABELS[field]} differs in an imported file" if field else "Identical file contents",
                          "field": field, "previous_value": material["previous_value"],
                          "proposed_value": material["proposed_value"], "resolution": row["resolution"],
                          "evidence": evidence, "is_previous_import_set": row["retention_generation"] != generation,
                          "created_at_ms": row["created_at_ms"]})
        result = {"current_profile_version_id": current[0] if current else None,
                  "scope": scope, "offset": offset, "limit": limit, "total_items": counts[scope],
                  "next_offset": offset + len(items) if offset + len(items) < counts[scope] and offset + len(items) <= 10_000 else None,
                  "counts": counts, "items": items}
        while len(_json(result).encode()) > 170 * 1024 and len(items) > 1:
            items.pop()
            result["next_offset"] = offset + len(items) if offset + len(items) <= 10_000 else None
        if len(_json(result).encode()) > 170 * 1024:
            raise VaultError("source_review_limit", "The source review page is too large.")
        connection.commit()
        return result
    finally:
        connection.close()


def _normalize_decisions(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict) or set(params) != {"request_id", "expected_parent_profile_version_id", "decisions"}:
        raise ValueError("source review request fields are invalid")
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    parent = _canonical_uuid(params["expected_parent_profile_version_id"], label="expected_parent_profile_version_id")
    raw_decisions = params["decisions"]
    if not isinstance(raw_decisions, list) or not 1 <= len(raw_decisions) <= 50:
        raise ValueError("source review needs 1 to 50 decisions")
    decisions = []
    seen = set()
    for raw in raw_decisions:
        if not isinstance(raw, dict) or set(raw) != {"item_id", "expected_state", "expected_revision", "action"}:
            raise ValueError("source review decision fields are invalid")
        item_id = _canonical_uuid(raw["item_id"], label="item_id")
        if (item_id in seen or not isinstance(raw["expected_state"], str)
            or raw["expected_state"] not in {"inbox", "deferred"}
            or not isinstance(raw["action"], str) or raw["action"] not in ACTIONS
            or not _integer(raw["expected_revision"], 0, MAX_RPC_SAFE_COUNT - 1)):
            raise ValueError("source review decision is invalid or duplicated")
        seen.add(item_id)
        if (raw["action"] == "reopen") != (raw["expected_state"] == "deferred"):
            raise ValueError("source review action is not valid for this state")
        decisions.append({**raw, "item_id": item_id})
    return {"request_id": request_id, "expected_parent_profile_version_id": parent, "decisions": decisions}


def _receipt_result(connection: sqlite3.Connection, data_dir: Path, receipt: sqlite3.Row, *, created: bool) -> dict[str, Any]:
    try:
        request = _normalize_decisions(json.loads(receipt["request_json"]))
        result = json.loads(receipt["result_json"])
        if not isinstance(result, dict) or set(result) != {
            "request_id", "created", "created_at_ms", "parent_profile_version_id", "profile_version_id",
            "version_number", "profile_changed", "decisions", "counts"
        } or result["created"] is not True or not isinstance(result["profile_changed"], bool):
            raise ValueError
        if (not isinstance(result["counts"], dict) or set(result["counts"]) != SCOPES
            or any(not _integer(value, 0, MAX_RPC_SAFE_COUNT) for value in result["counts"].values())):
            raise ValueError
        if _digest(request) != receipt["request_fingerprint"] or request["request_id"] != receipt["request_id"]:
            raise ValueError
        output = _stored_version(connection.execute("SELECT * FROM profile_versions WHERE id=?", (receipt["output_profile_version_id"],)).fetchone())
        events = connection.execute("SELECT * FROM source_review_events WHERE request_id=?", (receipt["request_id"],)).fetchall()
        by_id = {event["item_id"]: event for event in events}
        expected_decisions = []
        patch = {}
        parent = _stored_version(connection.execute("SELECT * FROM profile_versions WHERE id=?", (receipt["parent_profile_version_id"],)).fetchone())
        for decision in request["decisions"]:
            event = by_id[decision["item_id"]]
            if (event["action"] != decision["action"] or event["expected_state"] != decision["expected_state"]
                or event["expected_revision"] != decision["expected_revision"]):
                raise ValueError
            expected_state = "deferred" if decision["action"] == "defer" else "inbox" if decision["action"] == "reopen" else "resolved"
            if event["state"] != expected_state or event["revision"] != decision["expected_revision"] + 1:
                raise ValueError
            if decision["action"] == "use_source":
                item = connection.execute("SELECT * FROM source_review_items WHERE id=?", (decision["item_id"],)).fetchone()
                material, _ = _material(connection, data_dir, item, verify_files=False)
                field = material["field"]
                if field in patch or _field_value(parent["profile"], field) != material["previous_value"]:
                    raise ValueError
                patch[field] = material["proposed_value"]
            expected_decisions.append({"item_id": event["item_id"], "action": event["action"],
                                       "state": event["state"], "state_revision": event["revision"]})
        expected_profile = _apply_basic_patch(parent["profile"], _normalize_patch(patch))[0] if patch else parent["profile"]
        if (expected_profile != output["profile"] or bool(patch) != result["profile_changed"]
            or request["expected_parent_profile_version_id"] != parent["id"]
            or (bool(patch) and (output["parent_version_id"] != parent["id"]
                or output["version_number"] != parent["version_number"] + 1 or output["source"] != "local_edit"))
            or (not patch and output["id"] != parent["id"])
            or len(events) != len(expected_decisions) or result["decisions"] != expected_decisions
            or result["request_id"] != receipt["request_id"]
            or result["parent_profile_version_id"] != receipt["parent_profile_version_id"]
            or result["profile_version_id"] != receipt["output_profile_version_id"]
            or result["version_number"] != output["version_number"]
            or result["profile_changed"] != bool(receipt["profile_changed"])
            or result["created_at_ms"] != receipt["created_at_ms"]):
            raise ValueError
        result["created"] = created
        return result
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise VaultError("vault_integrity_error", "A source review receipt failed its integrity check.") from exc


def apply_source_review_decisions(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    request = _normalize_decisions(params)
    fingerprint = _digest(request)
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute("SELECT * FROM source_review_receipts WHERE request_id=?", (request["request_id"],)).fetchone()
        if receipt is not None:
            if receipt["request_fingerprint"] != fingerprint:
                raise VaultError("source_review_request_conflict", "This source review request was already used for different decisions.")
            result = _receipt_result(connection, data_dir, receipt, created=False)
            connection.commit()
            return result
        current_row = connection.execute("SELECT * FROM profile_versions ORDER BY version_number DESC LIMIT 1").fetchone()
        if current_row is None:
            raise VaultError("profile_missing", "Create a profile before reviewing source checks.")
        current = _stored_version(current_row)
        if current["id"] != request["expected_parent_profile_version_id"]:
            raise VaultError("source_review_profile_conflict", "The profile changed. Refresh source checks before applying these decisions.")
        generation = connection.execute("SELECT current_generation FROM profile_source_retention_state WHERE singleton_id=1").fetchone()[0]
        patch: dict[str, Any] = {}
        results = []
        for decision in request["decisions"]:
            item = connection.execute(STATE_CTE + "SELECT * FROM states WHERE id=?", (decision["item_id"],)).fetchone()
            if item is None:
                raise VaultError("source_review_invalid", "This source check is unavailable.")
            if item["retention_generation"] != generation:
                raise VaultError("source_review_generation_conflict", "Checks from a previous import set are read-only.")
            if item["state"] != decision["expected_state"] or item["state_revision"] != decision["expected_revision"]:
                raise VaultError("source_review_state_conflict", "A source check changed before this decision could be saved.")
            material, _evidence = _material(connection, data_dir, item, verify_files=True)
            action = decision["action"]
            if (material["kind"] == "basic_conflict" and action == "acknowledge_duplicate") or (material["kind"] == "duplicate_document" and action in {"keep_profile", "use_source"}):
                raise ValueError("source review action does not match the check")
            if action == "use_source":
                field = material["field"]
                if field in patch:
                    raise VaultError("source_review_field_conflict", "Choose only one incoming value for each Basic Details field.")
                if _field_value(current["profile"], field) != material["previous_value"]:
                    raise VaultError("source_review_field_conflict", "The profile value for this source check has changed. Review it in Basic Details.")
                patch[field] = material["proposed_value"]
            state = "deferred" if action == "defer" else "inbox" if action == "reopen" else "resolved"
            results.append({"item_id": item["id"], "action": action, "state": state,
                            "state_revision": item["state_revision"] + 1})
        output_id, version_number = current["id"], current["version_number"]
        now_ms = max(_utc_now_ms(), current["created_at_ms"])
        if patch:
            if version_number >= MAX_RPC_SAFE_COUNT:
                raise VaultError("profile_version_limit", "The local profile history cannot be advanced.")
            profile, changed = _apply_basic_patch(current["profile"], _normalize_patch(patch))
            if not changed:
                raise VaultError("source_review_field_conflict", "These source values are already in the profile.")
            encoded = _json_text(profile, label="profile", expected_type=dict, max_bytes=MAX_DRAFT_PROFILE_BYTES)
            output_id, version_number = str(uuid.uuid4()), version_number + 1
            connection.execute("""INSERT INTO profile_versions
              (id,version_number,parent_version_id,canonical_json,checksum_sha256,source,created_at_ms)
              VALUES (?,?,?,?,?,'local_edit',?)""",
              (output_id, version_number, current["id"], encoded, hashlib.sha256(encoded.encode()).hexdigest(), now_ms))
        counts = source_review_counts(connection)
        for decision, result in zip(request["decisions"], results, strict=True):
            counts[decision["expected_state"]] -= 1
            counts["history" if result["state"] == "resolved" else result["state"]] += 1
        result = {"request_id": request["request_id"], "created": True, "created_at_ms": now_ms,
                  "parent_profile_version_id": current["id"], "profile_version_id": output_id,
                  "version_number": version_number, "profile_changed": bool(patch),
                  "decisions": results, "counts": counts}
        connection.execute("""INSERT INTO source_review_receipts
          (request_id,request_fingerprint,request_json,parent_profile_version_id,
           output_profile_version_id,profile_changed,result_json,created_at_ms) VALUES (?,?,?,?,?,?,?,?)""",
          (request["request_id"], fingerprint, _json(request), current["id"], output_id, int(bool(patch)), _json(result), now_ms))
        for decision, outcome in zip(request["decisions"], results, strict=True):
            connection.execute("""INSERT INTO source_review_events
              (request_id,item_id,action,expected_state,expected_revision,state,revision) VALUES (?,?,?,?,?,?,?)""",
              (request["request_id"], decision["item_id"], decision["action"], decision["expected_state"],
               decision["expected_revision"], outcome["state"], outcome["state_revision"]))
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
