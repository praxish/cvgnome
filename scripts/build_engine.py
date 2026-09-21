# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile
from pathlib import Path

from cvgnome_engine.legacy_compatibility import LEGACY_DATABASE_FILENAME

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "engine"
BUILD_ROOT = ENGINE_ROOT / "build" / "sidecar"
TAURI_BINARIES = ROOT / "src-tauri" / "binaries"
EXPECTED_PROTOCOL_VERSION = 1
EXPECTED_ENGINE_VERSION = tomllib.loads((ENGINE_ROOT / "pyproject.toml").read_text())["project"]["version"]


def _smoke_docx_bytes() -> bytes:
    paragraphs = (
        "Name: CVGnome Smoke Test",
        "Email: smoke@example.test",
        "## Experience",
        "Company: Local Engine",
        "Position: Verification Engineer",
        "Start Date: 2026-01",
        "Highlight: Built deterministic offline document exports.",
        "## Education",
        "Institution: Review Inbox Institute",
        "Degree: Certificate",
        "Field: Evidence-backed Profile Review",
        "## Skills",
        "Skills: Review queues, Evidence trails",
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{value}</w:t></w:r></w:p>" for value in paragraphs)
        + "<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("word/document.xml", document)
        return buffer.getvalue()


def _preview_smoke_diagnostics(result: object) -> str:
    """Report only bounded status metadata for the synthetic source fixture."""
    if not isinstance(result, dict):
        return json.dumps({"result_type": type(result).__name__})
    summary: dict[str, object] = {}
    for key in ("can_build", "review_candidate_count", "source_review_count"):
        value = result.get(key)
        if value is None or isinstance(value, (bool, int)):
            summary[key] = value
    counts = result.get("file_counts")
    if isinstance(counts, dict):
        summary["file_counts"] = {
            key: counts[key]
            for key in ("discovered", "staged", "parsed", "duplicates", "skipped", "failed")
            if isinstance(counts.get(key), int)
        }
    warnings = result.get("warnings")
    if isinstance(warnings, list):
        summary["warnings"] = [
            {
                key: value[:160] if isinstance(value, str) else value
                for key in ("code", "message", "stage", "severity", "count")
                if isinstance((value := warning.get(key)), (str, int, bool))
            }
            for warning in warnings[:12]
            if isinstance(warning, dict)
        ]
    return json.dumps(summary, sort_keys=True)


def _rustc_path() -> str:
    configured = os.environ.get("RUSTC")
    if configured:
        return configured
    discovered = shutil.which("rustc")
    if discovered:
        return discovered
    homebrew_proxy = Path("/opt/homebrew/opt/rustup/bin/rustc")
    if homebrew_proxy.exists():
        return str(homebrew_proxy)
    raise SystemExit("rustc was not found; install the stable Rust toolchain first")


def _host_triple() -> str:
    completed = subprocess.run(
        [_rustc_path(), "--print", "host-tuple"],
        check=True,
        capture_output=True,
        text=True,
    )
    target = completed.stdout.strip()
    if not target:
        raise SystemExit("rustc did not report a host target triple")
    return target


def _target_triple() -> str:
    host = _host_triple()
    configured = os.environ.get("CARGO_BUILD_TARGET")
    if configured:
        requested = configured.strip()
        if requested != host:
            raise SystemExit(
                "The Python sidecar must be built natively for each target; "
                f"requested {requested}, but this host is {host}"
            )

    machine = platform.machine().lower()
    python_arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
    rust_arch = host.split("-", 1)[0]
    if python_arch != rust_arch:
        raise SystemExit(
            "The Python interpreter and Rust host target use different architectures: "
            f"{python_arch} versus {rust_arch}"
        )
    return host


def _smoke_test(binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="cvgnome-sidecar-smoke-") as directory:
        def request(request_id: str, method: str, params: dict[str, object]) -> dict[str, object]:
            payload = json.dumps(
                {
                    "protocol_version": EXPECTED_PROTOCOL_VERSION,
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                separators=(",", ":"),
            )
            try:
                completed = subprocess.run(
                    [str(binary), "request", "--data-dir", directory],
                    input=payload + "\n",
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                raise SystemExit(
                    "The frozen Python sidecar timed out during its smoke test"
                ) from exc
            try:
                response = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise SystemExit("The frozen Python sidecar returned invalid JSON") from exc
            if (
                completed.returncode != 0
                or response.get("protocol_version") != EXPECTED_PROTOCOL_VERSION
                or response.get("id") != request_id
                or not response.get("ok")
            ):
                raise SystemExit(
                    f"The frozen Python sidecar failed its {method} smoke test"
                )
            return response

        initial_status = request("status", "system.status", {}).get("result")
        if (
            not isinstance(initial_status, dict)
            or initial_status.get("engine_version") != EXPECTED_ENGINE_VERSION
            or initial_status.get("schema_version") != 19
            or initial_status.get("memory_count") != 0
            or initial_status.get("source_retention_generation") != 1
            or initial_status.get("retained_source_imports") != 0
            or initial_status.get("review_inbox_items") != 0
            or initial_status.get("review_deferred_items") != 0
            or initial_status.get("review_history_items") != 0
            or initial_status.get("source_review_inbox_items") != 0
            or initial_status.get("source_review_deferred_items") != 0
            or initial_status.get("source_review_history_items") != 0
        ):
            raise SystemExit("The frozen Python sidecar returned invalid version or retention status")

        canonical_scan_id = str(uuid.uuid4())
        canonical_staging = Path(
            directory,
            "imports",
            "staging",
            canonical_scan_id,
        )
        canonical_staging.mkdir(parents=True)
        canonical_path = canonical_staging / "0000.json"
        canonical_bytes = (
            b"\xef\xbb\xbf \n{\n"
            b'  "basics": {"name": "CVGnome Smoke Test", '
            b'"summary": "Builds deterministic local Python systems."},\n'
            b'  "skills": [{"name": "Engineering", "keywords": ["Python"]}]\n'
            b"}\n"
        )
        canonical_path.write_bytes(canonical_bytes)
        if os.name != "nt":
            canonical_staging.chmod(0o700)
            canonical_path.chmod(0o600)
        canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
        imported = request(
            "canonical-import",
            "profile.import",
            {
                "source": {
                    "scan_id": canonical_scan_id,
                    "managed_relative_path": (
                        f"imports/staging/{canonical_scan_id}/0000.json"
                    ),
                    "display_name": "private-smoke-canonical.json",
                    "raw_sha256": canonical_hash,
                    "raw_bytes": len(canonical_bytes),
                }
            },
        ).get("result")
        retained_blob = Path(
            directory,
            "sources",
            "blobs",
            canonical_hash[:2],
            canonical_hash,
        )
        if (
            not isinstance(imported, dict)
            or imported.get("renderable") is not True
            or canonical_staging.exists()
            or retained_blob.read_bytes() != canonical_bytes
        ):
            raise SystemExit("The frozen Python sidecar did not retain a canonical import")
        profile_review = request(
            "profile-review",
            "profile.review",
            {},
        ).get("result")
        if (
            not isinstance(profile_review, dict)
            or profile_review.get("profile_version_id")
            != imported.get("profile_version_id")
            or profile_review.get("name") != "CVGnome Smoke Test"
            or profile_review.get("source_kind") != "structured_file"
            or "private-smoke-canonical" in json.dumps(profile_review)
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid profile review")
        skills_page = request(
            "profile-review-skills",
            "profile.review.section",
            {
                "profile_version_id": imported.get("profile_version_id"),
                "section": "skills",
                "offset": 0,
            },
        ).get("result")
        if (
            not isinstance(skills_page, dict)
            or skills_page.get("profile_version_id")
            != imported.get("profile_version_id")
            or skills_page.get("total_items") != 1
            or len(skills_page.get("items") or []) != 1
            or skills_page["items"][0].get("title") != "Engineering"
        ):
            raise SystemExit("The frozen Python sidecar could not page profile review")
        imported_profile_version_id = imported.get("profile_version_id")
        profile_versions = request(
            "profile-versions-initial",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        initial_version_items = (
            profile_versions.get("items")
            if isinstance(profile_versions, dict)
            else None
        )
        if (
            not isinstance(profile_versions, dict)
            or profile_versions.get("current_profile_version_id")
            != imported_profile_version_id
            or profile_versions.get("total_items") != 1
            or profile_versions.get("offset") != 0
            or profile_versions.get("limit") != 20
            or profile_versions.get("next_offset") is not None
            or not isinstance(initial_version_items, list)
            or len(initial_version_items) != 1
            or initial_version_items[0].get("profile_version_id")
            != imported_profile_version_id
            or initial_version_items[0].get("source_kind") != "structured_file"
        ):
            raise SystemExit("The frozen Python sidecar returned invalid profile history")
        imported_basics = request(
            "profile-basics-imported",
            "profile.basics.get",
            {"profile_version_id": imported_profile_version_id},
        ).get("result")
        imported_location = (
            imported_basics.get("location")
            if isinstance(imported_basics, dict)
            else None
        )
        if (
            not isinstance(imported_basics, dict)
            or set(imported_basics)
            != {
                "profile_version_id",
                "version_number",
                "created_at_ms",
                "renderable",
                "name",
                "headline",
                "summary",
                "email",
                "phone",
                "url",
                "location",
            }
            or imported_basics.get("profile_version_id")
            != imported_profile_version_id
            or imported_basics.get("name") != "CVGnome Smoke Test"
            or imported_basics.get("headline") is not None
            or not isinstance(imported_location, dict)
            or set(imported_location) != {"city", "region", "country_code"}
            or imported_location
            != {"city": None, "region": None, "country_code": None}
        ):
            raise SystemExit("The frozen Python sidecar returned invalid profile basics")
        exact_imported_review = request(
            "profile-review-imported-version",
            "profile.review.version",
            {"profile_version_id": imported_profile_version_id},
        ).get("result")
        if exact_imported_review != profile_review:
            raise SystemExit("The frozen Python sidecar changed exact profile review")
        basic_update_params = {
            "expected_parent_profile_version_id": imported_profile_version_id,
            "request_id": "d1404572-0e0e-4a27-8c72-269d839ba897",
            "patch": {
                "headline": "Local Systems Engineer",
                "location": {"city": "Test Harbor"},
            },
        }
        basic_update = request(
            "profile-basics-update",
            "profile.basics.update",
            basic_update_params,
        ).get("result")
        edited_profile_version_id = (
            basic_update.get("profile_version_id")
            if isinstance(basic_update, dict)
            else None
        )
        if (
            not isinstance(basic_update, dict)
            or not isinstance(edited_profile_version_id, str)
            or basic_update.get("parent_profile_version_id")
            != imported_profile_version_id
            or basic_update.get("version_number") != 2
            or basic_update.get("created") is not True
            or basic_update.get("changed_fields")
            != ["headline", "location.city"]
            or basic_update.get("profile_name") != "CVGnome Smoke Test"
            or basic_update.get("headline") != "Local Systems Engineer"
        ):
            raise SystemExit("The frozen Python sidecar could not update profile basics")
        basic_update_retry = request(
            "profile-basics-update-retry",
            "profile.basics.update",
            basic_update_params,
        ).get("result")
        if (
            not isinstance(basic_update_retry, dict)
            or basic_update_retry.get("profile_version_id")
            != edited_profile_version_id
            or basic_update_retry.get("created") is not False
        ):
            raise SystemExit("The frozen Python sidecar did not reuse a profile edit retry")
        profile_versions = request(
            "profile-versions-edited",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        edited_version_items = (
            profile_versions.get("items")
            if isinstance(profile_versions, dict)
            else None
        )
        if (
            not isinstance(profile_versions, dict)
            or profile_versions.get("current_profile_version_id")
            != edited_profile_version_id
            or profile_versions.get("total_items") != 2
            or not isinstance(edited_version_items, list)
            or len(edited_version_items) != 2
            or edited_version_items[0].get("profile_version_id")
            != edited_profile_version_id
            or edited_version_items[0].get("parent_profile_version_id")
            != imported_profile_version_id
            or edited_version_items[0].get("source_kind") != "local_edit"
        ):
            raise SystemExit("The frozen Python sidecar duplicated a profile edit")
        profile_diff = request(
            "profile-versions-diff",
            "profile.versions.diff",
            {
                "from_profile_version_id": imported_profile_version_id,
                "to_profile_version_id": edited_profile_version_id,
            },
        ).get("result")
        if (
            not isinstance(profile_diff, dict)
            or profile_diff.get("total_changes") != 2
            or [change.get("field") for change in profile_diff.get("basic_changes", [])]
            != ["headline", "location.city"]
            or profile_diff.get("section_changes") != []
            or profile_diff.get("from_version", {}).get("profile_version_id")
            != imported_profile_version_id
            or profile_diff.get("to_version", {}).get("profile_version_id")
            != edited_profile_version_id
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid profile diff")
        historical_review = request(
            "profile-review-imported-after-edit",
            "profile.review.version",
            {"profile_version_id": imported_profile_version_id},
        ).get("result")
        if historical_review != profile_review:
            raise SystemExit("The frozen Python sidecar mutated historical profile review")
        work_page_keys = {
            "profile_version_id",
            "version_number",
            "total_items",
            "offset",
            "limit",
            "next_offset",
            "items",
        }
        work_item_keys = {
            "entry_index",
            "name",
            "position",
            "url",
            "start_date",
            "end_date",
            "summary",
            "highlights",
            "location",
        }
        work_receipt_keys = {
            "request_id",
            "profile_version_id",
            "parent_profile_version_id",
            "version_number",
            "created_at_ms",
            "created",
            "operation",
            "entry_index",
            "changed_fields",
            "profile_name",
            "work_entries",
            "renderable",
        }
        empty_work = request(
            "profile-work-empty",
            "profile.work.list",
            {"profile_version_id": edited_profile_version_id, "offset": 0},
        ).get("result")
        if (
            not isinstance(empty_work, dict)
            or set(empty_work) != work_page_keys
            or empty_work.get("profile_version_id") != edited_profile_version_id
            or empty_work.get("version_number") != 2
            or empty_work.get("total_items") != 0
            or empty_work.get("offset") != 0
            or empty_work.get("limit") != 20
            or empty_work.get("next_offset") is not None
            or empty_work.get("items") != []
        ):
            raise SystemExit("The frozen Python sidecar returned invalid empty Work History")
        work_add_params = {
            "expected_parent_profile_version_id": edited_profile_version_id,
            "request_id": "1b720d1a-ac65-45ee-84ca-d7f3dd7a986b",
            "operation": {
                "kind": "add",
                "entry": {
                    "name": "Local Engine",
                    "position": "Verification Engineer",
                    "url": "https://example.com/work/verification",
                    "start_date": "2025-01",
                    "end_date": None,
                    "summary": "Built deterministic offline document exports.",
                    "highlights": ["Kept all customer data on device."],
                    "location": "Test Harbor",
                },
            },
        }
        work_add = request(
            "profile-work-add",
            "profile.work.update",
            work_add_params,
        ).get("result")
        work_added_profile_version_id = (
            work_add.get("profile_version_id")
            if isinstance(work_add, dict)
            else None
        )
        if (
            not isinstance(work_add, dict)
            or set(work_add) != work_receipt_keys
            or not isinstance(work_added_profile_version_id, str)
            or work_add.get("parent_profile_version_id") != edited_profile_version_id
            or work_add.get("version_number") != 3
            or work_add.get("created") is not True
            or work_add.get("operation") != "add"
            or work_add.get("entry_index") != 0
            or work_add.get("changed_fields")
            != [
                "name",
                "position",
                "url",
                "start_date",
                "summary",
                "highlights",
                "location",
            ]
            or work_add.get("profile_name") != "CVGnome Smoke Test"
            or work_add.get("work_entries") != 1
            or not isinstance(work_add.get("renderable"), bool)
        ):
            raise SystemExit("The frozen Python sidecar could not add Work History")
        work_add_retry = request(
            "profile-work-add-retry",
            "profile.work.update",
            work_add_params,
        ).get("result")
        expected_work_add_retry = {**work_add, "created": False}
        if (
            not isinstance(work_add_retry, dict)
            or set(work_add_retry) != work_receipt_keys
            or work_add_retry != expected_work_add_retry
        ):
            raise SystemExit("The frozen Python sidecar did not reuse a Work History retry")
        added_work = request(
            "profile-work-added",
            "profile.work.list",
            {"profile_version_id": work_added_profile_version_id, "offset": 0},
        ).get("result")
        added_work_items = (
            added_work.get("items") if isinstance(added_work, dict) else None
        )
        if (
            not isinstance(added_work, dict)
            or set(added_work) != work_page_keys
            or added_work.get("total_items") != 1
            or not isinstance(added_work_items, list)
            or len(added_work_items) != 1
            or set(added_work_items[0]) != work_item_keys
            or added_work_items[0].get("entry_index") != 0
            or added_work_items[0].get("name") != "Local Engine"
            or added_work_items[0].get("position") != "Verification Engineer"
            or added_work_items[0].get("end_date") is not None
            or added_work_items[0].get("highlights")
            != ["Kept all customer data on device."]
        ):
            raise SystemExit("The frozen Python sidecar returned invalid added Work History")
        work_update = request(
            "profile-work-update",
            "profile.work.update",
            {
                "expected_parent_profile_version_id": work_added_profile_version_id,
                "request_id": "d2902b29-f04d-486d-93cd-e881530437fa",
                "operation": {
                    "kind": "update",
                    "entry_index": 0,
                    "patch": {
                        "position": "Senior Verification Engineer",
                        "url": None,
                        "end_date": "2026-08",
                        "highlights": None,
                    },
                },
            },
        ).get("result")
        work_updated_profile_version_id = (
            work_update.get("profile_version_id")
            if isinstance(work_update, dict)
            else None
        )
        if (
            not isinstance(work_update, dict)
            or set(work_update) != work_receipt_keys
            or not isinstance(work_updated_profile_version_id, str)
            or work_update.get("parent_profile_version_id")
            != work_added_profile_version_id
            or work_update.get("version_number") != 4
            or work_update.get("created") is not True
            or work_update.get("operation") != "update"
            or work_update.get("entry_index") != 0
            or work_update.get("changed_fields")
            != ["position", "url", "end_date", "highlights"]
            or work_update.get("work_entries") != 1
        ):
            raise SystemExit("The frozen Python sidecar could not update Work History")
        exact_added_work = request(
            "profile-work-added-after-update",
            "profile.work.list",
            {"profile_version_id": work_added_profile_version_id, "offset": 0},
        ).get("result")
        if exact_added_work != added_work:
            raise SystemExit("The frozen Python sidecar mutated historical Work History")
        work_remove = request(
            "profile-work-remove",
            "profile.work.update",
            {
                "expected_parent_profile_version_id": work_updated_profile_version_id,
                "request_id": "25e36e93-b089-4dab-9d39-dc773656088a",
                "operation": {"kind": "remove", "entry_index": 0},
            },
        ).get("result")
        work_removed_profile_version_id = (
            work_remove.get("profile_version_id")
            if isinstance(work_remove, dict)
            else None
        )
        if (
            not isinstance(work_remove, dict)
            or set(work_remove) != work_receipt_keys
            or not isinstance(work_removed_profile_version_id, str)
            or work_remove.get("parent_profile_version_id")
            != work_updated_profile_version_id
            or work_remove.get("version_number") != 5
            or work_remove.get("created") is not True
            or work_remove.get("operation") != "remove"
            or work_remove.get("entry_index") != 0
            or work_remove.get("changed_fields")
            != [
                "name",
                "position",
                "start_date",
                "end_date",
                "summary",
                "location",
            ]
            or work_remove.get("work_entries") != 0
        ):
            raise SystemExit("The frozen Python sidecar could not remove Work History")
        removed_work = request(
            "profile-work-removed",
            "profile.work.list",
            {"profile_version_id": work_removed_profile_version_id, "offset": 0},
        ).get("result")
        if (
            not isinstance(removed_work, dict)
            or set(removed_work) != work_page_keys
            or removed_work.get("total_items") != 0
            or removed_work.get("items") != []
        ):
            raise SystemExit("The frozen Python sidecar retained removed Work History")
        historical_review_after_work = request(
            "profile-review-imported-after-work",
            "profile.review.version",
            {"profile_version_id": imported_profile_version_id},
        ).get("result")
        if historical_review_after_work != profile_review:
            raise SystemExit("The frozen Python sidecar mutated its imported profile history")
        restore_receipt_keys = {
            "request_id",
            "profile_version_id",
            "parent_profile_version_id",
            "restored_from_profile_version_id",
            "restored_from_version_number",
            "version_number",
            "created_at_ms",
            "created",
            "profile_name",
            "renderable",
        }
        restore_params = {
            "source_profile_version_id": imported_profile_version_id,
            "expected_parent_profile_version_id": work_removed_profile_version_id,
            "request_id": "f05f1e76-b6e6-4a16-9f61-3e0861eeef06",
        }
        restored = request(
            "profile-version-restore",
            "profile.versions.restore",
            restore_params,
        ).get("result")
        restored_profile_version_id = (
            restored.get("profile_version_id")
            if isinstance(restored, dict)
            else None
        )
        if (
            not isinstance(restored, dict)
            or set(restored) != restore_receipt_keys
            or not isinstance(restored_profile_version_id, str)
            or restored.get("parent_profile_version_id")
            != work_removed_profile_version_id
            or restored.get("restored_from_profile_version_id")
            != imported_profile_version_id
            or restored.get("restored_from_version_number") != 1
            or restored.get("version_number") != 6
            or restored.get("created") is not True
            or restored.get("profile_name") != "CVGnome Smoke Test"
            or restored.get("renderable") is not True
        ):
            raise SystemExit("The frozen Python sidecar could not restore profile history")
        restored_retry = request(
            "profile-version-restore-retry",
            "profile.versions.restore",
            restore_params,
        ).get("result")
        if restored_retry != {**restored, "created": False}:
            raise SystemExit("The frozen Python sidecar did not reuse a profile restore retry")
        restored_review = request(
            "profile-review-restored",
            "profile.review",
            {},
        ).get("result")
        if (
            not isinstance(restored_review, dict)
            or restored_review.get("profile_version_id")
            != restored_profile_version_id
            or restored_review.get("version_number") != 6
            or restored_review.get("source_kind") != "local_restore"
            or restored_review.get("name") != profile_review.get("name")
            or restored_review.get("headline") != profile_review.get("headline")
            or restored_review.get("summary") != profile_review.get("summary")
            or restored_review.get("contacts") != profile_review.get("contacts")
            or restored_review.get("sections") != profile_review.get("sections")
        ):
            raise SystemExit("The frozen Python sidecar returned invalid restored profile data")
        restored_diff = request(
            "profile-version-restored-diff",
            "profile.versions.diff",
            {
                "from_profile_version_id": imported_profile_version_id,
                "to_profile_version_id": restored_profile_version_id,
            },
        ).get("result")
        if (
            not isinstance(restored_diff, dict)
            or restored_diff.get("total_changes") != 0
            or restored_diff.get("basic_changes") != []
            or restored_diff.get("section_changes") != []
        ):
            raise SystemExit("The frozen Python sidecar did not copy the restored profile exactly")
        restored_versions = request(
            "profile-versions-restored",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        restored_items = (
            restored_versions.get("items")
            if isinstance(restored_versions, dict)
            else None
        )
        if (
            not isinstance(restored_versions, dict)
            or restored_versions.get("current_profile_version_id")
            != restored_profile_version_id
            or restored_versions.get("total_items") != 6
            or not isinstance(restored_items, list)
            or len(restored_items) != 6
            or restored_items[0].get("profile_version_id")
            != restored_profile_version_id
            or restored_items[0].get("source_kind") != "local_restore"
        ):
            raise SystemExit("The frozen Python sidecar returned invalid restored history")
        section_page_keys = {
            "profile_version_id",
            "version_number",
            "section",
            "total_items",
            "offset",
            "limit",
            "next_offset",
            "items",
        }
        section_receipt_keys = {
            "request_id",
            "profile_version_id",
            "parent_profile_version_id",
            "version_number",
            "created_at_ms",
            "created",
            "section",
            "operation",
            "entry_index",
            "changed_fields",
            "profile_name",
            "section_entries",
            "renderable",
        }
        project_add_params = {
            "expected_parent_profile_version_id": restored_profile_version_id,
            "request_id": "fa53d271-e1d4-48bb-88b4-5e1b8dac09e5",
            "operation": {
                "kind": "add",
                "entry": {
                    "name": "Offline Resume Pipeline",
                    "description": "Packaged a private, deterministic desktop workflow.",
                    "url": "https://example.com/projects/offline-resume",
                    "start_date": "2026-01",
                    "end_date": None,
                    "highlights": ["Verified the frozen local engine."],
                    "keywords": ["Python", "SQLite"],
                },
            },
        }
        project_add = request(
            "profile-project-add",
            "profile.projects.update",
            project_add_params,
        ).get("result")
        project_profile_version_id = (
            project_add.get("profile_version_id")
            if isinstance(project_add, dict)
            else None
        )
        if (
            not isinstance(project_add, dict)
            or set(project_add) != section_receipt_keys
            or not isinstance(project_profile_version_id, str)
            or project_add.get("parent_profile_version_id")
            != restored_profile_version_id
            or project_add.get("version_number") != 7
            or project_add.get("created") is not True
            or project_add.get("section") != "projects"
            or project_add.get("operation") != "add"
            or project_add.get("entry_index") != 0
            or project_add.get("changed_fields")
            != [
                "name",
                "description",
                "url",
                "start_date",
                "highlights",
                "keywords",
            ]
            or project_add.get("section_entries") != 1
        ):
            raise SystemExit("The frozen Python sidecar could not add a project")
        project_add_retry = request(
            "profile-project-add-retry",
            "profile.projects.update",
            project_add_params,
        ).get("result")
        if project_add_retry != {**project_add, "created": False}:
            raise SystemExit("The frozen Python sidecar did not reuse a project edit retry")
        project_page = request(
            "profile-project-list",
            "profile.projects.list",
            {"profile_version_id": project_profile_version_id, "offset": 0},
        ).get("result")
        project_items = (
            project_page.get("items") if isinstance(project_page, dict) else None
        )
        if (
            not isinstance(project_page, dict)
            or set(project_page) != section_page_keys
            or project_page.get("profile_version_id") != project_profile_version_id
            or project_page.get("version_number") != 7
            or project_page.get("section") != "projects"
            or project_page.get("total_items") != 1
            or project_page.get("offset") != 0
            or project_page.get("limit") != 20
            or project_page.get("next_offset") is not None
            or not isinstance(project_items, list)
            or len(project_items) != 1
            or project_items[0].get("entry_index") != 0
            or project_items[0].get("name") != "Offline Resume Pipeline"
            or project_items[0].get("end_date") is not None
            or project_items[0].get("keywords") != ["Python", "SQLite"]
        ):
            raise SystemExit("The frozen Python sidecar returned invalid Projects data")
        education_add = request(
            "profile-education-add",
            "profile.education.update",
            {
                "expected_parent_profile_version_id": project_profile_version_id,
                "request_id": "72307912-5e2e-4b4b-890e-9c15bfc2ce99",
                "operation": {
                    "kind": "add",
                    "entry": {
                        "institution": "Local Systems Institute",
                        "study_type": "Certificate",
                        "area": "Reliable Desktop Software",
                        "url": "https://example.com/credentials/local-systems",
                        "start_date": "2025-09",
                        "end_date": None,
                        "score": "Completed",
                        "courses": ["Offline Data Integrity"],
                    },
                },
            },
        ).get("result")
        education_profile_version_id = (
            education_add.get("profile_version_id")
            if isinstance(education_add, dict)
            else None
        )
        if (
            not isinstance(education_add, dict)
            or set(education_add) != section_receipt_keys
            or not isinstance(education_profile_version_id, str)
            or education_add.get("parent_profile_version_id")
            != project_profile_version_id
            or education_add.get("version_number") != 8
            or education_add.get("created") is not True
            or education_add.get("section") != "education"
            or education_add.get("operation") != "add"
            or education_add.get("entry_index") != 0
            or education_add.get("changed_fields")
            != [
                "institution",
                "study_type",
                "area",
                "url",
                "start_date",
                "score",
                "courses",
            ]
            or education_add.get("section_entries") != 1
        ):
            raise SystemExit("The frozen Python sidecar could not add education")
        education_page = request(
            "profile-education-list",
            "profile.education.list",
            {"profile_version_id": education_profile_version_id, "offset": 0},
        ).get("result")
        education_items = (
            education_page.get("items")
            if isinstance(education_page, dict)
            else None
        )
        if (
            not isinstance(education_page, dict)
            or set(education_page) != section_page_keys
            or education_page.get("profile_version_id")
            != education_profile_version_id
            or education_page.get("version_number") != 8
            or education_page.get("section") != "education"
            or education_page.get("total_items") != 1
            or not isinstance(education_items, list)
            or len(education_items) != 1
            or education_items[0].get("entry_index") != 0
            or education_items[0].get("institution")
            != "Local Systems Institute"
            or education_items[0].get("area") != "Reliable Desktop Software"
            or education_items[0].get("courses") != ["Offline Data Integrity"]
        ):
            raise SystemExit("The frozen Python sidecar returned invalid Education data")
        skill_update_params = {
            "expected_parent_profile_version_id": education_profile_version_id,
            "request_id": "8b8adeb3-a3dd-4907-9df9-dae30841000d",
            "operation": {
                "kind": "update",
                "entry_index": 0,
                "patch": {
                    "level": "Advanced",
                    "keywords": ["Python", "SQLite"],
                },
            },
        }
        skill_update = request(
            "profile-skill-update",
            "profile.skills.update",
            skill_update_params,
        ).get("result")
        skill_profile_version_id = (
            skill_update.get("profile_version_id")
            if isinstance(skill_update, dict)
            else None
        )
        if (
            not isinstance(skill_update, dict)
            or set(skill_update) != section_receipt_keys
            or not isinstance(skill_profile_version_id, str)
            or skill_update.get("parent_profile_version_id")
            != education_profile_version_id
            or skill_update.get("version_number") != 9
            or skill_update.get("created") is not True
            or skill_update.get("section") != "skills"
            or skill_update.get("operation") != "update"
            or skill_update.get("entry_index") != 0
            or skill_update.get("changed_fields") != ["level", "keywords"]
            or skill_update.get("section_entries") != 1
        ):
            raise SystemExit("The frozen Python sidecar could not update a skill group")
        skill_update_retry = request(
            "profile-skill-update-retry",
            "profile.skills.update",
            skill_update_params,
        ).get("result")
        if skill_update_retry != {**skill_update, "created": False}:
            raise SystemExit("The frozen Python sidecar did not reuse a skill edit retry")
        skill_page = request(
            "profile-skills-list",
            "profile.skills.list",
            {"profile_version_id": skill_profile_version_id, "offset": 0},
        ).get("result")
        skill_items = skill_page.get("items") if isinstance(skill_page, dict) else None
        if (
            not isinstance(skill_page, dict)
            or set(skill_page) != section_page_keys
            or skill_page.get("profile_version_id") != skill_profile_version_id
            or skill_page.get("version_number") != 9
            or skill_page.get("section") != "skills"
            or skill_page.get("total_items") != 1
            or not isinstance(skill_items, list)
            or len(skill_items) != 1
            or skill_items[0].get("entry_index") != 0
            or skill_items[0].get("name") != "Engineering"
            or skill_items[0].get("level") != "Advanced"
            or skill_items[0].get("keywords") != ["Python", "SQLite"]
        ):
            raise SystemExit("The frozen Python sidecar returned invalid Skills data")
        restored_skill_page = request(
            "profile-skills-restored-history",
            "profile.skills.list",
            {"profile_version_id": restored_profile_version_id, "offset": 0},
        ).get("result")
        if (
            not isinstance(restored_skill_page, dict)
            or restored_skill_page.get("items", [{}])[0].get("level") is not None
            or restored_skill_page.get("items", [{}])[0].get("keywords") != ["Python"]
        ):
            raise SystemExit("The frozen Python sidecar mutated historical Skills data")
        duplicate_skill = request(
            "profile-skill-duplicate-add",
            "profile.skills.update",
            {
                "expected_parent_profile_version_id": skill_profile_version_id,
                "request_id": "f0f3ce60-9fd2-4bf7-9213-85f2627b29fc",
                "operation": {
                    "kind": "add",
                    "entry": {
                        "name": "Engineering",
                        "level": "Advanced",
                        "keywords": ["Rust"],
                    },
                },
            },
        ).get("result")
        duplicate_skill_profile_version_id = (
            duplicate_skill.get("profile_version_id")
            if isinstance(duplicate_skill, dict)
            else None
        )
        if (
            not isinstance(duplicate_skill, dict)
            or not isinstance(duplicate_skill_profile_version_id, str)
            or duplicate_skill.get("version_number") != 10
            or duplicate_skill.get("section") != "skills"
            or duplicate_skill.get("operation") != "add"
            or duplicate_skill.get("entry_index") != 1
            or duplicate_skill.get("changed_fields")
            != ["name", "level", "keywords"]
            or duplicate_skill.get("section_entries") != 2
        ):
            raise SystemExit("The frozen Python sidecar could not add a duplicate skill")
        changes_preview = request(
            "profile-changes-preview",
            "profile.changes.preview",
            {"profile_version_id": duplicate_skill_profile_version_id},
        ).get("result")
        suggestions = (
            changes_preview.get("suggestions")
            if isinstance(changes_preview, dict)
            else None
        )
        if (
            not isinstance(changes_preview, dict)
            or set(changes_preview)
            != {
                "profile_version_id",
                "version_number",
                "algorithm_version",
                "suggestions",
                "truncated",
            }
            or changes_preview.get("profile_version_id")
            != duplicate_skill_profile_version_id
            or changes_preview.get("version_number") != 10
            or changes_preview.get("algorithm_version") != 1
            or changes_preview.get("truncated") is not False
            or not isinstance(suggestions, list)
            or len(suggestions) != 1
            or suggestions[0].get("section") != "skills"
            or suggestions[0].get("keep_entry_index") != 0
            or suggestions[0].get("remove_entry_index") != 1
            or suggestions[0].get("reason_codes") != ["matching_skill_group"]
            or suggestions[0].get("merged_entry", {}).get("keywords")
            != ["Python", "SQLite", "Rust"]
            or not isinstance(suggestions[0].get("suggestion_id"), str)
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid changes preview")
        profile_changes_params = {
            "expected_parent_profile_version_id": duplicate_skill_profile_version_id,
            "request_id": "e3d31a47-dd08-4fd1-9c2b-52927b51fd3c",
            "operations": [
                {
                    "kind": "update",
                    "section": "projects",
                    "entry_index": 0,
                    "patch": {
                        "description": "Packaged and batch-reviewed a private desktop workflow."
                    },
                },
                {
                    "kind": "merge",
                    "section": "skills",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "suggestion_id": suggestions[0]["suggestion_id"],
                },
            ],
        }
        profile_changes = request(
            "profile-changes-apply",
            "profile.changes.apply",
            profile_changes_params,
        ).get("result")
        profile_changes_version_id = (
            profile_changes.get("profile_version_id")
            if isinstance(profile_changes, dict)
            else None
        )
        if (
            not isinstance(profile_changes, dict)
            or set(profile_changes)
            != {
                "request_id",
                "profile_version_id",
                "parent_profile_version_id",
                "version_number",
                "created_at_ms",
                "created",
                "operation_count",
                "changed_sections",
                "section_counts",
                "profile_name",
                "renderable",
            }
            or not isinstance(profile_changes_version_id, str)
            or profile_changes.get("parent_profile_version_id")
            != duplicate_skill_profile_version_id
            or profile_changes.get("version_number") != 11
            or profile_changes.get("created") is not True
            or profile_changes.get("operation_count") != 2
            or profile_changes.get("changed_sections") != ["projects", "skills"]
            or profile_changes.get("section_counts")
            != {"work": 0, "projects": 1, "education": 1, "skills": 1}
        ):
            raise SystemExit("The frozen Python sidecar could not apply profile changes")
        profile_changes_retry = request(
            "profile-changes-apply-retry",
            "profile.changes.apply",
            profile_changes_params,
        ).get("result")
        if profile_changes_retry != {**profile_changes, "created": False}:
            raise SystemExit("The frozen Python sidecar did not reuse a profile changes retry")
        changed_project_page = request(
            "profile-project-list-after-changes",
            "profile.projects.list",
            {"profile_version_id": profile_changes_version_id, "offset": 0},
        ).get("result")
        changed_skill_page = request(
            "profile-skills-list-after-changes",
            "profile.skills.list",
            {"profile_version_id": profile_changes_version_id, "offset": 0},
        ).get("result")
        if (
            not isinstance(changed_project_page, dict)
            or changed_project_page.get("items", [{}])[0].get("description")
            != "Packaged and batch-reviewed a private desktop workflow."
            or not isinstance(changed_skill_page, dict)
            or changed_skill_page.get("total_items") != 1
            or changed_skill_page.get("items", [{}])[0].get("keywords")
            != ["Python", "SQLite", "Rust"]
        ):
            raise SystemExit("The frozen Python sidecar did not apply one atomic profile batch")
        edited_sections_versions = request(
            "profile-versions-sections-edited",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        if (
            not isinstance(edited_sections_versions, dict)
            or edited_sections_versions.get("current_profile_version_id")
            != profile_changes_version_id
            or edited_sections_versions.get("total_items") != 11
            or edited_sections_versions.get("items", [{}])[0].get("source_kind")
            != "local_edit"
        ):
            raise SystemExit("The frozen Python sidecar returned invalid section-edit history")
        reset = request(
            "source-reset",
            "profile.sources.reset",
            {
                "expected_generation": 1,
                "request_id": str(uuid.uuid4()),
            },
        ).get("result")
        if (
            not isinstance(reset, dict)
            or reset.get("source_retention_generation") != 2
            or reset.get("retained_source_imports") != 0
            or reset.get("historical_source_imports") != 1
            or retained_blob.read_bytes() != canonical_bytes
        ):
            raise SystemExit("The frozen Python sidecar did not logically reset imports")

        scan_id = str(uuid.uuid4())
        staged_directory = Path(directory, "imports", "staging", scan_id)
        staged_directory.mkdir(parents=True)
        staged = staged_directory / "0000.docx"
        staged_bytes = _smoke_docx_bytes()
        staged.write_bytes(staged_bytes)
        if os.name != "nt":
            staged_directory.chmod(0o700)
            staged.chmod(0o600)
        preview = request(
            "source-preview",
            "profile.sources.preview",
            {
                "scan_id": scan_id,
                "sources": [
                    {
                        "ordinal": 0,
                        "managed_relative_path": (
                            f"imports/staging/{scan_id}/0000.docx"
                        ),
                        "display_name": "private-smoke-resume.docx",
                        "format": "docx",
                        "byte_size": len(staged_bytes),
                        "checksum_sha256": hashlib.sha256(staged_bytes).hexdigest(),
                    }
                ],
                "file_counts": {"discovered": 1, "staged": 1, "skipped": 0},
                "scan_issues": {},
            },
        )
        preview_result = preview.get("result")
        if (
            not isinstance(preview_result, dict)
            or preview_result.get("can_build") is not True
            or preview_result.get("review_candidate_count") != 2
        ):
            raise SystemExit(
                "The frozen Python sidecar could not build a source preview: "
                + _preview_smoke_diagnostics(preview_result)
            )
        if "private-smoke-resume" in json.dumps(preview_result):
            raise SystemExit("The frozen Python sidecar leaked source details in its preview")
        committed = request(
            "source-commit",
            "profile.sources.commit",
            {"scan_id": scan_id},
        )
        committed_result = committed.get("result")
        if (
            not isinstance(committed_result, dict)
            or committed_result.get("scan_id") != scan_id
            or committed_result.get("renderable") is not True
            or committed_result.get("review_item_count") != 2
            or committed_result.get("profile_counts", {}).get("work_entries") != 1
            or committed_result.get("profile_counts", {}).get("education_entries") != 1
            or committed_result.get("profile_counts", {}).get("skill_groups") != 1
        ):
            raise SystemExit("The frozen Python sidecar could not commit a source preview")

        committed_profile_version_id = committed_result.get("profile_version_id")
        quarantined_education = request(
            "review-quarantine-education",
            "profile.education.list",
            {"profile_version_id": committed_profile_version_id, "offset": 0},
        ).get("result")
        quarantined_skills = request(
            "review-quarantine-skills",
            "profile.skills.list",
            {"profile_version_id": committed_profile_version_id, "offset": 0},
        ).get("result")
        if (
            not isinstance(quarantined_education, dict)
            or quarantined_education.get("total_items") != 1
            or quarantined_education.get("items", [{}])[0].get("institution")
            != "Local Systems Institute"
            or "Review Inbox Institute" in json.dumps(quarantined_education)
            or not isinstance(quarantined_skills, dict)
            or quarantined_skills.get("total_items") != 1
            or quarantined_skills.get("items", [{}])[0].get("name") != "Engineering"
            or "Review queues" in json.dumps(quarantined_skills)
        ):
            raise SystemExit("The frozen Python sidecar applied quarantined review material")

        review_page = request(
            "review-inbox-list",
            "review.inbox.list",
            {"scope": "inbox", "offset": 0},
        ).get("result")
        review_items = review_page.get("items") if isinstance(review_page, dict) else None
        review_item_keys = {
            "id",
            "state",
            "state_revision",
            "candidate_kind",
            "operation_kind",
            "candidate",
            "title",
            "subtitle",
            "date_range",
            "summary",
            "highlights",
            "tags",
            "details",
            "changed_fields",
            "evidence",
            "is_previous_import_set",
            "created_at_ms",
        }
        review_evidence_keys = {
            "display_name",
            "source_format",
            "source_kind",
            "location_label",
            "excerpt",
        }
        if (
            not isinstance(review_page, dict)
            or set(review_page)
            != {
                "scope",
                "current_profile_version_id",
                "offset",
                "limit",
                "total_items",
                "next_offset",
                "counts",
                "items",
            }
            or review_page.get("scope") != "inbox"
            or review_page.get("current_profile_version_id")
            != committed_profile_version_id
            or review_page.get("offset") != 0
            or review_page.get("limit") != 10
            or review_page.get("total_items") != 2
            or review_page.get("next_offset") is not None
            or review_page.get("counts") != {"inbox": 2, "deferred": 0, "history": 0}
            or not isinstance(review_items, list)
            or len(review_items) != 2
            or {item.get("candidate_kind") for item in review_items} != {"education", "skills"}
            or any(
                not isinstance(item, dict)
                or set(item) != review_item_keys
                or item.get("state") != "inbox"
                or item.get("state_revision") != 0
                or item.get("operation_kind") != "add"
                or not isinstance(item.get("candidate"), dict)
                or item.get("is_previous_import_set") is not False
                or not isinstance(item.get("changed_fields"), list)
                or not item.get("changed_fields")
                or not isinstance(item.get("evidence"), list)
                or not item.get("evidence")
                or any(
                    not isinstance(evidence, dict)
                    or set(evidence) != review_evidence_keys
                    or evidence.get("display_name") != "private-smoke-resume.docx"
                    or evidence.get("source_format") != "docx"
                    or evidence.get("source_kind") != "resume"
                    or not isinstance(evidence.get("excerpt"), str)
                    or not evidence["excerpt"].strip()
                    or len(evidence["excerpt"]) > 360
                    for evidence in item["evidence"]
                )
                for item in review_items
            )
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid review inbox")
        review_by_kind = {item["candidate_kind"]: item for item in review_items}
        if (
            review_by_kind["education"].get("subtitle") != "Review Inbox Institute"
            or "Review Inbox Institute"
            not in " ".join(
                evidence["excerpt"] for evidence in review_by_kind["education"]["evidence"]
            )
            or review_by_kind["skills"].get("title") != "Core"
            or "Review queues"
            not in " ".join(
                evidence["excerpt"] for evidence in review_by_kind["skills"]["evidence"]
            )
        ):
            raise SystemExit("The frozen Python sidecar detached review items from their evidence")
        forbidden_review_fields = {
            "candidate_checksum_sha256",
            "previous_checksum_sha256",
            "profile_version_id",
            "import_id",
            "retention_generation",
            "source_id",
            "source_ordinal",
            "relative_path",
            "managed_relative_path",
            "extracted_text",
            "proposed",
            "previous",
        }
        if any(forbidden_review_fields.intersection(item) for item in review_items):
            raise SystemExit("The frozen Python sidecar leaked private review fields")

        review_versions_before = request(
            "review-profile-versions-before",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        review_version_count = (
            review_versions_before.get("total_items")
            if isinstance(review_versions_before, dict)
            else None
        )
        if not isinstance(review_version_count, int) or review_version_count < 1:
            raise SystemExit("The frozen Python sidecar lost review profile history")

        transition_item = review_items[0]
        transition_receipt_keys = {
            "request_id",
            "review_item_id",
            "action",
            "previous_state",
            "state",
            "state_revision",
            "created_at_ms",
            "created",
        }
        defer_params = {
            "review_item_id": transition_item["id"],
            "request_id": str(uuid.uuid4()),
            "action": "defer",
            "expected_state": "inbox",
            "expected_state_revision": 0,
        }
        deferred = request(
            "review-item-defer",
            "review.inbox.transition",
            defer_params,
        ).get("result")
        if (
            not isinstance(deferred, dict)
            or set(deferred) != transition_receipt_keys
            or deferred.get("review_item_id") != transition_item["id"]
            or deferred.get("previous_state") != "inbox"
            or deferred.get("state") != "deferred"
            or deferred.get("state_revision") != 1
            or deferred.get("created") is not True
        ):
            raise SystemExit("The frozen Python sidecar could not defer a review item")
        deferred_retry = request(
            "review-item-defer-retry",
            "review.inbox.transition",
            defer_params,
        ).get("result")
        if deferred_retry != {**deferred, "created": False}:
            raise SystemExit("The frozen Python sidecar did not reuse a review transition retry")
        deferred_page = request(
            "review-deferred-list",
            "review.inbox.list",
            {"scope": "deferred", "offset": 0},
        ).get("result")
        if (
            not isinstance(deferred_page, dict)
            or deferred_page.get("total_items") != 1
            or deferred_page.get("counts") != {"inbox": 1, "deferred": 1, "history": 0}
            or deferred_page.get("items", [{}])[0].get("id") != transition_item["id"]
            or deferred_page.get("items", [{}])[0].get("state_revision") != 1
        ):
            raise SystemExit("The frozen Python sidecar did not persist a deferred review item")

        reopened = request(
            "review-item-reopen",
            "review.inbox.transition",
            {
                "review_item_id": transition_item["id"],
                "request_id": str(uuid.uuid4()),
                "action": "reopen",
                "expected_state": "deferred",
                "expected_state_revision": 1,
            },
        ).get("result")
        if (
            not isinstance(reopened, dict)
            or reopened.get("previous_state") != "deferred"
            or reopened.get("state") != "inbox"
            or reopened.get("state_revision") != 2
            or reopened.get("created") is not True
        ):
            raise SystemExit("The frozen Python sidecar could not reopen a review item")
        rejected = request(
            "review-item-reject",
            "review.inbox.transition",
            {
                "review_item_id": transition_item["id"],
                "request_id": str(uuid.uuid4()),
                "action": "reject",
                "expected_state": "inbox",
                "expected_state_revision": 2,
            },
        ).get("result")
        if (
            not isinstance(rejected, dict)
            or rejected.get("previous_state") != "inbox"
            or rejected.get("state") != "rejected"
            or rejected.get("state_revision") != 3
            or rejected.get("created") is not True
        ):
            raise SystemExit("The frozen Python sidecar could not reject a review item")
        review_history = request(
            "review-history-list",
            "review.inbox.list",
            {"scope": "history", "offset": 0},
        ).get("result")
        if (
            not isinstance(review_history, dict)
            or review_history.get("total_items") != 1
            or review_history.get("counts") != {"inbox": 1, "deferred": 0, "history": 1}
            or review_history.get("items", [{}])[0].get("id") != transition_item["id"]
            or review_history.get("items", [{}])[0].get("state") != "rejected"
        ):
            raise SystemExit("The frozen Python sidecar did not retain review history")
        review_versions_after = request(
            "review-profile-versions-after",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        if (
            not isinstance(review_versions_after, dict)
            or review_versions_after.get("total_items") != review_version_count
            or review_versions_after.get("current_profile_version_id")
            != committed_profile_version_id
        ):
            raise SystemExit("A review inbox action changed the saved profile")

        apply_item = next(
            item for item in review_items if item["id"] != transition_item["id"]
        )
        apply_candidate = dict(apply_item["candidate"])
        if apply_item["candidate_kind"] == "skills":
            apply_candidate["keywords"] = [
                *apply_candidate["keywords"],
                "Applied locally",
            ]
        else:
            apply_candidate["score"] = "Reviewed locally"
        apply_request_id = str(uuid.uuid4())
        apply_params = {
            "review_item_id": apply_item["id"],
            "request_id": apply_request_id,
            "expected_state": "inbox",
            "expected_state_revision": 0,
            "expected_parent_profile_version_id": committed_profile_version_id,
            "candidate": apply_candidate,
        }
        applied = request(
            "review-item-apply",
            "review.inbox.apply",
            apply_params,
        ).get("result")
        apply_receipt_keys = {
            "request_id",
            "review_item_id",
            "previous_state",
            "state",
            "state_revision",
            "profile_version_id",
            "parent_profile_version_id",
            "version_number",
            "candidate_kind",
            "operation_kind",
            "entry_index",
            "changed_fields",
            "created_at_ms",
            "created",
        }
        if (
            not isinstance(applied, dict)
            or set(applied) != apply_receipt_keys
            or applied.get("request_id") != apply_request_id
            or applied.get("review_item_id") != apply_item["id"]
            or applied.get("previous_state") != "inbox"
            or applied.get("state") != "applied"
            or applied.get("state_revision") != 1
            or applied.get("parent_profile_version_id")
            != committed_profile_version_id
            or applied.get("candidate_kind") != apply_item["candidate_kind"]
            or applied.get("operation_kind") != "add"
            or not isinstance(applied.get("changed_fields"), list)
            or not applied["changed_fields"]
            or applied.get("created") is not True
        ):
            raise SystemExit("The frozen Python sidecar could not apply a review item")
        applied_profile_version_id = applied.get("profile_version_id")
        applied_retry = request(
            "review-item-apply-retry",
            "review.inbox.apply",
            apply_params,
        ).get("result")
        if applied_retry != {**applied, "created": False}:
            raise SystemExit("The frozen Python sidecar did not reuse a review apply retry")
        applied_history = request(
            "review-applied-history",
            "review.inbox.list",
            {"scope": "history", "offset": 0},
        ).get("result")
        applied_history_items = (
            applied_history.get("items") if isinstance(applied_history, dict) else None
        )
        applied_history_item = next(
            (
                item
                for item in applied_history_items or []
                if item.get("id") == apply_item["id"]
            ),
            None,
        )
        if (
            not isinstance(applied_history, dict)
            or applied_history.get("current_profile_version_id")
            != applied_profile_version_id
            or applied_history.get("counts")
            != {"inbox": 0, "deferred": 0, "history": 2}
            or not isinstance(applied_history_item, dict)
            or applied_history_item.get("state") != "applied"
            or applied_history_item.get("candidate") != apply_candidate
        ):
            raise SystemExit("The frozen Python sidecar did not retain applied review history")
        applied_versions = request(
            "review-profile-versions-applied",
            "profile.versions.list",
            {"offset": 0},
        ).get("result")
        if (
            not isinstance(applied_versions, dict)
            or applied_versions.get("total_items") != review_version_count + 1
            or applied_versions.get("current_profile_version_id")
            != applied_profile_version_id
        ):
            raise SystemExit("A review apply did not append exactly one profile version")

        retained_status = request("retained-status", "system.status", {}).get("result")
        if (
            not isinstance(retained_status, dict)
            or retained_status.get("source_retention_generation") != 2
            or retained_status.get("retained_source_files") != 1
            or retained_status.get("retained_source_imports") != 1
            or retained_status.get("historical_source_files") != 1
            or retained_status.get("historical_source_imports") != 1
            or retained_status.get("review_inbox_items") != 0
            or retained_status.get("review_deferred_items") != 0
            or retained_status.get("review_history_items") != 2
        ):
            raise SystemExit("The frozen Python sidecar returned invalid retained-source totals")
        retained_list = request(
            "retained-list",
            "profile.sources.list",
            {"scope": "all", "limit": 25, "offset": 0},
        ).get("result")
        if (
            not isinstance(retained_list, dict)
            or retained_list.get("current_generation") != 2
            or retained_list.get("total_entries") != 2
            or retained_list.get("total_unique_files") != 2
            or retained_list.get("total_imports") != 2
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid retained-source list")
        retained_items = retained_list.get("items")
        if not isinstance(retained_items, list) or len(retained_items) != 2:
            raise SystemExit("The frozen Python sidecar omitted retained import entries")
        retained_names = {
            item.get("display_name")
            for item in retained_items
            if isinstance(item, dict)
        }
        if retained_names != {
            "private-smoke-canonical.json",
            "private-smoke-resume.docx",
        }:
            raise SystemExit("The frozen Python sidecar returned incorrect import aliases")
        forbidden_material_fields = {
            "checksum_sha256",
            "source_id",
            "relative_path",
            "managed_relative_path",
            "extracted_text",
        }
        if any(
            not isinstance(item, dict)
            or forbidden_material_fields.intersection(item)
            for item in retained_items
        ):
            raise SystemExit("The frozen Python sidecar leaked private source fields")

        review_source_reset = request(
            "review-source-reset",
            "profile.sources.reset",
            {
                "expected_generation": 2,
                "request_id": str(uuid.uuid4()),
            },
        ).get("result")
        if (
            not isinstance(review_source_reset, dict)
            or review_source_reset.get("source_retention_generation") != 3
            or review_source_reset.get("retained_source_imports") != 0
            or review_source_reset.get("historical_source_imports") != 2
        ):
            raise SystemExit("The frozen Python sidecar did not archive a review import set")
        archived_review_status = request(
            "review-archived-status",
            "system.status",
            {},
        ).get("result")
        archived_review_history = request(
            "review-archived-history",
            "review.inbox.list",
            {"scope": "history", "offset": 0},
        ).get("result")
        archived_review_items = (
            archived_review_history.get("items")
            if isinstance(archived_review_history, dict)
            else None
        )
        if (
            not isinstance(archived_review_status, dict)
            or archived_review_status.get("review_inbox_items") != 0
            or archived_review_status.get("review_deferred_items") != 0
            or archived_review_status.get("review_history_items") != 2
            or not isinstance(archived_review_history, dict)
            or archived_review_history.get("counts")
            != {"inbox": 0, "deferred": 0, "history": 2}
            or not isinstance(archived_review_items, list)
            or len(archived_review_items) != 2
            or any(item.get("is_previous_import_set") is not True for item in archived_review_items)
        ):
            raise SystemExit("The frozen Python sidecar did not preserve prior-set review history")

        recovery_scan_id = str(uuid.uuid4())
        recovery_directory = Path(directory, "imports", "staging", recovery_scan_id)
        recovery_directory.mkdir(parents=True)
        recovery_path = recovery_directory / "0000.txt"
        recovery_bytes = b"Name: Recovery Preview\nSkill: Python\n"
        recovery_path.write_bytes(recovery_bytes)
        if os.name != "nt":
            recovery_directory.chmod(0o700)
            recovery_path.chmod(0o600)
        recovery_preview = request(
            "recovery-preview",
            "profile.sources.preview",
            {
                "scan_id": recovery_scan_id,
                "sources": [
                    {
                        "ordinal": 0,
                        "managed_relative_path": (
                            f"imports/staging/{recovery_scan_id}/0000.txt"
                        ),
                        "display_name": "recovery-preview.txt",
                        "format": "txt",
                        "byte_size": len(recovery_bytes),
                        "checksum_sha256": hashlib.sha256(recovery_bytes).hexdigest(),
                    }
                ],
                "file_counts": {"discovered": 1, "staged": 1, "skipped": 0},
                "scan_issues": {},
            },
        ).get("result")
        if not isinstance(recovery_preview, dict):
            raise SystemExit("The frozen Python sidecar could not create a recovery preview")
        discarded = request(
            "discard-all-previews",
            "profile.sources.discard_all",
            {},
        ).get("result")
        if (
            discarded != {"discarded_previews": 1}
            or recovery_directory.exists()
        ):
            raise SystemExit("The frozen Python sidecar could not discard unfinished previews")
        opportunity = request(
            "opportunity-save-match",
            "opportunities.save_match",
            {
                "title": "Verification Engineer",
                "company": "Local Engine",
                "location": "Local",
                "source_url": "https://example.test/jobs/sidecar-smoke",
                "apply_url": "https://example.test/apply/sidecar-smoke",
                "description": (
                    "Build deterministic offline document exports with local Python systems."
                ),
            },
        )
        opportunity_result = opportunity.get("result")
        if not isinstance(opportunity_result, dict):
            raise SystemExit("The frozen Python sidecar returned no opportunity detail")
        opportunity_metadata = opportunity_result.get("opportunity")
        opportunity_match = opportunity_result.get("match")
        if (
            not isinstance(opportunity_metadata, dict)
            or not isinstance(opportunity_match, dict)
            or opportunity_metadata.get("status") != "ready"
            or opportunity_metadata.get("tracker_stage") != "tracked"
            or opportunity_match.get("contract") != "deterministic-local-v1"
        ):
            raise SystemExit("The frozen Python sidecar could not match an opportunity")
        opportunity_id = opportunity_metadata.get("id")
        if not isinstance(opportunity_id, str):
            raise SystemExit("The frozen Python sidecar returned no opportunity ID")
        tracker_updated_at_ms = opportunity_metadata.get("tracker_updated_at_ms")
        if not isinstance(tracker_updated_at_ms, int) or tracker_updated_at_ms <= 0:
            raise SystemExit("The frozen Python sidecar returned no tracker action time")
        updated_opportunity = request(
            "opportunity-update",
            "opportunities.update",
            {
                "opportunity_id": opportunity_id,
                "expected_tracker_updated_at_ms": tracker_updated_at_ms,
                "tracker_stage": "to_apply",
                "notes": "Review the locally matched evidence before applying.",
            },
        ).get("result")
        if (
            not isinstance(updated_opportunity, dict)
            or not isinstance(updated_opportunity.get("opportunity"), dict)
            or updated_opportunity["opportunity"].get("tracker_stage") != "to_apply"
            or updated_opportunity["opportunity"].get("notes")
            != "Review the locally matched evidence before applying."
        ):
            raise SystemExit("The frozen Python sidecar could not update tracker metadata")
        listed = request(
            "opportunity-list",
            "opportunities.list",
            {
                "query": "Local Engine",
                "offset": 0,
                "stage": "to_apply",
                "sort": "last_action",
            },
        ).get("result")
        if (
            not isinstance(listed, dict)
            or listed.get("total") != 1
            or "description" in json.dumps(listed)
            or "evidence" in json.dumps(listed)
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid opportunity list")
        for request_id, method in (
            ("opportunity-get", "opportunities.get"),
            ("opportunity-rematch", "opportunities.rematch"),
        ):
            detail = request(
                request_id,
                method,
                {"opportunity_id": opportunity_id},
            ).get("result")
            if (
                not isinstance(detail, dict)
                or not isinstance(detail.get("opportunity"), dict)
                or detail["opportunity"].get("id") != opportunity_id
            ):
                raise SystemExit(
                    f"The frozen Python sidecar failed its {method} opportunity smoke test"
                )
        tailoring_prepared = request(
            "tailoring-prepare",
            "tailoring.prepare",
            {
                "opportunity_id": opportunity_id,
                "provider_kind": "openai_compatible_local",
                "chat_model_id": "smoke-chat-model",
                "embedding_model_id": "smoke-embedding-model",
            },
        ).get("result")
        if not isinstance(tailoring_prepared, dict):
            raise SystemExit("The frozen Python sidecar could not prepare tailoring")
        attempt_id = tailoring_prepared.get("attempt_id")
        embedding_input = tailoring_prepared.get("embedding_input")
        if (
            not isinstance(attempt_id, str)
            or not isinstance(embedding_input, list)
            or not embedding_input
            or not all(isinstance(value, str) and value for value in embedding_input)
        ):
            raise SystemExit("The frozen Python sidecar returned invalid embedding input")
        embeddings = [
            [1.0, float(index % 3), 0.25, 0.5]
            for index in range(len(embedding_input))
        ]
        tailoring_armed = request(
            "tailoring-arm",
            "tailoring.arm",
            {"attempt_id": attempt_id, "embeddings": embeddings},
        ).get("result")
        if (
            not isinstance(tailoring_armed, dict)
            or tailoring_armed.get("attempt_id") != attempt_id
            or tailoring_armed.get("chat_model_id") != "smoke-chat-model"
            or not isinstance(tailoring_armed.get("messages"), list)
            or not isinstance(tailoring_armed.get("response_format"), dict)
        ):
            raise SystemExit("The frozen Python sidecar could not arm tailoring")
        tailored = request(
            "tailoring-finalize",
            "tailoring.finalize",
            {
                "attempt_id": attempt_id,
                "response_text": json.dumps(
                    {
                        "basics": {
                            "label": "Verification Engineer",
                            "summary": (
                                "Builds deterministic offline document exports "
                                "with local Python systems."
                            ),
                        },
                        "work": [],
                    },
                    separators=(",", ":"),
                ),
            },
        ).get("result")
        if (
            not isinstance(tailored, dict)
            or tailored.get("attempt_id") != attempt_id
            or not isinstance(tailored.get("resume"), dict)
            or tailored["resume"].get("basics", {}).get("label")
            != "Verification Engineer"
            or tailored.get("models", {}).get("embedding_model_id")
            != "smoke-embedding-model"
        ):
            raise SystemExit("The frozen Python sidecar could not finalize tailoring")
        latest_tailored = request(
            "tailoring-latest",
            "tailoring.latest",
            {"opportunity_id": opportunity_id},
        ).get("result")
        if (
            not isinstance(latest_tailored, dict)
            or latest_tailored.get("id") != tailored.get("id")
            or latest_tailored.get("input_fingerprint")
            != tailored.get("input_fingerprint")
            or latest_tailored.get("snapshot") != tailored.get("snapshot")
        ):
            raise SystemExit("The frozen Python sidecar could not reopen tailoring")

        revision_list = request(
            "tailoring-revisions-empty",
            "tailoring.revisions.list",
            {"draft_id": tailored.get("id")},
        ).get("result")
        if (
            not isinstance(revision_list, dict)
            or revision_list.get("draft_id") != tailored.get("id")
            or not isinstance(revision_list.get("base_resume_checksum_sha256"), str)
            or len(revision_list["base_resume_checksum_sha256"]) != 64
            or revision_list.get("revisions") != []
            or revision_list.get("truncated") is not False
        ):
            raise SystemExit("The frozen Python sidecar returned invalid empty revision history")
        revision_request_id = "5bda16f3-1a2d-49de-a685-1bb2cc891e89"
        revision_params = {
            "request_id": revision_request_id,
            "draft_id": tailored.get("id"),
            "expected_parent_revision_id": None,
            "expected_parent_resume_checksum_sha256": revision_list[
                "base_resume_checksum_sha256"
            ],
            "edits": {
                "basics": {"label": "Senior Verification Engineer"},
                "work": [],
            },
        }
        revision = request(
            "tailoring-revision-create",
            "tailoring.revisions.create",
            revision_params,
        ).get("result")
        if (
            not isinstance(revision, dict)
            or revision.get("draft_id") != tailored.get("id")
            or revision.get("parent_revision_id") is not None
            or revision.get("revision_number") != 1
            or revision.get("author_kind") != "user"
            or revision.get("parent_resume_checksum_sha256")
            != revision_list["base_resume_checksum_sha256"]
            or revision.get("changed_fields") != ["/basics/label"]
            or not isinstance(revision.get("resume"), dict)
            or revision["resume"].get("basics", {}).get("label")
            != "Senior Verification Engineer"
        ):
            raise SystemExit("The frozen Python sidecar could not create a tailored revision")
        revision_id = revision.get("id")
        if not isinstance(revision_id, str):
            raise SystemExit("The frozen Python sidecar returned no revision identity")
        revision_retry = request(
            "tailoring-revision-create-retry",
            "tailoring.revisions.create",
            revision_params,
        ).get("result")
        if not isinstance(revision_retry, dict) or revision_retry.get("id") != revision_id:
            raise SystemExit("The frozen Python sidecar did not reuse an exact revision retry")
        revision_list = request(
            "tailoring-revisions-list",
            "tailoring.revisions.list",
            {"draft_id": tailored.get("id")},
        ).get("result")
        revision_summaries = (
            revision_list.get("revisions") if isinstance(revision_list, dict) else None
        )
        if (
            not isinstance(revision_summaries, list)
            or len(revision_summaries) != 1
            or revision_summaries[0].get("id") != revision_id
            or revision_summaries[0].get("changed_field_count") != 1
            or "changed_fields" in revision_summaries[0]
            or "resume" in revision_summaries[0]
        ):
            raise SystemExit("The frozen Python sidecar returned invalid revision summaries")
        reopened_revision = request(
            "tailoring-revision-get",
            "tailoring.revisions.get",
            {"revision_id": revision_id},
        ).get("result")
        if (
            not isinstance(reopened_revision, dict)
            or reopened_revision.get("id") != revision_id
            or reopened_revision.get("resume_checksum_sha256")
            != revision.get("resume_checksum_sha256")
            or reopened_revision.get("changed_fields") != ["/basics/label"]
            or reopened_revision.get("resume") != revision.get("resume")
        ):
            raise SystemExit("The frozen Python sidecar could not reopen a tailored revision")

        second_revision = request(
            "tailoring-revision-create-second",
            "tailoring.revisions.create",
            {
                "request_id": "20a39b85-090b-4f40-9061-9c7259690adc",
                "draft_id": tailored.get("id"),
                "expected_parent_revision_id": revision_id,
                "expected_parent_resume_checksum_sha256": revision.get(
                    "resume_checksum_sha256"
                ),
                "edits": {
                    "basics": {"label": "Principal Verification Engineer"},
                    "work": [],
                },
            },
        ).get("result")
        if (
            not isinstance(second_revision, dict)
            or second_revision.get("draft_id") != tailored.get("id")
            or second_revision.get("parent_revision_id") != revision_id
            or second_revision.get("revision_number") != 2
            or second_revision.get("parent_resume_checksum_sha256")
            != revision.get("resume_checksum_sha256")
            or second_revision.get("resume", {}).get("basics", {}).get("label")
            != "Principal Verification Engineer"
        ):
            raise SystemExit("The frozen Python sidecar could not append a second revision")
        two_revision_list = request(
            "tailoring-revisions-list-two",
            "tailoring.revisions.list",
            {"draft_id": tailored.get("id")},
        ).get("result")
        two_revision_summaries = (
            two_revision_list.get("revisions")
            if isinstance(two_revision_list, dict)
            else None
        )
        if (
            not isinstance(two_revision_summaries, list)
            or [item.get("id") for item in two_revision_summaries]
            != [revision_id, second_revision.get("id")]
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid two-revision chain")

        revision_artifact_ids: dict[str, str] = {}
        revision_artifact_checksums: dict[str, str] = {}
        for output_format, signature in (("docx", b"PK"), ("pdf", b"%PDF-")):
            revision_export = request(
                f"tailoring-revision-export-{output_format}",
                "tailoring.revisions.export",
                {"revision_id": revision_id, "format": output_format},
            ).get("result")
            if (
                not isinstance(revision_export, dict)
                or revision_export.get("revision_id") != revision_id
                or revision_export.get("draft_id") != tailored.get("id")
                or revision_export.get("opportunity_id") != opportunity_id
                or revision_export.get("format") != output_format
                or "Revision-1" not in str(revision_export.get("suggested_filename"))
            ):
                raise SystemExit(
                    "The frozen Python sidecar returned invalid revision export metadata"
                )
            relative_path = revision_export.get("relative_path")
            artifact_id = revision_export.get("artifact_id")
            checksum = revision_export.get("checksum_sha256")
            if (
                not isinstance(relative_path, str)
                or not isinstance(artifact_id, str)
                or not isinstance(checksum, str)
            ):
                raise SystemExit(
                    "The frozen Python sidecar returned invalid revision artifact identity"
                )
            artifact = Path(directory, relative_path)
            artifact_bytes = artifact.read_bytes() if artifact.is_file() else b""
            if (
                not artifact_bytes.startswith(signature)
                or len(artifact_bytes) != revision_export.get("byte_size")
                or hashlib.sha256(artifact_bytes).hexdigest() != checksum
            ):
                raise SystemExit(
                    f"The frozen Python sidecar produced an invalid revised {output_format} artifact"
                )
            if output_format == "docx":
                with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as archive:
                    document_xml = archive.read("word/document.xml")
                if (
                    b"Senior Verification Engineer" not in document_xml
                    or b"Principal Verification Engineer" in document_xml
                ):
                    raise SystemExit(
                        "The frozen Python sidecar did not export the exact older revision"
                    )
            revision_artifact_ids[output_format] = artifact_id
            revision_artifact_checksums[output_format] = checksum
        revision_export_retry = request(
            "tailoring-revision-export-pdf-retry",
            "tailoring.revisions.export",
            {"revision_id": revision_id, "format": "pdf"},
        ).get("result")
        if (
            not isinstance(revision_export_retry, dict)
            or revision_export_retry.get("artifact_id")
            != revision_artifact_ids["pdf"]
        ):
            raise SystemExit("The frozen Python sidecar did not reuse a revision artifact")

        tailored_artifact_ids: dict[str, str] = {}
        tailored_artifact_checksums: dict[str, str] = {}
        for output_format, signature in (("docx", b"PK"), ("pdf", b"%PDF-")):
            tailored_export = request(
                f"tailoring-export-{output_format}",
                "tailoring.export",
                {"draft_id": tailored.get("id"), "format": output_format},
            ).get("result")
            if (
                not isinstance(tailored_export, dict)
                or tailored_export.get("draft_id") != tailored.get("id")
                or tailored_export.get("opportunity_id") != opportunity_id
                or tailored_export.get("format") != output_format
            ):
                raise SystemExit("The frozen Python sidecar returned invalid tailored export metadata")
            relative_path = tailored_export.get("relative_path")
            artifact_id = tailored_export.get("artifact_id")
            checksum = tailored_export.get("checksum_sha256")
            if (
                not isinstance(relative_path, str)
                or not isinstance(artifact_id, str)
                or not isinstance(checksum, str)
            ):
                raise SystemExit("The frozen Python sidecar returned invalid tailored artifact identity")
            artifact = Path(directory, relative_path)
            artifact_bytes = artifact.read_bytes() if artifact.is_file() else b""
            if (
                not artifact_bytes.startswith(signature)
                or len(artifact_bytes) != tailored_export.get("byte_size")
                or hashlib.sha256(artifact_bytes).hexdigest() != checksum
            ):
                raise SystemExit(
                    f"The frozen Python sidecar produced an invalid tailored {output_format} artifact"
                )
            tailored_artifact_ids[output_format] = artifact_id
            tailored_artifact_checksums[output_format] = checksum
            if (
                artifact_id == revision_artifact_ids[output_format]
                or tailored_artifact_checksums[output_format]
                == revision_artifact_checksums[output_format]
            ):
                raise SystemExit(
                    "Original tailored and user-revised artifacts collided"
                )
        tailored_retry = request(
            "tailoring-export-pdf-retry",
            "tailoring.export",
            {"draft_id": tailored.get("id"), "format": "pdf"},
        ).get("result")
        if (
            not isinstance(tailored_retry, dict)
            or tailored_retry.get("artifact_id") != tailored_artifact_ids["pdf"]
        ):
            raise SystemExit("The frozen Python sidecar did not reuse a tailored artifact")
        for output_format, signature in (("docx", b"PK"), ("pdf", b"%PDF-")):
            response = request(
                f"export-{output_format}",
                "resume.export",
                {"format": output_format},
            )
            result = response.get("result")
            if (
                not isinstance(result, dict)
                or result.get("profile_version_id")
                != applied_profile_version_id
                or result.get("media_type")
                != (
                    "application/pdf"
                    if output_format == "pdf"
                    else (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    )
                )
            ):
                raise SystemExit("The frozen Python sidecar returned no artifact metadata")
            relative_path = result.get("relative_path")
            if not isinstance(relative_path, str):
                raise SystemExit("The frozen Python sidecar returned an invalid artifact path")
            artifact = Path(directory, relative_path)
            if not artifact.is_file() or not artifact.read_bytes().startswith(signature):
                raise SystemExit(
                    f"The frozen Python sidecar produced an invalid {output_format} artifact"
                )
            if result.get("artifact_id") == tailored_artifact_ids[output_format]:
                raise SystemExit("Baseline and tailored artifacts collided")
            if result.get("artifact_id") == revision_artifact_ids[output_format]:
                raise SystemExit("Baseline and revised artifacts collided")

        pinned_baseline = request(
            "export-pinned-profile-version",
            "resume.export",
            {
                "format": "docx",
                "profile_version_id": imported.get("profile_version_id"),
            },
        ).get("result")
        if (
            not isinstance(pinned_baseline, dict)
            or pinned_baseline.get("profile_version_id")
            != imported.get("profile_version_id")
            or pinned_baseline.get("format") != "docx"
        ):
            raise SystemExit(
                "The frozen Python sidecar did not pin baseline export to profile review"
            )

        provider_sentinel = Path(directory, "local-provider.json")
        provider_sentinel_bytes = b'{"cvgnome_smoke":"preserve-provider-settings"}\n'
        provider_sentinel.write_bytes(provider_sentinel_bytes)
        if os.name != "nt":
            provider_sentinel.chmod(0o600)
        spend_sentinel = Path(directory, "provider-spend.json")
        spend_sentinel_bytes = b'{"cvgnome_smoke":"preserve-provider-spend"}\n'
        spend_sentinel.write_bytes(spend_sentinel_bytes)
        if os.name != "nt":
            spend_sentinel.chmod(0o600)
        before_reset = request("workspace-reset-status", "system.status", {}).get("result")
        if (
            not isinstance(before_reset, dict)
            or before_reset.get("profile_versions", 0) < 1
            or before_reset.get("opportunities", 0) < 1
            or before_reset.get("artifacts", 0) < 1
            or before_reset.get("source_imports", 0) < 1
            or before_reset.get("source_previews") != 0
            or before_reset.get("review_history_items", 0) < 2
        ):
            raise SystemExit("The frozen Python sidecar had no populated workspace to reset")
        reset_request_id = str(uuid.uuid4())
        reset_params = {
            "request_id": reset_request_id,
            "expected": {
                field: before_reset[field]
                for field in (
                    "profile_versions",
                    "opportunities",
                    "artifacts",
                    "source_imports",
                    "source_previews",
                    "source_retention_generation",
                    "review_inbox_items",
                    "review_deferred_items",
                    "review_history_items",
                    "source_review_inbox_items",
                    "source_review_deferred_items",
                    "source_review_history_items",
                    "memory_count",
                )
            },
        }
        reset_result = request(
            "workspace-reset",
            "workspace.reset",
            reset_params,
        ).get("result")
        if (
            not isinstance(reset_result, dict)
            or reset_result.get("request_id") != reset_request_id
            or reset_result.get("created") is not True
            or reset_result.get("schema_version") != 19
            or reset_result.get("memory_count") != 0
            or reset_result.get("source_retention_generation") != 1
            or any(
                reset_result.get(field) != 0
                for field in (
                    "profile_versions",
                    "opportunities",
                    "artifacts",
                    "source_imports",
                    "source_previews",
                    "review_inbox_items",
                    "review_deferred_items",
                    "review_history_items",
                    "source_review_inbox_items",
                    "source_review_deferred_items",
                    "source_review_history_items",
                )
            )
        ):
            raise SystemExit("The frozen Python sidecar returned an invalid workspace reset receipt")
        reset_retry = request(
            "workspace-reset-retry",
            "workspace.reset",
            reset_params,
        ).get("result")
        if (
            not isinstance(reset_retry, dict)
            or reset_retry.get("created") is not False
            or {key: value for key, value in reset_retry.items() if key != "created"}
            != {key: value for key, value in reset_result.items() if key != "created"}
        ):
            raise SystemExit("The frozen Python sidecar did not idempotently retry workspace reset")
        after_reset = request("workspace-reset-fresh-status", "system.status", {}).get("result")
        if (
            not isinstance(after_reset, dict)
            or after_reset.get("schema_version") != 19
            or after_reset.get("memory_count") != 0
            or after_reset.get("source_retention_generation") != 1
            or after_reset.get("latest_profile_name") is not None
            or after_reset.get("latest_profile_renderable") is not None
            or any(
                after_reset.get(field) != 0
                for field in (
                    "profile_versions",
                    "opportunities",
                    "artifacts",
                    "pending_jobs",
                    "source_snapshots",
                    "source_imports",
                    "source_previews",
                    "review_inbox_items",
                    "review_deferred_items",
                    "review_history_items",
                    "source_review_inbox_items",
                    "source_review_deferred_items",
                    "source_review_history_items",
                    "retained_source_files",
                    "retained_source_bytes",
                    "retained_source_imports",
                    "historical_source_files",
                    "historical_source_imports",
                )
            )
            or provider_sentinel.read_bytes() != provider_sentinel_bytes
            or spend_sentinel.read_bytes() != spend_sentinel_bytes
            or any(Path(directory, name).exists() for name in ("sources", "imports", "artifacts"))
        ):
            raise SystemExit("The frozen Python sidecar did not reopen a pristine workspace")

        manual_request_id = str(uuid.uuid4())
        manual_params = {
            "expected_parent_profile_version_id": None,
            "request_id": manual_request_id,
            "patch": {
                "name": "CVGnome Manual Smoke Test",
                "headline": "Local-first profile",
                "summary": "Builds and verifies an entirely local career profile.",
            },
        }
        manual_start = request(
            "manual-profile-start",
            "profile.start.manual",
            manual_params,
        ).get("result")
        if (
            not isinstance(manual_start, dict)
            or set(manual_start) != {
                "request_id",
                "profile_version_id",
                "parent_profile_version_id",
                "version_number",
                "created_at_ms",
                "created",
                "changed_fields",
                "profile_name",
                "headline",
                "renderable",
            }
            or manual_start.get("request_id") != manual_request_id
            or manual_start.get("created") is not True
            or manual_start.get("parent_profile_version_id") is not None
            or manual_start.get("version_number") != 1
            or manual_start.get("profile_name") != "CVGnome Manual Smoke Test"
            or manual_start.get("headline") != "Local-first profile"
            or manual_start.get("renderable") is not True
            or manual_start.get("changed_fields") != ["name", "headline", "summary"]
            or not isinstance(manual_start.get("profile_version_id"), str)
        ):
            raise SystemExit("The frozen Python sidecar could not create a manual first profile")
        manual_retry = request(
            "manual-profile-start-retry",
            "profile.start.manual",
            manual_params,
        ).get("result")
        if (
            not isinstance(manual_retry, dict)
            or manual_retry.get("created") is not False
            or {key: value for key, value in manual_retry.items() if key != "created"}
            != {key: value for key, value in manual_start.items() if key != "created"}
        ):
            raise SystemExit("The frozen Python sidecar did not idempotently retry manual start")
        manual_review = request(
            "manual-profile-review",
            "profile.review",
            {},
        ).get("result")
        manual_status = request(
            "manual-profile-status",
            "system.status",
            {},
        ).get("result")
        if (
            not isinstance(manual_review, dict)
            or manual_review.get("profile_version_id")
            != manual_start.get("profile_version_id")
            or manual_review.get("name") != "CVGnome Manual Smoke Test"
            or manual_review.get("source_kind") != "manual_start"
            or not isinstance(manual_status, dict)
            or manual_status.get("profile_versions") != 1
            or manual_status.get("latest_profile_name") != "CVGnome Manual Smoke Test"
            or manual_status.get("source_imports") != 0
            or manual_status.get("retained_source_imports") != 0
            or manual_status.get("source_previews") != 0
            or manual_status.get("review_inbox_items") != 0
            or manual_status.get("review_deferred_items") != 0
            or manual_status.get("review_history_items") != 0
            or any(Path(directory, name).exists() for name in ("sources", "imports", "artifacts"))
        ):
            raise SystemExit("The frozen Python sidecar did not reopen the manual first profile")

        manual_work_request_id = str(uuid.uuid4())
        manual_work = request(
            "manual-profile-work-add",
            "profile.work.update",
            {
                "expected_parent_profile_version_id": manual_start.get(
                    "profile_version_id"
                ),
                "request_id": manual_work_request_id,
                "operation": {
                    "kind": "add",
                    "entry": {
                        "name": "Local Workspace",
                        "position": "Owner",
                        "url": None,
                        "start_date": "2026",
                        "end_date": None,
                        "summary": "Built a private, customer-operated career workspace.",
                        "highlights": ["Kept profile data and operating costs local."],
                        "location": None,
                    },
                },
            },
        ).get("result")
        manual_work_version_id = (
            manual_work.get("profile_version_id")
            if isinstance(manual_work, dict)
            else None
        )
        if (
            not isinstance(manual_work, dict)
            or set(manual_work) != work_receipt_keys
            or not isinstance(manual_work_version_id, str)
            or manual_work.get("parent_profile_version_id")
            != manual_start.get("profile_version_id")
            or manual_work.get("version_number") != 2
            or manual_work.get("created") is not True
            or manual_work.get("operation") != "add"
            or manual_work.get("entry_index") != 0
            or manual_work.get("profile_name") != "CVGnome Manual Smoke Test"
            or manual_work.get("work_entries") != 1
            or manual_work.get("renderable") is not True
        ):
            raise SystemExit("The frozen Python sidecar could not extend a manual profile")
        manual_retry_after_edit = request(
            "manual-profile-start-retry-after-edit",
            "profile.start.manual",
            manual_params,
        ).get("result")
        if (
            not isinstance(manual_retry_after_edit, dict)
            or manual_retry_after_edit != manual_retry
        ):
            raise SystemExit(
                "The frozen Python sidecar lost manual-start idempotency after a later edit"
            )

        manual_export = request(
            "manual-profile-export",
            "resume.export",
            {
                "format": "pdf",
                "profile_version_id": manual_work_version_id,
            },
        ).get("result")
        manual_export_path = (
            manual_export.get("relative_path")
            if isinstance(manual_export, dict)
            else None
        )
        if (
            not isinstance(manual_export, dict)
            or manual_export.get("profile_version_id") != manual_work_version_id
            or manual_export.get("format") != "pdf"
            or manual_export.get("media_type") != "application/pdf"
            or not isinstance(manual_export_path, str)
            or not Path(directory, manual_export_path).is_file()
            or not Path(directory, manual_export_path).read_bytes().startswith(b"%PDF-")
        ):
            raise SystemExit("The frozen Python sidecar could not export the manual profile")


def _source_review_smoke_test(binary: Path) -> None:
    """Exercise the new import/queued-review boundary in the frozen package."""
    with tempfile.TemporaryDirectory(prefix="cvgnome-source-review-smoke-") as directory:
        def request(method: str, params: dict[str, object]) -> dict[str, object]:
            request_id = str(uuid.uuid4())
            completed = subprocess.run(
                [str(binary), "request", "--data-dir", directory],
                input=json.dumps({
                    "protocol_version": EXPECTED_PROTOCOL_VERSION,
                    "id": request_id, "method": method, "params": params,
                }) + "\n",
                capture_output=True, text=True, timeout=60, check=False,
            )
            response = json.loads(completed.stdout)
            if completed.returncode or response.get("id") != request_id or not response.get("ok"):
                raise SystemExit(f"The packaged source-review workflow failed at {method}")
            return response["result"]

        baseline_bytes = json.dumps({
            "basics": {
                "name": "Source Review Smoke",
                "email": "old@example.com",
                "summary": "Builds local tools.",
            },
            "skills": [{"name": "Engineering", "keywords": ["Python"]}],
        }).encode("utf-8")
        conflict_bytes = json.dumps({
            "basics": {"name": "Source Review Smoke", "email": "new@example.com"},
        }).encode("utf-8")
        scan_id = str(uuid.uuid4())
        staging = Path(directory, "imports", "staging", scan_id)
        staging.mkdir(parents=True, mode=0o700)
        sources = []
        for ordinal, (filename, content) in enumerate((
            ("a-resume.json", baseline_bytes),
            ("b-copy.json", baseline_bytes),
            ("z-contact.json", conflict_bytes),
        )):
            relative = f"imports/staging/{scan_id}/{ordinal:04d}.json"
            source = Path(directory, relative)
            source.write_bytes(content)
            if os.name != "nt":
                source.chmod(0o600)
            sources.append({
                "ordinal": ordinal, "managed_relative_path": relative,
                "display_name": filename, "format": "json",
                "byte_size": len(content),
                "checksum_sha256": hashlib.sha256(content).hexdigest(),
            })
        preview = request("profile.sources.preview", {
            "scan_id": scan_id, "sources": sources,
            "file_counts": {"discovered": 3, "staged": 3, "skipped": 0},
            "scan_issues": {},
        })
        if preview.get("source_review_count") != 2 or not preview.get("can_build"):
            raise SystemExit("The packaged import did not detect a conflict and duplicate")
        committed = request("profile.sources.commit", {"scan_id": scan_id})
        if committed.get("source_review_count") != 2:
            raise SystemExit("The packaged import did not retain both source checks")
        page = request("profile.source_review.list", {"scope": "inbox", "offset": 0, "limit": 10})
        items = page.get("items", [])
        if len(items) != 2 or page.get("counts") != {"inbox": 2, "deferred": 0, "history": 0}:
            raise SystemExit("The packaged source-check list has incorrect counts")
        conflict = next(item for item in items if item["kind"] == "basic_conflict")
        duplicate = next(item for item in items if item["kind"] == "duplicate_document")
        if (
            conflict.get("field") != "email"
            or conflict.get("previous_value") != "old@example.com"
            or conflict.get("proposed_value") != "new@example.com"
            or len(duplicate.get("evidence", [])) != 2
        ):
            raise SystemExit("The packaged source-check comparison lost its evidence")
        payload = {
            "request_id": str(uuid.uuid4()),
            "expected_parent_profile_version_id": page["current_profile_version_id"],
            "decisions": [
                {"item_id": conflict["id"], "expected_state": "inbox", "expected_revision": 0, "action": "use_source"},
                {"item_id": duplicate["id"], "expected_state": "inbox", "expected_revision": 0, "action": "defer"},
            ],
        }
        receipt = request("profile.source_review.apply", payload)
        if (
            receipt.get("profile_changed") is not True
            or receipt.get("version_number") != 2
            or receipt.get("counts") != {"inbox": 0, "deferred": 1, "history": 1}
        ):
            raise SystemExit("The packaged review batch did not create exactly one version")
        retry = request("profile.source_review.apply", payload)
        if retry != {**receipt, "created": False}:
            raise SystemExit("The packaged review batch did not reuse its exact retry")
        basics = request("profile.basics.get", {"profile_version_id": receipt["profile_version_id"]})
        if basics.get("email") != "new@example.com":
            raise SystemExit("The packaged review batch did not apply the selected field")
        for revision, state, action in ((1, "deferred", "reopen"), (2, "inbox", "acknowledge_duplicate")):
            organized = request("profile.source_review.apply", {
                "request_id": str(uuid.uuid4()),
                "expected_parent_profile_version_id": receipt["profile_version_id"],
                "decisions": [{
                    "item_id": duplicate["id"], "expected_state": state,
                    "expected_revision": revision, "action": action,
                }],
            })
            if organized.get("profile_changed") is not False or organized.get("version_number") != 2:
                raise SystemExit("The packaged duplicate decision unexpectedly changed the profile")
        history = request("profile.source_review.list", {"scope": "history", "offset": 0, "limit": 10})
        if history.get("counts") != {"inbox": 0, "deferred": 0, "history": 2}:
            raise SystemExit("The packaged source-check history lost completed decisions")
        status = request("system.status", {})
        if status.get("source_review_history_items") != 2 or status.get("retained_source_files") != 2:
            raise SystemExit("The packaged review workflow lost retained originals")


def _memory_smoke_test(binary: Path) -> None:
    """Verify original-story retention and the explicit review handoff offline."""
    with tempfile.TemporaryDirectory(prefix="cvgnome-memory-smoke-") as directory:
        def request(method: str, params: dict[str, object]) -> dict[str, object]:
            request_id = str(uuid.uuid4())
            completed = subprocess.run(
                [str(binary), "request", "--data-dir", directory],
                input=json.dumps({
                    "protocol_version": EXPECTED_PROTOCOL_VERSION,
                    "id": request_id, "method": method, "params": params,
                }) + "\n",
                capture_output=True, text=True, timeout=60, check=False,
            )
            response = json.loads(completed.stdout)
            if completed.returncode or response.get("id") != request_id or not response.get("ok"):
                raise SystemExit(f"The packaged memory workflow failed at {method}")
            return response["result"]

        profile = request("profile.start.manual", {
            "request_id": str(uuid.uuid4()),
            "expected_parent_profile_version_id": None,
            "patch": {"name": "Memory Smoke", "summary": "Builds local tools."},
        })
        original = "  A career story, in my own words.\r\n\tThe date may have been 2022.\nNo verified metric.  "
        save_params = {
            "request_id": str(uuid.uuid4()),
            "expected_parent_profile_version_id": profile["profile_version_id"],
            "expected_retention_generation": 1,
            "title": "An uncertain project", "narrative": original,
        }
        saved = request("memory.save", save_params)
        if request("memory.save", save_params) != {**saved, "created": False}:
            raise SystemExit("The packaged memory save did not reuse its exact retry")
        page = request("memory.list", {"scope": "current", "limit": 10, "offset": 0})
        if page.get("total_items") != 1 or "narrative" in page["items"][0]:
            raise SystemExit("The packaged memory list is not a bounded summary")
        detail = request("memory.get", {"memory_id": saved["memory_id"]})
        if detail.get("narrative") != original or detail.get("review_item_id") is not None:
            raise SystemExit("The packaged memory save altered the original or silently proposed facts")
        status = request("system.status", {})
        if status.get("profile_versions") != 1 or status.get("memory_count") != 1 or status.get("review_inbox_items") != 0:
            raise SystemExit("Saving a packaged memory unexpectedly changed the profile or inbox")
        proposal_params = {
            "request_id": str(uuid.uuid4()), "memory_id": saved["memory_id"],
            "expected_parent_profile_version_id": profile["profile_version_id"],
            "expected_retention_generation": 1,
            "name": "Local tools", "description": "Helped build a local tool.",
        }
        proposed = request("memory.propose_project", proposal_params)
        if request("memory.propose_project", proposal_params) != {**proposed, "created": False}:
            raise SystemExit("The packaged memory suggestion did not reuse its exact retry")
        inbox = request("review.inbox.list", {"scope": "inbox", "offset": 0})
        item = inbox["items"][0]
        if (
            item.get("id") != proposed["review_item_id"]
            or item.get("candidate_kind") != "projects"
            or item["candidate"].get("description") != proposal_params["description"]
            or item["evidence"][0].get("location_label") != "Original memory"
            or request("system.status", {}).get("profile_versions") != 1
        ):
            raise SystemExit("The packaged project suggestion did not retain its review boundary")
        apply_params = {
            "request_id": str(uuid.uuid4()), "review_item_id": item["id"],
            "expected_state": "inbox", "expected_state_revision": 0,
            "expected_parent_profile_version_id": profile["profile_version_id"],
            "candidate": item["candidate"],
        }
        applied = request("review.inbox.apply", apply_params)
        if applied.get("version_number") != 2 or request("review.inbox.apply", apply_params) != {**applied, "created": False}:
            raise SystemExit("The packaged memory review did not apply exactly once")
        if request("memory.get", {"memory_id": saved["memory_id"]}).get("narrative") != original:
            raise SystemExit("Applying a memory suggestion changed the original story")
        request("profile.sources.reset", {"expected_generation": 1, "request_id": str(uuid.uuid4())})
        history = request("memory.list", {"scope": "history", "limit": 10, "offset": 0})
        if history.get("total_items") != 1 or not history["items"][0].get("is_previous_import_set"):
            raise SystemExit("The packaged import-set reset did not retain memory history")
        before_reset = request("system.status", {})
        request("workspace.reset", {
            "request_id": str(uuid.uuid4()),
            "expected": {field: before_reset[field] for field in (
                "profile_versions", "opportunities", "artifacts", "source_imports",
                "source_previews", "source_retention_generation", "review_inbox_items",
                "review_deferred_items", "review_history_items", "source_review_inbox_items",
                "source_review_deferred_items", "source_review_history_items", "memory_count",
            )},
        })
        if request("system.status", {}).get("memory_count") != 0:
            raise SystemExit("The packaged career reset did not remove local memories")


def _transfer_smoke_test(binary: Path) -> None:
    """Exercise frozen offline backup, restore and explicit legacy migration."""
    with tempfile.TemporaryDirectory(prefix="cvgnome-transfer-smoke-") as directory:
        root = Path(directory)
        source = root / "source"

        def request(data_dir: Path, method: str, params: dict[str, object]) -> dict[str, object]:
            request_id = str(uuid.uuid4())
            completed = subprocess.run(
                [str(binary), "request", "--data-dir", str(data_dir)],
                input=json.dumps({"protocol_version": EXPECTED_PROTOCOL_VERSION,
                                  "id": request_id, "method": method, "params": params}) + "\n",
                capture_output=True, text=True, timeout=60, check=False,
            )
            response = json.loads(completed.stdout)
            if completed.returncode or not response.get("ok") or response.get("id") != request_id:
                raise SystemExit(f"The packaged transfer fixture failed at {method}")
            return response["result"]

        def transfer(*arguments: str, succeeds: bool = True) -> None:
            result = subprocess.run([str(binary), *arguments], capture_output=True,
                                    text=True, timeout=60, check=False)
            response = json.loads(result.stdout)
            if (result.returncode == 0 and response.get("ok") is True) != succeeds:
                raise SystemExit(f"The packaged transfer command failed at {arguments[0]}")

        def fingerprints(data_dir: Path) -> dict[str, str]:
            return {str(p.relative_to(data_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in data_dir.rglob("*") if p.is_file()}

        request(source, "profile.start.manual", {
            "request_id": str(uuid.uuid4()), "expected_parent_profile_version_id": None,
            "patch": {"name": "Transfer Smoke", "summary": "Synthetic offline backup evidence."},
        })
        expected = request(source, "profile.review", {})
        artifact = source / "artifacts" / "transfer-smoke.txt"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text("Synthetic retained artifact\n")
        settings = source / "local-provider.json"
        settings.write_text('{"synthetic":"non-secret settings"}')
        before = fingerprints(source)
        archive, restored = root / "backup.zip", root / "restored"
        transfer("backup", "--data-dir", str(source), "--output", str(archive), "--app-closed")
        if fingerprints(source) != before:
            raise SystemExit("Packaged backup changed the original workspace")
        transfer("restore", "--archive", str(archive), "--data-dir", str(restored), "--app-closed")
        if (request(restored, "profile.review", {}) != expected
                or (restored / "artifacts" / artifact.name).read_bytes() != artifact.read_bytes()
                or (restored / settings.name).read_bytes() != settings.read_bytes()):
            raise SystemExit("Packaged restore did not preserve the career workspace")
        restored_before = fingerprints(restored)
        transfer("restore", "--archive", str(archive), "--data-dir", str(restored),
                 "--app-closed", succeeds=False)
        if fingerprints(restored) != restored_before:
            raise SystemExit("Packaged restore overwrote an existing workspace")
        legacy, migrated = root / "legacy", root / "migrated"
        shutil.copytree(source, legacy)
        (legacy / "cvgnome.sqlite3").rename(legacy / LEGACY_DATABASE_FILENAME)
        legacy_before = fingerprints(legacy)
        transfer("migrate", "--source-dir", str(legacy), "--data-dir", str(migrated), "--app-closed")
        if fingerprints(legacy) != legacy_before or request(migrated, "profile.review", {}) != expected:
            raise SystemExit("Packaged migration changed the original or lost the profile")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and verify the native Python sidecar")
    parser.add_argument("--smoke-test-only", action="store_true",
                        help="Verify the existing target sidecar without rebuilding it")
    arguments = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to build the Python sidecar")

    target_triple = _target_triple()
    extension = ".exe" if sys.platform == "win32" else ""
    target = TAURI_BINARIES / f"cvgnome-engine-{target_triple}{extension}"
    if arguments.smoke_test_only:
        if not target.is_file():
            raise SystemExit("No target sidecar exists; run npm run engine:build first")
        _smoke_test(target)
        _source_review_smoke_test(target)
        _memory_smoke_test(target)
        _transfer_smoke_test(target)
        subprocess.run([sys.executable, str(ROOT / "scripts/verify_release.py"),
                        "--engine-binary", str(target)], check=True)
        print(f"Verified {target.relative_to(ROOT)}")
        return 0
    subprocess.run(
        [uv, "sync", "--locked", "--project", str(ENGINE_ROOT), "--group", "dev"],
        check=True,
    )
    # Resolve the locked target dependency graph and preserve upstream notices.
    # Missing notices are a build failure, never a silently incomplete release.
    subprocess.run([uv, "run", "--frozen", "--no-sync", "--project", str(ENGINE_ROOT),
                    "python", str(ROOT / "scripts/release_notices.py"),
                    "--target", target_triple], check=True)
    font_directory = subprocess.check_output(
        [uv, "run", "--frozen", "--no-sync", "--project", str(ENGINE_ROOT), "python", "-c",
         "import pathlib, reportlab; print(pathlib.Path(reportlab.__file__).parent / 'fonts')"],
        text=True,
    ).strip()
    data_arguments = []
    # Keep the actual PDF renderer fonts and their license; do not redistribute
    # every optional ReportLab font (including unrelated DarkGarden assets).
    for filename in ("Vera.ttf", "VeraBd.ttf", "VeraIt.ttf", "bitstream-vera-license.txt"):
        data_arguments.extend(["--add-data", f"{Path(font_directory) / filename}:reportlab/fonts"])
    for filename in ("THIRD_PARTY_NOTICES.txt", "third-party-inventory.json"):
        data_arguments.extend(["--add-data", f"{ROOT / 'src-tauri/resources' / filename}:licenses"])
    for filename in ("LICENSE", "NOTICE"):
        data_arguments.extend(["--add-data", f"{ROOT / filename}:licenses"])

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    dist_dir = BUILD_ROOT / "dist"
    work_dir = BUILD_ROOT / "work"
    spec_dir = BUILD_ROOT / "spec"
    dist_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            uv,
            "run",
            "--frozen",
            "--project",
            str(ENGINE_ROOT),
            "pyinstaller",
            "--clean",
            "--noconfirm",
            "--onefile",
            "--name",
            "cvgnome-engine",
            *data_arguments,
            "--hidden-import",
            "cvgnome_engine.source_ingest.parser_child",
            "--paths",
            str(ENGINE_ROOT / "src"),
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(work_dir),
            "--specpath",
            str(spec_dir),
            str(ENGINE_ROOT / "sidecar_entry.py"),
        ],
        check=True,
    )

    source = dist_dir / f"cvgnome-engine{extension}"
    _smoke_test(source)
    _source_review_smoke_test(source)
    _memory_smoke_test(source)
    _transfer_smoke_test(source)
    subprocess.run([uv, "run", "--frozen", "--no-sync", "--project", str(ENGINE_ROOT),
                    "python", str(ROOT / "scripts/verify_release.py"),
                    "--engine-binary", str(source)], check=True)
    TAURI_BINARIES.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if sys.platform != "win32":
        target.chmod(target.stat().st_mode | 0o111)
    print(f"Built {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
