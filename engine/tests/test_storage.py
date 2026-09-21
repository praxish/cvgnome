# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest import mock

from pypdf import PdfWriter

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

import cvgnome_engine.storage as storage_module  # noqa: E402
from cvgnome_engine.storage import (  # noqa: E402
    MIGRATION_APP_VERSIONS,
    MIGRATIONS,
    MIGRATION_NAMES,
    SCHEMA_VERSION,
    VaultError,
    commit_profile_source_scan,
    create_profile_source_scan,
    discard_all_profile_source_scans,
    discard_profile_source_scan,
    get_profile_version,
    import_canonical_profile_source,
    initialize_vault,
    latest_profile,
    reset_profile_source_retention,
    save_artifact_bytes,
    save_profile,
)
from cvgnome_engine.opportunities import list_opportunities  # noqa: E402


class StorageTests(unittest.TestCase):
    def _stage_canonical(
        self,
        data_dir: Path,
        content: bytes,
        *,
        scan_id: str | None = None,
        display_name: str = "private/canonical-profile.json",
    ) -> dict[str, object]:
        scan_id = scan_id or str(uuid.uuid4())
        staged = data_dir / "imports" / "staging" / scan_id / "0000.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        return {
            "scan_id": scan_id,
            "managed_relative_path": staged.relative_to(data_dir).as_posix(),
            "display_name": display_name,
            "raw_sha256": hashlib.sha256(content).hexdigest(),
            "raw_bytes": len(content),
        }

    def _stage_item(
        self,
        data_dir: Path,
        scan_id: str,
        *,
        content: bytes = b"Ada Lovelace\nAnalytics Engineer\nPython and SQL",
        ordinal: int = 0,
        filename: str | None = None,
        source_format: str = "txt",
        parser_contract: str = "plain-text-v1",
        extraction_status: str = "parsed",
    ) -> dict[str, object]:
        filename = filename or f"{ordinal:04d}.{source_format}"
        staged = data_dir / "imports" / "staging" / scan_id / filename
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        extracted_text = "" if extraction_status == "failed" else content.decode("utf-8")
        item: dict[str, object] = {
            "ordinal": ordinal,
            "managed_relative_path": staged.relative_to(data_dir).as_posix(),
            "display_name": f"source-{ordinal}.{source_format}",
            "format": source_format,
            "media_type": "text/plain",
            "source_kind": "resume",
            "byte_size": len(content),
            "checksum_sha256": hashlib.sha256(content).hexdigest(),
            "parser_contract": parser_contract,
            "extraction_status": extraction_status,
            "extracted_text": extracted_text,
            "candidate_profile": {"basics": {"name": "Ada Lovelace"}},
            "warnings": [],
        }
        if extraction_status != "failed":
            item["extracted_text_sha256"] = hashlib.sha256(
                extracted_text.encode("utf-8")
            ).hexdigest()
        else:
            item["issue_code"] = "source.parse_failed"
            item["candidate_profile"] = None
        return item

    def _create_scan(
        self,
        data_dir: Path,
        *,
        scan_id: str | None = None,
        base: dict[str, object] | None = None,
        items: list[dict[str, object]] | None = None,
        draft_profile: dict[str, object] | None = None,
        report: dict[str, object] | None = None,
        expires_at_ms: int | None = None,
    ) -> tuple[str, dict[str, object]]:
        scan_id = scan_id or str(uuid.uuid4())
        if items is None:
            items = [self._stage_item(data_dir, scan_id)]
        result = create_profile_source_scan(
            data_dir,
            scan_id=scan_id,
            base_profile_version_id=str(base["id"]) if base is not None else None,
            base_profile_checksum_sha256=(
                str(base["checksum_sha256"]) if base is not None else None
            ),
            items=items,
            draft_profile=draft_profile
            or {
                "basics": {"name": "Ada Lovelace"},
                "skills": [{"name": "Data", "keywords": ["Python", "SQL"]}],
            },
            report=report
            or {
                "synthesis_contract": "deterministic-local-v1",
                "conflicts": [],
                "ui": {"can_build": True},
            },
            expires_at_ms=expires_at_ms or int(time.time() * 1000) + 60_000,
        )
        return scan_id, result

    def test_initialize_vault_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = initialize_vault(data_dir)
            second = initialize_vault(data_dir)

            self.assertEqual(first.schema_version, SCHEMA_VERSION)
            self.assertEqual(second.schema_version, SCHEMA_VERSION)
            self.assertEqual(first.database_path, second.database_path)
            self.assertTrue(Path(first.database_path).exists())
            with closing(sqlite3.connect(first.database_path)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )

    def test_concurrent_first_run_migrates_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with ThreadPoolExecutor(max_workers=4) as executor:
                statuses = list(executor.map(lambda _: initialize_vault(data_dir), range(4)))

            self.assertEqual(
                [status.schema_version for status in statuses],
                [SCHEMA_VERSION] * 4,
            )
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                    SCHEMA_VERSION,
                )

    @unittest.skipIf(os.name == "nt", "POSIX file modes do not apply on Windows")
    def test_vault_files_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            status = initialize_vault(data_dir)

            self.assertEqual(data_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(Path(status.database_path).stat().st_mode & 0o777, 0o600)

    def test_changed_migration_checksum_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            status = initialize_vault(data_dir)
            with closing(sqlite3.connect(status.database_path)) as connection:
                connection.execute(
                    "UPDATE schema_migrations SET checksum_sha256 = ? WHERE version = 1",
                    ("0" * 64,),
                )
                connection.commit()

            with self.assertRaisesRegex(VaultError, "does not match"):
                initialize_vault(data_dir)

    def test_profile_versions_are_immutable_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = save_profile(data_dir, {"basics": {"name": "Ada"}})
            duplicate = save_profile(data_dir, {"basics": {"name": "Ada"}})
            second = save_profile(data_dir, {"basics": {"name": "Grace"}})

            self.assertTrue(first.created)
            self.assertFalse(duplicate.created)
            self.assertEqual(first.id, duplicate.id)
            self.assertEqual(second.version_number, 2)
            self.assertEqual(latest_profile(data_dir)["profile"]["basics"]["name"], "Grace")

            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_versions SET source = 'changed' WHERE id = ?",
                        (first.id,),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_versions WHERE id = ?", (first.id,))

    def test_stored_profile_decode_failures_are_vault_integrity_errors(self) -> None:
        for stored_json in ("not-json", "[]"):
            with self.subTest(stored_json=stored_json), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                saved = save_profile(data_dir, {"basics": {"name": "Ada"}})
                database = data_dir / "cvgnome.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute("DROP TRIGGER profile_versions_no_update")
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        "UPDATE profile_versions SET canonical_json = ? WHERE id = ?",
                        (stored_json, saved.id),
                    )
                    connection.commit()

                original_connect = storage_module._connect

                def connect_ignoring_checks(path: Path) -> sqlite3.Connection:
                    connection = original_connect(path)
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    return connection

                with mock.patch.object(
                    storage_module,
                    "_connect",
                    side_effect=connect_ignoring_checks,
                ):
                    for loader in (
                        lambda: latest_profile(data_dir),
                        lambda: get_profile_version(data_dir, saved.id),
                    ):
                        with self.assertRaises(VaultError) as raised:
                            loader()
                        self.assertEqual(raised.exception.code, "vault_integrity_error")

    def test_profile_rejects_non_json_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "valid JSON values"):
                save_profile(Path(directory), {"score": float("nan")})

    def test_artifact_bytes_are_managed_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            profile = save_profile(data_dir, {"basics": {"name": "Ada"}})
            pdf_buffer = BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.write(pdf_buffer)
            pdf_bytes = pdf_buffer.getvalue()
            artifact = save_artifact_bytes(
                data_dir,
                profile_version_id=profile.id,
                kind="resume_pdf",
                extension="pdf",
                content=pdf_bytes,
                metadata={"suggested_filename": "ada_resume.pdf"},
            )
            duplicate = save_artifact_bytes(
                data_dir,
                profile_version_id=profile.id,
                kind="resume_pdf",
                extension="pdf",
                content=pdf_bytes,
                metadata={"suggested_filename": "ada_resume.pdf"},
            )

            managed_path = data_dir / artifact.relative_path
            self.assertEqual(artifact.id, duplicate.id)
            self.assertFalse(Path(artifact.relative_path).is_absolute())
            self.assertEqual(managed_path.read_bytes(), pdf_bytes)
            self.assertEqual(artifact.byte_size, len(pdf_bytes))
            self.assertEqual(initialize_vault(data_dir).artifacts, 1)
            if os.name != "nt":
                self.assertEqual(managed_path.stat().st_mode & 0o777, 0o600)

    def test_immutable_migration_contracts_are_unchanged(self) -> None:
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[1].encode("utf-8")).hexdigest(),
            "b564434da6e9cf0d0af6bf1a2931ce9b9636d71f05a315fdb87f5ccb9b0b8b81",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[2].encode("utf-8")).hexdigest(),
            "39d2277e1292eee36da89970561003061889a88a4df55914d65d23b028d6dbb0",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[3].encode("utf-8")).hexdigest(),
            "6dda7e62dc910ba0a43915e9f42d026ff91391e043db58bde0320c8f9926e00b",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[4].encode("utf-8")).hexdigest(),
            "2c764e5e0cb1a12534eefb7cf806199816e2efc9c005be4ccfe316a0e4d51c8a",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[5].encode("utf-8")).hexdigest(),
            "e01bfc7c1d6604917dfc737894effdc37184563790d1a06cd99a878e74ee75fa",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[6].encode("utf-8")).hexdigest(),
            "56e16897495f5bd5d29640271c226e28a8a1d2c197dc4e28e0fed0158c735d68",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[7].encode("utf-8")).hexdigest(),
            "8cfd57de48c03ab2d6a2cc7bc69f81ef7966a83d0e04d3044285778d5f3e8a64",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[8].encode("utf-8")).hexdigest(),
            "cb791abee2e0ebbf117c78050a55444e01c68c22e88abdb76a8a8cd65314319a",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[9].encode("utf-8")).hexdigest(),
            "c4b58ad8d2caafe1771b8bac889e0c61c08e4fb81de76f37d6a39313a49b5f42",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[10].encode("utf-8")).hexdigest(),
            "e166925a75bedc6b60d0d7aec4feb9a32d93f65a7aaddf929a77c790861ca201",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[11].encode("utf-8")).hexdigest(),
            "a40a42f4c027af1aae5874fb84129d4e9e21684b0afbf1dd8ce798ed87147b45",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[12].encode("utf-8")).hexdigest(),
            "0e6eaa29420c0cbf6cc5743f5370befd355dc1d1183d6f97d160a71b4835a77c",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[13].encode("utf-8")).hexdigest(),
            "122182b59e2982831e054e3c6a56486ea652ac7385fddbcfd8d0fc4b3beb1fc2",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[14].encode("utf-8")).hexdigest(),
            "e7906069ebeb5ec8d6a20cd3855b08ca8ce0a31a4128dcb03f31dbc0459d313a",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[15].encode("utf-8")).hexdigest(),
            "eb23d33c0f4633c9fb2307d96098a5d45f266ecaa0a33cef1b9cea24c6ef41ac",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[16].encode("utf-8")).hexdigest(),
            "bcabec2040f4a01c0374f35e605deb84c957800275a54fb41e1d47dc802c01d1",
        )
        self.assertEqual(
            hashlib.sha256(MIGRATIONS[17].encode("utf-8")).hexdigest(),
            "90239d579b45b5d7fa86566c7d1ba273e9b749590f89c276d8203057f2bc9205",
        )
        self.assertEqual(
            MIGRATION_APP_VERSIONS,
            {
                1: "0.2.0",
                2: "0.2.0",
                3: "0.2.0",
                4: "0.2.2",
                5: "0.2.3",
                6: "0.2.4",
                7: "0.3.0",
                8: "0.3.1",
                9: "0.4.3",
                10: "0.4.5",
                11: "0.4.6",
                12: "0.4.7",
                13: "0.4.8",
                14: "0.4.10",
                15: "0.4.11",
                16: "0.4.12",
                17: "0.4.13",
                18: "0.4.15",
                19: "0.4.16",
            },
        )

    def test_fresh_vault_records_per_migration_app_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = initialize_vault(Path(directory))
            with closing(sqlite3.connect(status.database_path)) as connection:
                rows = connection.execute(
                    "SELECT version, app_version FROM schema_migrations ORDER BY version"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (1, "0.2.0"),
                    (2, "0.2.0"),
                    (3, "0.2.0"),
                    (4, "0.2.2"),
                    (5, "0.2.3"),
                    (6, "0.2.4"),
                    (7, "0.3.0"),
                    (8, "0.3.1"),
                    (9, "0.4.3"),
                    (10, "0.4.5"),
                    (11, "0.4.6"),
                    (12, "0.4.7"),
                    (13, "0.4.8"),
                    (14, "0.4.10"),
                    (15, "0.4.11"),
                    (16, "0.4.12"),
                    (17, "0.4.13"),
                    (18, "0.4.15"),
                    (19, "0.4.16"),
                ],
            )

    def test_populated_schema_nine_vault_adds_receipts_without_changing_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            first_id = str(uuid.uuid4())
            second_id = str(uuid.uuid4())
            first_json = '{"basics":{"name":"Ada","summary":"First"}}'
            second_json = '{"basics":{"name":"Ada","summary":"Second"}}'
            first_checksum = hashlib.sha256(first_json.encode("utf-8")).hexdigest()
            second_checksum = hashlib.sha256(second_json.encode("utf-8")).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 10):
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
                            hashlib.sha256(
                                MIGRATIONS[version].encode("utf-8")
                            ).hexdigest(),
                            version,
                            MIGRATION_APP_VERSIONS[version],
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-v9', 1)
                    """,
                    (first_id, first_json, first_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 2, ?, ?, ?, 'legacy-v9', 2)
                    """,
                    (second_id, first_id, second_json, second_checksum),
                )
                connection.execute("PRAGMA user_version = 9")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(status.profile_versions, 2)
            self.assertEqual(latest_profile(data_dir)["id"], second_id)
            with closing(sqlite3.connect(database)) as connection:
                profiles = connection.execute(
                    """
                    SELECT id, version_number, parent_version_id, canonical_json,
                           checksum_sha256, source, created_at_ms
                    FROM profile_versions ORDER BY version_number
                    """
                ).fetchall()
                receipt_count = connection.execute(
                    "SELECT count(*) FROM profile_basic_update_receipts"
                ).fetchone()[0]
                migration = connection.execute(
                    """
                    SELECT name, checksum_sha256, app_version
                    FROM schema_migrations WHERE version = 10
                    """
                ).fetchone()
            self.assertEqual(
                profiles,
                [
                    (first_id, 1, None, first_json, first_checksum, "legacy-v9", 1),
                    (
                        second_id,
                        2,
                        first_id,
                        second_json,
                        second_checksum,
                        "legacy-v9",
                        2,
                    ),
                ],
            )
            self.assertEqual(receipt_count, 0)
            self.assertEqual(
                migration,
                (
                    MIGRATION_NAMES[10],
                    hashlib.sha256(MIGRATIONS[10].encode("utf-8")).hexdigest(),
                    MIGRATION_APP_VERSIONS[10],
                ),
            )

    def test_schema_seven_vault_adds_nullable_tailored_artifact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            profile_id = str(uuid.uuid4())
            artifact_id = str(uuid.uuid4())
            profile_json = '{"basics":{"name":"Ada"}}'
            profile_checksum = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                for version in range(1, 8):
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
                            hashlib.sha256(MIGRATIONS[version].encode("utf-8")).hexdigest(),
                            version,
                            MIGRATION_APP_VERSIONS[version],
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 1, NULL, ?, ?, 'schema-seven', 1)
                    """,
                    (profile_id, profile_json, profile_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        id, kind, profile_version_id, opportunity_id, payload_json,
                        relative_path, content_fingerprint, created_at_ms, status,
                        format, checksum_sha256, byte_size, render_fingerprint,
                        template_id, suggested_filename
                    ) VALUES (?, 'resume_pdf', ?, NULL, '{}', ?, ?, 1, 'failed',
                              'pdf', ?, 1, ?, 'classic', 'Ada-Resume.pdf')
                    """,
                    (
                        artifact_id,
                        profile_id,
                        f"artifacts/resumes/{artifact_id}.pdf",
                        "a" * 64,
                        "a" * 64,
                        "b" * 64,
                    ),
                )
                connection.execute("PRAGMA user_version = 7")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            with closing(sqlite3.connect(database)) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(artifacts)")
                }
                self.assertIn("tailored_resume_draft_id", columns)
                self.assertIn("source_resume_checksum_sha256", columns)
                self.assertIn("tailored_resume_revision_id", columns)
                migrated = connection.execute(
                    """
                    SELECT tailored_resume_draft_id, source_resume_checksum_sha256,
                           tailored_resume_revision_id
                    FROM artifacts WHERE id = ?
                    """,
                    (artifact_id,),
                ).fetchone()
                self.assertEqual(migrated, (None, None, None))
                migration_rows = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            self.assertEqual(migration_rows, [(version,) for version in range(1, 20)])

    def test_schema_eight_preserves_and_reuses_ready_original_tailored_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            profile_id = str(uuid.uuid4())
            opportunity_id = str(uuid.uuid4())
            snapshot_id = str(uuid.uuid4())
            match_id = str(uuid.uuid4())
            attempt_id = str(uuid.uuid4())
            draft_id = str(uuid.uuid4())
            artifact_id = str(uuid.uuid4())
            resume_json = json.dumps(
                {
                    "basics": {
                        "name": "Ada Lovelace",
                        "summary": "A preserved schema-eight tailored resume.",
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            profile_checksum = hashlib.sha256(resume_json.encode("utf-8")).hexdigest()
            snapshot_checksum = "1" * 64
            input_fingerprint = "2" * 64
            selected_evidence_json = '[{"chunk_id":"legacy"}]'
            pdf_buffer = BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.write(pdf_buffer)
            pdf_bytes = pdf_buffer.getvalue()
            artifact_checksum = hashlib.sha256(pdf_bytes).hexdigest()
            render_fingerprint = hashlib.sha256(
                "\0".join(
                    (
                        draft_id,
                        profile_checksum,
                        "tailored-resume-v1",
                        "renderer-v1",
                        "pdf",
                        "classic",
                    )
                ).encode("utf-8")
            ).hexdigest()
            relative_path = f"artifacts/resumes/{artifact_id}.pdf"

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 9):
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
                            hashlib.sha256(MIGRATIONS[version].encode("utf-8")).hexdigest(),
                            version,
                            MIGRATION_APP_VERSIONS[version],
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 1, NULL, ?, ?, 'schema-eight', 1)
                    """,
                    (profile_id, resume_json, profile_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO opportunities(
                        id, company, title, source_url, status, job_description,
                        notes, created_at_ms, updated_at_ms, location, apply_url,
                        identity_sha256, tracker_stage, tracker_updated_at_ms
                    ) VALUES (?, 'Legacy Labs', 'Analytics Engineer', NULL, 'saved',
                              'Build local analytics systems.', '', 1, 1, '', NULL,
                              ?, 'tracked', 1)
                    """,
                    (opportunity_id, "3" * 64),
                )
                connection.execute(
                    """
                    INSERT INTO opportunity_snapshots(
                        id, opportunity_id, snapshot_number, title, company,
                        location, source_url, apply_url, description, search_text,
                        checksum_sha256, captured_at_ms
                    ) VALUES (?, ?, 1, 'Analytics Engineer', 'Legacy Labs', '',
                              NULL, NULL, 'Build local analytics systems.',
                              'analytics engineer legacy labs local systems', ?, 1)
                    """,
                    (snapshot_id, opportunity_id, snapshot_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO opportunity_matches(
                        id, opportunity_id, match_number, snapshot_id,
                        snapshot_checksum_sha256, profile_version_id,
                        profile_checksum_sha256, contract, input_fingerprint,
                        score, signal, result_json, created_at_ms
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, 'deterministic-local-v1', ?,
                              0, 'limited', '{"terms_to_review":[]}', 1)
                    """,
                    (
                        match_id,
                        opportunity_id,
                        snapshot_id,
                        snapshot_checksum,
                        profile_id,
                        profile_checksum,
                        "4" * 64,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO tailoring_attempts(
                        id, opportunity_id, snapshot_id, snapshot_checksum_sha256,
                        profile_version_id, profile_checksum_sha256, status,
                        input_fingerprint, baseline_resume_json, evidence_chunks_json,
                        selected_evidence_json, provider_kind, chat_model_id,
                        embedding_model_id, embedding_count, embedding_dimension,
                        error_code, created_at_ms, armed_at_ms, finished_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, '[{}]', NULL,
                              'lmstudio', 'legacy/chat', 'legacy/embedding', 2,
                              NULL, NULL, 2, NULL, NULL)
                    """,
                    (
                        attempt_id,
                        opportunity_id,
                        snapshot_id,
                        snapshot_checksum,
                        profile_id,
                        profile_checksum,
                        input_fingerprint,
                        resume_json,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tailoring_attempts
                    SET status = 'armed', selected_evidence_json = ?,
                        embedding_dimension = 2, armed_at_ms = 3
                    WHERE id = ?
                    """,
                    (selected_evidence_json, attempt_id),
                )
                connection.execute(
                    """
                    INSERT INTO tailored_resume_drafts(
                        id, attempt_id, opportunity_id, snapshot_id,
                        snapshot_checksum_sha256, profile_version_id,
                        profile_checksum_sha256, provider_kind, chat_model_id,
                        embedding_model_id, input_fingerprint, baseline_resume_json,
                        result_resume_json, selected_evidence_json,
                        quality_issue_codes_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'lmstudio', 'legacy/chat',
                              'legacy/embedding', ?, ?, ?, ?, '[]', 3)
                    """,
                    (
                        draft_id,
                        attempt_id,
                        opportunity_id,
                        snapshot_id,
                        snapshot_checksum,
                        profile_id,
                        profile_checksum,
                        input_fingerprint,
                        resume_json,
                        resume_json,
                        selected_evidence_json,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tailoring_attempts
                    SET status = 'succeeded', finished_at_ms = 4
                    WHERE id = ?
                    """,
                    (attempt_id,),
                )
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        id, kind, profile_version_id, opportunity_id, payload_json,
                        relative_path, content_fingerprint, created_at_ms, status,
                        format, checksum_sha256, byte_size, render_fingerprint,
                        template_id, suggested_filename, tailored_resume_draft_id,
                        source_resume_checksum_sha256
                    ) VALUES (?, 'tailored_resume_pdf', ?, ?, '{}', ?, ?, 5,
                              'ready', 'pdf', ?, ?, ?, 'classic',
                              'Ada-Legacy-Tailored-Resume.pdf', ?, ?)
                    """,
                    (
                        artifact_id,
                        profile_id,
                        opportunity_id,
                        relative_path,
                        artifact_checksum,
                        artifact_checksum,
                        len(pdf_bytes),
                        render_fingerprint,
                        draft_id,
                        profile_checksum,
                    ),
                )
                connection.execute("PRAGMA user_version = 8")
                connection.commit()
            artifact_path = data_dir / relative_path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(pdf_bytes)

            status = initialize_vault(data_dir)
            reused = save_artifact_bytes(
                data_dir,
                profile_version_id=profile_id,
                kind="tailored_resume_pdf",
                extension="pdf",
                content=pdf_bytes,
                metadata={
                    "renderer_contract_version": 1,
                    "suggested_filename": "Ada-Legacy-Tailored-Resume.pdf",
                    "template_id": "classic",
                },
                tailored_resume_draft_id=draft_id,
            )

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(reused.id, artifact_id)
            self.assertEqual(reused.relative_path, relative_path)
            self.assertEqual(artifact_path.read_bytes(), pdf_bytes)
            with closing(sqlite3.connect(database)) as connection:
                migrated = connection.execute(
                    """
                    SELECT status, tailored_resume_draft_id,
                           tailored_resume_revision_id,
                           source_resume_checksum_sha256, render_fingerprint
                    FROM artifacts WHERE id = ?
                    """,
                    (artifact_id,),
                ).fetchone()
                ready_count = connection.execute(
                    "SELECT count(*) FROM artifacts WHERE status = 'ready'"
                ).fetchone()[0]
            self.assertEqual(
                migrated,
                (
                    "ready",
                    draft_id,
                    None,
                    profile_checksum,
                    render_fingerprint,
                ),
            )
            self.assertEqual(ready_count, 1)

    def test_schema_two_vault_migrates_without_changing_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            profile_json = '{"basics":{"name":"Ada"}}'
            checksum = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
            legacy_opportunity_id = str(uuid.uuid4())
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(MIGRATIONS[1])
                connection.execute(
                    """
                    INSERT INTO schema_migrations(
                        version, name, checksum_sha256, applied_at_ms, app_version
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        MIGRATION_NAMES[1],
                        hashlib.sha256(MIGRATIONS[1].encode("utf-8")).hexdigest(),
                        1,
                        "0.1.0",
                    ),
                )
                connection.executescript(MIGRATIONS[2])
                connection.execute(
                    """
                    INSERT INTO schema_migrations(
                        version, name, checksum_sha256, applied_at_ms, app_version
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        MIGRATION_NAMES[2],
                        hashlib.sha256(MIGRATIONS[2].encode("utf-8")).hexdigest(),
                        2,
                        "0.1.1",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, canonical_json, checksum_sha256,
                        source, created_at_ms
                    ) VALUES (?, 1, ?, ?, 'legacy', 1)
                    """,
                    (str(uuid.uuid4()), profile_json, checksum),
                )
                connection.execute(
                    """
                    INSERT INTO opportunities(
                        id, company, title, source_url, status, job_description,
                        notes, created_at_ms, updated_at_ms
                    ) VALUES (?, 'Legacy Co', 'Legacy Role', NULL, 'saved',
                              'Original posting', '', 1, 1)
                    """,
                    (legacy_opportunity_id,),
                )
                connection.execute("PRAGMA user_version = 2")
                connection.execute("PRAGMA application_id = 1129729108")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(status.profile_versions, 1)
            self.assertEqual(status.opportunities, 0)
            self.assertEqual(latest_profile(data_dir)["profile"]["basics"]["name"], "Ada")
            self.assertEqual(
                list_opportunities(
                    data_dir,
                    {"query": "", "offset": 0, "stage": "all", "sort": "last_action"},
                ),
                {"items": [], "total": 0},
            )
            with closing(sqlite3.connect(database)) as connection:
                legacy = connection.execute(
                    """
                    SELECT title, location, apply_url, identity_sha256,
                           tracker_stage, tracker_updated_at_ms
                    FROM opportunities WHERE id = ?
                    """,
                    (legacy_opportunity_id,),
                ).fetchone()
                self.assertEqual(legacy, ("Legacy Role", "", None, None, "tracked", 1))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint"):
                    connection.execute(
                        "UPDATE opportunities SET tracker_stage = 'someday' WHERE id = ?",
                        (legacy_opportunity_id,),
                    )
                connection.rollback()

    def test_populated_schema_four_vault_backfills_retained_link_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            profile_id = str(uuid.uuid4())
            source_id = str(uuid.uuid4())
            import_id = str(uuid.uuid4())
            profile_json = '{"basics":{"name":"Ada","summary":"Python engineer"}}'
            profile_checksum = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
            source_bytes = b"Legacy resume evidence\n"
            source_checksum = hashlib.sha256(source_bytes).hexdigest()
            source_relative_path = (
                Path("sources") / "blobs" / source_checksum[:2] / source_checksum
            )
            source_blob = data_dir / source_relative_path
            source_blob.parent.mkdir(parents=True)
            source_blob.write_bytes(source_bytes)

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 5):
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
                            hashlib.sha256(MIGRATIONS[version].encode("utf-8")).hexdigest(),
                            version,
                            MIGRATION_APP_VERSIONS[version],
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-import', 1)
                    """,
                    (profile_id, profile_json, profile_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, display_name, media_type, checksum_sha256,
                        relative_path, extracted_text, imported_at_ms,
                        byte_size, source_format, content_addressed
                    ) VALUES (?, 'legacy-resume.txt', 'text/plain', ?, ?, NULL, 1, ?, 'txt', 1)
                    """,
                    (
                        source_id,
                        source_checksum,
                        source_relative_path.as_posix(),
                        len(source_bytes),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO profile_source_imports(
                        id, base_profile_version_id, base_profile_checksum_sha256,
                        output_profile_version_id, source_set_checksum_sha256,
                        synthesis_contract, report_json, result_json, committed_at_ms
                    ) VALUES (?, NULL, NULL, ?, ?, 'legacy-v4', '{}', '{}', 1)
                    """,
                    (import_id, profile_id, source_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO profile_version_sources(
                        import_id, ordinal, profile_version_id, source_id,
                        extraction_id, source_format, source_kind,
                        extraction_status, issue_code
                    ) VALUES (?, 0, ?, ?, NULL, 'txt', 'resume', 'parsed', NULL)
                    """,
                    (import_id, profile_id, source_id),
                )
                connection.execute("PRAGMA user_version = 4")
                connection.execute("PRAGMA application_id = 1129729108")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(status.source_retention_generation, 1)
            self.assertEqual(status.retained_source_files, 1)
            self.assertEqual(status.retained_source_bytes, len(source_bytes))
            self.assertEqual(status.retained_source_imports, 1)
            self.assertEqual(status.historical_source_files, 0)
            self.assertEqual(status.historical_source_imports, 0)
            self.assertEqual(source_blob.read_bytes(), source_bytes)
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                imported = connection.execute(
                    "SELECT retention_generation FROM profile_source_imports WHERE id = ?",
                    (import_id,),
                ).fetchone()
                link = connection.execute(
                    "SELECT * FROM profile_version_sources WHERE import_id = ?",
                    (import_id,),
                ).fetchone()
                self.assertEqual(imported["retention_generation"], 1)
                self.assertEqual(link["display_name"], "legacy-resume.txt")
                table_info = {
                    str(row["name"]): row
                    for row in connection.execute(
                        "PRAGMA table_info(profile_version_sources)"
                    ).fetchall()
                }
                self.assertEqual(table_info["display_name"]["notnull"], 1)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_version_sources SET display_name = 'changed.txt'"
                    )
                connection.rollback()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_version_sources")
                connection.rollback()
                self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())

    def test_canonical_import_retains_exact_bytes_and_deduplicates_blob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            content = (
                b"\xef\xbb\xbf \n{\n  \"basics\": {\"name\": \"Ada Lovelace\", "
                b"\"summary\": \"Local analytics engineer.\"},\n"
                b"  \"skills\": [{\"name\": \"Data\", \"keywords\": [\"Python\"]}]\n}\n"
            )
            first_manifest = self._stage_canonical(data_dir, content)
            first = import_canonical_profile_source(data_dir, first_manifest)
            second_manifest = self._stage_canonical(
                data_dir,
                content,
                display_name="renamed-canonical.json",
            )
            second = import_canonical_profile_source(data_dir, second_manifest)

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["profile_version_id"], second["profile_version_id"])
            self.assertFalse(
                (data_dir / "imports" / "staging" / str(first_manifest["scan_id"])).exists()
            )
            self.assertFalse(
                (data_dir / "imports" / "staging" / str(second_manifest["scan_id"])).exists()
            )
            status = initialize_vault(data_dir)
            self.assertEqual(status.source_snapshots, 1)
            self.assertEqual(status.source_imports, 2)
            self.assertEqual(status.retained_source_files, 1)
            self.assertEqual(status.retained_source_bytes, len(content))
            self.assertEqual(status.retained_source_imports, 2)
            self.assertIsNotNone(status.last_source_import_at_ms)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                connection.row_factory = sqlite3.Row
                source = connection.execute(
                    "SELECT * FROM sources WHERE content_addressed = 1"
                ).fetchone()
                self.assertEqual((data_dir / source["relative_path"]).read_bytes(), content)
                imports = connection.execute(
                    """
                    SELECT synthesis_contract, retention_generation
                    FROM profile_source_imports ORDER BY committed_at_ms, id
                    """
                ).fetchall()
                self.assertEqual(
                    [(row["synthesis_contract"], row["retention_generation"]) for row in imports],
                    [("canonical-file-v1", 1), ("canonical-file-v1", 1)],
                )
                links = connection.execute(
                    """
                    SELECT display_name, extraction_id, source_format, source_kind,
                           extraction_status
                    FROM profile_version_sources ORDER BY display_name
                    """
                ).fetchall()
                self.assertEqual(len(links), 2)
                self.assertEqual(
                    [row["display_name"] for row in links],
                    ["canonical-profile.json", "renamed-canonical.json"],
                )
                self.assertTrue(all(row["extraction_id"] is None for row in links))
                self.assertTrue(
                    all(
                        (row["source_format"], row["source_kind"], row["extraction_status"])
                        == ("json", "unclassified", "parsed")
                        for row in links
                    )
                )

    def test_canonical_import_rejects_tampering_and_cleans_terminal_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            original = b'{"basics":{"name":"Ada"}}'
            manifest = self._stage_canonical(data_dir, original)
            staged = data_dir / str(manifest["managed_relative_path"])
            staged.write_bytes(b'{"basics":{"name":"Eve"}}')

            with self.assertRaisesRegex(ValueError, "integrity"):
                import_canonical_profile_source(data_dir, manifest)

            self.assertFalse(staged.parent.exists())
            invalid = b"[1, 2, 3]"
            invalid_manifest = self._stage_canonical(data_dir, invalid)
            with self.assertRaises(VaultError) as raised:
                import_canonical_profile_source(data_dir, invalid_manifest)
            self.assertEqual(raised.exception.code, "canonical_profile.not_object")
            self.assertFalse(
                (data_dir / "imports" / "staging" / str(invalid_manifest["scan_id"])).exists()
            )
            status = initialize_vault(data_dir)
            self.assertEqual(status.profile_versions, 0)
            self.assertEqual(status.source_snapshots, 0)
            self.assertEqual(status.source_imports, 0)

    def test_canonical_cleanup_never_removes_an_active_source_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            scan_id = str(uuid.uuid4())
            content = b'{"basics":{"name":"Ada"}}'
            item = self._stage_item(
                data_dir,
                scan_id,
                content=content,
                filename="0000.json",
                source_format="json",
                parser_contract="canonical-json-v1",
            )
            self._create_scan(data_dir, scan_id=scan_id, items=[item])
            manifest = {
                "scan_id": scan_id,
                "managed_relative_path": f"imports/staging/{scan_id}/0000.json",
                "display_name": "canonical-profile.json",
                "raw_sha256": hashlib.sha256(content).hexdigest(),
                "raw_bytes": len(content),
            }

            with self.assertRaises(VaultError) as raised:
                import_canonical_profile_source(data_dir, manifest)

            self.assertEqual(raised.exception.code, "profile_source_scan_conflict")
            self.assertTrue((data_dir / "imports" / "staging" / scan_id / "0000.json").is_file())
            self.assertTrue(discard_profile_source_scan(data_dir, scan_id)["discarded"])

    def test_source_retention_reset_is_logical_idempotent_and_generation_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            canonical = b'{"basics":{"name":"Ada","summary":"Python engineer"}}'
            import_canonical_profile_source(data_dir, self._stage_canonical(data_dir, canonical))
            before = initialize_vault(data_dir)
            request_id = str(uuid.uuid4())

            reset = reset_profile_source_retention(
                data_dir,
                expected_generation=1,
                request_id=request_id,
            )
            retry = reset_profile_source_retention(
                data_dir,
                expected_generation=1,
                request_id=request_id,
            )

            self.assertEqual(reset, retry)
            self.assertEqual(reset.source_retention_generation, 2)
            self.assertEqual(reset.retained_source_files, 0)
            self.assertEqual(reset.retained_source_bytes, 0)
            self.assertEqual(reset.retained_source_imports, 0)
            self.assertEqual(reset.historical_source_files, 1)
            self.assertEqual(reset.historical_source_imports, 1)
            self.assertIsNone(reset.last_source_import_at_ms)
            self.assertIsNotNone(reset.source_retention_reset_at_ms)
            self.assertEqual(before.profile_versions, initialize_vault(data_dir).profile_versions)
            self.assertEqual(before.source_snapshots, initialize_vault(data_dir).source_snapshots)

            with self.assertRaises(VaultError) as stale:
                reset_profile_source_retention(
                    data_dir,
                    expected_generation=1,
                    request_id=str(uuid.uuid4()),
                )
            self.assertEqual(stale.exception.code, "profile_source_reset_conflict")

            base = latest_profile(data_dir)
            busy_scan, _ = self._create_scan(data_dir, base=base)
            with self.assertRaises(VaultError) as busy:
                reset_profile_source_retention(
                    data_dir,
                    expected_generation=2,
                    request_id=str(uuid.uuid4()),
                )
            self.assertEqual(busy.exception.code, "profile_source_reset_busy")
            discard_profile_source_scan(data_dir, busy_scan)

            generation_two_scan, _ = self._create_scan(data_dir, base=base)
            commit_profile_source_scan(data_dir, generation_two_scan)
            after = initialize_vault(data_dir)
            self.assertEqual(after.source_retention_generation, 2)
            self.assertEqual(after.retained_source_files, 1)
            self.assertEqual(after.retained_source_imports, 1)
            self.assertEqual(after.historical_source_files, 1)
            self.assertEqual(after.historical_source_imports, 1)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                generations = connection.execute(
                    """
                    SELECT retention_generation
                    FROM profile_source_imports ORDER BY retention_generation
                    """
                ).fetchall()
                self.assertEqual([row[0] for row in generations], [1, 2])

    def test_preview_creates_no_profile_and_returns_no_source_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)

            scan_id, preview = self._create_scan(data_dir)

            self.assertEqual(preview["status"], "preview")
            self.assertEqual(preview["source_count"], 1)
            self.assertEqual(preview["skill_groups"], 1)
            self.assertEqual(preview["warning_count"], 0)
            self.assertEqual(preview["conflict_count"], 0)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 0)
            serialized = str(preview)
            self.assertNotIn("managed_relative_path", serialized)
            self.assertNotIn("Python and SQL", serialized)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_source_scans WHERE id = ?",
                        (scan_id,),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0],
                    0,
                )

    def test_commit_is_atomic_retry_safe_and_reuses_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id, _ = self._create_scan(data_dir)

            first = commit_profile_source_scan(data_dir, scan_id)
            retry = commit_profile_source_scan(data_dir, scan_id)

            self.assertEqual(first, retry)
            self.assertTrue(first["created"])
            self.assertEqual(first["source_snapshots_created"], 1)
            self.assertEqual(first["extractions_created"], 1)
            self.assertFalse((data_dir / "imports" / "staging" / scan_id).exists())
            status = initialize_vault(data_dir)
            self.assertEqual(status.profile_versions, 1)
            self.assertEqual(status.source_snapshots, 1)
            self.assertEqual(status.source_imports, 1)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                connection.row_factory = sqlite3.Row
                source = connection.execute(
                    "SELECT * FROM sources WHERE content_addressed = 1"
                ).fetchone()
                extraction = connection.execute("SELECT * FROM source_extractions").fetchone()
                link = connection.execute("SELECT * FROM profile_version_sources").fetchone()
                self.assertIsNotNone(source)
                self.assertIsNotNone(extraction)
                self.assertEqual(link["profile_version_id"], first["profile_version_id"])
                blob = data_dir / str(source["relative_path"])
                self.assertEqual(
                    hashlib.sha256(blob.read_bytes()).hexdigest(),
                    source["checksum_sha256"],
                )
                if os.name != "nt":
                    self.assertEqual(blob.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE sources SET display_name = 'changed' WHERE id = ?",
                        (source["id"],),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE source_extractions SET extracted_text = 'changed' WHERE id = ?",
                        (extraction["id"],),
                    )

            base = latest_profile(data_dir)
            second_scan_id = str(uuid.uuid4())
            second_item = self._stage_item(data_dir, second_scan_id)
            second_item["display_name"] = "renamed-evidence.txt"
            self._create_scan(
                data_dir,
                scan_id=second_scan_id,
                base=base,
                items=[second_item],
            )
            second = commit_profile_source_scan(data_dir, second_scan_id)
            self.assertFalse(second["created"])
            self.assertEqual(second["profile_version_id"], first["profile_version_id"])
            self.assertEqual(second["source_snapshots_created"], 0)
            self.assertEqual(second["source_snapshots_reused"], 1)
            self.assertEqual(second["extractions_reused"], 1)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                aliases = connection.execute(
                    "SELECT display_name FROM profile_version_sources ORDER BY display_name"
                ).fetchall()
                self.assertEqual(
                    [row[0] for row in aliases],
                    ["renamed-evidence.txt", "source-0.txt"],
                )

    def test_commit_refuses_a_non_buildable_preview_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id, _ = self._create_scan(
                data_dir,
                draft_profile={"basics": {}},
                report={
                    "synthesis_contract": "deterministic-local-v1",
                    "ui": {"can_build": False},
                },
            )

            with self.assertRaisesRegex(VaultError, "usable profile content") as raised:
                commit_profile_source_scan(data_dir, scan_id)

            self.assertEqual(raised.exception.code, "profile_source_not_buildable")
            self.assertIsNone(latest_profile(data_dir))
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM profile_source_scans WHERE id = ?",
                        (scan_id,),
                    ).fetchone()[0],
                    "preview",
                )

    def test_csv_and_failed_source_are_lineaged_without_leaking_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id = str(uuid.uuid4())
            csv_item = self._stage_item(
                data_dir,
                scan_id,
                content=b"skill,years\nPython,10\n",
                ordinal=0,
                source_format="csv",
                parser_contract="csv-profile-v1",
            )
            failed_item = self._stage_item(
                data_dir,
                scan_id,
                content=b"not a usable document",
                ordinal=1,
                source_format="txt",
                parser_contract="plain-text-v1",
                extraction_status="failed",
            )
            self._create_scan(data_dir, scan_id=scan_id, items=[csv_item, failed_item])

            result = commit_profile_source_scan(data_dir, scan_id)

            self.assertEqual(result["parsed_sources"], 1)
            self.assertEqual(result["failed_sources"], 1)
            self.assertEqual(result["warning_count"], 1)
            serialized = str(result)
            self.assertNotIn("Python,10", serialized)
            self.assertNotIn("managed_relative_path", serialized)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sources WHERE content_addressed = 1")
                    .fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM source_extractions").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM profile_version_sources").fetchone()[0],
                    2,
                )

    def test_commit_rechecks_base_and_preserves_preview_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = save_profile(data_dir, {"basics": {"name": "Ada"}})
            base = latest_profile(data_dir)
            scan_id, _ = self._create_scan(data_dir, base=base)
            save_profile(data_dir, {"basics": {"name": "Grace"}})

            with self.assertRaisesRegex(VaultError, "current profile changed"):
                commit_profile_source_scan(data_dir, scan_id)

            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                row = connection.execute(
                    "SELECT status FROM profile_source_scans WHERE id = ?",
                    (scan_id,),
                ).fetchone()
                self.assertEqual(row[0], "preview")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sources WHERE content_addressed = 1")
                    .fetchone()[0],
                    0,
                )
            self.assertEqual(latest_profile(data_dir)["id"], save_profile(
                data_dir, {"basics": {"name": "Grace"}}
            ).id)
            self.assertNotEqual(first.id, latest_profile(data_dir)["id"])

    def test_discard_is_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id, _ = self._create_scan(data_dir)
            sibling_id = str(uuid.uuid4())
            sibling = data_dir / "imports" / "staging" / sibling_id
            sibling.mkdir(parents=True)
            (sibling / "keep.txt").write_text("keep", encoding="utf-8")

            first = discard_profile_source_scan(data_dir, scan_id)
            second = discard_profile_source_scan(data_dir, scan_id)

            self.assertTrue(first["discarded"])
            self.assertFalse(second["discarded"])
            self.assertTrue(sibling.is_dir())
            self.assertEqual((sibling / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(initialize_vault(data_dir).source_previews, 0)

    def test_discard_all_clears_unfinished_scans_without_touching_committed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            committed_scan, _ = self._create_scan(data_dir)
            committed = commit_profile_source_scan(data_dir, committed_scan)
            committed_staging = data_dir / "imports" / "staging" / committed_scan
            committed_staging.mkdir(parents=True)
            committed_marker = committed_staging / "committed-marker.txt"
            committed_marker.write_text("keep", encoding="utf-8")

            preview_scan, _ = self._create_scan(data_dir, base=latest_profile(data_dir))
            interrupted_scan, _ = self._create_scan(
                data_dir,
                base=latest_profile(data_dir),
            )
            orphan_scan = str(uuid.uuid4())
            orphan_staging = data_dir / "imports" / "staging" / orphan_scan
            orphan_staging.mkdir(parents=True)
            orphan_marker = orphan_staging / "orphan-marker.txt"
            orphan_marker.write_text("keep", encoding="utf-8")
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                connection.execute(
                    "UPDATE profile_source_scans SET status = 'committing' WHERE id = ?",
                    (interrupted_scan,),
                )
                source = connection.execute(
                    "SELECT relative_path FROM sources WHERE content_addressed = 1"
                ).fetchone()
                connection.commit()
            blob = data_dir / str(source[0])
            blob_bytes = blob.read_bytes()
            self.assertEqual(initialize_vault(data_dir).source_previews, 2)
            with self.assertRaises(VaultError) as busy:
                reset_profile_source_retention(
                    data_dir,
                    expected_generation=1,
                    request_id=str(uuid.uuid4()),
                )
            self.assertEqual(busy.exception.code, "profile_source_reset_busy")

            first = discard_all_profile_source_scans(data_dir)
            second = discard_all_profile_source_scans(data_dir)

            self.assertEqual(first, {"discarded_previews": 2})
            self.assertEqual(second, {"discarded_previews": 0})
            self.assertFalse((data_dir / "imports" / "staging" / preview_scan).exists())
            self.assertFalse((data_dir / "imports" / "staging" / interrupted_scan).exists())
            self.assertEqual(committed_marker.read_text(encoding="utf-8"), "keep")
            self.assertEqual(orphan_marker.read_text(encoding="utf-8"), "keep")
            self.assertEqual(blob.read_bytes(), blob_bytes)
            status = initialize_vault(data_dir)
            self.assertEqual(status.profile_versions, 1)
            self.assertEqual(status.source_imports, 1)
            self.assertEqual(status.source_snapshots, 1)
            self.assertEqual(status.source_previews, 0)
            self.assertEqual(
                latest_profile(data_dir)["id"],
                committed["profile_version_id"],
            )

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior differs on Windows")
    def test_discard_all_unlinks_staging_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            data_dir = Path(directory)
            outside_dir = Path(outside)
            initialize_vault(data_dir)
            scan_id, _ = self._create_scan(data_dir)
            staging = data_dir / "imports" / "staging" / scan_id
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
            marker = outside_dir / "must-remain.txt"
            marker.write_text("outside", encoding="utf-8")
            staging.symlink_to(outside_dir, target_is_directory=True)

            result = discard_all_profile_source_scans(data_dir)

            self.assertEqual(result, {"discarded_previews": 1})
            self.assertFalse(staging.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "outside")

    def test_scan_rejects_unsafe_manifest_and_changed_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id = str(uuid.uuid4())
            item = self._stage_item(data_dir, scan_id)

            unsafe = dict(item)
            unsafe["managed_relative_path"] = "../outside.txt"
            with self.assertRaisesRegex(ValueError, "outside|invalid"):
                self._create_scan(data_dir, scan_id=scan_id, items=[unsafe])

            bad_ordinal = dict(item)
            bad_ordinal["ordinal"] = 1
            with self.assertRaisesRegex(ValueError, "ordinals"):
                self._create_scan(data_dir, scan_id=scan_id, items=[bad_ordinal])

            bad_hash = dict(item)
            bad_hash["checksum_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "integrity"):
                self._create_scan(data_dir, scan_id=scan_id, items=[bad_hash])

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior differs on Windows")
    def test_scan_rejects_symlinked_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id = str(uuid.uuid4())
            target = data_dir / "target.txt"
            target.write_bytes(b"target")
            staged = data_dir / "imports" / "staging" / scan_id / "0000.txt"
            staged.parent.mkdir(parents=True)
            staged.symlink_to(target)
            item = {
                "ordinal": 0,
                "managed_relative_path": staged.relative_to(data_dir).as_posix(),
                "format": "txt",
                "byte_size": 6,
                "checksum_sha256": hashlib.sha256(b"target").hexdigest(),
                "parser_contract": "plain-text-v1",
                "source_kind": "resume",
                "extraction_status": "parsed",
                "extracted_text": "target",
            }
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                self._create_scan(data_dir, scan_id=scan_id, items=[item])

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior differs on Windows")
    def test_discard_never_follows_a_symlinked_staging_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            data_dir = Path(directory)
            outside_root = Path(outside)
            initialize_vault(data_dir)
            (data_dir / "imports").mkdir(exist_ok=True)
            (data_dir / "imports" / "staging").symlink_to(
                outside_root,
                target_is_directory=True,
            )
            scan_id = str(uuid.uuid4())
            outside_scan = outside_root / scan_id
            outside_scan.mkdir()
            marker = outside_scan / "must-remain.txt"
            marker.write_text("outside the vault", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unsafe managed ancestor"):
                discard_profile_source_scan(data_dir, scan_id)

            self.assertEqual(marker.read_text(encoding="utf-8"), "outside the vault")

    def test_commit_detects_post_preview_file_change_without_profile_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            scan_id, _ = self._create_scan(data_dir)
            staged = next((data_dir / "imports" / "staging" / scan_id).iterdir())
            staged.write_bytes(b"changed")

            with self.assertRaisesRegex(ValueError, "size changed|integrity"):
                commit_profile_source_scan(data_dir, scan_id)

            self.assertEqual(initialize_vault(data_dir).profile_versions, 0)
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM profile_source_scans WHERE id = ?",
                        (scan_id,),
                    ).fetchone()[0],
                    "preview",
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sources WHERE content_addressed = 1")
                    .fetchone()[0],
                    0,
                )

    def test_initialize_reconciles_expired_and_interrupted_previews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            expired_id, _ = self._create_scan(data_dir)
            interrupted_id, _ = self._create_scan(data_dir)
            orphan_id = str(uuid.uuid4())
            orphan = data_dir / "imports" / "staging" / orphan_id
            orphan.mkdir(parents=True)
            (orphan / "0000.txt").write_text("orphan", encoding="utf-8")
            os.utime(orphan, (1, 1))
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                connection.execute(
                    "UPDATE profile_source_scans SET expires_at_ms = 1 WHERE id = ?",
                    (expired_id,),
                )
                connection.execute(
                    """
                    UPDATE profile_source_scans
                    SET status = 'committing', updated_at_ms = 1
                    WHERE id = ?
                    """,
                    (interrupted_id,),
                )
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.source_previews, 0)
            self.assertFalse((data_dir / "imports" / "staging" / expired_id).exists())
            self.assertFalse((data_dir / "imports" / "staging" / interrupted_id).exists())
            self.assertFalse(orphan.exists())
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM profile_source_scans").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
