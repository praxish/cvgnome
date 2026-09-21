# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from . import __version__
from .profile import (
    CanonicalProfileValidationError,
    prepare_canonical_profile,
)
from .profile_sources import (
    ProfileSourceWorkflowError,
    commit_profile_sources,
    discard_all_profile_sources,
    discard_profile_sources,
    preview_profile_sources,
)
from .profile_review import (
    build_profile_review_section,
    build_profile_review_summary,
)
from .review_inbox import (
    apply_profile_review_item,
    list_profile_review_items,
    transition_profile_review_item,
)
from .source_review import apply_source_review_decisions, list_source_review_items
from .memories import get_memory, list_memories, propose_memory_project, save_memory
from .profile_changes import apply_profile_changes, preview_profile_changes
from .profile_manual_start import start_manual_profile
from .source_recovery import recover_profile_sources, resume_profile_source_scan
from .profile_versions import (
    diff_profile_versions,
    get_profile_basics,
    get_profile_review_version,
    list_profile_versions,
    restore_profile_version,
    update_profile_basics,
)
from .profile_work import list_profile_work, update_profile_work
from .profile_sections import (
    list_profile_education,
    list_profile_projects,
    list_profile_skills,
    update_profile_education,
    update_profile_projects,
    update_profile_skills,
)
from .opportunities import (
    get_opportunity,
    list_opportunities,
    rematch_opportunity,
    save_and_match_opportunity,
    update_opportunity,
)
from .retained_sources import list_retained_sources
from .tailoring import (
    arm_tailoring,
    create_tailoring_revision,
    export_tailoring,
    export_tailoring_revision,
    fail_tailoring,
    finalize_tailoring,
    get_tailoring_revision,
    latest_tailoring,
    list_tailoring_revisions,
    prepare_tailoring,
)
from .resume import (
    derive_baseline_resume,
    is_baseline_resume_renderable,
    render_docx_bytes,
    render_pdf_bytes,
)
from .source_ingest import parser_child_main
from .storage import (
    VaultError,
    get_profile_version,
    import_canonical_profile_source,
    initialize_vault,
    latest_profile,
    reset_profile_source_retention,
    save_artifact_bytes,
    save_profile,
)
from .workspace_reset import reset_workspace

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 9 * 1024 * 1024


class RpcError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _profile_name(profile: dict[str, Any]) -> str:
    basics = profile.get("basics")
    if isinstance(basics, dict):
        candidate = basics.get("name")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:160]
    return "Unnamed profile"


def _profile_is_renderable(profile: dict[str, Any]) -> bool:
    return is_baseline_resume_renderable(profile)


