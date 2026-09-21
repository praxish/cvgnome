# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .storage import DATABASE_FILENAME, MAX_RPC_SAFE_COUNT, SCHEMA_VERSION, VaultError

RESET_CONTRACT_VERSION = 4
LEGACY_RESET_CONTRACT_VERSION = 1
MIN_COMPATIBLE_RESET_RECEIPT_SCHEMA_VERSION = 13
RESET_JOURNAL_FILENAME = ".cvgnome-workspace-reset-journal.json"
RESET_RECEIPT_FILENAME = ".cvgnome-workspace-reset-receipt.json"
RESET_LOCK_FILENAME = ".cvgnome-workspace-reset.lock"
MAX_RESET_STATE_BYTES = 16 * 1024
RESET_LOCK_TIMEOUT_SECONDS = 5.0

LEGACY_RESET_EXPECTED_FIELDS = frozenset(
    {
        "profile_versions",
        "opportunities",
        "artifacts",
        "source_imports",
        "source_previews",
        "source_retention_generation",
    }
)
V2_RESET_EXPECTED_FIELDS = frozenset(
    {
        *LEGACY_RESET_EXPECTED_FIELDS,
        "review_inbox_items",
        "review_deferred_items",
        "review_history_items",
    }
)
V3_RESET_EXPECTED_FIELDS = frozenset({*V2_RESET_EXPECTED_FIELDS,
    "source_review_inbox_items", "source_review_deferred_items", "source_review_history_items"})
RESET_EXPECTED_FIELDS = frozenset({*V3_RESET_EXPECTED_FIELDS, "memory_count"})
RESET_RESULT_FIELDS = frozenset(
    {
        "request_id",
        "reset_at_ms",
        "schema_version",
        *RESET_EXPECTED_FIELDS,
    }
)
LEGACY_RESET_RESULT_FIELDS = frozenset(
    {
        "request_id",
        "reset_at_ms",
        "schema_version",
        *LEGACY_RESET_EXPECTED_FIELDS,
    }
)
RESET_JOURNAL_FIELDS = frozenset(
    {
        "contract_version",
        "phase",
        "request_id",
        "reset_at_ms",
        "expected",
    }
)
RESET_RECEIPT_FIELDS = frozenset(
    {"contract_version", "request_id", "expected", "result"}
)
RESET_PHASES = frozenset({"prepared", "files_removed", "initialized"})
MANAGED_DIRECTORY_NAMES = ("sources", "imports", "artifacts")
MANAGED_DATABASE_NAMES = (
    DATABASE_FILENAME,
    f"{DATABASE_FILENAME}-wal",
    f"{DATABASE_FILENAME}-shm",
)
PRISTINE_TABLE_COUNTS = {
    "memories": 0,
    "memory_project_receipts": 0,
    "artifacts": 0,
    "model_usage_events": 0,
    "opportunities": 0,
    "opportunity_matches": 0,
    "opportunity_snapshots": 0,
    "profile_basic_update_receipts": 0,
    "profile_change_receipts": 0,
    "profile_manual_start_receipts": 0,
    "profile_review_apply_receipts": 0,
    "profile_review_item_events": 0,
    "profile_review_items": 0,
    "profile_restore_receipts": 0,
    "profile_section_update_receipts": 0,
    "profile_source_imports": 0,
    "profile_source_retention_state": 1,
    "profile_source_scan_items": 0,
    "profile_source_scans": 0,
    "profile_version_sources": 0,
    "profile_versions": 0,
    "profile_work_update_receipts": 0,
    "schema_migrations": SCHEMA_VERSION,
    "settings": 0,
    "source_extractions": 0,
    "sources": 0,
    "source_review_items": 0,
    "source_review_events": 0,
    "source_review_receipts": 0,
    "tailored_resume_drafts": 0,
    "tailored_resume_revisions": 0,
    "tailoring_attempts": 0,
    "workflow_jobs": 0,
}


