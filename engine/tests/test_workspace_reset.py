# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

import cvgnome_engine.workspace_reset as reset_module  # noqa: E402
from cvgnome_engine.cli import _handle_request  # noqa: E402
from cvgnome_engine.opportunities import save_and_match_opportunity  # noqa: E402
from cvgnome_engine.storage import (  # noqa: E402
    SCHEMA_VERSION,
    VaultError,
    create_profile_source_scan,
    import_canonical_profile_source,
    initialize_vault,
    latest_profile,
    save_artifact_bytes,
    save_profile,
)
from cvgnome_engine.workspace_reset import reset_workspace  # noqa: E402


RESET_FIELDS = (
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


class WorkspaceResetTests(unittest.TestCase):
    def _expected(self, data_dir: Path) -> dict[str, int]:
        status = initialize_vault(data_dir)
        return {field: int(getattr(status, field)) for field in RESET_FIELDS}

    def _import_profile(self, data_dir: Path) -> dict[str, object]:
        profile = {
            "basics": {"name": "Ada Lovelace", "label": "Analytics Engineer"},
            "work": [
                {
                    "name": "Analytical Engines",
                    "position": "Engineer",
                    "summary": "Built reliable data systems.",
                }
            ],
            "skills": [{"name": "Data", "keywords": ["Python", "SQL"]}],
        }
        content = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
        scan_id = str(uuid.uuid4())
        staged = data_dir / "imports" / "staging" / scan_id / "0000.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        return import_canonical_profile_source(
            data_dir,
            {
                "scan_id": scan_id,
                "managed_relative_path": f"imports/staging/{scan_id}/0000.json",
                "display_name": "canonical-profile.json",
                "raw_sha256": hashlib.sha256(content).hexdigest(),
                "raw_bytes": len(content),
            },
        )

    def _create_preview(self, data_dir: Path) -> None:
        base = latest_profile(data_dir)
        self.assertIsNotNone(base)
        scan_id = str(uuid.uuid4())
        content = b"Grace Hopper\nCompiler engineer\nCOBOL"
        staged = data_dir / "imports" / "staging" / scan_id / "0000.txt"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        extracted = content.decode()
        create_profile_source_scan(
            data_dir,
            scan_id=scan_id,
            base_profile_version_id=str(base["id"]),
            base_profile_checksum_sha256=str(base["checksum_sha256"]),
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
                    "extracted_text_sha256": hashlib.sha256(
                        extracted.encode()
                    ).hexdigest(),
                    "candidate_profile": {"basics": {"name": "Grace Hopper"}},
                    "warnings": [],
                }
            ],
            draft_profile={
                "basics": {"name": "Grace Hopper"},
                "skills": [{"name": "Programming", "keywords": ["COBOL"]}],
            },
            report={
                "synthesis_contract": "deterministic-local-v1",
                "conflicts": [],
                "ui": {"can_build": True},
            },
            expires_at_ms=int(time.time() * 1000) + 60_000,
        )

    def _populate(self, data_dir: Path) -> None:
        imported = self._import_profile(data_dir)
        profile_id = str(imported["profile_version_id"])
        save_and_match_opportunity(
            data_dir,
            {
                "title": "Senior Analytics Engineer",
                "description": "Build Python and SQL data systems.",
                "company": "Example Company",
                "location": "Remote",
                "source_url": "https://example.com/job",
                "apply_url": "https://example.com/apply",
            },
        )
        pdf_stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(pdf_stream)
        save_artifact_bytes(
            data_dir,
            profile_version_id=profile_id,
            kind="resume_pdf",
            extension="pdf",
            content=pdf_stream.getvalue(),
        )
        self._create_preview(data_dir)

    def test_reset_removes_only_managed_vault_content_and_is_retry_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._populate(data_dir)
            provider = data_dir / "local-provider.json"
            provider.write_text('{"provider":"preserve-me"}', encoding="utf-8")
            provider_spend = data_dir / "provider-spend.json"
            provider_spend.write_text('{"ledger":"preserve-me"}', encoding="utf-8")
            sibling = data_dir / "license-state.json"
            sibling.write_text("preserve-me", encoding="utf-8")
            external_original = data_dir / "original-resume.txt"
            external_original.write_text("original", encoding="utf-8")
            expected = self._expected(data_dir)
            self.assertGreater(expected["profile_versions"], 0)
            self.assertEqual(expected["opportunities"], 1)
            self.assertEqual(expected["artifacts"], 1)
            self.assertEqual(expected["source_imports"], 1)
            self.assertEqual(expected["source_previews"], 1)

            request_id = str(uuid.uuid4())
            first = reset_workspace(
                data_dir,
                {"request_id": request_id, "expected": expected},
            ).to_dict()

            self.assertTrue(first["created"])
            self.assertEqual(first["schema_version"], SCHEMA_VERSION)
            self.assertEqual(first["source_retention_generation"], 1)
            for field in RESET_FIELDS:
                if field == "source_retention_generation":
                    continue
                self.assertEqual(first[field], 0)
            self.assertNotIn(str(data_dir), json.dumps(first))
            self.assertEqual(provider.read_text(encoding="utf-8"), '{"provider":"preserve-me"}')
            self.assertEqual(
                provider_spend.read_text(encoding="utf-8"),
                '{"ledger":"preserve-me"}',
            )
            self.assertEqual(sibling.read_text(encoding="utf-8"), "preserve-me")
            self.assertEqual(external_original.read_text(encoding="utf-8"), "original")
            self.assertFalse((data_dir / "sources").exists())
            self.assertFalse((data_dir / "imports").exists())
            self.assertFalse((data_dir / "artifacts").exists())
            self.assertFalse((data_dir / reset_module.RESET_JOURNAL_FILENAME).exists())

            post_reset_profile = save_profile(
                data_dir,
                {"basics": {"name": "New User"}},
                source="local_edit",
            )
            retry = reset_workspace(
                data_dir,
                {"request_id": request_id, "expected": expected},
            ).to_dict()
            self.assertFalse(retry["created"])
            self.assertEqual(retry["reset_at_ms"], first["reset_at_ms"])
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)
            self.assertEqual(post_reset_profile.version_number, 1)

    def test_stale_snapshot_and_changed_request_are_rejected_without_erasure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._import_profile(data_dir)
            expected = self._expected(data_dir)
            stale = {**expected, "profile_versions": expected["profile_versions"] - 1}
            with self.assertRaisesRegex(VaultError, "changed") as conflict:
                reset_workspace(
                    data_dir,
                    {"request_id": str(uuid.uuid4()), "expected": stale},
                )
            self.assertEqual(conflict.exception.code, "workspace_reset_conflict")
            self.assertEqual(self._expected(data_dir), expected)

            request_id = str(uuid.uuid4())
            reset_workspace(data_dir, {"request_id": request_id, "expected": expected})
            changed_expected = {**expected, "artifacts": expected["artifacts"] + 1}
            with self.assertRaises(VaultError) as request_conflict:
                reset_workspace(
                    data_dir,
                    {"request_id": request_id, "expected": changed_expected},
                )
            self.assertEqual(
                request_conflict.exception.code,
                "workspace_reset_request_conflict",
            )

    def test_historical_contract_one_receipts_do_not_block_a_new_reset(self) -> None:
        for historical_schema in (13, 14, 15):
            with (
                self.subTest(schema=historical_schema),
                tempfile.TemporaryDirectory() as directory,
            ):
                data_dir = Path(directory)
                self._import_profile(data_dir)
                expected = self._expected(data_dir)
                legacy_expected = {
                    field: expected[field]
                    for field in reset_module.LEGACY_RESET_EXPECTED_FIELDS
                }
                historical_request_id = str(uuid.uuid4())
                historical_result = {
                    **legacy_expected,
                    "profile_versions": 0,
                    "source_imports": 0,
                    "source_retention_generation": 1,
                }
                reset_module._write_state_file(
                    data_dir / reset_module.RESET_RECEIPT_FILENAME,
                    {
                        "contract_version": reset_module.LEGACY_RESET_CONTRACT_VERSION,
                        "request_id": historical_request_id,
                        "expected": legacy_expected,
                        "result": {
                            "request_id": historical_request_id,
                            "reset_at_ms": 1,
                            "schema_version": historical_schema,
                            **historical_result,
                        },
                    },
                )

                result = reset_workspace(
                    data_dir,
                    {"request_id": str(uuid.uuid4()), "expected": expected},
                ).to_dict()

                self.assertTrue(result["created"])
                self.assertEqual(result["schema_version"], SCHEMA_VERSION)
                completed = reset_module._validated_receipt(data_dir)
                self.assertIsNotNone(completed)
                self.assertEqual(
                    completed["contract_version"], reset_module.RESET_CONTRACT_VERSION
                )
                self.assertEqual(completed["result"]["schema_version"], SCHEMA_VERSION)

    def test_contract_one_receipt_exact_retry_normalizes_new_review_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            current_expected = self._expected(data_dir)
            legacy_expected = {
                field: current_expected[field]
                for field in reset_module.LEGACY_RESET_EXPECTED_FIELDS
            }
            request_id = str(uuid.uuid4())
            reset_module._write_state_file(
                data_dir / reset_module.RESET_RECEIPT_FILENAME,
                {
                    "contract_version": reset_module.LEGACY_RESET_CONTRACT_VERSION,
                    "request_id": request_id,
                    "expected": legacy_expected,
                    "result": {
                        "request_id": request_id,
                        "reset_at_ms": 1,
                        "schema_version": 15,
                        **{
                            field: 1 if field == "source_retention_generation" else 0
                            for field in reset_module.LEGACY_RESET_EXPECTED_FIELDS
                        },
                    },
                },
            )

            retry = reset_workspace(
                data_dir,
                {"request_id": request_id, "expected": current_expected},
            ).to_dict()

            self.assertFalse(retry["created"])
            self.assertEqual(retry["schema_version"], 15)
            self.assertEqual(retry["review_inbox_items"], 0)
            self.assertEqual(retry["review_deferred_items"], 0)
            self.assertEqual(retry["review_history_items"], 0)

    def test_rpc_contract_is_strict_and_response_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            expected = self._expected(data_dir)
            request = {
                "protocol_version": 1,
                "id": "reset",
                "method": "workspace.reset",
                "params": {
                    "request_id": str(uuid.uuid4()),
                    "expected": expected,
                },
            }
            response = _handle_request(data_dir, request)
            self.assertTrue(response["ok"])
            self.assertLess(len(json.dumps(response).encode()), 2048)
            self.assertEqual(
                set(response["result"]),
                {
                    "request_id",
                    "reset_at_ms",
                    "schema_version",
                    *RESET_FIELDS,
                    "created",
                },
            )

            bad_requests = [
                {**request["params"], "unexpected": True},
                {**request["params"], "request_id": str(uuid.uuid4()).upper()},
                {
                    **request["params"],
                    "request_id": str(uuid.uuid4()),
                    "expected": {**expected, "unexpected": 0},
                },
                {
                    **request["params"],
                    "request_id": str(uuid.uuid4()),
                    "expected": {**expected, "profile_versions": True},
                },
            ]
            for index, params in enumerate(bad_requests):
                with self.subTest(index=index):
                    rejected = _handle_request(
                        data_dir,
                        {**request, "id": f"bad-{index}", "params": params},
                    )
                    self.assertFalse(rejected["ok"])
                    self.assertEqual(rejected["error"]["code"], "invalid_params")

    def test_pending_journal_recovers_before_an_ordinary_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._populate(data_dir)
            provider = data_dir / "local-provider.json"
            provider.write_text("preserved", encoding="utf-8")
            expected = self._expected(data_dir)
            request_id = str(uuid.uuid4())
            reset_at_ms = int(time.time() * 1000)
            reset_module._write_state_file(
                data_dir / reset_module.RESET_JOURNAL_FILENAME,
                {
                    "contract_version": reset_module.RESET_CONTRACT_VERSION,
                    "phase": "prepared",
                    "request_id": request_id,
                    "reset_at_ms": reset_at_ms,
                    "expected": expected,
                },
            )

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(status.profile_versions, 0)
            self.assertEqual(status.opportunities, 0)
            self.assertEqual(status.artifacts, 0)
            self.assertEqual(status.source_imports, 0)
            self.assertEqual(status.source_previews, 0)
            self.assertEqual(status.source_retention_generation, 1)
            self.assertEqual(provider.read_text(encoding="utf-8"), "preserved")
            self.assertFalse((data_dir / reset_module.RESET_JOURNAL_FILENAME).exists())
            receipt = reset_module._validated_receipt(data_dir)
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["request_id"], request_id)
            self.assertEqual(receipt["result"]["reset_at_ms"], reset_at_ms)

    def test_contract_one_pending_journal_recovers_after_schema_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._populate(data_dir)
            current_expected = self._expected(data_dir)
            legacy_expected = {
                field: current_expected[field]
                for field in reset_module.LEGACY_RESET_EXPECTED_FIELDS
            }
            request_id = str(uuid.uuid4())
            reset_module._write_state_file(
                data_dir / reset_module.RESET_JOURNAL_FILENAME,
                {
                    "contract_version": reset_module.LEGACY_RESET_CONTRACT_VERSION,
                    "phase": "prepared",
                    "request_id": request_id,
                    "reset_at_ms": int(time.time() * 1000),
                    "expected": legacy_expected,
                },
            )

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            for field in RESET_FIELDS:
                expected_value = 1 if field == "source_retention_generation" else 0
                self.assertEqual(getattr(status, field), expected_value)
            receipt = reset_module._validated_receipt(data_dir)
            self.assertIsNotNone(receipt)
            self.assertEqual(
                receipt["contract_version"],
                reset_module.LEGACY_RESET_CONTRACT_VERSION,
            )
            self.assertEqual(receipt["request_id"], request_id)
            self.assertEqual(receipt["result"]["schema_version"], SCHEMA_VERSION)
            self.assertNotIn("review_inbox_items", receipt["result"])

    def test_invalid_pending_journal_fails_closed_before_database_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            saved = save_profile(
                data_dir,
                {"basics": {"name": "Preserve on failure"}},
                source="local_edit",
            )
            journal = data_dir / reset_module.RESET_JOURNAL_FILENAME
            journal.write_text('{"not":"a valid journal"}', encoding="utf-8")
            if os.name != "nt":
                journal.chmod(0o600)

            with self.assertRaises(VaultError) as failure:
                initialize_vault(data_dir)

            self.assertEqual(failure.exception.code, "workspace_reset_journal_invalid")
            self.assertTrue((data_dir / "cvgnome.sqlite3").exists())
            journal.unlink()
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)
            self.assertEqual(saved.version_number, 1)

    def test_initialized_journal_fails_closed_if_vault_is_not_pristine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            expected = self._expected(data_dir)
            with closing(
                sqlite3.connect(data_dir / "cvgnome.sqlite3")
            ) as connection:
                connection.execute(
                    """
                    INSERT INTO settings(key, value_json, revision, updated_at_ms)
                    VALUES ('unexpected', '{}', 1, 1)
                    """
                )
                connection.commit()
            reset_module._write_state_file(
                data_dir / reset_module.RESET_JOURNAL_FILENAME,
                {
                    "contract_version": reset_module.RESET_CONTRACT_VERSION,
                    "phase": "initialized",
                    "request_id": str(uuid.uuid4()),
                    "reset_at_ms": int(time.time() * 1000),
                    "expected": expected,
                },
            )

            with self.assertRaises(VaultError) as failure:
                initialize_vault(data_dir)

            self.assertEqual(failure.exception.code, "workspace_reset_recovery_failed")
            self.assertTrue((data_dir / reset_module.RESET_JOURNAL_FILENAME).exists())
            (data_dir / reset_module.RESET_JOURNAL_FILENAME).unlink()
            self.assertEqual(initialize_vault(data_dir).profile_versions, 0)
            with closing(
                sqlite3.connect(data_dir / "cvgnome.sqlite3")
            ) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM settings").fetchone()[0],
                    1,
                )

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior is platform-specific")
    def test_managed_symlink_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            outside_file = Path(outside) / "must-survive.txt"
            outside_file.write_text("survive", encoding="utf-8")
            (data_dir / "sources").symlink_to(Path(outside), target_is_directory=True)

            with self.assertRaises(VaultError) as failure:
                reset_workspace(
                    data_dir,
                    {
                        "request_id": str(uuid.uuid4()),
                        "expected": self._expected(data_dir),
                    },
                )

            self.assertEqual(failure.exception.code, "workspace_reset_unsafe")
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "survive")
            self.assertTrue((data_dir / "cvgnome.sqlite3").exists())
            self.assertFalse((data_dir / reset_module.RESET_JOURNAL_FILENAME).exists())

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            data_dir = Path(directory)
            initialize_vault(data_dir)
            outside_file = Path(outside) / "nested-must-survive.txt"
            outside_file.write_text("survive nested", encoding="utf-8")
            nested = data_dir / "artifacts" / "resumes"
            nested.mkdir(parents=True)
            (nested / "linked").symlink_to(Path(outside), target_is_directory=True)

            with self.assertRaises(VaultError) as failure:
                reset_workspace(
                    data_dir,
                    {
                        "request_id": str(uuid.uuid4()),
                        "expected": self._expected(data_dir),
                    },
                )

            self.assertEqual(failure.exception.code, "workspace_reset_unsafe")
            self.assertEqual(
                outside_file.read_text(encoding="utf-8"),
                "survive nested",
            )
            self.assertTrue((data_dir / "cvgnome.sqlite3").exists())
            self.assertFalse((data_dir / reset_module.RESET_JOURNAL_FILENAME).exists())

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior is platform-specific")
    def test_workspace_root_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as container:
            actual = Path(container) / "actual"
            linked = Path(container) / "linked"
            expected = self._expected(actual)
            linked.symlink_to(actual, target_is_directory=True)

            with self.assertRaises(VaultError) as failure:
                reset_workspace(
                    linked,
                    {"request_id": str(uuid.uuid4()), "expected": expected},
                )

            self.assertEqual(failure.exception.code, "workspace_reset_unsafe")
            self.assertTrue((actual / "cvgnome.sqlite3").exists())

    @unittest.skipIf(os.name == "nt", "POSIX file modes do not apply on Windows")
    def test_journal_receipt_and_lock_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            expected = self._expected(data_dir)
            result = reset_workspace(
                data_dir,
                {"request_id": str(uuid.uuid4()), "expected": expected},
            )
            self.assertTrue(result.created)
            for filename in (
                reset_module.RESET_RECEIPT_FILENAME,
                reset_module.RESET_LOCK_FILENAME,
            ):
                mode = stat.S_IMODE((data_dir / filename).stat().st_mode)
                self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
