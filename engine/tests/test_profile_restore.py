# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.cli import _handle_request  # noqa: E402
from cvgnome_engine.profile_versions import (  # noqa: E402
    diff_profile_versions,
    list_profile_versions,
    restore_profile_version,
)
from cvgnome_engine.storage import (  # noqa: E402
    MIGRATIONS,
    MIGRATION_APP_VERSIONS,
    MIGRATION_NAMES,
    SCHEMA_VERSION,
    VaultError,
    initialize_vault,
    latest_profile,
    save_profile,
)


class ProfileRestoreTests(unittest.TestCase):
    def _save(
        self,
        data_dir: Path,
        profile: dict[str, object],
        *,
        source: str = "file_import:test",
    ):
        return save_profile(data_dir, profile, source=source)

    def _restore_params(
        self,
        *,
        source_id: str,
        parent_id: str,
        request_id: str | None = None,
    ) -> dict[str, str]:
        return {
            "source_profile_version_id": source_id,
            "expected_parent_profile_version_id": parent_id,
            "request_id": request_id or str(uuid.uuid4()),
        }

    def _request(
        self,
        data_dir: Path,
        params: dict[str, object],
    ) -> dict[str, object]:
        return _handle_request(
            data_dir,
            {
                "protocol_version": 1,
                "id": str(uuid.uuid4()),
                "method": "profile.versions.restore",
                "params": params,
            },
        )

    def test_restore_appends_exact_historical_snapshot_with_public_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source_profile = {
                "basics": {
                    "name": "Ada Lovelace",
                    "label": "Analytical Engineer",
                    "private_identity": {"token": "never return this"},
                },
                "work": [
                    {
                        "name": "Difference Engine",
                        "position": "Programmer",
                        "private_claim": "preserve exactly",
                    }
                ],
                "meta": {
                    "raw_source_path": "/private/resume.pdf",
                    "nested": {"keep": [3, 2, 1]},
                },
                "unknown_extension": {"enabled": True},
            }
            source = self._save(data_dir, source_profile)
            parent = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada Lovelace", "label": "Principal Engineer"},
                    "work": [{"name": "New Employer", "position": "Principal"}],
                },
                source="local_edit",
            )
            database_path = initialize_vault(data_dir).database_path
            with closing(sqlite3.connect(database_path)) as connection:
                source_row = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions WHERE id = ?",
                    (source.id,),
                ).fetchone()

            result = restore_profile_version(
                data_dir,
                self._restore_params(source_id=source.id, parent_id=parent.id),
            )

            self.assertEqual(
                set(result),
                {
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
                },
            )
            self.assertTrue(result["created"])
            self.assertEqual(result["parent_profile_version_id"], parent.id)
            self.assertEqual(result["restored_from_profile_version_id"], source.id)
            self.assertEqual(result["restored_from_version_number"], 1)
            self.assertEqual(result["version_number"], 3)
            self.assertEqual(result["profile_name"], "Ada Lovelace")
            self.assertIsInstance(result["renderable"], bool)
            self.assertLess(len(json.dumps(result).encode("utf-8")), 2_048)
            self.assertNotIn("checksum", result)
            self.assertNotIn("private", json.dumps(result))

            current = latest_profile(data_dir)
            self.assertEqual(current["id"], result["profile_version_id"])
            self.assertEqual(current["source"], "local_restore")
            self.assertEqual(current["profile"], source_profile)
            with closing(sqlite3.connect(database_path)) as connection:
                output_row = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions WHERE id = ?",
                    (result["profile_version_id"],),
                ).fetchone()
            self.assertEqual(output_row, source_row)

            history = list_profile_versions(data_dir, 0)
            self.assertEqual(history["current_profile_version_id"], result["profile_version_id"])
            self.assertEqual(history["items"][0]["source_kind"], "local_restore")
            self.assertEqual(
                diff_profile_versions(
                    data_dir,
                    source.id,
                    str(result["profile_version_id"]),
                )["total_changes"],
                0,
            )

    def test_nameless_historical_profile_uses_bounded_receipt_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source = self._save(
                data_dir,
                {
                    "basics": {"label": "Identity pending"},
                    "meta": {"private": "preserved but not returned"},
                },
            )
            parent = self._save(
                data_dir,
                {"basics": {"name": "Ada", "summary": "Current"}},
            )

            result = restore_profile_version(
                data_dir,
                self._restore_params(source_id=source.id, parent_id=parent.id),
            )

            self.assertEqual(result["profile_name"], "Unnamed profile")
            self.assertFalse(result["renderable"])
            self.assertNotIn("private", json.dumps(result))

    def test_distinct_json_scalars_restore_by_checksum_not_python_equality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada", "summary": "Same public profile"},
                    "extension": {"exact_scalar": True},
                },
            )
            parent = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada", "summary": "Same public profile"},
                    "extension": {"exact_scalar": 1},
                },
                source="local_edit",
            )
            self.assertEqual(
                latest_profile(data_dir)["profile"],
                {
                    "basics": {"name": "Ada", "summary": "Same public profile"},
                    "extension": {"exact_scalar": 1},
                },
            )
            self.assertEqual(
                {"exact_scalar": True},
                {"exact_scalar": 1},
                "This regression relies on Python object equality collapsing JSON scalars",
            )
            database_path = initialize_vault(data_dir).database_path
            with closing(sqlite3.connect(database_path)) as connection:
                source_row = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions WHERE id = ?",
                    (source.id,),
                ).fetchone()
                parent_row = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions WHERE id = ?",
                    (parent.id,),
                ).fetchone()
            self.assertNotEqual(source_row, parent_row)

            restored = restore_profile_version(
                data_dir,
                self._restore_params(source_id=source.id, parent_id=parent.id),
            )

            self.assertTrue(restored["created"])
            with closing(sqlite3.connect(database_path)) as connection:
                output_row = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions WHERE id = ?",
                    (restored["profile_version_id"],),
                ).fetchone()
            self.assertEqual(output_row, source_row)
            self.assertNotEqual(output_row, parent_row)

    def test_exact_retry_is_durable_even_after_history_advances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source = self._save(data_dir, {"basics": {"name": "Ada", "summary": "Old"}})
            parent = self._save(data_dir, {"basics": {"name": "Ada", "summary": "New"}})
            params = self._restore_params(source_id=source.id, parent_id=parent.id)

            created = restore_profile_version(data_dir, params)
            later = self._save(
                data_dir,
                {"basics": {"name": "Ada", "summary": "Later"}},
                source="local_edit",
            )
            replayed = restore_profile_version(data_dir, params)

            self.assertTrue(created["created"])
            self.assertFalse(replayed["created"])
            self.assertEqual(
                {key: value for key, value in created.items() if key != "created"},
                {key: value for key, value in replayed.items() if key != "created"},
            )
            self.assertEqual(latest_profile(data_dir)["id"], later.id)
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 4)

            with self.assertRaisesRegex(VaultError, "different inputs") as conflict:
                restore_profile_version(
                    data_dir,
                    {
                        **params,
                        "expected_parent_profile_version_id": later.id,
                    },
                )
            self.assertEqual(conflict.exception.code, "profile_update_request_conflict")

    def test_concurrent_exact_retries_append_only_one_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source = self._save(data_dir, {"basics": {"name": "Ada", "summary": "Old"}})
            parent = self._save(data_dir, {"basics": {"name": "Ada", "summary": "New"}})
            params = self._restore_params(source_id=source.id, parent_id=parent.id)

            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(
                    executor.map(
                        lambda _index: restore_profile_version(data_dir, params),
                        range(6),
                    )
                )

            self.assertEqual(sum(bool(result["created"]) for result in results), 1)
            self.assertEqual(
                len({str(result["profile_version_id"]) for result in results}),
                1,
            )
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 3)

    def test_stale_parent_current_source_missing_source_and_noop_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = self._save(data_dir, {"basics": {"name": "Ada", "summary": "One"}})
            second = self._save(data_dir, {"basics": {"name": "Ada", "summary": "Two"}})
            current = self._save(data_dir, {"basics": {"name": "Ada", "summary": "Three"}})

            with self.assertRaises(VaultError) as stale:
                restore_profile_version(
                    data_dir,
                    self._restore_params(source_id=first.id, parent_id=second.id),
                )
            self.assertEqual(stale.exception.code, "profile_update_conflict")

            with self.assertRaisesRegex(ValueError, "historical version"):
                restore_profile_version(
                    data_dir,
                    self._restore_params(source_id=current.id, parent_id=current.id),
                )

            with self.assertRaises(VaultError) as missing:
                restore_profile_version(
                    data_dir,
                    self._restore_params(
                        source_id=str(uuid.uuid4()),
                        parent_id=current.id,
                    ),
                )
            self.assertEqual(missing.exception.code, "profile_version_not_found")
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 3)

            restored = restore_profile_version(
                data_dir,
                self._restore_params(source_id=first.id, parent_id=current.id),
            )
            with self.assertRaisesRegex(ValueError, "already matches"):
                restore_profile_version(
                    data_dir,
                    self._restore_params(
                        source_id=first.id,
                        parent_id=str(restored["profile_version_id"]),
                    ),
                )
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 4)

    def test_rpc_contract_is_exact_and_maps_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source = self._save(data_dir, {"basics": {"name": "Ada", "summary": "Old"}})
            parent = self._save(data_dir, {"basics": {"name": "Ada", "summary": "New"}})
            valid = self._restore_params(source_id=source.id, parent_id=parent.id)

            response = self._request(data_dir, valid)
            self.assertTrue(response["ok"])
            receipt = response["result"]
            self.assertEqual(receipt["restored_from_profile_version_id"], source.id)

            invalid_params = (
                {},
                {**valid, "extra": True},
                {**valid, "request_id": valid["request_id"].upper()},
                {**valid, "source_profile_version_id": 1},
            )
            for params in invalid_params:
                with self.subTest(params=params):
                    invalid = self._request(data_dir, params)
                    self.assertFalse(invalid["ok"])
                    self.assertEqual(invalid["error"]["code"], "invalid_params")

    def test_receipts_are_immutable_and_trigger_rejects_forged_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            source = self._save(data_dir, {"basics": {"name": "Ada", "summary": "Old"}})
            parent = self._save(data_dir, {"basics": {"name": "Ada", "summary": "New"}})
            result = restore_profile_version(
                data_dir,
                self._restore_params(source_id=source.id, parent_id=parent.id),
            )
            self._save(
                data_dir,
                {"basics": {"name": "Ada", "summary": "After restore"}},
                source="local_edit",
            )
            database_path = initialize_vault(data_dir).database_path

            with closing(sqlite3.connect(database_path)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_restore_receipts "
                        "SET created_at_ms = created_at_ms + 1"
                    )
                connection.rollback()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_restore_receipts")
                connection.rollback()

                current = connection.execute(
                    """
                    SELECT id, version_number, canonical_json, checksum_sha256, created_at_ms
                    FROM profile_versions ORDER BY version_number DESC LIMIT 1
                    """
                ).fetchone()
                forged_output_id = str(uuid.uuid4())
                forged_json = '{"basics":{"name":"Forged"}}'
                forged_checksum = hashlib.sha256(forged_json.encode()).hexdigest()
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'local_restore', ?)
                    """,
                    (
                        forged_output_id,
                        int(current[1]) + 1,
                        current[0],
                        forged_json,
                        forged_checksum,
                        current[4],
                    ),
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "restore receipt lineage is invalid",
                ):
                    connection.execute(
                        """
                        INSERT INTO profile_restore_receipts(
                            request_id, request_fingerprint, output_profile_version_id,
                            parent_profile_version_id, source_profile_version_id,
                            created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            "a" * 64,
                            forged_output_id,
                            current[0],
                            source.id,
                            current[4],
                        ),
                    )
                connection.rollback()

            replay = restore_profile_version(
                data_dir,
                {
                    "source_profile_version_id": source.id,
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(result["request_id"]),
                },
            )
            self.assertFalse(replay["created"])

    def test_schema_eleven_migration_preserves_profile_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            source_id = str(uuid.uuid4())
            parent_id = str(uuid.uuid4())
            source_json = '{"basics":{"name":"Ada","summary":"Old"}}'
            parent_json = '{"basics":{"name":"Ada","summary":"Current"}}'
            source_checksum = hashlib.sha256(source_json.encode()).hexdigest()
            parent_checksum = hashlib.sha256(parent_json.encode()).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 12):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(
                            version, name, checksum_sha256, applied_at_ms, app_version
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            version,
                            MIGRATION_NAMES[version],
                            hashlib.sha256(MIGRATIONS[version].encode()).hexdigest(),
                            version,
                            MIGRATION_APP_VERSIONS[version],
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-v11', 1)
                    """,
                    (source_id, source_json, source_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 2, ?, ?, ?, 'legacy-v11', 2)
                    """,
                    (parent_id, source_id, parent_json, parent_checksum),
                )
                connection.execute("PRAGMA user_version = 11")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(status.profile_versions, 2)
            self.assertEqual(latest_profile(data_dir)["id"], parent_id)
            with closing(sqlite3.connect(database)) as connection:
                profiles = connection.execute(
                    """
                    SELECT id, version_number, parent_version_id, canonical_json,
                           checksum_sha256, source, created_at_ms
                    FROM profile_versions ORDER BY version_number
                    """
                ).fetchall()
                receipt_count = connection.execute(
                    "SELECT count(*) FROM profile_restore_receipts"
                ).fetchone()[0]
                migration = connection.execute(
                    """
                    SELECT name, checksum_sha256, app_version
                    FROM schema_migrations WHERE version = 12
                    """
                ).fetchone()
            self.assertEqual(
                profiles,
                [
                    (
                        source_id,
                        1,
                        None,
                        source_json,
                        source_checksum,
                        "legacy-v11",
                        1,
                    ),
                    (
                        parent_id,
                        2,
                        source_id,
                        parent_json,
                        parent_checksum,
                        "legacy-v11",
                        2,
                    ),
                ],
            )
            self.assertEqual(receipt_count, 0)
            self.assertEqual(
                migration,
                (
                    MIGRATION_NAMES[12],
                    hashlib.sha256(MIGRATIONS[12].encode()).hexdigest(),
                    MIGRATION_APP_VERSIONS[12],
                ),
            )


if __name__ == "__main__":
    unittest.main()