@dataclass(frozen=True)
class WorkspaceResetResult:
    request_id: str
    reset_at_ms: int
    schema_version: int
    profile_versions: int
    opportunities: int
    artifacts: int
    source_imports: int
    source_previews: int
    source_retention_generation: int
    review_inbox_items: int
    review_deferred_items: int
    review_history_items: int
    source_review_inbox_items: int
    source_review_deferred_items: int
    source_review_history_items: int
    memory_count: int
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_uuid(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID string") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{label} must use canonical lowercase UUID form")
    return canonical


def _safe_integer(value: Any, *, label: str, minimum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= MAX_RPC_SAFE_COUNT
    ):
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{label} must be a {qualifier} safe integer")
    return value


def _expected_fields(contract_version: int) -> frozenset[str]:
    if contract_version == RESET_CONTRACT_VERSION:
        return RESET_EXPECTED_FIELDS
    if contract_version == LEGACY_RESET_CONTRACT_VERSION:
        return LEGACY_RESET_EXPECTED_FIELDS
    if contract_version == 2:
        return V2_RESET_EXPECTED_FIELDS
    if contract_version == 3:
        return V3_RESET_EXPECTED_FIELDS
    raise ValueError("reset contract version is unsupported")


def _validated_expected(
    value: Any, *, contract_version: int = RESET_CONTRACT_VERSION
) -> dict[str, int]:
    expected_fields = _expected_fields(contract_version)
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("expected must contain exactly the workspace status fields")
    return {
        field: _safe_integer(
            value[field],
            label=f"expected.{field}",
            minimum=1 if field == "source_retention_generation" else 0,
        )
        for field in sorted(expected_fields)
    }


def _lexical_root(data_dir: Path) -> Path:
    try:
        root = Path(os.path.abspath(os.fspath(data_dir.expanduser())))
    except (OSError, TypeError, ValueError) as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "The local career workspace location is invalid.",
        ) from exc
    anchor = Path(root.anchor)
    try:
        home = Path(os.path.abspath(os.fspath(Path.home())))
    except (OSError, RuntimeError):
        home = anchor
    if root == anchor or root == home or len(root.parts) < 3:
        raise VaultError(
            "workspace_reset_unsafe",
            "The local career workspace location is not safe to reset.",
        )
    return root


