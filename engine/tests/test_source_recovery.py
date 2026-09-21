# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvgnome_engine.profile_sources import preview_profile_sources
from cvgnome_engine.source_recovery import recover_profile_sources, resume_profile_source_scan
from cvgnome_engine.storage import VaultError, latest_profile, save_profile


class SourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)

    def scan(self, content=b"Career material to organize and review locally.\n"):
        scan_id = str(uuid.uuid4())
        relative = f"imports/staging/{scan_id}/0000.txt"
        staged = self.data_dir / relative
        staged.parent.mkdir(parents=True)
        staged.write_bytes(content)
        params = {"scan_id": scan_id, "sources": [{"ordinal": 0, "managed_relative_path": relative,
            "display_name": "fictional-resume.txt", "format": "txt", "byte_size": len(content),
            "checksum_sha256": hashlib.sha256(content).hexdigest()}],
            "file_counts": {"discovered": 1, "staged": 1, "skipped": 0}, "scan_issues": {}}
        return preview_profile_sources(self.data_dir, params), staged

    def request(self, scan, name="Avery Example", summary=None):
        return {"scan_id": scan["scan_id"], "patch": {"name": name, "summary": summary}}

    def connect(self):
        connection = sqlite3.connect(self.data_dir / "cvgnome.sqlite3")
        self.addCleanup(connection.close)
        return connection

    def test_named_incomplete_recovery_retains_original_and_extraction(self):
        raw = b"Career material to organize and review locally.\n"
        scan, staged = self.scan(raw)
        self.assertFalse(scan["can_build"])
        result = recover_profile_sources(self.data_dir, self.request(scan))
        self.assertEqual(result["profile_name"], "Avery Example")
        self.assertFalse(result["renderable"])
        self.assertEqual(result["version_number"], 1)
        self.assertFalse(staged.exists())
        connection = self.connect()
        source_path = connection.execute("SELECT relative_path FROM sources").fetchone()[0]
        self.assertEqual((self.data_dir / source_path).read_bytes(), raw)
        self.assertEqual(connection.execute("SELECT extracted_text FROM source_extractions").fetchone()[0], raw.decode())
        profile = latest_profile(self.data_dir)["profile"]
        self.assertEqual(profile["meta"]["source_import_recovery"]["confirmed_fields"], ["name"])
        self.assertNotIn("missing_identity", [w["code"] for w in result["warnings"]])
        self.assertEqual([w["severity"] for w in result["warnings"] if w["code"] == "insufficient_resume_content"], ["warning"])

    def test_summary_correction_is_user_authored_and_renderable(self):
        scan, _ = self.scan()
        result = recover_profile_sources(self.data_dir, self.request(scan, summary="Builds reliable local tools."))
        self.assertTrue(result["renderable"])
        profile = latest_profile(self.data_dir)["profile"]
        self.assertEqual(profile["basics"]["summary"], "Builds reliable local tools.")
        self.assertEqual(profile["meta"]["source_import_recovery"]["confirmed_fields"], ["name", "summary"])

    def test_unchanged_name_confirmation_can_keep_an_incomplete_profile(self):
        scan, _ = self.scan(b"Name: Avery Example\nCareer material to organize locally.\n")
        self.assertFalse(scan["can_build"])
        result = recover_profile_sources(self.data_dir, self.request(scan))
        self.assertFalse(result["renderable"])
        self.assertEqual(result["profile_name"], "Avery Example")
        self.assertEqual(latest_profile(self.data_dir)["profile"]["meta"]["source_import_recovery"]["changed_fields"], [])

    def test_exact_retry_never_creates_another_version_and_changed_retry_fails(self):
        scan, _ = self.scan()
        first = recover_profile_sources(self.data_dir, self.request(scan))
        second = recover_profile_sources(self.data_dir, self.request(scan))
        self.assertEqual(first["profile_version_id"], second["profile_version_id"])
        self.assertEqual(self.connect().execute("SELECT count(*) FROM profile_versions").fetchone()[0], 1)
        with self.assertRaises(VaultError) as raised:
            recover_profile_sources(self.data_dir, self.request(scan, name="Different Example"))
        self.assertEqual(raised.exception.code, "profile_source_recovery_conflict")

    def test_resume_restores_pinned_request_after_interrupted_commit(self):
        scan, staged = self.scan()
        request = self.request(scan, summary="Builds reliable local tools.")
        with patch("cvgnome_engine.source_recovery.commit_profile_sources", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                recover_profile_sources(self.data_dir, request)
        resumed = resume_profile_source_scan(self.data_dir)
        self.assertEqual(resumed["scan"]["scan_id"], scan["scan_id"])
        self.assertEqual(resumed["recovery_patch"], request["patch"])
        self.assertTrue(staged.exists())
        with self.assertRaises(VaultError):
            recover_profile_sources(self.data_dir, self.request(scan, name="Changed Example"))
        self.assertTrue(recover_profile_sources(self.data_dir, request)["renderable"])
        self.assertIsNone(resume_profile_source_scan(self.data_dir))

    @unittest.skipUnless(hasattr(os, "fork"), "requires process-exit crash simulation")
    def test_process_exit_during_commit_restores_resumable_preview(self):
        scan, staged = self.scan()
        request = self.request(scan)
        child = os.fork()
        if child == 0:
            with patch("cvgnome_engine.storage._read_verified_source_file", side_effect=lambda *a, **kw: os._exit(73)):
                recover_profile_sources(self.data_dir, request)
            os._exit(74)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        self.assertTrue(staged.exists())
        resumed = resume_profile_source_scan(self.data_dir)
        self.assertEqual(resumed["recovery_patch"], request["patch"])
        self.assertEqual(self.connect().execute("SELECT status FROM profile_source_scans").fetchone()[0], "preview")
        result = recover_profile_sources(self.data_dir, request)
        self.assertEqual(result["version_number"], 1)
        self.assertFalse(result["renderable"])

    def test_resume_returns_aggregate_only_and_no_patch_for_original_scan(self):
        self.assertIsNone(resume_profile_source_scan(self.data_dir))
        scan, _ = self.scan()
        result = resume_profile_source_scan(self.data_dir)
        self.assertEqual(result, {"scan": scan, "recovery_patch": None})
        serialized = json.dumps(result)
        self.assertNotIn("fictional-resume.txt", serialized)
        self.assertNotIn("Career material", serialized)
        self.assertNotIn("imports/staging", serialized)

    def test_recovery_preserves_extracted_fields_and_builds_valid_source_checks(self):
        scan, _ = self.scan(b"Name: Original Example\nEmail: original@example.test\nSkills: Python, SQL\n")
        result = recover_profile_sources(self.data_dir, self.request(scan))
        self.assertTrue(result["renderable"])
        self.assertEqual(result["source_review_count"], 1)
        profile = latest_profile(self.data_dir)["profile"]
        self.assertEqual(profile["basics"]["email"], "original@example.test")
        self.assertEqual(profile["skills"][0]["keywords"], ["Python", "SQL"])
        material = json.loads(self.connect().execute("SELECT material_json FROM source_review_items").fetchone()[0])
        self.assertEqual(material["previous_value"], "Avery Example")
        self.assertEqual(material["proposed_value"], "Original Example")

    def test_invalid_patch_never_changes_scan(self):
        scan, staged = self.scan()
        for invalid in ({"name": "", "summary": None}, {"name": "A" * 161, "summary": None},
                        {"name": "Avery Example", "summary": "x" * 4001},
                        {"name": "Avery Example", "summary": None, "extra": True},
                        {"name": "Avery Example"}, {"name": None, "summary": None}):
            with self.subTest(patch=tuple(invalid)):
                with self.assertRaises(ValueError):
                    recover_profile_sources(self.data_dir, {"scan_id": scan["scan_id"], "patch": invalid})
        self.assertIsNone(latest_profile(self.data_dir))
        self.assertEqual(resume_profile_source_scan(self.data_dir)["recovery_patch"], None)
        self.assertTrue(staged.exists())

    def test_staged_byte_tampering_is_rejected_and_keeps_original_preview(self):
        scan, staged = self.scan()
        staged.write_bytes(b"Modified document")
        with self.assertRaises(VaultError):
            recover_profile_sources(self.data_dir, self.request(scan))
        self.assertIsNone(latest_profile(self.data_dir))
        self.assertTrue(staged.exists())
        self.assertEqual(resume_profile_source_scan(self.data_dir)["recovery_patch"], self.request(scan)["patch"])

    def test_expired_scan_and_changed_base_cannot_be_recovered(self):
        scan, _ = self.scan()
        connection = self.connect()
        connection.execute("UPDATE profile_source_scans SET expires_at_ms = 1")
        connection.commit()
        with self.assertRaises(VaultError) as raised:
            recover_profile_sources(self.data_dir, self.request(scan))
        self.assertEqual(raised.exception.code, "profile_source_scan_expired")
        self.assertIsNone(resume_profile_source_scan(self.data_dir))
        scan, _ = self.scan()
        save_profile(self.data_dir, {"basics": {"name": "Existing Example"}})
        with self.assertRaises(VaultError) as raised:
            recover_profile_sources(self.data_dir, self.request(scan))
        self.assertEqual(raised.exception.code, "profile_source_base_changed")
        self.assertIsNone(resume_profile_source_scan(self.data_dir))

    def test_existing_profile_update_cannot_use_first_profile_recovery(self):
        save_profile(self.data_dir, {"basics": {"name": "Existing Example"}})
        scan, _ = self.scan()
        with self.assertRaises(VaultError) as raised:
            recover_profile_sources(self.data_dir, self.request(scan))
        self.assertEqual(raised.exception.code, "profile_source_scan_not_recoverable")


if __name__ == "__main__":
    unittest.main()
