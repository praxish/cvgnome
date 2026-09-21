# SPDX-License-Identifier: MPL-2.0
"""Offline, non-overwriting career-workspace backup, restore, and migration.

Archives contain career data and local provider settings/spend records, not OS
credentials or webview preferences. They are not encrypted. The caller must
close the desktop app and acknowledge that requirement. A metadata recheck
detects ordinary concurrent changes; it is not a cross-process writer lock.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import tempfile
from typing import Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from .storage import APPLICATION_ID, DATABASE_FILENAME, MIGRATIONS, MIGRATION_NAMES, SCHEMA_VERSION
from .workspace_reset import RESET_JOURNAL_FILENAME, RESET_RECEIPT_FILENAME

MAX_FILES = 20_000
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_ROOT_FILES = {DATABASE_FILENAME, "local-provider.json", "provider-spend.json", RESET_RECEIPT_FILENAME}
_DIRECTORIES = {"sources", "imports", "artifacts"}


class WorkspaceTransferError(ValueError):
    pass


def _require_closed(app_closed: bool) -> None:
    if app_closed is not True:
        raise WorkspaceTransferError("Close the desktop app first, then supply --app-closed.")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or ":" in value:
        raise WorkspaceTransferError("The archive contains an unsafe path.")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {".", ".."} for part in path.parts):
        raise WorkspaceTransferError("The archive contains an unsafe path.")
    if any(ord(char) < 32 for char in value) or not (
        value in _ROOT_FILES or (len(path.parts) > 1 and path.parts[0] in _DIRECTORIES)
    ):
        raise WorkspaceTransferError("The archive contains an unsupported workspace path.")
    return value


def _source_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceTransferError("The source must be a real directory, not a link.")
    return path.resolve()


def _inventory(root: Path, database_name: str) -> dict[str, tuple[int, ...]]:
    if (root / RESET_JOURNAL_FILENAME).exists() or (root / RESET_JOURNAL_FILENAME).is_symlink():
        raise WorkspaceTransferError("Finish the interrupted workspace reset before making a backup.")
    if (root / f"{database_name}-journal").exists():
        raise WorkspaceTransferError("Open and close the original app to recover its database journal before backing up.")
    found: dict[str, tuple[int, ...]] = {}
    root_files = (_ROOT_FILES - {DATABASE_FILENAME}) | {database_name, f"{database_name}-wal"}
    candidates = [root / name for name in root_files | _DIRECTORIES]
    while candidates:
        path = candidates.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        relative = path.relative_to(root).as_posix()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise WorkspaceTransferError("Workspace links and special files cannot be backed up.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise WorkspaceTransferError("Hard-linked workspace files cannot be backed up.")
        found[relative] = _stamp(info)
        if len(found) > MAX_FILES:
            raise WorkspaceTransferError("The workspace exceeds the backup file limit.")
        if stat.S_ISDIR(info.st_mode):
            if relative.split("/")[0] not in _DIRECTORIES:
                raise WorkspaceTransferError("A workspace file is unexpectedly a directory.")
            candidates.extend(path.iterdir())
        elif info.st_size > MAX_FILE_BYTES:
            raise WorkspaceTransferError("A workspace file exceeds the backup size limit.")
    if database_name not in found or not stat.S_ISREG(found[database_name][2]):
        raise WorkspaceTransferError("The source workspace database is missing.")
    if sum(entry[3] for entry in found.values() if stat.S_ISREG(entry[2])) > MAX_TOTAL_BYTES:
        raise WorkspaceTransferError("The workspace exceeds the backup size limit.")
    return found


def _copy_source(root: Path, relative: str, destination: Path, expected: tuple[int, ...]) -> None:
    # Walk from an open directory descriptor; O_NOFOLLOW protects every component.
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise WorkspaceTransferError("Secure offline workspace copying is unavailable on this platform.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(file_descriptor, "rb") as source, destination.open("xb") as target:
        os.chmod(destination, 0o600)
        if _stamp(os.fstat(source.fileno())) != expected:
            raise WorkspaceTransferError("The source changed; close the app and retry.")
        remaining = expected[3]
        while chunk := source.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise WorkspaceTransferError("The source changed; close the app and retry.")
            target.write(chunk)
        if remaining or _stamp(os.fstat(source.fileno())) != expected:
            raise WorkspaceTransferError("The source changed; close the app and retry.")
        target.flush()
        os.fsync(target.fileno())


def _validate_database(path: Path, expected_schema: int | None = None) -> int:
    # Only self-contained snapshots created/extracted in our private staging
    # directory reach this verifier; immutable mode avoids creating WAL/SHM.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if not 1 <= version <= SCHEMA_VERSION or expected_schema not in (None, version):
            raise WorkspaceTransferError("The backup database schema is unsupported or mismatched.")
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
            raise WorkspaceTransferError("The backup does not contain a CVGnome database.")
        rows = connection.execute("SELECT version, name, checksum_sha256 FROM schema_migrations ORDER BY version").fetchall()
        expected = [(number, MIGRATION_NAMES[number], hashlib.sha256(MIGRATIONS[number].encode()).hexdigest())
                    for number in range(1, version + 1)]
        if rows != expected or connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise WorkspaceTransferError("The backup database failed its integrity or migration-history check.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkspaceTransferError("The backup database contains invalid relationships.")
        return version
    finally:
        connection.close()


def _snapshot(source: Path, stage: Path, database_name: str) -> int:
    source = _source_root(source)
    before = _inventory(source, database_name)
    with tempfile.TemporaryDirectory(prefix=".cvgnome-db-", dir=stage.parent) as temporary:
        scratch = Path(temporary)
        for relative, info in before.items():
            if not stat.S_ISREG(info[2]):
                continue
            if relative in {database_name, f"{database_name}-wal"}:
                target = scratch / relative.replace(database_name, DATABASE_FILENAME, 1)
            else:
                _safe_path(relative)
                target = stage / relative
            _copy_source(source, relative, target, info)
        # SQLite sees only the copied DB/WAL. It cannot create SHM or change any
        # file in the source, even when recovering committed WAL frames.
        original = sqlite3.connect((scratch / DATABASE_FILENAME).as_uri() + "?mode=ro", uri=True)
        snapshot = sqlite3.connect(stage / DATABASE_FILENAME)
        try:
            original.backup(snapshot)
        finally:
            snapshot.close()
            original.close()
        os.chmod(stage / DATABASE_FILENAME, 0o600)
    if _inventory(source, database_name) != before:
        raise WorkspaceTransferError("The source changed; close the app and retry.")
    return _validate_database(stage / DATABASE_FILENAME)


def _manifest(stage: Path, schema_version: int) -> dict[str, Any]:
    entries = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            relative = _safe_path(path.relative_to(stage).as_posix())
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append({"path": relative, "size": path.stat().st_size, "sha256": digest})
    return {"format_version": 1, "schema_version": schema_version, "files": entries}


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_workspace(source: Path, output: Path, *, app_closed: bool = False) -> dict[str, Any]:
    _require_closed(app_closed)
    source = _source_root(source)
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink() or output.parent.resolve().is_relative_to(source):
        raise WorkspaceTransferError("Choose a new backup filename outside the source workspace.")
    with tempfile.TemporaryDirectory(prefix=".cvgnome-backup-", dir=output.parent) as temporary:
        stage = Path(temporary) / "workspace"
        stage.mkdir(mode=0o700)
        version = _snapshot(source, stage, DATABASE_FILENAME)
        manifest = _manifest(stage, version)
        encoded = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise WorkspaceTransferError("The backup manifest exceeds its size limit.")
        archive_path = Path(temporary) / "archive.zip"
        with ZipFile(archive_path, "x", compression=ZIP_DEFLATED) as archive:
            for entry in manifest["files"]:
                archive.write(stage / entry["path"], entry["path"])
            archive.writestr("manifest.json", encoded)
        os.chmod(archive_path, 0o600)
        with archive_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(archive_path, output)  # Atomic publication, never replaces a raced destination.
        try:
            _sync_directory(output.parent)
        except OSError:
            pass  # The complete archive has already been published.
    return {"operation": "backup", "files": len(manifest["files"]), "schema_version": version}


def _read_manifest(archive: ZipFile) -> dict[str, Any]:
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(members) > MAX_FILES + 1 or len(set(names)) != len(names) or "manifest.json" not in names:
        raise WorkspaceTransferError("The backup has duplicate, missing, or excessive archive entries.")
    if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
        raise WorkspaceTransferError("The backup manifest exceeds its size limit.")
    for member in members:
        mode = member.external_attr >> 16
        if member.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or member.flag_bits & 1:
            raise WorkspaceTransferError("Backup links, directories, and encrypted members are unsupported.")
        if member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
            raise WorkspaceTransferError("The backup compression format is unsupported.")
    manifest = json.loads(archive.read("manifest.json"))
    if not isinstance(manifest, dict) or set(manifest) != {"format_version", "schema_version", "files"}:
        raise WorkspaceTransferError("The backup manifest is invalid.")
    if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
        raise WorkspaceTransferError("The backup format is unsupported.")
    version = manifest["schema_version"]
    entries = manifest["files"]
    if type(version) is not int or not 1 <= version <= SCHEMA_VERSION or not isinstance(entries, list):
        raise WorkspaceTransferError("The backup manifest is invalid.")
    expected = {"manifest.json"}
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise WorkspaceTransferError("The backup file manifest is invalid.")
        path = _safe_path(entry["path"])
        size, checksum = entry["size"], entry["sha256"]
        if path in expected or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise WorkspaceTransferError("The backup file manifest is invalid.")
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise WorkspaceTransferError("The backup checksum manifest is invalid.")
        expected.add(path)
        total += size
    if set(names) != expected or DATABASE_FILENAME not in expected or total > MAX_TOTAL_BYTES:
        raise WorkspaceTransferError("The backup has missing, unexpected, or oversized files.")
    return manifest


def _extract(archive_path: Path, stage: Path) -> int:
    with ZipFile(archive_path) as archive:
        manifest = _read_manifest(archive)
        for entry in manifest["files"]:
            member = archive.getinfo(entry["path"])
            if member.file_size != entry["size"]:
                raise WorkspaceTransferError("The backup file size does not match its manifest.")
            destination = stage / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest, remaining = hashlib.sha256(), entry["size"]
            with archive.open(member) as source, destination.open("xb") as target:
                os.chmod(destination, 0o600)
                while chunk := source.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise WorkspaceTransferError("The backup file exceeds its declared size.")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if remaining or digest.hexdigest() != entry["sha256"]:
                raise WorkspaceTransferError("The backup file failed its checksum check.")
    return _validate_database(stage / DATABASE_FILENAME, manifest["schema_version"])


def _publish_directory(stage: Path, destination: Path) -> None:
    # Unlike os.rename on POSIX, these APIs refuse even an existing empty target.
    libc = ctypes.CDLL(None, use_errno=True)
    required = "renamex_np" if sys.platform == "darwin" else "renameat2"
    if not hasattr(libc, required):
        raise WorkspaceTransferError("Atomic non-overwriting restoration is unavailable on this platform.")
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(stage), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise WorkspaceTransferError("Atomic workspace restoration is unavailable on this platform.")
    if result != 0:
        raise WorkspaceTransferError("The destination appeared or the workspace could not be installed; nothing was overwritten.")
    try:
        _sync_directory(destination.parent)
    except OSError:
        pass  # Publication succeeded; do not report an unchanged destination.


def _install(destination: Path, populate: Callable[[Path], int]) -> dict[str, Any]:
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise WorkspaceTransferError("Restore and migration require a destination that does not exist.")
    with tempfile.TemporaryDirectory(prefix=".cvgnome-restore-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "workspace"
        stage.mkdir(mode=0o700)
        populate(stage)
        from .storage import _apply_migrations, _connect_without_workspace_reset_recovery
        connection = _connect_without_workspace_reset_recovery(stage)
        try:
            _apply_migrations(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        _validate_database(stage / DATABASE_FILENAME, SCHEMA_VERSION)
        for directory in sorted((path for path in stage.rglob("*") if path.is_dir()),
                                key=lambda path: len(path.parts), reverse=True):
            os.chmod(directory, 0o700)
            _sync_directory(directory)
        _sync_directory(stage)
        _publish_directory(stage, destination)
    return {"schema_version": SCHEMA_VERSION, "installed": True}


def restore_workspace(archive: Path, destination: Path, *, app_closed: bool = False) -> dict[str, Any]:
    _require_closed(app_closed)
    return {"operation": "restore", **_install(destination, lambda stage: _extract(archive, stage))}


def migrate_workspace(source: Path, destination: Path, *, app_closed: bool = False) -> dict[str, Any]:
    _require_closed(app_closed)
    from .legacy_compatibility import LEGACY_DATABASE_FILENAME
    source = _source_root(source)
    if destination.expanduser().absolute().parent.resolve().is_relative_to(source):
        raise WorkspaceTransferError("Choose a migration destination outside the original workspace.")
    return {"operation": "migrate", **_install(destination, lambda stage: _snapshot(source, stage, LEGACY_DATABASE_FILENAME))}