def _validated_root(data_dir: Path, *, create: bool) -> Path:
    root = _lexical_root(data_dir)
    if create:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultError(
                "workspace_reset_unsafe",
                "The local career workspace location could not be prepared safely.",
            ) from exc
    try:
        root_status = root.lstat()
    except FileNotFoundError as exc:
        raise VaultError(
            "workspace_reset_recovery_failed",
            "The interrupted career workspace reset cannot be recovered.",
        ) from exc
    except OSError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "The local career workspace location could not be inspected safely.",
        ) from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise VaultError(
            "workspace_reset_unsafe",
            "The local career workspace must be a real directory, not a link.",
        )
    if os.name != "nt":
        if root_status.st_uid != os.geteuid():
            raise VaultError(
                "workspace_reset_unsafe",
                "The local career workspace must be owned by the current user.",
            )
        try:
            root.chmod(0o700)
        except OSError as exc:
            raise VaultError(
                "workspace_reset_unsafe",
                "The local career workspace permissions could not be secured.",
            ) from exc
    return root


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_state_file(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESET_STATE_BYTES:
        raise VaultError(
            "workspace_reset_recovery_failed",
            "The career workspace reset state exceeds its safety limit.",
        )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_state_file(path: Path, *, kind: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VaultError(
            f"workspace_reset_{kind}_invalid",
            f"The career workspace reset {kind} could not be inspected safely.",
        ) from exc
    try:
        path_status = os.fstat(descriptor)
        if not stat.S_ISREG(path_status.st_mode):
            raise VaultError(
                f"workspace_reset_{kind}_invalid",
                f"The career workspace reset {kind} is not a regular file.",
            )
        if path_status.st_size <= 0 or path_status.st_size > MAX_RESET_STATE_BYTES:
            raise VaultError(
                f"workspace_reset_{kind}_invalid",
                f"The career workspace reset {kind} has an invalid size.",
            )
        if os.name != "nt" and (
            path_status.st_uid != os.geteuid()
            or stat.S_IMODE(path_status.st_mode) & 0o077
        ):
            raise VaultError(
                f"workspace_reset_{kind}_invalid",
                f"The career workspace reset {kind} is not owner-only.",
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            encoded = stream.read(MAX_RESET_STATE_BYTES + 1)
        value = json.loads(encoded)
    except VaultError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VaultError(
            f"workspace_reset_{kind}_invalid",
            f"The career workspace reset {kind} is invalid.",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise VaultError(
            f"workspace_reset_{kind}_invalid",
            f"The career workspace reset {kind} is invalid.",
        )
    return value


def _validated_journal(root: Path) -> dict[str, Any] | None:
    value = _read_state_file(root / RESET_JOURNAL_FILENAME, kind="journal")
    if value is None:
        return None
    try:
        if set(value) != RESET_JOURNAL_FIELDS:
            raise ValueError
        contract_version = _safe_integer(
            value["contract_version"], label="contract_version", minimum=1
        )
        _expected_fields(contract_version)
        if value["phase"] not in RESET_PHASES:
            raise ValueError
        request_id = _canonical_uuid(value["request_id"], label="request_id")
        reset_at_ms = _safe_integer(
            value["reset_at_ms"],
            label="reset_at_ms",
            minimum=1,
        )
        expected = _validated_expected(
            value["expected"], contract_version=contract_version
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError(
            "workspace_reset_journal_invalid",
            "The career workspace reset journal is invalid.",
        ) from exc
    return {
        "contract_version": contract_version,
        "phase": value["phase"],
        "request_id": request_id,
        "reset_at_ms": reset_at_ms,
        "expected": expected,
    }


def _validated_base_result(
    value: Any,
    *,
    contract_version: int,
    allow_historical_schema: bool = False,
) -> dict[str, int | str]:
    expected_fields = _expected_fields(contract_version)
    result_fields = frozenset({"request_id", "reset_at_ms", "schema_version", *expected_fields})
    if not isinstance(value, dict) or set(value) != result_fields:
        raise ValueError("result is invalid")
    request_id = _canonical_uuid(value["request_id"], label="result.request_id")
    result: dict[str, int | str] = {
        "request_id": request_id,
        "reset_at_ms": _safe_integer(
            value["reset_at_ms"], label="result.reset_at_ms", minimum=1
        ),
        "schema_version": _safe_integer(
            value["schema_version"], label="result.schema_version", minimum=1
        ),
    }
    result.update(
        _validated_expected(
            {field: value[field] for field in expected_fields},
            contract_version=contract_version,
        )
    )
    schema_version = int(result["schema_version"])
    schema_is_compatible = schema_version == SCHEMA_VERSION or (
        allow_historical_schema
        and contract_version < RESET_CONTRACT_VERSION
        and MIN_COMPATIBLE_RESET_RECEIPT_SCHEMA_VERSION
        <= schema_version
        < SCHEMA_VERSION
    )
    if (
        not schema_is_compatible
        or result["source_retention_generation"] != 1
        or any(
            result[field] != 0
            for field in expected_fields - {"source_retention_generation"}
        )
    ):
        raise ValueError("result is not pristine")
    return result


def _validated_receipt(root: Path) -> dict[str, Any] | None:
    value = _read_state_file(root / RESET_RECEIPT_FILENAME, kind="receipt")
    if value is None:
        return None
    try:
        if set(value) != RESET_RECEIPT_FIELDS:
            raise ValueError
        contract_version = _safe_integer(
            value["contract_version"], label="contract_version", minimum=1
        )
        _expected_fields(contract_version)
        request_id = _canonical_uuid(value["request_id"], label="request_id")
        expected = _validated_expected(
            value["expected"], contract_version=contract_version
        )
        result = _validated_base_result(
            value["result"],
            contract_version=contract_version,
            allow_historical_schema=True,
        )
        if result["request_id"] != request_id:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError(
            "workspace_reset_receipt_invalid",
            "The career workspace reset receipt is invalid.",
        ) from exc
    return {
        "contract_version": contract_version,
        "request_id": request_id,
        "expected": expected,
        "result": result,
    }


@contextmanager
def _reset_lock(root: Path) -> Iterator[None]:
    lock_path = root / RESET_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise VaultError(
            "workspace_reset_busy",
            "The career workspace reset lock could not be acquired safely.",
        ) from exc
    try:
        lock_status = os.fstat(descriptor)
        if not stat.S_ISREG(lock_status.st_mode):
            raise VaultError(
                "workspace_reset_busy",
                "The career workspace reset lock is invalid.",
            )
        if os.name != "nt":
            if lock_status.st_uid != os.geteuid():
                raise VaultError(
                    "workspace_reset_busy",
                    "The career workspace reset lock is not owned by this user.",
                )
            os.fchmod(descriptor, 0o600)
            import fcntl

            deadline = time.monotonic() + RESET_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise VaultError(
                            "workspace_reset_busy",
                            "Another career workspace operation is still finishing.",
                        ) from exc
                    time.sleep(0.05)
        yield
    finally:
        if os.name != "nt":
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _validate_regular_target(path: Path) -> None:
    try:
        target_status = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace file could not be inspected safely.",
        ) from exc
    if stat.S_ISLNK(target_status.st_mode) or not stat.S_ISREG(target_status.st_mode):
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace file has an unsafe type.",
        )


def _validate_managed_tree(path: Path) -> None:
    try:
        root_status = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory could not be inspected safely.",
        ) from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory has an unsafe type.",
        )
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory could not be inspected safely.",
        ) from exc
    for entry in entries:
        try:
            entry_status = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise VaultError(
                "workspace_reset_unsafe",
                "A managed career workspace entry could not be inspected safely.",
            ) from exc
        if stat.S_ISLNK(entry_status.st_mode):
            raise VaultError(
                "workspace_reset_unsafe",
                "A managed career workspace directory contains a link.",
            )
        if stat.S_ISDIR(entry_status.st_mode):
            _validate_managed_tree(Path(entry.path))
        elif not stat.S_ISREG(entry_status.st_mode):
            raise VaultError(
                "workspace_reset_unsafe",
                "A managed career workspace directory contains an unsafe entry.",
            )


