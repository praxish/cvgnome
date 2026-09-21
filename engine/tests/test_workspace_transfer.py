# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine import workspace_transfer as transfer
from cvgnome_engine.legacy_compatibility import LEGACY_DATABASE_FILENAME
from cvgnome_engine.storage import DATABASE_FILENAME, SCHEMA_VERSION, initialize_vault, latest_profile, save_profile


class WorkspaceTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        initialize_vault(self.source)
        save_profile(self.source, {"basics": {"name": "Example Person", "summary": "Synthetic career story."}})
        for relative, data in {
            "sources/sha256/fixture.blob": b"original source evidence",
            "imports/staging/fixture/original.txt": b"unfinished source preview",
            "artifacts/fixture.pdf": b"synthetic managed artifact",
            "local-provider.json": b'{"synthetic":"provider settings; no key"}',
            "provider-spend.json": b'{"synthetic":"retained spend ledger"}',
        }.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.archive = self.root / "backup.zip"
        self.destination = self.root / "restored"

    def _backup(self) -> None:
        transfer.backup_workspace(self.source, self.archive, app_closed=True)

    def _bytes(self, root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def _rewrite(self, mutate) -> Path:
        with ZipFile(self.archive) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(entries["manifest.json"])
        mutate(entries, manifest)
        entries["manifest.json"] = json.dumps(manifest).encode()
        corrupted = self.root / "modified.zip"
        with ZipFile(corrupted, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return corrupted

    def test_round_trip_preserves_career_files_settings_and_database(self) -> None:
        original = self._bytes(self.source)
        expected_profile = latest_profile(self.source)
        self._backup()
        result = transfer.restore_workspace(self.archive, self.destination, app_closed=True)
        self.assertEqual(result, {"operation": "restore", "schema_version": SCHEMA_VERSION, "installed": True})
        self.assertEqual(latest_profile(self.destination), expected_profile)
        for relative, content in original.items():
            if not relative.startswith(DATABASE_FILENAME):
                self.assertEqual((self.destination / relative).read_bytes(), content)
        self.assertEqual(self._bytes(self.source), original)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        with ZipFile(self.archive) as archive:
            self.assertNotIn(f"{DATABASE_FILENAME}-wal", archive.namelist())
            self.assertNotIn(f"{DATABASE_FILENAME}-shm", archive.namelist())

    def test_committed_wal_is_snapshotted_without_touching_source(self) -> None:
        connection = sqlite3.connect(self.source / DATABASE_FILENAME)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO settings VALUES ('wal-only', '{}', 1, 1)")
        connection.commit()
        before = self._bytes(self.source)
        self.assertIn(f"{DATABASE_FILENAME}-wal", before)
        self._backup()
        self.assertEqual(self._bytes(self.source), before)
        transfer.restore_workspace(self.archive, self.destination, app_closed=True)
        restored = sqlite3.connect(self.destination / DATABASE_FILENAME)
        try:
            self.assertEqual(restored.execute("SELECT value_json FROM settings WHERE key='wal-only'").fetchone(), ("{}",))
        finally:
            restored.close()

    def test_explicit_legacy_migration_preserves_original(self) -> None:
        (self.source / DATABASE_FILENAME).rename(self.source / LEGACY_DATABASE_FILENAME)
        original = self._bytes(self.source)
        result = transfer.migrate_workspace(self.source, self.destination, app_closed=True)
        self.assertEqual(result["operation"], "migrate")
        self.assertTrue((self.destination / DATABASE_FILENAME).is_file())
        self.assertFalse((self.destination / LEGACY_DATABASE_FILENAME).exists())
        self.assertEqual(latest_profile(self.destination)["profile"]["basics"]["name"], "Example Person")
        self.assertEqual(self._bytes(self.source), original)

    def test_offline_acknowledgement_and_new_destination_are_required(self) -> None:
        with self.assertRaisesRegex(transfer.WorkspaceTransferError, "Close the desktop"):
            transfer.backup_workspace(self.source, self.archive)
        self._backup()
        self.destination.mkdir()
        with self.assertRaisesRegex(transfer.WorkspaceTransferError, "does not exist"):
            transfer.restore_workspace(self.archive, self.destination, app_closed=True)
        self.assertEqual(list(self.destination.iterdir()), [])
        original = self.archive.read_bytes()
        with self.assertRaises(transfer.WorkspaceTransferError):
            self._backup()
        self.assertEqual(self.archive.read_bytes(), original)

    def test_raced_empty_destination_is_not_overwritten(self) -> None:
        self._backup()
        publish = transfer._publish_directory
        def race(stage: Path, destination: Path) -> None:
            destination.mkdir()
            publish(stage, destination)
        with patch.object(transfer, "_publish_directory", side_effect=race):
            with self.assertRaises(transfer.WorkspaceTransferError):
                transfer.restore_workspace(self.archive, self.destination, app_closed=True)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_corrupted_file_rejects_restore_without_publishing(self) -> None:
        self._backup()
        def corrupt(entries, manifest):
            entries["artifacts/fixture.pdf"] = b"different artifact content"
        corrupted = self._rewrite(corrupt)
        with self.assertRaises(transfer.WorkspaceTransferError):
            transfer.restore_workspace(corrupted, self.destination, app_closed=True)
        self.assertFalse(self.destination.exists())

    def test_matching_manifest_hash_does_not_bypass_database_validation(self) -> None:
        self._backup()
        def corrupt(entries, manifest):
            data = b"not a SQLite database"
            entries[DATABASE_FILENAME] = data
            record = next(entry for entry in manifest["files"] if entry["path"] == DATABASE_FILENAME)
            record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        corrupted = self._rewrite(corrupt)
        with self.assertRaises((transfer.WorkspaceTransferError, sqlite3.Error)):
            transfer.restore_workspace(corrupted, self.destination, app_closed=True)
        self.assertFalse(self.destination.exists())

    def test_traversal_archive_is_rejected_before_extraction(self) -> None:
        self._backup()
        def corrupt(entries, manifest):
            data = entries.pop("artifacts/fixture.pdf")
            entries["../escaped.pdf"] = data
            record = next(entry for entry in manifest["files"] if entry["path"] == "artifacts/fixture.pdf")
            record["path"] = "../escaped.pdf"
        corrupted = self._rewrite(corrupt)
        with self.assertRaises(transfer.WorkspaceTransferError):
            transfer.restore_workspace(corrupted, self.destination, app_closed=True)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "escaped.pdf").exists())

    def test_symlinks_in_source_or_archive_are_rejected(self) -> None:
        (self.source / "sources/link").symlink_to(self.root / "outside")
        with self.assertRaises(transfer.WorkspaceTransferError):
            self._backup()
        self.assertFalse(self.archive.exists())
        (self.source / "sources/link").unlink()
        self._backup()
        linked = self.root / "linked.zip"
        with ZipFile(self.archive) as archive, ZipFile(linked, "w") as modified:
            for member in archive.infolist():
                data = archive.read(member)
                if member.filename == "artifacts/fixture.pdf":
                    member.external_attr = (stat.S_IFLNK | 0o777) << 16
                modified.writestr(member, data)
        with self.assertRaises(transfer.WorkspaceTransferError):
            transfer.restore_workspace(linked, self.destination, app_closed=True)
        self.assertFalse(self.destination.exists())

    def test_source_change_aborts_backup_and_preserves_changed_source(self) -> None:
        inventory = transfer._inventory
        count = 0
        def changed(root, database):
            nonlocal count
            count += 1
            if count == 2:
                (root / "provider-spend.json").write_text("changed by another writer")
            return inventory(root, database)
        with patch.object(transfer, "_inventory", side_effect=changed):
            with self.assertRaisesRegex(transfer.WorkspaceTransferError, "source changed"):
                self._backup()
        self.assertFalse(self.archive.exists())
        self.assertEqual((self.source / "provider-spend.json").read_text(), "changed by another writer")

    def test_cli_backup_and_restore_emit_safe_results(self) -> None:
        for operation, extra in [("backup", ["--output", str(self.archive)]),
                                 ("restore", ["--archive", str(self.archive)])]:
            directory = self.source if operation == "backup" else self.destination
            result = subprocess.run(
                [sys.executable, "-m", "cvgnome_engine", operation,
                 "--data-dir", str(directory), "--app-closed", *extra],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PYTHONPATH": str(ENGINE_SRC)},
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["result"]["operation"], operation)


if __name__ == "__main__":
    unittest.main()
