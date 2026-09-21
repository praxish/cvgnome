# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.retained_sources import list_retained_sources  # noqa: E402
from cvgnome_engine.storage import (  # noqa: E402
    commit_profile_source_scan,
    create_profile_source_scan,
    import_canonical_profile_source,
    latest_profile,
    reset_profile_source_retention,
)


class RetainedSourcesTests(unittest.TestCase):
    def _stage_canonical(
        self,
        data_dir: Path,
        *,
        scan_id: str,
        display_name: str,
        content: bytes,
    ) -> dict[str, object]:
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
        *,
        scan_id: str,
        ordinal: int,
        display_name: str,
        source_format: str,
        content: bytes,
        failed: bool = False,
    ) -> dict[str, Any]:
        staged = (
            data_dir
            / "imports"
            / "staging"
            / scan_id
            / f"{ordinal:04d}.{source_format}"
        )
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        extracted_text = "" if failed else content.decode("utf-8")
        item: dict[str, Any] = {
            "ordinal": ordinal,
            "managed_relative_path": staged.relative_to(data_dir).as_posix(),
            "display_name": display_name,
            "format": source_format,
            "media_type": "text/plain",
            "source_kind": "evidence" if ordinal else "resume",
            "byte_size": len(content),
            "checksum_sha256": hashlib.sha256(content).hexdigest(),
            "parser_contract": "plain-text-v1",
            "extraction_status": "failed" if failed else "parsed",
            "extracted_text": extracted_text,
            "candidate_profile": None if failed else {"basics": {"name": "Ada"}},
            "warnings": [],
        }
        if failed:
            item["issue_code"] = "source.parse_failed"
        else:
            item["extracted_text_sha256"] = hashlib.sha256(
                extracted_text.encode("utf-8")
            ).hexdigest()
        return item

    def test_empty_list_and_params_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            result = list_retained_sources(
                data_dir,
                {"scope": "all", "limit": 25, "offset": 0},
            )
            self.assertEqual(
                result,
                {
                    "scope": "all",
                    "current_generation": 1,
                    "offset": 0,
                    "limit": 25,
                    "total_entries": 0,
                    "total_unique_files": 0,
                    "total_imports": 0,
                    "items": [],
                },
            )

            invalid_params = (
                {},
                {"scope": "all", "limit": 25},
                {"scope": "all", "limit": 25, "offset": 0, "path": True},
                {"scope": "deleted", "limit": 25, "offset": 0},
                {"scope": "all", "limit": True, "offset": 0},
                {"scope": "all", "limit": 0, "offset": 0},
                {"scope": "all", "limit": 26, "offset": 0},
                {"scope": "all", "limit": 25, "offset": False},
                {"scope": "all", "limit": 25, "offset": 100_001},
            )
            for params in invalid_params:
                with self.subTest(params=params):
                    with self.assertRaises(ValueError):
                        list_retained_sources(data_dir, params)

    def test_lists_alias_events_generations_counts_and_safe_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            canonical = json.dumps(
                {
                    "basics": {
                        "name": "Ada Lovelace",
                        "summary": "Local Python systems engineer.",
                    }
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            canonical_hash = hashlib.sha256(canonical).hexdigest()
            multibyte_alias = f"{'📄' * 235}.json"
            self.assertEqual(len(multibyte_alias), 240)
            self.assertEqual(len(multibyte_alias.encode("utf-8")), 945)
            canonical_imports = (
                (
                    "00000000-0000-4000-8000-000000000001",
                    multibyte_alias,
                    1_000,
                ),
                (
                    "00000000-0000-4000-8000-000000000002",
                    "renamed/current-canonical.json",
                    1_100,
                ),
            )
            for scan_id, display_name, committed_at_ms in canonical_imports:
                with patch(
                    "cvgnome_engine.storage._utc_now_ms",
                    return_value=committed_at_ms,
                ):
                    import_canonical_profile_source(
                        data_dir,
                        self._stage_canonical(
                            data_dir,
                            scan_id=scan_id,
                            display_name=display_name,
                            content=canonical,
                        ),
                    )

            reset_profile_source_retention(
                data_dir,
                expected_generation=1,
                request_id=str(uuid.uuid4()),
            )
            base = latest_profile(data_dir)
            assert base is not None
            source_set_id = "00000000-0000-4000-8000-000000000003"
            secret_text = "PRIVATE EVIDENCE CONTENT MUST NOT LEAK"
            items = [
                self._stage_item(
                    data_dir,
                    scan_id=source_set_id,
                    ordinal=0,
                    display_name="materials/resume.txt",
                    source_format="txt",
                    content=b"Ada Lovelace\nPython systems engineer",
                ),
                self._stage_item(
                    data_dir,
                    scan_id=source_set_id,
                    ordinal=1,
                    display_name="materials/notes.md",
                    source_format="md",
                    content=secret_text.encode("utf-8"),
                    failed=True,
                ),
            ]
            with patch(
                "cvgnome_engine.storage._utc_now_ms",
                return_value=2_000,
            ):
                create_profile_source_scan(
                    data_dir,
                    scan_id=source_set_id,
                    base_profile_version_id=str(base["id"]),
                    base_profile_checksum_sha256=str(base["checksum_sha256"]),
                    items=items,
                    draft_profile={
                        "basics": {
                            "name": "Ada Lovelace",
                            "summary": "Local Python systems engineer.",
                        },
                        "skills": [{"name": "Systems", "keywords": ["SQLite"]}],
                    },
                    report={
                        "synthesis_contract": "deterministic-local-v1",
                        "conflicts": [],
                        "ui": {"can_build": True},
                    },
                    expires_at_ms=62_000,
                )
                commit_profile_source_scan(data_dir, source_set_id)

            active = list_retained_sources(
                data_dir,
                {"scope": "active", "limit": 25, "offset": 0},
            )
            self.assertEqual(active["current_generation"], 2)
            self.assertEqual(active["total_entries"], 2)
            self.assertEqual(active["total_unique_files"], 2)
            self.assertEqual(active["total_imports"], 1)
            self.assertEqual([item["ordinal"] for item in active["items"]], [0, 1])
            self.assertEqual(
                {item["import_mode"] for item in active["items"]},
                {"source_set"},
            )
            self.assertEqual(
                {item["import_file_count"] for item in active["items"]},
                {2},
            )
            self.assertEqual(
                [item["extraction_status"] for item in active["items"]],
                ["parsed", "failed"],
            )
            self.assertEqual(active["items"][1]["issue_code"], "source.parse_failed")

            historical = list_retained_sources(
                data_dir,
                {"scope": "historical", "limit": 25, "offset": 0},
            )
            self.assertEqual(historical["total_entries"], 2)
            self.assertEqual(historical["total_unique_files"], 1)
            self.assertEqual(historical["total_imports"], 2)
            self.assertEqual(
                [item["display_name"] for item in historical["items"]],
                ["current-canonical.json", multibyte_alias],
            )
            self.assertEqual(
                {item["content_reference_count"] for item in historical["items"]},
                {2},
            )
            self.assertEqual(
                {item["retention_status"] for item in historical["items"]},
                {"historical"},
            )

            all_materials = list_retained_sources(
                data_dir,
                {"scope": "all", "limit": 25, "offset": 0},
            )
            self.assertEqual(all_materials["total_entries"], 4)
            self.assertEqual(all_materials["total_unique_files"], 3)
            self.assertEqual(all_materials["total_imports"], 3)
            self.assertEqual(
                [item["retention_status"] for item in all_materials["items"]],
                ["active", "active", "historical", "historical"],
            )

            second_active = list_retained_sources(
                data_dir,
                {"scope": "active", "limit": 1, "offset": 1},
            )
            self.assertEqual(second_active["total_entries"], 2)
            self.assertEqual(len(second_active["items"]), 1)
            self.assertEqual(second_active["items"][0]["ordinal"], 1)

            serialized = json.dumps(all_materials, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(canonical_hash, serialized)
            self.assertNotIn(secret_text, serialized)
            self.assertNotIn("managed_relative_path", serialized)
            self.assertNotIn("extracted_text", serialized)
            self.assertLess(len(serialized.encode("utf-8")), 64 * 1024)


if __name__ == "__main__":
    unittest.main()