def _preflight_managed_targets(root: Path) -> None:
    try:
        for name in MANAGED_DATABASE_NAMES:
            _validate_regular_target(root / name)
        for name in MANAGED_DIRECTORY_NAMES:
            _validate_managed_tree(root / name)
    except RecursionError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory is nested too deeply to reset safely.",
        ) from exc


def _remove_managed_tree(path: Path) -> None:
    try:
        root_status = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory changed during reset.",
        ) from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory changed during reset.",
        )
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory changed during reset.",
        ) from exc
    for entry in entries:
        entry_path = Path(entry.path)
        entry_status = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_status.st_mode):
            raise VaultError(
                "workspace_reset_unsafe",
                "A managed career workspace directory changed during reset.",
            )
        if stat.S_ISDIR(entry_status.st_mode):
            _remove_managed_tree(entry_path)
            entry_path.rmdir()
        elif stat.S_ISREG(entry_status.st_mode):
            entry_path.unlink()
        else:
            raise VaultError(
                "workspace_reset_unsafe",
                "A managed career workspace entry changed during reset.",
            )


def _remove_managed_targets(root: Path) -> None:
    _preflight_managed_targets(root)
    try:
        for name in MANAGED_DATABASE_NAMES:
            try:
                (root / name).unlink()
            except FileNotFoundError:
                pass
        for name in MANAGED_DIRECTORY_NAMES:
            target = root / name
            _remove_managed_tree(target)
            try:
                target.rmdir()
            except FileNotFoundError:
                pass
    except RecursionError as exc:
        raise VaultError(
            "workspace_reset_unsafe",
            "A managed career workspace directory is nested too deeply to reset safely.",
        ) from exc
    _fsync_directory(root)


