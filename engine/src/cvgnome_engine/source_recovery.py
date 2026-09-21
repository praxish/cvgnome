# SPDX-License-Identifier: MPL-2.0
"""User-confirmed first-import recovery, retaining the staged source evidence."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .profile_sources import (
    MAX_EXTRACTED_CHARS, MAX_EXTRACTED_TOTAL_CHARS, MAX_SOURCE_FILES,
    _profile_counts, commit_profile_sources,
)
from .profile_versions import _apply_basic_patch, _normalize_patch
from .resume import is_baseline_resume_renderable
from .source_ingest.synthesis import SynthesisLimits, synthesize_canonical_profile
from .source_review import build_source_checks
from .storage import (
    MAX_DRAFT_PROFILE_BYTES, MAX_SCAN_REPORT_BYTES, MAX_REVIEW_CANDIDATE_BYTES,
    VaultError, _apply_migrations, _canonical_uuid, _connect, _current_profile_row,
    _json_text, _utc_now_ms, _validate_profile_base,
)


def normalize_recovery_patch(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"name", "summary"}:
        raise ValueError("Recovery requires a name and an optional summary")
    return _normalize_patch(value)


def _fingerprint(scan_id: str, patch: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        {"scan_id": scan_id, "patch": patch}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _checked_record(value: Any, scan_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, dict) or set(value) != {"contract", "patch", "fingerprint"}:
            raise ValueError("invalid record")
        patch = normalize_recovery_patch(value["patch"])
        if (value["contract"] != "first-profile-recovery-v1" or patch != value["patch"]
                or value["fingerprint"] != _fingerprint(scan_id, patch)):
            raise ValueError("invalid record")
    except (TypeError, ValueError, KeyError) as exc:
        raise VaultError("vault_integrity_error", "The import recovery record is invalid.") from exc
    return value


def validate_recovered_scan(scan: Any, profile: dict[str, Any], report: dict[str, Any]) -> bool:
    """Allow incomplete content only after a pinned, explicit first-profile correction."""
    record = _checked_record(report.get("first_profile_recovery"), str(scan["id"]))
    if record is None:
        return False
    patch = record["patch"]
    basics = profile.get("basics", {})
    if (scan["base_profile_version_id"] is not None
            or scan["base_profile_checksum_sha256"] is not None
            or not isinstance(basics, dict) or basics.get("name") != patch["name"]
            or (patch["summary"] is not None and basics.get("summary") != patch["summary"])
            or hashlib.sha256(str(scan["draft_profile_json"]).encode()).hexdigest()
            != scan["draft_profile_checksum_sha256"]):
        raise VaultError("vault_integrity_error", "The corrected import no longer matches its confirmation.")
    ui = report.get("ui", {})
    if (not isinstance(ui, dict) or not ui.get("file_counts", {}).get("parsed")
            or any(w.get("severity") == "blocking" for w in ui.get("warnings", []))):
        raise VaultError("profile_source_scan_not_recoverable", "This import needs a new source scan.")
    return True


def _prepare_recovery(data_dir: Path, scan_id: str, patch: dict[str, Any]) -> None:
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        committed = connection.execute(
            "SELECT report_json FROM profile_source_imports WHERE id = ?", (scan_id,),
        ).fetchone()
        if committed is not None:
            record = _checked_record(json.loads(committed["report_json"]).get("first_profile_recovery"), scan_id)
            if record is None or record["patch"] != patch:
                raise VaultError("profile_source_recovery_conflict", "This import was confirmed with different details.")
            connection.commit()
            return
        scan = connection.execute("SELECT * FROM profile_source_scans WHERE id = ?", (scan_id,)).fetchone()
        if scan is None:
            raise VaultError("profile_source_scan_not_found", "This source preview no longer exists. Choose the file again.")
        if scan["expires_at_ms"] <= _utc_now_ms():
            raise VaultError("profile_source_scan_expired", "This source preview expired. Choose the file again.")
        if scan["status"] != "preview":
            raise VaultError("profile_source_scan_busy", "This source preview is being saved. Retry the same details.")
        _validate_profile_base(_current_profile_row(connection),
            base_profile_version_id=scan["base_profile_version_id"],
            base_profile_checksum_sha256=scan["base_profile_checksum_sha256"])
        if scan["base_profile_version_id"] is not None:
            raise VaultError("profile_source_scan_not_recoverable", "Complete details is available only for the first profile.")
        report = json.loads(scan["report_json"])
        profile = json.loads(scan["draft_profile_json"])
        if hashlib.sha256(str(scan["draft_profile_json"]).encode()).hexdigest() != scan["draft_profile_checksum_sha256"]:
            raise VaultError("vault_integrity_error", "The source preview failed its integrity check.")
        record = _checked_record(report.get("first_profile_recovery"), scan_id)
        if record is not None:
            if record["patch"] != patch:
                raise VaultError("profile_source_recovery_conflict", "Retry the details already confirmed for this import.")
            validate_recovered_scan(scan, profile, report)
            connection.commit()
            return
        items = [dict(row) for row in connection.execute(
            "SELECT * FROM profile_source_scan_items WHERE scan_id = ? ORDER BY ordinal", (scan_id,),
        )]
        parsed = [item for item in items if item["extraction_status"] == "parsed" and item["extracted_text"].strip()]
        if not parsed:
            raise VaultError("profile_source_scan_not_recoverable", "No readable text was found. Choose another file or start manually.")
        for item in parsed:
            if hashlib.sha256(item["extracted_text"].encode()).hexdigest() != item["extracted_text_sha256"]:
                raise VaultError("vault_integrity_error", "The source extraction failed its integrity check.")
        # Blank optional summary keeps extracted content; entered text is an
        # explicit user correction, never attributed to the source document.
        applied_patch = {key: value for key, value in patch.items() if value is not None}
        basics = profile.get("basics", {})
        if isinstance(basics, dict) and all(basics.get(key) == value for key, value in applied_patch.items()):
            # Confirmation is meaningful even when extraction got the name
            # right but the draft still has no exportable career content.
            profile, changed = deepcopy(profile), []
        else:
            profile, changed = _apply_basic_patch(profile, applied_patch)
        profile.setdefault("meta", {})["source_import_recovery"] = {
            "contract": "first-profile-recovery-v1", "confirmed_fields": list(applied_patch),
            "changed_fields": changed,
        }
        sources = [{"display_name": item["display_name"], "media_type": item["media_type"],
                    "sha256": item["checksum_sha256"], "text": item["extracted_text"],
                    "origin_ordinal": item["ordinal"]} for item in parsed]
        _, synthesis = synthesize_canonical_profile(existing_profile=None, sources=sources,
            limits=SynthesisLimits(max_sources=MAX_SOURCE_FILES, max_chars_per_source=MAX_EXTRACTED_CHARS,
                                   max_total_chars=MAX_EXTRACTED_TOTAL_CHARS))
        checks = build_source_checks(profile=profile, basic_candidates=synthesis.get("basic_candidates", []),
                                     sources=sources, items=items)
        ui = report["ui"]
        warnings = [warning for warning in ui["warnings"]
                    if warning["code"] not in {"missing_identity", "insufficient_resume_content"}]
        if any(warning["severity"] == "blocking" for warning in warnings):
            raise VaultError("profile_source_scan_not_recoverable", "This preview needs a new source scan.")
        renderable = is_baseline_resume_renderable(profile)
        if not renderable:
            warnings.append({"code": "insufficient_resume_content", "stage": "synthesis",
                             "severity": "warning", "count": 1})
        ui.update(warnings=warnings, can_build=renderable, profile_counts=_profile_counts(profile),
                  source_review_count=len(checks))
        report["warning_count"] = sum(warning["count"] for warning in warnings)
        report["first_profile_recovery"] = {"contract": "first-profile-recovery-v1", "patch": patch,
                                             "fingerprint": _fingerprint(scan_id, patch)}
        profile_json = _json_text(profile, label="corrected profile", expected_type=dict, max_bytes=MAX_DRAFT_PROFILE_BYTES)
        report_json = _json_text(report, label="source report", expected_type=dict, max_bytes=MAX_SCAN_REPORT_BYTES)
        checks_json = _json_text(checks, label="source checks", expected_type=list, max_bytes=MAX_REVIEW_CANDIDATE_BYTES)
        connection.execute("""UPDATE profile_source_scans SET draft_profile_json = ?,
            draft_profile_checksum_sha256 = ?, report_json = ?, source_review_json = ?, updated_at_ms = ?
            WHERE id = ?""", (profile_json, hashlib.sha256(profile_json.encode()).hexdigest(),
                               report_json, checks_json, _utc_now_ms(), scan_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_profile_sources(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    scan_id = _canonical_uuid(params.get("scan_id"), label="scan_id")
    patch = normalize_recovery_patch(params.get("patch"))
    _prepare_recovery(data_dir, scan_id, patch)
    try:
        result = commit_profile_sources(data_dir, scan_id)
    except ValueError as exc:
        # Input validation has already succeeded. A commit-time verification
        # failure must not invite editing a request whose details are pinned.
        raise VaultError("profile_source_scan_not_recoverable",
                         "The staged source could not be verified. Choose the original file again.") from exc
    # A replay represents the same immutable import; no new version is made.
    return result


def resume_profile_source_scan(data_dir: Path) -> dict[str, Any] | None:
    """Resume the latest live first-profile preview without exposing raw sources."""
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        if _current_profile_row(connection) is not None:
            return None
        scan = connection.execute("""SELECT * FROM profile_source_scans
            WHERE base_profile_version_id IS NULL AND status = 'preview' AND expires_at_ms > ?
            ORDER BY created_at_ms DESC, id DESC LIMIT 1""", (_utc_now_ms(),)).fetchone()
        if scan is None:
            return None
        report = json.loads(scan["report_json"])
        ui = report["ui"]
        record = _checked_record(report.get("first_profile_recovery"), str(scan["id"]))
        result = {key: ui[key] for key in ("file_counts", "source_counts", "format_counts", "warnings",
                                           "can_build", "review_candidate_count", "source_review_count")}
        result.update(scan_id=str(scan["id"]), expires_at_ms=int(scan["expires_at_ms"]), base_profile=None)
        return {"scan": result, "recovery_patch": record["patch"] if record else None}
    finally:
        connection.close()
