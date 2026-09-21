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
import time
import unittest
import uuid


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.cli import _handle_request  # noqa: E402
from cvgnome_engine.profile_manual_start import start_manual_profile  # noqa: E402
from cvgnome_engine.profile_versions import (  # noqa: E402
    get_profile_review_version,
    list_profile_versions,
    update_profile_basics,
)
from cvgnome_engine.storage import (  # noqa: E402
    DATABASE_FILENAME,
    MIGRATIONS,
    MIGRATION_APP_VERSIONS,
    MIGRATION_NAMES,
    SCHEMA_VERSION,
    VaultError,
    create_profile_source_scan,
    initialize_vault,
    latest_profile,
    save_profile,
)
from cvgnome_engine.workspace_reset import reset_workspace  # noqa: E402


class ManualProfileStartTests(unittest.TestCase):
    def _params(
        self,
        *,
        request_id: str | None = None,
        patch: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "expected_parent_profile_version_id": None,
            "request_id": request_id or str(uuid.uuid4()),
            "patch": patch or {"name": "Ada Lovelace"},
        }

    def _rpc(
        self,
        data_dir: Path,
        params: dict[str, object],
    ) -> dict[str, object]:
        return _handle_request(
            data_dir,
            {
                "protocol_version": 1,
                "id": str(uuid.uuid4()),
                "method": "profile.start.manual",
                "params": params,
            },
        )

    def _create_preview(self, data_dir: Path) -> str:
        scan_id = str(uuid.uuid4())
        content = b"Ada Lovelace\nComputing pioneer"
        staged = data_dir / "imports" / "staging" / scan_id / "0000.txt"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        extracted = content.decode()
        create_profile_source_scan(
            data_dir,
            scan_id=scan_id,
            base_profile_version_id=None,
            base_profile_checksum_sha256=None,
            items=[
                {
                    "ordinal": 0,
                    "managed_relative_path": f"imports/staging/{scan_id}/0000.txt",
                    "display_name": "resume.txt",
                    "format": "txt",
                    "media_type": "text/plain",
                    "source_kind": "resume",
                    "byte_size": len(content),
                    "checksum_sha256": hashlib.sha256(content).hexdigest(),
                    "parser_contract": "plain-text-v1",
                    "extraction_status": "parsed",
                    "extracted_text": extracted,
                    "extracted_text_sha256": hashlib.sha256(extracted.encode()).hexdigest(),
                    "candidate_profile": {"basics": {"name": "Ada Lovelace"}},
                    "warnings": [],
                }
            ],
            draft_profile={"basics": {"name": "Ada Lovelace"}},
            report={
                "synthesis_contract": "deterministic-local-v1",
                "conflicts": [],
                "ui": {"can_build": False},
            },
            expires_at_ms=int(time.time() * 1000) + 60_000,
        )
        return scan_id

    def test_start_is_atomic_public_and_has_no_source_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            request_id = str(uuid.uuid4())
            result = start_manual_profile(
                data_dir,
                self._params(
                    request_id=request_id,
                    patch={
                        "name": "  Ada Lovelace  ",
                        "headline": "Computing pioneer",
                        "summary": "Designed the first published algorithm.",
                        "email": "ada@example.com",
                        "phone": "+44 123 456",
                        "url": "https://example.com/ada",
                        "location": {
                            "city": "London",
                            "region": "England",
                            "country_code": "gb",
                        },
                    },
                ),
            )

            self.assertEqual(
                set(result),
                {
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
                },
            )
            self.assertEqual(result["request_id"], request_id)
            self.assertIsNone(result["parent_profile_version_id"])
            self.assertEqual(result["version_number"], 1)
            self.assertTrue(result["created"])
            self.assertTrue(result["renderable"])
            self.assertEqual(result["profile_name"], "Ada Lovelace")
            self.assertEqual(result["headline"], "Computing pioneer")
            self.assertEqual(
                result["changed_fields"],
                [
                    "name",
                    "headline",
                    "summary",
                    "email",
                    "phone",
                    "url",
                    "location.city",
                    "location.region",
                    "location.country_code",
                ],
            )
            self.assertLess(len(json.dumps(result).encode()), 4 * 1024)

            current = latest_profile(data_dir)
            self.assertEqual(current["id"], result["profile_version_id"])
            self.assertEqual(current["source"], "local_start")
            self.assertEqual(current["profile"]["basics"]["location"]["countryCode"], "GB")
            self.assertEqual(
                get_profile_review_version(data_dir, result["profile_version_id"])[
                    "source_kind"
                ],
                "manual_start",
            )
            self.assertEqual(
                list_profile_versions(data_dir, 0)["items"][0]["source_kind"],
                "manual_start",
            )
            with closing(sqlite3.connect(data_dir / DATABASE_FILENAME)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT parent_version_id FROM profile_versions"
                    ).fetchone()[0]
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM profile_versions").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM profile_manual_start_receipts"
                    ).fetchone()[0],
                    1,
                )
                for table in (
                    "sources",
                    "source_extractions",
                    "profile_source_imports",
                    "profile_version_sources",
                ):
                    self.assertEqual(
                        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                        0,
                        table,
                    )

    def test_exact_retry_resolves_before_later_profile_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            params = self._params(
                patch={"name": "Ada Lovelace", "headline": "Mathematician"}
            )
            first = start_manual_profile(data_dir, params)
            advanced = update_profile_basics(
                data_dir,
                {
                    "expected_parent_profile_version_id": first["profile_version_id"],
                    "request_id": str(uuid.uuid4()),
                    "patch": {"headline": "Programmer"},
                },
            )

            retry = start_manual_profile(data_dir, params)

            self.assertFalse(retry["created"])
            self.assertEqual(retry["profile_version_id"], first["profile_version_id"])
            self.assertEqual(retry["created_at_ms"], first["created_at_ms"])
            self.assertEqual(retry["headline"], "Mathematician")
            self.assertNotEqual(advanced["profile_version_id"], retry["profile_version_id"])
            self.assertEqual(initialize_vault(data_dir).profile_versions, 2)

    def test_optional_nulls_reuse_basic_details_noop_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            result = start_manual_profile(
                data_dir,
                self._params(
                    patch={
                        "name": "Ada Lovelace",
                        "headline": None,
                        "location": {"city": None, "country_code": None},
                    }
                ),
            )

            self.assertEqual(result["changed_fields"], ["name"])
            self.assertEqual(
                latest_profile(data_dir)["profile"],
                {"basics": {"name": "Ada Lovelace"}},
            )

    def test_changed_request_reuse_is_rejected_without_another_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            request_id = str(uuid.uuid4())
            start_manual_profile(
                data_dir,
                self._params(request_id=request_id, patch={"name": "Ada"}),
            )

            with self.assertRaises(VaultError) as conflict:
                start_manual_profile(
                    data_dir,
                    self._params(request_id=request_id, patch={"name": "Grace"}),
                )

            self.assertEqual(conflict.exception.code, "profile_start_request_conflict")
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)

    def test_existing_profile_and_unfinished_preview_are_distinct_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as existing_directory:
            existing_dir = Path(existing_directory)
            save_profile(existing_dir, {"basics": {"name": "Existing"}}, source="test")
            with self.assertRaises(VaultError) as conflict:
                start_manual_profile(existing_dir, self._params())
            self.assertEqual(conflict.exception.code, "profile_start_conflict")
            self.assertEqual(initialize_vault(existing_dir).profile_versions, 1)

        with tempfile.TemporaryDirectory() as preview_directory:
            preview_dir = Path(preview_directory)
            self._create_preview(preview_dir)
            with self.assertRaises(VaultError) as conflict:
                start_manual_profile(preview_dir, self._params())
            self.assertEqual(conflict.exception.code, "profile_start_preview_conflict")
            status = initialize_vault(preview_dir)
            self.assertEqual(status.profile_versions, 0)
            self.assertEqual(status.source_previews, 1)

    def test_competing_first_writers_create_exactly_one_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            params = [
                self._params(patch={"name": "Ada Lovelace"}),
                self._params(patch={"name": "Grace Hopper"}),
            ]

            def attempt(value: dict[str, object]) -> tuple[str, object]:
                try:
                    return "ok", start_manual_profile(data_dir, value)
                except VaultError as exc:
                    return "error", exc.code

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, params))

            self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
            self.assertEqual(
                [value for kind, value in results if kind == "error"],
                ["profile_start_conflict"],
            )
            with closing(sqlite3.connect(data_dir / DATABASE_FILENAME)) as connection:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM profile_versions").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM profile_manual_start_receipts"
                    ).fetchone()[0],
                    1,
                )

    def test_concurrent_exact_retries_share_one_durable_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            params = self._params(patch={"name": "Ada Lovelace"})
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(
                    executor.map(lambda _index: start_manual_profile(data_dir, params), range(4))
                )
            self.assertEqual(sum(bool(result["created"]) for result in results), 1)
            self.assertEqual(len({result["profile_version_id"] for result in results}), 1)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)

    def test_name_and_rpc_shape_are_required_without_writing(self) -> None:
        invalid_params = [
            self._params(patch={"headline": "No name"}),
            self._params(patch={"name": "   "}),
            self._params(patch={"name": None}),
            {
                **self._params(),
                "expected_parent_profile_version_id": str(uuid.uuid4()),
            },
            {**self._params(), "extra": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            for params in invalid_params:
                with self.subTest(params=params):
                    response = self._rpc(data_dir, params)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], "invalid_params")
            self.assertEqual(initialize_vault(data_dir).profile_versions, 0)

    def test_receipt_is_immutable_and_rejects_source_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            result = start_manual_profile(data_dir, self._params())
            database = data_dir / DATABASE_FILENAME
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE profile_manual_start_receipts SET created_at_ms = created_at_ms + 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM profile_manual_start_receipts")
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "manual start receipt",
                ):
                    connection.execute(
                        """
                        INSERT INTO profile_source_imports(
                            id, base_profile_version_id, base_profile_checksum_sha256,
                            output_profile_version_id, source_set_checksum_sha256,
                            synthesis_contract, report_json, result_json, committed_at_ms,
                            retention_generation
                        ) VALUES (?, NULL, NULL, ?, ?, 'test-v1', '{}', '{}', ?, 1)
                        """,
                        (
                            str(uuid.uuid4()),
                            result["profile_version_id"],
                            "0" * 64,
                            result["created_at_ms"],
                        ),
                    )

    def test_schema_fourteen_upgrade_preserves_profiles_and_adds_empty_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / DATABASE_FILENAME
            profile_id = str(uuid.uuid4())
            canonical_json = '{"basics":{"name":"Legacy"}}'
            checksum = hashlib.sha256(canonical_json.encode()).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 15):
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
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-v14', 1)
                    """,
                    (profile_id, canonical_json, checksum),
                )
                connection.execute("PRAGMA user_version = 14")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(status.profile_versions, 1)
            self.assertEqual(latest_profile(data_dir)["id"], profile_id)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM profile_manual_start_receipts"
                    ).fetchone()[0],
                    0,
                )
                migration = connection.execute(
                    """
                    SELECT name, checksum_sha256, app_version
                    FROM schema_migrations WHERE version = 15
                    """
                ).fetchone()
            self.assertEqual(
                migration,
                (
                    MIGRATION_NAMES[15],
                    hashlib.sha256(MIGRATIONS[15].encode()).hexdigest(),
                    MIGRATION_APP_VERSIONS[15],
                ),
            )

    def test_workspace_reset_removes_manual_receipt_and_restores_pristine_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            start_manual_profile(data_dir, self._params())
            status = initialize_vault(data_dir)
            expected = {
                "profile_versions": status.profile_versions,
                "opportunities": status.opportunities,
                "artifacts": status.artifacts,
                "source_imports": status.source_imports,
                "source_previews": status.source_previews,
                "source_retention_generation": status.source_retention_generation,
                "review_inbox_items": status.review_inbox_items,
                "review_deferred_items": status.review_deferred_items,
                "review_history_items": status.review_history_items,
                "source_review_inbox_items": status.source_review_inbox_items,
                "source_review_deferred_items": status.source_review_deferred_items,
                "source_review_history_items": status.source_review_history_items,
                "memory_count": status.memory_count,
            }

            reset = reset_workspace(
                data_dir,
                {"request_id": str(uuid.uuid4()), "expected": expected},
            ).to_dict()

            self.assertTrue(reset["created"])
            self.assertEqual(reset["schema_version"], SCHEMA_VERSION)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 0)
            with closing(sqlite3.connect(data_dir / DATABASE_FILENAME)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM profile_manual_start_receipts"
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