def _base_result(journal: dict[str, Any]) -> dict[str, int | str]:
    result: dict[str, int | str] = {
        "request_id": journal["request_id"],
        "reset_at_ms": journal["reset_at_ms"],
        "schema_version": SCHEMA_VERSION,
        "profile_versions": 0,
        "opportunities": 0,
        "artifacts": 0,
        "source_imports": 0,
        "source_previews": 0,
        "source_retention_generation": 1,
    }
    if journal["contract_version"] >= 2:
        result.update(
            {
                "review_inbox_items": 0,
                "review_deferred_items": 0,
                "review_history_items": 0,
            }
        )
    if journal["contract_version"] >= 3:
        result.update({"source_review_inbox_items": 0, "source_review_deferred_items": 0,
                       "source_review_history_items": 0})
    if journal["contract_version"] >= 4:
        result["memory_count"] = 0
    return result


def _current_result(value: dict[str, int | str]) -> dict[str, int | str]:
    """Normalize a pristine legacy receipt to the current public result shape."""

    return {
        **value,
        "review_inbox_items": int(value.get("review_inbox_items", 0)),
        "review_deferred_items": int(value.get("review_deferred_items", 0)),
        "review_history_items": int(value.get("review_history_items", 0)),
        "source_review_inbox_items": int(value.get("source_review_inbox_items", 0)),
        "source_review_deferred_items": int(value.get("source_review_deferred_items", 0)),
        "source_review_history_items": int(value.get("source_review_history_items", 0)),
        "memory_count": int(value.get("memory_count", 0)),
    }


def _validate_pristine_vault(root: Path) -> None:
    from . import storage

    connection = storage._connect_without_workspace_reset_recovery(root)
    try:
        schema_version = storage._apply_migrations(connection)
        snapshot = storage._workspace_reset_snapshot(connection)
        table_names = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }
        table_counts = (
            {
                table_name: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                    ).fetchone()[0]
                )
                for table_name in PRISTINE_TABLE_COUNTS
            }
            if table_names == set(PRISTINE_TABLE_COUNTS)
            else {}
        )
        retention_row = (
            connection.execute(
                """
                SELECT singleton_id, current_generation, last_reset_request_id, reset_at_ms
                FROM profile_source_retention_state
                """
            ).fetchone()
            if "profile_source_retention_state" in table_names
            else None
        )
    finally:
        connection.close()
    if (
        schema_version != SCHEMA_VERSION
        or table_names != set(PRISTINE_TABLE_COUNTS)
        or table_counts != PRISTINE_TABLE_COUNTS
        or snapshot["source_retention_generation"] != 1
        or any(
            snapshot[field] != 0
            for field in RESET_EXPECTED_FIELDS - {"source_retention_generation"}
        )
        or retention_row is None
        or tuple(retention_row) != (1, 1, None, None)
    ):
        raise VaultError(
            "workspace_reset_recovery_failed",
            "The career workspace reset did not produce a pristine local vault.",
        )


