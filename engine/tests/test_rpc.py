# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SRC = ENGINE_ROOT / "src"


class RpcTests(unittest.TestCase):
    def _one_shot(
        self,
        directory: str,
        request: dict[str, object],
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "cvgnome_engine",
                "request",
                "--data-dir",
                directory,
            ],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ENGINE_SRC)},
        )
        return completed, json.loads(completed.stdout)

    def _canonical_import_request(
        self,
        directory: str,
        profile: dict[str, object],
        *,
        request_id: str = "profile",
        scan_id: str | None = None,
        content: bytes | None = None,
        display_name: str = "canonical-profile.json",
    ) -> dict[str, object]:
        scan_id = scan_id or str(uuid.uuid4())
        content = content or json.dumps(
            profile,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        staged = Path(directory, "imports", "staging", scan_id, "0000.json")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        return {
            "protocol_version": 1,
            "id": request_id,
            "method": "profile.import",
            "params": {
                "source": {
                    "scan_id": scan_id,
                    "managed_relative_path": f"imports/staging/{scan_id}/0000.json",
                    "display_name": display_name,
                    "raw_sha256": hashlib.sha256(content).hexdigest(),
                    "raw_bytes": len(content),
                }
            },
        }

    def test_one_shot_request_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed, response = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "single",
                    "method": "system.status",
                    "params": {},
                },
            )

            self.assertEqual(completed.returncode, 0)
            self.assertTrue(response["ok"])
            self.assertEqual(response["id"], "single")
            self.assertEqual(response["result"]["engine_version"], "0.5.0")

    def test_module_cli_uses_public_cvgnome_branding(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "cvgnome_engine", "--help"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONPATH": str(ENGINE_SRC)},
        )

        self.assertIn("usage: cvgnome-engine", completed.stdout)

    def test_status_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = json.dumps(
                {
                    "protocol_version": 1,
                    "id": "one",
                    "method": "system.status",
                    "params": {},
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cvgnome_engine",
                    "rpc",
                    "--data-dir",
                    directory,
                ],
                input=request + "\n",
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONPATH": str(ENGINE_SRC)},
            )

            response = json.loads(completed.stdout)
            self.assertTrue(response["ok"])
            self.assertEqual(response["protocol_version"], 1)
            self.assertEqual(response["id"], "one")
            self.assertEqual(response["result"]["schema_version"], 19)

    def test_unknown_method_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requests = [
                {
                    "protocol_version": 1,
                    "id": "missing",
                    "method": "not.a.method",
                    "params": {},
                },
                {
                    "protocol_version": 1,
                    "id": "after",
                    "method": "system.status",
                    "params": {},
                },
            ]
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cvgnome_engine",
                    "rpc",
                    "--data-dir",
                    directory,
                ],
                input="\n".join(json.dumps(request) for request in requests) + "\n",
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONPATH": str(ENGINE_SRC)},
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(len(responses), 2)
            self.assertFalse(responses[0]["ok"])
            self.assertEqual(responses[0]["id"], "missing")
            self.assertEqual(responses[0]["error"]["code"], "method_not_found")
            self.assertTrue(responses[1]["ok"])
            self.assertEqual(responses[1]["id"], "after")

    def test_protocol_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed, response = self._one_shot(
                directory,
                {
                    "protocol_version": 99,
                    "id": "wrong-version",
                    "method": "system.status",
                    "params": {},
                },
            )

            self.assertEqual(completed.returncode, 1)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "protocol_mismatch")

    def test_import_deduplicates_and_exports_searchable_documents(self) -> None:
        profile = {
            "basics": {
                "name": "Ada Lovelace",
                "label": "Analytics Engineer",
                "summary": "Builds durable local decision systems.",
            },
            "skills": [
                {"name": "Data", "keywords": ["Python"]},
                {"name": "Data", "keywords": ["SQL"]},
            ],
            "work": [
                {
                    "name": "Analytical Engines",
                    "position": "Lead Engineer",
                    "startDate": "2023-01",
                    "highlights": ["Shipped deterministic local exports."],
                }
            ],
            "meta": {"private_evidence": "must not appear"},
        }
        with tempfile.TemporaryDirectory() as directory:
            import_request = self._canonical_import_request(
                directory,
                profile,
                request_id="import-one",
            )
            first_completed, first = self._one_shot(directory, import_request)
            second_completed, second = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    profile,
                    request_id="import-two",
                ),
            )

            self.assertEqual(first_completed.returncode, 0)
            self.assertEqual(second_completed.returncode, 0)
            self.assertTrue(first["result"]["created"])
            self.assertFalse(second["result"]["created"])
            self.assertEqual(first["result"]["profile_version_id"], second["result"]["profile_version_id"])
            self.assertEqual(first["result"]["skill_groups"], 1)

            for output_format, signature in (("docx", b"PK"), ("pdf", b"%PDF-")):
                completed, response = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": f"export-{output_format}",
                        "method": "resume.export",
                        "params": {"format": output_format},
                    },
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                artifact_path = Path(directory, response["result"]["relative_path"])
                self.assertTrue(artifact_path.read_bytes().startswith(signature))
                self.assertNotIn("must not appear", artifact_path.read_bytes().decode("latin-1"))

            status_completed, status = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "status-after",
                    "method": "system.status",
                    "params": {},
                },
            )
            self.assertEqual(status_completed.returncode, 0)
            self.assertEqual(status["result"]["profile_versions"], 1)
            self.assertEqual(status["result"]["artifacts"], 2)
            self.assertEqual(status["result"]["retained_source_files"], 1)
            self.assertEqual(status["result"]["retained_source_imports"], 2)

    def test_baseline_export_can_pin_an_immutable_profile_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_v1 = {
                "basics": {"name": "Ada Version One", "summary": "PINNED V1 SUMMARY"},
                "work": [{"name": "V1 Organization", "position": "V1 Role"}],
            }
            profile_v2 = {
                "basics": {"name": "Grace Version Two", "summary": "LATEST V2 SUMMARY"},
                "work": [{"name": "V2 Organization", "position": "V2 Role"}],
            }
            completed, imported_v1 = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    profile_v1,
                    request_id="import-export-v1",
                    display_name="profile-v1.json",
                ),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            completed, imported_v2 = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    profile_v2,
                    request_id="import-export-v2",
                    display_name="profile-v2.json",
                ),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            v1_id = imported_v1["result"]["profile_version_id"]
            v2_id = imported_v2["result"]["profile_version_id"]
            self.assertNotEqual(v1_id, v2_id)

            completed, pinned = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "export-pinned-v1",
                    "method": "resume.export",
                    "params": {"format": "docx", "profile_version_id": v1_id},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(pinned["result"]["profile_version_id"], v1_id)
            self.assertEqual(
                set(pinned["result"]),
                {
                    "artifact_id",
                    "profile_version_id",
                    "format",
                    "media_type",
                    "relative_path",
                    "checksum_sha256",
                    "byte_size",
                    "suggested_filename",
                },
            )
            with zipfile.ZipFile(Path(directory, pinned["result"]["relative_path"])) as archive:
                pinned_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("PINNED V1 SUMMARY", pinned_xml)
            self.assertNotIn("LATEST V2 SUMMARY", pinned_xml)

            completed, latest = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "export-latest-v2",
                    "method": "resume.export",
                    "params": {"format": "docx"},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(latest["result"]["profile_version_id"], v2_id)
            with zipfile.ZipFile(Path(directory, latest["result"]["relative_path"])) as archive:
                latest_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("LATEST V2 SUMMARY", latest_xml)

            for request_id, params in (
                ("export-extra-param", {"format": "pdf", "profile_version_id": v1_id, "extra": True}),
                ("export-invalid-profile", {"format": "pdf", "profile_version_id": "not-a-uuid"}),
            ):
                completed, rejected = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": request_id,
                        "method": "resume.export",
                        "params": params,
                    },
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(rejected["error"]["code"], "invalid_params")

    def test_canonical_import_manifest_and_retention_reset_rpc_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = {
                "basics": {"name": "Ada", "summary": "Local Python engineer."}
            }
            completed, imported = self._one_shot(
                directory,
                self._canonical_import_request(directory, profile, request_id="import"),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(imported["ok"])

            completed, materials = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "materials-active",
                    "method": "profile.sources.list",
                    "params": {"scope": "active", "limit": 25, "offset": 0},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                set(materials["result"]),
                {
                    "scope",
                    "current_generation",
                    "offset",
                    "limit",
                    "total_entries",
                    "total_unique_files",
                    "total_imports",
                    "items",
                },
            )
            self.assertEqual(materials["result"]["total_entries"], 1)
            self.assertEqual(materials["result"]["items"][0]["import_mode"], "canonical")
            self.assertEqual(
                set(materials["result"]["items"][0]),
                {
                    "import_id",
                    "ordinal",
                    "display_name",
                    "source_format",
                    "source_kind",
                    "byte_size",
                    "extraction_status",
                    "issue_code",
                    "retention_generation",
                    "retention_status",
                    "committed_at_ms",
                    "profile_version_number",
                    "import_mode",
                    "import_file_count",
                    "content_reference_count",
                },
            )
            serialized_materials = json.dumps(materials["result"])
            self.assertNotIn(imported["result"]["checksum_sha256"], serialized_materials)
            self.assertNotIn("managed_relative_path", serialized_materials)

            reset_request_id = str(uuid.uuid4())
            completed, reset = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "reset",
                    "method": "profile.sources.reset",
                    "params": {
                        "expected_generation": 1,
                        "request_id": reset_request_id,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                set(reset["result"]),
                {
                    "source_retention_generation",
                    "retained_source_files",
                    "retained_source_bytes",
                    "retained_source_imports",
                    "historical_source_files",
                    "historical_source_imports",
                    "last_source_import_at_ms",
                    "source_retention_reset_at_ms",
                },
            )
            self.assertEqual(reset["result"]["source_retention_generation"], 2)
            self.assertEqual(reset["result"]["retained_source_files"], 0)
            self.assertEqual(reset["result"]["historical_source_files"], 1)

            completed, historical = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "materials-historical",
                    "method": "profile.sources.list",
                    "params": {"scope": "historical", "limit": 25, "offset": 0},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(historical["result"]["total_entries"], 1)
            self.assertEqual(
                historical["result"]["items"][0]["retention_status"],
                "historical",
            )

            completed, retry = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "reset-retry",
                    "method": "profile.sources.reset",
                    "params": {
                        "expected_generation": 1,
                        "request_id": reset_request_id,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(retry["result"], reset["result"])

            completed, invalid = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "reset-extra",
                    "method": "profile.sources.reset",
                    "params": {
                        "expected_generation": 2,
                        "request_id": str(uuid.uuid4()),
                        "erase": True,
                    },
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(invalid["error"]["code"], "invalid_params")

            for index, params in enumerate(
                (
                    {"scope": "active", "limit": 25},
                    {"scope": "active", "limit": True, "offset": 0},
                    {"scope": "all", "limit": 26, "offset": 0},
                    {"scope": "deleted", "limit": 25, "offset": 0},
                    {"scope": "all", "limit": 25, "offset": 0, "paths": True},
                )
            ):
                completed, invalid_list = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": f"materials-invalid-{index}",
                        "method": "profile.sources.list",
                        "params": params,
                    },
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(invalid_list["error"]["code"], "invalid_params")

        with tempfile.TemporaryDirectory() as directory:
            completed, legacy_inline = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "legacy-inline",
                    "method": "profile.import",
                    "params": {"profile": {"basics": {"name": "Ada"}}},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(legacy_inline["error"]["code"], "invalid_params")

    def test_discard_all_source_previews_rpc_is_aggregate_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed, discarded = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "discard-all",
                    "method": "profile.sources.discard_all",
                    "params": {},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(discarded["result"], {"discarded_previews": 0})

            completed, invalid = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "discard-all-extra",
                    "method": "profile.sources.discard_all",
                    "params": {"scan_id": str(uuid.uuid4())},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(invalid["error"]["code"], "invalid_params")

    def test_source_preview_commit_and_discard_round_trip(self) -> None:
        profile = {
            "basics": {"name": "Ada Lovelace", "summary": "Local analytics engineer."},
            "skills": [{"name": "Data", "keywords": ["Python", "SQL"]}],
        }
        content = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def stage(scan_id: str) -> dict[str, object]:
                staged = root / "imports" / "staging" / scan_id / "0000.json"
                staged.parent.mkdir(parents=True)
                staged.write_bytes(content)
                return {
                    "scan_id": scan_id,
                    "sources": [
                        {
                            "ordinal": 0,
                            "managed_relative_path": staged.relative_to(root).as_posix(),
                            "display_name": "private/canonical-profile.json",
                            "format": "json",
                            "byte_size": len(content),
                            "checksum_sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                    "file_counts": {"discovered": 1, "staged": 1, "skipped": 0},
                    "scan_issues": {},
                }

            committed_scan = str(uuid.uuid4())
            completed, preview = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "source-preview",
                    "method": "profile.sources.preview",
                    "params": stage(committed_scan),
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(preview["result"]["can_build"])
            self.assertEqual(preview["result"]["scan_id"], committed_scan)
            self.assertNotIn("private/canonical-profile.json", json.dumps(preview["result"]))

            completed, committed = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "source-commit",
                    "method": "profile.sources.commit",
                    "params": {"scan_id": committed_scan},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(committed["result"]["renderable"])
            self.assertEqual(committed["result"]["scan_id"], committed_scan)

            discarded_scan = str(uuid.uuid4())
            completed, second_preview = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "discard-preview",
                    "method": "profile.sources.preview",
                    "params": stage(discarded_scan),
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(second_preview["result"]["scan_id"], discarded_scan)
            completed, discarded = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "source-discard",
                    "method": "profile.sources.discard",
                    "params": {"scan_id": discarded_scan},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(discarded["result"], {"discarded": True})
            self.assertFalse((root / "imports" / "staging" / discarded_scan).exists())

    def test_opportunity_save_list_get_and_rematch_round_trip(self) -> None:
        profile = {
            "basics": {
                "name": "Ada Lovelace",
                "label": "Analytics Engineer",
                "summary": "Python analytics and local decision systems.",
            },
            "skills": [{"name": "Data", "keywords": ["Python", "SQL"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            completed, imported = self._one_shot(
                directory,
                self._canonical_import_request(directory, profile),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            completed, saved = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "save-match",
                    "method": "opportunities.save_match",
                    "params": {
                        "title": "Analytics Engineer",
                        "company": "Example Labs",
                        "location": "Remote",
                        "source_url": "https://EXAMPLE.test/jobs/one",
                        "apply_url": "https://example.test/apply/one",
                        "description": "Build Python SQL analytics. Review Kubernetes delivery.",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            detail = saved["result"]
            opportunity_id = detail["opportunity"]["id"]
            self.assertEqual(detail["opportunity"]["status"], "ready")
            self.assertEqual(detail["opportunity"]["source_url"], "https://example.test/jobs/one")
            self.assertEqual(detail["match"]["contract"], "deterministic-local-v1")
            self.assertEqual(
                detail["profile_version"]["id"],
                imported["result"]["profile_version_id"],
            )
            self.assertIn("python", detail["match"]["matched_terms"])
            self.assertIn("kubernetes", detail["match"]["terms_to_review"])

            completed, listed = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "list",
                    "method": "opportunities.list",
                    "params": {
                        "query": "Example",
                        "offset": 0,
                        "stage": "all",
                        "sort": "last_action",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(listed["result"]["total"], 1)
            self.assertEqual(listed["result"]["items"][0]["id"], opportunity_id)
            self.assertNotIn("description", json.dumps(listed["result"]))
            self.assertNotIn("evidence", json.dumps(listed["result"]))
            self.assertNotIn("notes", listed["result"]["items"][0])

            completed, updated = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "update-tracker",
                    "method": "opportunities.update",
                    "params": {
                        "opportunity_id": opportunity_id,
                        "expected_tracker_updated_at_ms": detail["opportunity"][
                            "tracker_updated_at_ms"
                        ],
                        "tracker_stage": "to_apply",
                        "notes": "ÉCOLE ÜBERPRÜFUNG STRAẞE",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(updated["result"]["opportunity"]["tracker_stage"], "to_apply")
            self.assertEqual(
                updated["result"]["opportunity"]["notes"],
                "ÉCOLE ÜBERPRÜFUNG STRAẞE",
            )
            self.assertGreater(
                updated["result"]["opportunity"]["tracker_updated_at_ms"],
                detail["opportunity"]["tracker_updated_at_ms"],
            )

            completed, note_search = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "search-tracker-notes",
                    "method": "opportunities.list",
                    "params": {
                        "query": "école überprüfung strasse",
                        "offset": 0,
                        "stage": "all",
                        "sort": "last_action",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(note_search["result"]["total"], 1)
            self.assertEqual(note_search["result"]["items"][0]["id"], opportunity_id)
            self.assertNotIn("notes", note_search["result"]["items"][0])

            for request_id, method in (("get", "opportunities.get"), ("rematch", "opportunities.rematch")):
                completed, response = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": request_id,
                        "method": method,
                        "params": {"opportunity_id": opportunity_id},
                    },
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(response["result"]["opportunity"]["id"], opportunity_id)
                self.assertLess(len(completed.stdout.encode("utf-8")), 64 * 1024)

    def test_opportunity_rpc_rejects_missing_profile_invalid_metadata_and_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = {
                "protocol_version": 1,
                "id": "missing-profile",
                "method": "opportunities.save_match",
                "params": {
                    "title": "Engineer",
                    "description": "Build local Python systems.",
                    "company": None,
                    "location": None,
                    "source_url": None,
                    "apply_url": None,
                },
            }
            completed, missing = self._one_shot(directory, request)
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(missing["error"]["code"], "profile_missing")

            self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    {"basics": {"name": "Ada", "summary": "Python engineer"}},
                ),
            )
            request["id"] = "null-optionals"
            completed, minimal = self._one_shot(directory, request)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(minimal["result"]["opportunity"]["company"], "")
            self.assertEqual(minimal["result"]["opportunity"]["location"], "")

            request["id"] = "bad-url"
            request["params"]["source_url"] = "http://example.test/job"
            completed, invalid = self._one_shot(directory, request)
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(invalid["error"]["code"], "invalid_params")

            completed, missing_opportunity = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "missing-opportunity",
                    "method": "opportunities.get",
                    "params": {
                        "opportunity_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                    },
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                missing_opportunity["error"]["code"],
                "opportunity_not_found",
            )

            for method, params in (
                (
                    "opportunities.save_match",
                    {"title": "Engineer", "description": "Python", "unexpected": True},
                ),
                ("opportunities.list", {"unexpected": True}),
                (
                    "opportunities.get",
                    {
                        "opportunity_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "unexpected": True,
                    },
                ),
                (
                    "opportunities.rematch",
                    {
                        "opportunity_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "unexpected": True,
                    },
                ),
                (
                    "opportunities.update",
                    {
                        "opportunity_id": minimal["result"]["opportunity"]["id"],
                        "expected_tracker_updated_at_ms": minimal["result"]["opportunity"][
                            "tracker_updated_at_ms"
                        ],
                        "notes": "hello",
                        "unexpected": True,
                    },
                ),
            ):
                completed, unknown = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": f"unknown-{method}",
                        "method": method,
                        "params": params,
                    },
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(unknown["error"]["code"], "invalid_params")

            completed, conflict = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tracker-conflict",
                    "method": "opportunities.update",
                    "params": {
                        "opportunity_id": minimal["result"]["opportunity"]["id"],
                        "expected_tracker_updated_at_ms": 0,
                        "notes": "stale",
                    },
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(conflict["error"]["code"], "opportunity_tracker_conflict")

    def test_opportunity_detail_rpc_stays_under_native_response_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    {"basics": {"name": "Ada", "summary": "Python SQL analytics"}},
                ),
            )
            completed, response = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "bounded",
                    "method": "opportunities.save_match",
                    "params": {
                        "title": "Analytics Engineer",
                        "description": "x" * 31_999,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(response["ok"])
            self.assertLess(len(completed.stdout.encode("utf-8")), 64 * 1024)

            completed, oversized_escape = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "escaped-overflow",
                    "method": "opportunities.save_match",
                    "params": {
                        "title": "Quoted Engineer",
                        "description": '"' * 31_999,
                    },
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(oversized_escape["error"]["code"], "invalid_params")

    def test_export_requires_renderable_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed, missing = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "missing",
                    "method": "resume.export",
                    "params": {"format": "pdf"},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(missing["error"]["code"], "profile_missing")

            self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    {"basics": {"name": "Ada"}},
                    request_id="name-only",
                ),
            )
            completed, not_renderable = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "not-renderable",
                    "method": "resume.export",
                    "params": {"format": "pdf"},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(not_renderable["error"]["code"], "profile.not_renderable")

            completed, structurally_empty = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    {
                        "basics": {"name": "Ada"},
                        "projects": [{"unknown_private_field": "not resume content"}],
                    },
                    request_id="empty-project",
                ),
            )
            self.assertEqual(completed.returncode, 0)
            self.assertFalse(structurally_empty["result"]["renderable"])

            _, status = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "empty-status",
                    "method": "system.status",
                    "params": {},
                },
            )
            self.assertFalse(status["result"]["latest_profile_renderable"])

            completed, still_not_renderable = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "empty-export",
                    "method": "resume.export",
                    "params": {"format": "docx"},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                still_not_renderable["error"]["code"],
                "profile.not_renderable",
            )

    def test_profile_review_is_private_paged_and_version_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed, missing = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "review-missing",
                    "method": "profile.review",
                    "params": {},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(missing["error"]["code"], "profile_missing")

            profile_v1 = {
                "basics": {
                    "name": "Ada Lovelace",
                    "label": "Profile V1",
                    "email": "ada@example.test",
                    "private_nested": "BASICS SECRET",
                },
                "work": [
                    {
                        "name": f"V1 Organization {index}",
                        "position": f"V1 Role {index}",
                        "startDate": str(2000 + index),
                        "private_nested": f"WORK SECRET {index}",
                    }
                    for index in range(13)
                ],
                "meta": {
                    "source_ingest": {
                        "filename": "SOURCE SECRET.pdf",
                        "content_sha256": "HASH SECRET",
                    },
                    "dossier": {"claims": [{"text": "DOSSIER SECRET"}]},
                },
                "private_top_level": "TOP SECRET",
            }
            completed, imported_v1 = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    profile_v1,
                    request_id="review-v1-import",
                    display_name="PERSISTED SOURCE SECRET.json",
                ),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            version_v1 = imported_v1["result"]["profile_version_id"]

            completed, summary = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "review-summary",
                    "method": "profile.review",
                    "params": {},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(summary["result"]["profile_version_id"], version_v1)
            self.assertEqual(summary["result"]["source_kind"], "structured_file")
            encoded_summary = json.dumps(summary, ensure_ascii=False)
            for secret in (
                "BASICS SECRET",
                "WORK SECRET",
                "SOURCE SECRET",
                "HASH SECRET",
                "DOSSIER SECRET",
                "TOP SECRET",
                "PERSISTED SOURCE SECRET",
            ):
                self.assertNotIn(secret, encoded_summary)

            completed, first_page = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "review-page-one",
                    "method": "profile.review.section",
                    "params": {
                        "profile_version_id": version_v1,
                        "section": "work",
                        "offset": 0,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(first_page["result"]["total_items"], 13)
            self.assertEqual(first_page["result"]["next_offset"], 10)
            self.assertEqual(len(first_page["result"]["items"]), 10)

            completed, imported_v2 = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    {
                        "basics": {"name": "Ada Lovelace", "label": "Profile V2"},
                        "work": [{"name": "V2 Organization", "position": "V2 Role"}],
                    },
                    request_id="review-v2-import",
                ),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotEqual(imported_v2["result"]["profile_version_id"], version_v1)

            completed, pinned_page = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "review-pinned-page",
                    "method": "profile.review.section",
                    "params": {
                        "profile_version_id": version_v1,
                        "section": "work",
                        "offset": 10,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            pinned_text = json.dumps(pinned_page)
            self.assertIn("V1 Role", pinned_text)
            self.assertNotIn("V2 Role", pinned_text)
            self.assertEqual(pinned_page["result"]["profile_version_id"], version_v1)
            self.assertEqual([item["ordinal"] for item in pinned_page["result"]["items"]], [10, 11, 12])

            invalid_requests = (
                {
                    "profile_version_id": version_v1,
                    "section": "private",
                    "offset": 0,
                },
                {
                    "profile_version_id": version_v1.upper(),
                    "section": "work",
                    "offset": 0,
                },
                {
                    "profile_version_id": version_v1,
                    "section": "work",
                    "offset": True,
                },
                {
                    "profile_version_id": version_v1,
                    "section": "work",
                    "offset": 0,
                    "unexpected": True,
                },
            )
            for index, params in enumerate(invalid_requests):
                completed, invalid = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": f"review-invalid-{index}",
                        "method": "profile.review.section",
                        "params": params,
                    },
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(invalid["error"]["code"], "invalid_params")

            completed, unknown_summary_param = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "review-summary-invalid",
                    "method": "profile.review",
                    "params": {"unexpected": True},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(unknown_summary_param["error"]["code"], "invalid_params")

    def test_profile_review_classifies_stored_profile_corruption(self) -> None:
        for stored_json in ("not-json", "[]"):
            with self.subTest(stored_json=stored_json), tempfile.TemporaryDirectory() as directory:
                completed, imported = self._one_shot(
                    directory,
                    self._canonical_import_request(
                        directory,
                        {
                            "basics": {"name": "Ada Lovelace"},
                            "work": [{"name": "Analytical Engines", "position": "Programmer"}],
                        },
                        request_id="corrupt-review-import",
                    ),
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                version_id = imported["result"]["profile_version_id"]

                with closing(sqlite3.connect(Path(directory, "cvgnome.sqlite3"))) as connection:
                    connection.execute("DROP TRIGGER profile_versions_no_update")
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        "UPDATE profile_versions SET canonical_json = ? WHERE id = ?",
                        (stored_json, version_id),
                    )
                    connection.commit()

                requests = (
                    {
                        "protocol_version": 1,
                        "id": "corrupt-review-summary",
                        "method": "profile.review",
                        "params": {},
                    },
                    {
                        "protocol_version": 1,
                        "id": "corrupt-review-section",
                        "method": "profile.review.section",
                        "params": {
                            "profile_version_id": version_id,
                            "section": "work",
                            "offset": 0,
                        },
                    },
                )
                for request in requests:
                    completed, response = self._one_shot(directory, request)
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(response["error"]["code"], "vault_integrity_error")

                completed, bad_caller = self._one_shot(
                    directory,
                    {
                        "protocol_version": 1,
                        "id": "corrupt-review-bad-caller",
                        "method": "profile.review.section",
                        "params": {
                            "profile_version_id": "NOT-A-UUID",
                            "section": "work",
                            "offset": 0,
                        },
                    },
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(bad_caller["error"]["code"], "invalid_params")

    def test_tailoring_rpc_round_trip_uses_exact_provider_neutral_contracts(self) -> None:
        profile = {
            "basics": {
                "name": "Ada Lovelace",
                "label": "Analytics Engineer",
                "email": "ada@example.test",
                "summary": "Analytics engineer improving activation by 12% with Python.",
            },
            "work": [
                {
                    "name": "Analytical Engines",
                    "position": "Lead Engineer",
                    "startDate": "2022",
                    "endDate": "2024",
                    "summary": "Led Python analytics delivery.",
                    "highlights": ["Improved activation by 12%."],
                }
            ],
            "skills": [{"name": "Data", "keywords": ["Python", "SQL"]}],
            "meta": {"private_evidence": "PRIVATE RPC SECRET 99%"},
        }
        with tempfile.TemporaryDirectory() as directory:
            completed, imported = self._one_shot(
                directory,
                self._canonical_import_request(
                    directory,
                    profile,
                    request_id="tailoring-profile",
                ),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(imported["result"]["renderable"])

            completed, matched = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-role",
                    "method": "opportunities.save_match",
                    "params": {
                        "title": "Senior Analytics Engineer",
                        "company": "Example Labs",
                        "location": "Remote",
                        "source_url": "https://example.test/jobs/rpc-tailoring",
                        "apply_url": "https://example.test/apply/rpc-tailoring",
                        "description": "Build Python and SQL analytics systems.",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            opportunity_id = matched["result"]["opportunity"]["id"]

            completed, prepared = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-prepare",
                    "method": "tailoring.prepare",
                    "params": {
                        "opportunity_id": opportunity_id,
                        "provider_kind": "openai",
                        "chat_model_id": "gpt-5-mini",
                        "embedding_model_id": "text-embedding-3-small",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prepared_result = prepared["result"]
            self.assertEqual(
                set(prepared_result),
                {"attempt_id", "embedding_model_id", "embedding_input"},
            )
            self.assertEqual(
                prepared_result["embedding_model_id"],
                "text-embedding-3-small",
            )
            embeddings = [[1.0, 0.0] for _ in prepared_result["embedding_input"]]

            completed, armed = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-arm",
                    "method": "tailoring.arm",
                    "params": {
                        "attempt_id": prepared_result["attempt_id"],
                        "embeddings": embeddings,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                set(armed["result"]),
                {
                    "attempt_id",
                    "chat_model_id",
                    "messages",
                    "response_format",
                    "max_tokens",
                },
            )
            self.assertEqual(armed["result"]["chat_model_id"], "gpt-5-mini")

            response_text = json.dumps(
                {
                    "basics": {
                        "label": "Analytics Engineer",
                        "summary": (
                            "Analytics engineer improving activation by 12% with Python."
                        ),
                    },
                    "work": [
                        {
                            "source_index": 0,
                            "summary": "Led Python analytics delivery.",
                            "highlights": ["Improved activation by 12%."],
                        }
                    ],
                }
            )
            completed, finalized = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-finalize",
                    "method": "tailoring.finalize",
                    "params": {
                        "attempt_id": prepared_result["attempt_id"],
                        "response_text": response_text,
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            draft = finalized["result"]
            self.assertIn("resume", draft)
            self.assertNotIn("resume_json", draft)
            self.assertNotIn("response_text", draft)
            self.assertNotIn("job_description", draft)
            self.assertNotIn("PRIVATE RPC SECRET", json.dumps(draft))
            self.assertEqual(
                draft["models"],
                {
                    "provider_kind": "openai",
                    "chat_model_id": "gpt-5-mini",
                    "embedding_model_id": "text-embedding-3-small",
                },
            )

            completed, latest = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-latest",
                    "method": "tailoring.latest",
                    "params": {"opportunity_id": opportunity_id},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(latest["result"], draft)

            base_resume_json = json.dumps(
                draft["resume"],
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            base_resume_checksum = hashlib.sha256(
                base_resume_json.encode("utf-8")
            ).hexdigest()
            completed, empty_revisions = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-revisions-empty",
                    "method": "tailoring.revisions.list",
                    "params": {"draft_id": draft["id"]},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                empty_revisions["result"],
                {
                    "draft_id": draft["id"],
                    "base_resume_checksum_sha256": base_resume_checksum,
                    "revisions": [],
                    "truncated": False,
                },
            )

            revision_request_id = str(uuid.uuid4())
            completed, created_revision = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-revision-create",
                    "method": "tailoring.revisions.create",
                    "params": {
                        "request_id": revision_request_id,
                        "draft_id": draft["id"],
                        "expected_parent_revision_id": None,
                        "expected_parent_resume_checksum_sha256": base_resume_checksum,
                        "edits": {
                            "basics": {"label": "User Revised Analytics Engineer"},
                            "work": [],
                        },
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            revision = created_revision["result"]
            self.assertEqual(revision["draft_id"], draft["id"])
            self.assertEqual(revision["author_kind"], "user")
            self.assertEqual(revision["changed_fields"], ["/basics/label"])
            self.assertEqual(
                revision["resume"]["basics"]["label"],
                "User Revised Analytics Engineer",
            )

            completed, fetched_revision = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-revision-get",
                    "method": "tailoring.revisions.get",
                    "params": {"revision_id": revision["id"]},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(fetched_revision["result"], revision)

            completed, listed_revisions = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-revisions-list",
                    "method": "tailoring.revisions.list",
                    "params": {"draft_id": draft["id"]},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            revision_summary = listed_revisions["result"]["revisions"][0]
            self.assertEqual(revision_summary["id"], revision["id"])
            self.assertEqual(revision_summary["changed_field_count"], 1)
            self.assertNotIn("resume", revision_summary)
            self.assertNotIn("changed_fields", revision_summary)

            completed, exported = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-export",
                    "method": "tailoring.export",
                    "params": {"draft_id": draft["id"], "format": "pdf"},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            export_result = exported["result"]
            self.assertEqual(
                set(export_result),
                {
                    "draft_id",
                    "opportunity_id",
                    "profile_version_id",
                    "artifact_id",
                    "format",
                    "media_type",
                    "relative_path",
                    "checksum_sha256",
                    "byte_size",
                    "suggested_filename",
                },
            )
            self.assertEqual(export_result["draft_id"], draft["id"])
            self.assertEqual(export_result["opportunity_id"], opportunity_id)
            self.assertTrue(
                Path(directory, export_result["relative_path"]).read_bytes().startswith(b"%PDF-")
            )

            completed, revision_exported = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-revision-export",
                    "method": "tailoring.revisions.export",
                    "params": {"revision_id": revision["id"], "format": "docx"},
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            revision_export_result = revision_exported["result"]
            self.assertEqual(
                set(revision_export_result),
                {
                    "revision_id",
                    "draft_id",
                    "opportunity_id",
                    "profile_version_id",
                    "artifact_id",
                    "format",
                    "media_type",
                    "relative_path",
                    "checksum_sha256",
                    "byte_size",
                    "suggested_filename",
                },
            )
            self.assertEqual(revision_export_result["revision_id"], revision["id"])
            self.assertEqual(revision_export_result["draft_id"], draft["id"])
            self.assertIn(
                "Tailored-Resume-Revision-1.docx",
                revision_export_result["suggested_filename"],
            )

            completed, invalid_revision_get = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-revision-get-invalid",
                    "method": "tailoring.revisions.get",
                    "params": {"revision_id": revision["id"], "extra": True},
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(invalid_revision_get["error"]["code"], "invalid_params")

            completed, invalid_export = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-export-invalid",
                    "method": "tailoring.export",
                    "params": {
                        "draft_id": draft["id"],
                        "format": "pdf",
                        "opportunity_id": opportunity_id,
                    },
                },
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(invalid_export["error"]["code"], "invalid_params")

            completed, second_prepare = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-prepare-to-fail",
                    "method": "tailoring.prepare",
                    "params": {
                        "opportunity_id": opportunity_id,
                        "provider_kind": "openai",
                        "chat_model_id": "gpt-5-mini",
                        "embedding_model_id": "text-embedding-3-small",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            completed, failed = self._one_shot(
                directory,
                {
                    "protocol_version": 1,
                    "id": "tailoring-fail",
                    "method": "tailoring.fail",
                    "params": {
                        "attempt_id": second_prepare["result"]["attempt_id"],
                        "error_code": "provider_timeout",
                    },
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                failed["result"],
                {
                    "attempt_id": second_prepare["result"]["attempt_id"],
                    "status": "failed",
                    "error_code": "provider_timeout",
                },
            )


if __name__ == "__main__":
    unittest.main()
