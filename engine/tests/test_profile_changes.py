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
from cvgnome_engine.profile_changes import (  # noqa: E402
    PROFILE_CHANGE_LIMIT,
    apply_profile_changes,
    preview_profile_changes,
)
from cvgnome_engine.storage import (  # noqa: E402
    DATABASE_FILENAME,
    MIGRATIONS,
    MIGRATION_APP_VERSIONS,
    MIGRATION_NAMES,
    SCHEMA_VERSION,
    VaultError,
    initialize_vault,
    latest_profile,
    save_profile,
)


def _profile() -> dict[str, object]:
    return {
        "basics": {"name": "Ada Lovelace", "private": {"keep": True}},
        "work": [
            {
                "name": "Analytical Engines",
                "position": "Engineer",
                "startDate": "1842",
                "endDate": "1843",
                "summary": "Built reliable systems.",
                "highlights": ["Published the first program."],
                "private_evidence": {"first": True},
            },
            {
                "name": "Analytical Engines",
                "position": "Engineer",
                "startDate": "1842",
                "endDate": "1843",
                "summary": "Built reliable systems.",
                "highlights": ["Explained the engine's wider possibilities."],
                "private_evidence": {"second": True},
            },
            {"name": "Remove Me", "position": "Old role"},
            {"name": "Keep Me", "position": "Writer", "summary": "Old summary"},
        ],
        "projects": [
            {
                "name": "Notes",
                "description": "Original description",
                "startDate": "1842",
                "endDate": "1843",
            },
            {
                "name": "Notes",
                "description": "Original description",
                "startDate": "1842",
                "endDate": "1843",
                "keywords": ["Mathematics"],
            },
        ],
        "education": [
            {
                "institution": "University of London",
                "studyType": "Certificate",
                "area": "Mathematics",
                "courses": ["Algebra"],
            },
            {
                "institution": "University of London",
                "studyType": "Certificate",
                "area": "Mathematics",
                "courses": ["Analysis"],
            },
        ],
        "skills": [
            {"name": "Computing", "level": "Advanced", "keywords": ["Algorithms"]},
            {"name": "Computing", "level": "Advanced", "keywords": ["Mathematics"]},
        ],
        "extension": {"keep": [1, 2]},
    }