def _finish_reset_locked(root: Path, journal: dict[str, Any]) -> None:
    phase = journal["phase"]
    if phase in {"prepared", "files_removed"}:
        _remove_managed_targets(root)
        journal = {**journal, "phase": "files_removed"}
        _write_state_file(root / RESET_JOURNAL_FILENAME, journal)
        _validate_pristine_vault(root)
        journal = {**journal, "phase": "initialized"}
        _write_state_file(root / RESET_JOURNAL_FILENAME, journal)
    else:
        _validate_pristine_vault(root)

    result = _base_result(journal)
    receipt = {
        "contract_version": journal["contract_version"],
        "request_id": journal["request_id"],
        "expected": journal["expected"],
        "result": result,
    }
    _write_state_file(root / RESET_RECEIPT_FILENAME, receipt)
    try:
        (root / RESET_JOURNAL_FILENAME).unlink()
    except FileNotFoundError:
        pass
    _fsync_directory(root)


def _recover_locked(root: Path) -> None:
    journal = _validated_journal(root)
    if journal is None:
        return
    try:
        _finish_reset_locked(root, journal)
    except VaultError:
        raise
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise VaultError(
            "workspace_reset_recovery_failed",
            "The interrupted career workspace reset could not be completed safely.",
        ) from exc


def recover_workspace_reset(data_dir: Path) -> None:
    """Finish a durable reset journal before an ordinary SQLite connection opens."""

    root = _lexical_root(data_dir)
    try:
        (root / RESET_JOURNAL_FILENAME).lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VaultError(
            "workspace_reset_journal_invalid",
            "The career workspace reset journal could not be inspected safely.",
        ) from exc
    root = _validated_root(root, create=False)
    with _reset_lock(root):
        _recover_locked(root)


def reset_workspace(data_dir: Path, params: Any) -> WorkspaceResetResult:
    if not isinstance(params, dict) or set(params) != {"request_id", "expected"}:
        raise ValueError("params must contain exactly request_id and expected")
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    expected = _validated_expected(params["expected"])
    root = _validated_root(data_dir, create=True)

    with _reset_lock(root):
        _recover_locked(root)
        receipt = _validated_receipt(root)
        if receipt is not None and receipt["request_id"] == request_id:
            receipt_expected_fields = _expected_fields(receipt["contract_version"])
            comparable_expected = {
                field: expected[field] for field in receipt_expected_fields
            }
            if receipt["expected"] != comparable_expected:
                raise VaultError(
                    "workspace_reset_request_conflict",
                    "This reset request identifier was already used with different expectations.",
                )
            return WorkspaceResetResult(
                **_current_result(receipt["result"]), created=False
            )

        from . import storage

        connection = storage._connect_without_workspace_reset_recovery(root)
        journal_written = False
        try:
            storage._apply_migrations(connection)
            storage._reconcile_artifacts(connection, root)
            storage._reconcile_profile_source_state(connection, root)
            connection.execute("BEGIN IMMEDIATE")
            actual = storage._workspace_reset_snapshot(connection)
            if actual != expected:
                raise VaultError(
                    "workspace_reset_conflict",
                    "The career workspace changed. Reload its status before resetting it.",
                )
            _preflight_managed_targets(root)
            journal = {
                "contract_version": RESET_CONTRACT_VERSION,
                "phase": "prepared",
                "request_id": request_id,
                "reset_at_ms": int(time.time() * 1000),
                "expected": expected,
            }
            _write_state_file(root / RESET_JOURNAL_FILENAME, journal)
            journal_written = True
        finally:
            connection.rollback()
            connection.close()

        if not journal_written:
            raise VaultError(
                "workspace_reset_recovery_failed",
                "The career workspace reset could not be prepared safely.",
            )
        _recover_locked(root)
        completed = _validated_receipt(root)
        if (
            completed is None
            or completed["request_id"] != request_id
            or completed["result"]["schema_version"] != SCHEMA_VERSION
        ):
            raise VaultError(
                "workspace_reset_recovery_failed",
                "The career workspace reset receipt could not be verified.",
            )
        return WorkspaceResetResult(
            **_current_result(completed["result"]), created=True
        )