def _safe_resume_stem(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-")[:80]
    return stem or "CVGnome"


def _prepare_profile(profile: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return prepare_canonical_profile(profile)
    except CanonicalProfileValidationError as exc:
        raise RpcError(exc.code, str(exc)) from exc


def _require_param_keys(params: dict[str, Any], allowed: set[str]) -> None:
    if any(key not in allowed for key in params):
        raise RpcError("invalid_params", "params contains unsupported fields")


def _dispatch(data_dir: Path, method: str, params: dict[str, Any]) -> Any:
    if method == "system.status":
        return {
            "engine_version": __version__,
            **initialize_vault(data_dir).to_dict(),
        }
    if method == "workspace.reset":
        _require_param_keys(params, {"request_id", "expected"})
        try:
            return reset_workspace(data_dir, params).to_dict()
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.save":
        profile = params.get("profile")
        prepared, _report = _prepare_profile(profile)
        try:
            return save_profile(
                data_dir,
                prepared,
                source=str(params.get("source") or "local_edit"),
            ).to_dict()
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.import":
        _require_param_keys(params, {"source"})
        try:
            return import_canonical_profile_source(data_dir, params.get("source"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.latest":
        return latest_profile(data_dir)
    if method == "profile.start.manual":
        try:
            return start_manual_profile(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.review":
        _require_param_keys(params, set())
        current = latest_profile(data_dir)
        if current is None:
            raise RpcError(
                "profile_missing",
                "Build or import a career profile before opening profile review.",
            )
        return build_profile_review_summary(current)
    if method == "profile.review.version":
        _require_param_keys(params, {"profile_version_id"})
        try:
            return get_profile_review_version(data_dir, params.get("profile_version_id"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.review.section":
        _require_param_keys(params, {"profile_version_id", "section", "offset"})
        try:
            version = get_profile_version(data_dir, params.get("profile_version_id"))
            return build_profile_review_section(
                version,
                section=params.get("section"),
                offset=params.get("offset"),
            )
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.versions.list":
        _require_param_keys(params, {"offset"})
        try:
            return list_profile_versions(data_dir, params.get("offset"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.basics.get":
        _require_param_keys(params, {"profile_version_id"})
        try:
            return get_profile_basics(data_dir, params.get("profile_version_id"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.versions.diff":
        _require_param_keys(
            params,
            {"from_profile_version_id", "to_profile_version_id"},
        )
        try:
            return diff_profile_versions(
                data_dir,
                params.get("from_profile_version_id"),
                params.get("to_profile_version_id"),
            )
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.versions.restore":
        try:
            return restore_profile_version(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.basics.update":
        try:
            return update_profile_basics(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.work.list":
        _require_param_keys(params, {"profile_version_id", "offset"})
        try:
            return list_profile_work(
                data_dir,
                params.get("profile_version_id"),
                params.get("offset"),
            )
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.work.update":
        try:
            return update_profile_work(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.changes.preview":
        _require_param_keys(params, {"profile_version_id"})
        try:
            return preview_profile_changes(data_dir, params.get("profile_version_id"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.changes.apply":
        try:
            return apply_profile_changes(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    section_lists = {
        "profile.projects.list": list_profile_projects,
        "profile.education.list": list_profile_education,
        "profile.skills.list": list_profile_skills,
    }
    if method in section_lists:
        _require_param_keys(params, {"profile_version_id", "offset"})
        try:
            return section_lists[method](
                data_dir,
                params.get("profile_version_id"),
                params.get("offset"),
            )
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    section_updates = {
        "profile.projects.update": update_profile_projects,
        "profile.education.update": update_profile_education,
        "profile.skills.update": update_profile_skills,
    }
    if method in section_updates:
        try:
            return section_updates[method](data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.sources.preview":
        return preview_profile_sources(data_dir, params)
    if method == "profile.sources.commit":
        return commit_profile_sources(data_dir, params.get("scan_id"))
    if method == "profile.sources.recover":
        _require_param_keys(params, {"scan_id", "patch"})
        try:
            return recover_profile_sources(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.sources.resume":
        _require_param_keys(params, set())
        return resume_profile_source_scan(data_dir)
    if method == "profile.sources.discard":
        return discard_profile_sources(data_dir, params.get("scan_id"))
    if method == "profile.sources.discard_all":
        _require_param_keys(params, set())
        return discard_all_profile_sources(data_dir)
    if method == "profile.sources.reset":
        _require_param_keys(params, {"expected_generation", "request_id"})
        try:
            return reset_profile_source_retention(
                data_dir,
                expected_generation=params.get("expected_generation"),
                request_id=params.get("request_id"),
            ).to_dict()
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.sources.list":
        _require_param_keys(params, {"scope", "limit", "offset"})
        try:
            return list_retained_sources(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method in {"memory.list", "memory.get", "memory.save", "memory.propose_project"}:
        handler = {"memory.list": list_memories, "memory.get": get_memory,
                   "memory.save": save_memory, "memory.propose_project": propose_memory_project}[method]
        try:
            return handler(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "review.inbox.list":
        _require_param_keys(params, {"scope", "offset"})
        try:
            return list_profile_review_items(
                data_dir,
                scope=params.get("scope"),
                offset=params.get("offset"),
            )
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.source_review.list":
        _require_param_keys(params, {"scope", "limit", "offset"})
        try:
            return list_source_review_items(data_dir, scope=params.get("scope"),
                                            limit=params.get("limit"), offset=params.get("offset"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "profile.source_review.apply":
        try:
            return apply_source_review_decisions(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "review.inbox.transition":
        _require_param_keys(
            params,
            {
                "review_item_id",
                "request_id",
                "action",
                "expected_state",
                "expected_state_revision",
            },
        )
        try:
            return transition_profile_review_item(
                data_dir,
                review_item_id=params.get("review_item_id"),
                request_id=params.get("request_id"),
                action=params.get("action"),
                expected_state=params.get("expected_state"),
                expected_state_revision=params.get("expected_state_revision"),
            )
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "review.inbox.apply":
        try:
            return apply_profile_review_item(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "opportunities.save_match":
        _require_param_keys(
            params,
            {"title", "description", "company", "location", "source_url", "apply_url"},
        )
        try:
            return save_and_match_opportunity(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "opportunities.list":
        _require_param_keys(params, {"query", "offset", "stage", "sort"})
        try:
            return list_opportunities(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "opportunities.get":
        _require_param_keys(params, {"opportunity_id"})
        try:
            return get_opportunity(data_dir, params.get("opportunity_id"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "opportunities.rematch":
        _require_param_keys(params, {"opportunity_id"})
        try:
            return rematch_opportunity(data_dir, params.get("opportunity_id"))
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "opportunities.update":
        _require_param_keys(
            params,
            {
                "opportunity_id",
                "expected_tracker_updated_at_ms",
                "tracker_stage",
                "notes",
            },
        )
        try:
            return update_opportunity(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.prepare":
        try:
            return prepare_tailoring(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.arm":
        try:
            return arm_tailoring(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.finalize":
        try:
            return finalize_tailoring(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.fail":
        try:
            return fail_tailoring(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.latest":
        try:
            return latest_tailoring(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.export":
        try:
            return export_tailoring(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.revisions.list":
        try:
            return list_tailoring_revisions(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.revisions.get":
        try:
            return get_tailoring_revision(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.revisions.create":
        try:
            return create_tailoring_revision(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "tailoring.revisions.export":
        try:
            return export_tailoring_revision(data_dir, params)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "resume.export":
        _require_param_keys(params, {"format", "profile_version_id"})
        output_format = str(params.get("format") or "").strip().lower()
        if output_format not in {"docx", "pdf"}:
            raise RpcError("invalid_params", "format must be either docx or pdf")
        if "profile_version_id" in params:
            try:
                current = get_profile_version(data_dir, params.get("profile_version_id"))
            except ValueError as exc:
                raise RpcError("invalid_params", str(exc)) from exc
        else:
            current = latest_profile(data_dir)
            if current is None:
                raise RpcError(
                    "profile_missing",
                    "Import a canonical profile before exporting a resume.",
                )
        profile = current["profile"]
        if not _profile_is_renderable(profile):
            raise RpcError(
                "profile.not_renderable",
                "The current profile needs a name and resume content before it can be exported.",
            )
        resume = derive_baseline_resume(profile)
        try:
            if output_format == "docx":
                content = render_docx_bytes(resume)
                media_type = (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                )
            else:
                content = render_pdf_bytes(resume)
                media_type = "application/pdf"
        except ValueError as exc:
            if str(exc).startswith("PDF font coverage does not support character"):
                raise RpcError("resume.unsupported_glyph", str(exc)) from exc
            raise RpcError(
                "resume.render_failed",
                "The current profile could not be rendered as a resume.",
            ) from exc
        except RuntimeError as exc:
            raise RpcError(
                "resume.render_failed",
                "The local resume renderer is unavailable.",
            ) from exc
        suggested_filename = (
            f"{_safe_resume_stem(_profile_name(profile))}-Resume-v{current['version_number']}."
            f"{output_format}"
        )
        try:
            artifact = save_artifact_bytes(
                data_dir,
                profile_version_id=current["id"],
                kind=f"resume_{output_format}",
                extension=output_format,
                content=content,
                metadata={
                    "format": output_format,
                    "media_type": media_type,
                    "profile_version": current["version_number"],
                    "suggested_filename": suggested_filename,
                    "template_id": "classic",
                    "renderer_contract_version": 1,
                },
            )
        except ValueError as exc:
            raise RpcError("artifact_invalid", str(exc)) from exc
        return {
            "artifact_id": artifact.id,
            "profile_version_id": artifact.profile_version_id,
            "format": output_format,
            "media_type": media_type,
            "relative_path": artifact.relative_path,
            "checksum_sha256": artifact.checksum_sha256,
            "byte_size": artifact.byte_size,
            "suggested_filename": suggested_filename,
        }
    raise RpcError("method_not_found", f"Unsupported method: {method}")


def _handle_request(data_dir: Path, request: Any) -> dict[str, Any]:
    request_id = request.get("id") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise RpcError("invalid_request", "Request must be a JSON object")
        request_protocol = request.get("protocol_version")
        if (
            not isinstance(request_protocol, int)
            or isinstance(request_protocol, bool)
            or request_protocol != PROTOCOL_VERSION
        ):
            raise RpcError(
                "protocol_mismatch",
                f"Request protocol must be version {PROTOCOL_VERSION}",
            )
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise RpcError(
                "invalid_request",
                "id must be a non-empty string of at most 128 characters",
            )
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not method:
            raise RpcError("invalid_request", "method must be a non-empty string")
        if not isinstance(params, dict):
            raise RpcError("invalid_params", "params must be a JSON object")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": True,
            "result": _dispatch(data_dir, method, params),
        }
    except RpcError as exc:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": {"code": exc.code, "message": str(exc)},
        }
    except VaultError as exc:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": {"code": exc.code, "message": str(exc)},
        }
    except ProfileSourceWorkflowError as exc:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": {"code": exc.code, "message": str(exc)},
        }
    except Exception:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": {
                "code": "engine_error",
                "message": "The local engine could not complete the request.",
            },
        }


def _write_response(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _error_response(code: str, message: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "id": None,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _read_request_line() -> tuple[bytes | None, dict[str, Any] | None]:
    raw_line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not raw_line:
        return None, None
    if len(raw_line) > MAX_REQUEST_BYTES:
        while raw_line and not raw_line.endswith(b"\n"):
            raw_line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        return None, _error_response(
            "request_too_large",
            "The local engine request exceeds the 9 MiB limit.",
        )
    return raw_line.strip(), None


def _decode_request(raw_line: bytes) -> tuple[Any | None, dict[str, Any] | None]:
    try:
        return json.loads(raw_line), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _error_response("invalid_json", "Malformed JSON request")


def _run_rpc(data_dir: Path) -> int:
    while True:
        raw_line, error = _read_request_line()
        if raw_line is None and error is None:
            break
        if error is not None:
            _write_response(error)
            continue
        if not raw_line:
            continue
        request, error = _decode_request(raw_line)
        if error is not None:
            _write_response(error)
            continue
        _write_response(_handle_request(data_dir, request))
    return 0


def _run_request(data_dir: Path) -> int:
    raw_line, error = _read_request_line()
    if raw_line is None:
        response = error or _error_response("invalid_request", "Request body is required")
    else:
        request, decode_error = _decode_request(raw_line)
        response = decode_error or _handle_request(data_dir, request)
    _write_response(response)
    return 0 if response["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cvgnome-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Initialize and inspect the local vault")
    status_parser.add_argument("--data-dir", type=Path, required=True)

    rpc_parser = subparsers.add_parser("rpc", help="Run the JSON Lines RPC loop")
    rpc_parser.add_argument("--data-dir", type=Path, required=True)
    request_parser = subparsers.add_parser("request", help="Handle one JSON request from stdin")
    request_parser.add_argument("--data-dir", type=Path, required=True)
    for name in ("backup", "restore", "migrate"):
        transfer = subparsers.add_parser(name, help=f"Offline career workspace {name}; close the desktop app first")
        transfer.add_argument("--data-dir", type=Path, required=True,
                              help="Source workspace for backup; new destination for restore or migration")
        transfer.add_argument("--app-closed", action="store_true", required=True,
                              help="Acknowledge that the desktop app is closed")
        if name == "backup":
            transfer.add_argument("--output", type=Path, required=True,
                                  help="New unencrypted ZIP archive outside the workspace")
        elif name == "restore":
            transfer.add_argument("--archive", type=Path, required=True)
        else:
            transfer.add_argument("--source-dir", type=Path, required=True,
                                  help="Previous application's data directory, preserved unchanged")
    subparsers.add_parser("source-parser-child", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"backup", "restore", "migrate"}:
        import sqlite3
        from zipfile import BadZipFile
        from .workspace_transfer import (
            WorkspaceTransferError, backup_workspace, migrate_workspace, restore_workspace,
        )
        try:
            if args.command == "backup":
                result = backup_workspace(args.data_dir, args.output, app_closed=args.app_closed)
            elif args.command == "restore":
                result = restore_workspace(args.archive, args.data_dir, app_closed=args.app_closed)
            else:
                result = migrate_workspace(args.source_dir, args.data_dir, app_closed=args.app_closed)
        except (WorkspaceTransferError, OSError, sqlite3.Error, BadZipFile, ValueError) as exc:
            _write_response({"ok": False, "error": {
                "code": "workspace_transfer_failed",
                "message": str(exc) if isinstance(exc, WorkspaceTransferError)
                else "The offline workspace operation failed; check the input and destination.",
            }})
            return 1
        _write_response({"ok": True, "result": result})
        return 0
    if args.command == "source-parser-child":
        return parser_child_main()
    if args.command == "rpc":
        return _run_rpc(args.data_dir)
    if args.command == "request":
        return _run_request(args.data_dir)
    if args.command == "status":
        response = _handle_request(
            args.data_dir,
            {
                "protocol_version": PROTOCOL_VERSION,
                "id": "status",
                "method": "system.status",
                "params": {},
            },
        )
        _write_response(response)
        return 0 if response["ok"] else 1
    return 2