class ProfileChangesTests(unittest.TestCase):
    def _request(
        self, data_dir: Path, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        return _handle_request(
            data_dir,
            {
                "protocol_version": 1,
                "id": str(uuid.uuid4()),
                "method": method,
                "params": params,
            },
        )

    def test_preview_is_bounded_ordered_conflict_free_and_renderer_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(data_dir, _profile(), source="test")

            preview = preview_profile_changes(data_dir, parent.id)

            self.assertEqual(
                set(preview),
                {
                    "profile_version_id",
                    "version_number",
                    "algorithm_version",
                    "suggestions",
                    "truncated",
                },
            )
            self.assertEqual(preview["profile_version_id"], parent.id)
            self.assertEqual(preview["algorithm_version"], 1)
            self.assertFalse(preview["truncated"])
            suggestions = preview["suggestions"]
            self.assertEqual(
                [item["section"] for item in suggestions],
                ["work", "projects", "education", "skills"],
            )
            self.assertTrue(
                all(item["keep_entry_index"] == 0 for item in suggestions)
            )
            self.assertTrue(
                all(item["remove_entry_index"] == 1 for item in suggestions)
            )
            self.assertEqual(
                suggestions[0]["reason_codes"],
                ["matching_employer_role_and_dates"],
            )
            self.assertEqual(
                suggestions[0]["merged_entry"]["highlights"],
                [
                    "Published the first program.",
                    "Explained the engine's wider possibilities.",
                ],
            )
            self.assertEqual(suggestions[0]["changed_fields"], ["highlights"])
            encoded = json.dumps(preview, ensure_ascii=False)
            self.assertNotIn("private_evidence", encoded)
            self.assertNotIn("extension", encoded)
            self.assertLessEqual(len(encoded.encode("utf-8")), 128 * 1024)

    def test_apply_uses_parent_indexes_and_creates_one_retry_safe_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(data_dir, _profile(), source="test")
            preview = preview_profile_changes(data_dir, parent.id)
            suggestions = {item["section"]: item for item in preview["suggestions"]}
            request_id = str(uuid.uuid4())
            operations = [
                {
                    "kind": "remove",
                    "section": "work",
                    "entry_index": 2,
                },
                {
                    "kind": "update",
                    "section": "work",
                    "entry_index": 3,
                    "patch": {"summary": "Updated summary"},
                },
                {
                    "kind": "merge",
                    "section": "work",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "suggestion_id": suggestions["work"]["suggestion_id"],
                },
                {
                    "kind": "merge",
                    "section": "skills",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "suggestion_id": suggestions["skills"]["suggestion_id"],
                },
            ]
            params = {
                "expected_parent_profile_version_id": parent.id,
                "request_id": request_id,
                "operations": operations,
            }

            first = apply_profile_changes(data_dir, params)
            retry = apply_profile_changes(data_dir, params)

            self.assertTrue(first["created"])
            self.assertFalse(retry["created"])
            self.assertEqual(first["profile_version_id"], retry["profile_version_id"])
            self.assertEqual(first["version_number"], parent.version_number + 1)
            self.assertEqual(first["operation_count"], 4)
            self.assertEqual(first["changed_sections"], ["work", "skills"])
            self.assertEqual(
                first["section_counts"],
                {"work": 2, "projects": 2, "education": 2, "skills": 1},
            )
            self.assertEqual(initialize_vault(data_dir).profile_versions, 2)
            current = latest_profile(data_dir)
            work = current["profile"]["work"]
            self.assertEqual(len(work), 2)
            self.assertEqual(work[0]["name"], "Analytical Engines")
            self.assertEqual(
                work[0]["private_evidence"], {"first": True, "second": True}
            )
            self.assertEqual(work[1]["name"], "Keep Me")
            self.assertEqual(work[1]["summary"], "Updated summary")
            self.assertEqual(
                current["profile"]["skills"][0]["keywords"],
                ["Algorithms", "Mathematics"],
            )
            self.assertEqual(current["profile"]["extension"], {"keep": [1, 2]})

            changed_params = {
                **params,
                "operations": [
                    {"kind": "remove", "section": "work", "entry_index": 2}
                ],
            }
            with self.assertRaises(VaultError) as reused:
                apply_profile_changes(data_dir, changed_params)
            self.assertEqual(reused.exception.code, "profile_changes_request_conflict")

            advanced = apply_profile_changes(
                data_dir,
                {
                    "expected_parent_profile_version_id": first["profile_version_id"],
                    "request_id": str(uuid.uuid4()),
                    "operations": [
                        {"kind": "remove", "section": "projects", "entry_index": 1}
                    ],
                },
            )
            self.assertEqual(advanced["version_number"], parent.version_number + 2)
            late_retry = apply_profile_changes(data_dir, params)
            self.assertFalse(late_retry["created"])
            self.assertEqual(late_retry["profile_version_id"], first["profile_version_id"])

            with self.assertRaises(VaultError) as stale:
                apply_profile_changes(
                    data_dir,
                    {
                        "expected_parent_profile_version_id": parent.id,
                        "request_id": str(uuid.uuid4()),
                        "operations": [
                            {"kind": "remove", "section": "projects", "entry_index": 0}
                        ],
                    },
                )
            self.assertEqual(stale.exception.code, "profile_changes_conflict")

    def test_conflicting_or_ambiguous_pairs_are_not_suggested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {
                            "name": "Example",
                            "position": "Engineer",
                            "startDate": "2020",
                            "endDate": "2021",
                            "summary": "First incompatible account",
                        },
                        {
                            "name": "Example",
                            "position": "Engineer",
                            "startDate": "2020",
                            "endDate": "2021",
                            "summary": "Second incompatible account",
                        },
                        {
                            "name": "Example",
                            "position": "Director",
                            "startDate": "2022",
                            "endDate": "2024",
                        },
                    ],
                    "education": [
                        {
                            "institution": "Example University",
                            "studyType": "BA",
                            "area": "Math",
                        },
                        {
                            "institution": "Example University",
                            "studyType": "PhD",
                            "area": "Physics",
                        },
                    ],
                    "projects": [
                        {
                            "name": "Website redesign",
                            "description": "Built for the first client.",
                        },
                        {
                            "name": "Website redesign",
                            "keywords": ["Second client"],
                        },
                    ],
                },
                source="test",
            )

            preview = preview_profile_changes(data_dir, parent.id)

            self.assertEqual(preview["suggestions"], [])

    def test_preview_caps_suggestions_and_comparisons_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            suggestion_parent = save_profile(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "skills": [
                        {
                            "name": f"Group {group}",
                            "keywords": [f"Skill {copy}"],
                        }
                        for group in range(PROFILE_CHANGE_LIMIT + 1)
                        for copy in range(2)
                    ],
                },
                source="test",
            )
            capped = preview_profile_changes(data_dir, suggestion_parent.id)
            self.assertTrue(capped["truncated"])
            self.assertEqual(len(capped["suggestions"]), PROFILE_CHANGE_LIMIT)
            self.assertEqual(
                [item["keep_entry_index"] for item in capped["suggestions"]],
                list(range(0, PROFILE_CHANGE_LIMIT * 2, 2)),
            )

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            comparison_parent = save_profile(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "projects": [
                        {"name": f"Unique project {index}"}
                        for index in range(201)
                    ],
                },
                source="test",
            )
            capped = preview_profile_changes(data_dir, comparison_parent.id)
            self.assertTrue(capped["truncated"])
            self.assertEqual(capped["suggestions"], [])

    def test_rejects_overlaps_bad_suggestions_limits_and_wrong_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(data_dir, _profile(), source="test")
            suggestion = preview_profile_changes(data_dir, parent.id)["suggestions"][0]
            invalid_operations = (
                [],
                [
                    {"kind": "remove", "section": "work", "entry_index": 0},
                    {
                        "kind": "update",
                        "section": "work",
                        "entry_index": 0,
                        "patch": {"summary": "X"},
                    },
                ],
                [
                    {
                        "kind": "merge",
                        "section": "work",
                        "keep_entry_index": 0,
                        "remove_entry_index": 1,
                        "suggestion_id": "0" * 64,
                    }
                ],
                [
                    {
                        "kind": "merge",
                        "section": "work",
                        "keep_entry_index": 1,
                        "remove_entry_index": 0,
                        "suggestion_id": suggestion["suggestion_id"],
                    }
                ],
                [
                    {"kind": "remove", "section": "basics", "entry_index": 0}
                ],
                [
                    {
                        "kind": "update",
                        "section": "projects",
                        "entry_index": 0,
                        "patch": {"unsupported": "X"},
                    }
                ],
                [
                    {"kind": "remove", "section": "projects", "entry_index": 0}
                ]
                * (PROFILE_CHANGE_LIMIT + 1),
            )
            for operations in invalid_operations:
                with self.subTest(operations=operations), self.assertRaises(ValueError):
                    apply_profile_changes(
                        data_dir,
                        {
                            "expected_parent_profile_version_id": parent.id,
                            "request_id": str(uuid.uuid4()),
                            "operations": operations,
                        },
                    )
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)

    def test_late_invalid_operation_rolls_back_the_entire_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(data_dir, _profile(), source="test")

            with self.assertRaises(ValueError):
                apply_profile_changes(
                    data_dir,
                    {
                        "expected_parent_profile_version_id": parent.id,
                        "request_id": str(uuid.uuid4()),
                        "operations": [
                            {
                                "kind": "update",
                                "section": "work",
                                "entry_index": 3,
                                "patch": {"summary": "This must roll back"},
                            },
                            {
                                "kind": "remove",
                                "section": "education",
                                "entry_index": 99,
                            },
                        ],
                    },
                )

            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)
            self.assertEqual(
                latest_profile(data_dir)["profile"]["work"][3]["summary"],
                "Old summary",
            )

    def test_concurrent_exact_retry_has_one_output_and_receipt_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(data_dir, _profile(), source="test")
            params = {
                "expected_parent_profile_version_id": parent.id,
                "request_id": str(uuid.uuid4()),
                "operations": [
                    {"kind": "remove", "section": "projects", "entry_index": 1}
                ],
            }
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(
                    executor.map(
                        lambda _index: apply_profile_changes(data_dir, params), range(4)
                    )
                )
            self.assertEqual(sum(bool(result["created"]) for result in results), 1)
            self.assertEqual(len({result["profile_version_id"] for result in results}), 1)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 2)
            self.assertEqual(SCHEMA_VERSION, 19)

            with closing(sqlite3.connect(data_dir / DATABASE_FILENAME)) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE profile_change_receipts SET algorithm_version = 2"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM profile_change_receipts")

    def test_rpc_surface_is_strict_and_uses_stable_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = save_profile(data_dir, _profile(), source="test")
            preview = self._request(
                data_dir,
                "profile.changes.preview",
                {"profile_version_id": parent.id},
            )
            self.assertTrue(preview["ok"])
            bad_preview = self._request(
                data_dir,
                "profile.changes.preview",
                {"profile_version_id": parent.id, "extra": True},
            )
            self.assertFalse(bad_preview["ok"])
            self.assertEqual(bad_preview["error"]["code"], "invalid_params")
            bad_apply = self._request(
                data_dir,
                "profile.changes.apply",
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(uuid.uuid4()),
                    "operations": [],
                },
            )
            self.assertFalse(bad_apply["ok"])
            self.assertEqual(bad_apply["error"]["code"], "invalid_params")

    def test_schema_thirteen_migration_preserves_profile_and_adds_empty_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / DATABASE_FILENAME
            profile_id = str(uuid.uuid4())
            canonical_json = '{"basics":{"name":"Ada"},"projects":[{"name":"Notes"}]}'
            checksum = hashlib.sha256(canonical_json.encode()).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 14):
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
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-v13', 1)
                    """,
                    (profile_id, canonical_json, checksum),
                )
                connection.execute("PRAGMA user_version = 13")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(latest_profile(data_dir)["id"], profile_id)
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions"
                ).fetchone()
                receipt_count = connection.execute(
                    "SELECT count(*) FROM profile_change_receipts"
                ).fetchone()[0]
                migration = connection.execute(
                    """
                    SELECT name, checksum_sha256, app_version
                    FROM schema_migrations WHERE version = 14
                    """
                ).fetchone()
            self.assertEqual(stored, (canonical_json, checksum))
            self.assertEqual(receipt_count, 0)
            self.assertEqual(
                migration,
                (
                    MIGRATION_NAMES[14],
                    hashlib.sha256(MIGRATIONS[14].encode()).hexdigest(),
                    MIGRATION_APP_VERSIONS[14],
                ),
            )


if __name__ == "__main__":
    unittest.main()
