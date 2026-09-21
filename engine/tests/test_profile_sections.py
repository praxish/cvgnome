# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
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
from cvgnome_engine.profile_sections import (  # noqa: E402
    list_profile_education,
    list_profile_projects,
    list_profile_skills,
    update_profile_education,
    update_profile_projects,
    update_profile_skills,
)
from cvgnome_engine.profile_versions import update_profile_basics  # noqa: E402
from cvgnome_engine.profile_work import update_profile_work  # noqa: E402
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


SECTION_CASES = {
    "projects": {
        "list": list_profile_projects,
        "update": update_profile_projects,
        "fields": (
            "name",
            "description",
            "url",
            "start_date",
            "end_date",
            "highlights",
            "keywords",
        ),
        "canonical": {
            "name": "Legacy Project",
            "description": "Old description",
            "url": "https://example.com/project",
            "startDate": "2020",
            "endDate": "2021",
            "highlights": ["One"],
            "keywords": ["Python"],
            "private_evidence": {"source": True},
        },
        "entry": {
            "name": "Analytical Engine",
            "description": "Designed a machine.",
            "url": "https://example.com/engine",
            "start_date": "1842",
            "end_date": "1843",
            "highlights": ["Published notes"],
            "keywords": ["Mathematics"],
        },
        "patch": {
            "description": "New\r\nDescription",
            "url": None,
            "highlights": [],
            "keywords": None,
        },
        "changed": ["description", "url", "highlights", "keywords"],
    },
    "education": {
        "list": list_profile_education,
        "update": update_profile_education,
        "fields": (
            "institution",
            "study_type",
            "area",
            "url",
            "start_date",
            "end_date",
            "score",
            "courses",
        ),
        "canonical": {
            "institution": "Legacy College",
            "studyType": "BSc",
            "area": "Mathematics",
            "url": "https://example.edu/program",
            "startDate": "1830",
            "endDate": "1834",
            "score": "First",
            "courses": ["Algebra"],
            "private_evidence": {"source": True},
        },
        "entry": {
            "institution": "University of London",
            "study_type": "Certificate",
            "area": None,
            "url": "https://example.edu/course",
            "start_date": "1835",
            "end_date": "1836",
            "score": None,
            "courses": ["Advanced mathematics"],
        },
        "patch": {
            "institution": " New College ",
            "study_type": None,
            "url": None,
            "courses": [],
        },
        "changed": ["institution", "study_type", "url", "courses"],
    },
    "skills": {
        "list": list_profile_skills,
        "update": update_profile_skills,
        "fields": ("name", "level", "keywords"),
        "canonical": {
            "name": "Engineering",
            "level": "Advanced",
            "keywords": ["Python", "SQLite"],
            "private_evidence": {"source": True},
        },
        "entry": {
            "name": "Analysis",
            "level": "Expert",
            "keywords": ["Statistics", "Research"],
        },
        "patch": {
            "name": " Technical Engineering ",
            "level": None,
            "keywords": ["Python", "Systems"],
        },
        "changed": ["name", "level", "keywords"],
    },
}

REQUIRED_FIELD_LIMITS = {"projects": 200, "education": 240, "skills": 120}


class ProfileSectionTests(unittest.TestCase):
    def _save(self, data_dir: Path, profile: dict[str, object], *, source: str = "test"):
        return save_profile(data_dir, profile, source=source)

    def _update(
        self,
        section: str,
        data_dir: Path,
        parent_id: str,
        operation: object,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return SECTION_CASES[section]["update"](
            data_dir,
            {
                "expected_parent_profile_version_id": parent_id,
                "request_id": request_id or str(uuid.uuid4()),
                "operation": operation,
            },
        )

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

    def test_lists_are_allowlisted_and_use_raw_canonical_indexes(self) -> None:
        for section, case in SECTION_CASES.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                canonical = deepcopy(case["canonical"])
                if "url" in canonical:
                    canonical["url"] = "http://127.0.0.1/private"
                canonical[next(iter(canonical))] = "X" * 5_000
                parent = self._save(
                    data_dir,
                    {
                        "basics": {"name": "Ada"},
                        section: ["malformed", canonical, 91],
                        "meta": {"source_path": "/private/resume.json"},
                    },
                )

                listed = case["list"](data_dir, parent.id, 0)

                self.assertEqual(
                    set(listed),
                    {
                        "profile_version_id",
                        "version_number",
                        "section",
                        "total_items",
                        "offset",
                        "limit",
                        "next_offset",
                        "items",
                    },
                )
                self.assertEqual(listed["profile_version_id"], parent.id)
                self.assertEqual(listed["section"], section)
                self.assertEqual(listed["total_items"], 1)
                self.assertEqual(listed["items"][0]["entry_index"], 1)
                required_field = case["fields"][0]
                self.assertEqual(
                    len(listed["items"][0][required_field]),
                    REQUIRED_FIELD_LIMITS[section],
                )
                self.assertEqual(
                    set(listed["items"][0]), {"entry_index", *case["fields"]}
                )
                if "url" in case["fields"]:
                    self.assertIsNone(listed["items"][0]["url"])
                encoded = json.dumps(listed)
                self.assertNotIn("private_evidence", encoded)
                self.assertNotIn("source_path", encoded)
                for invalid in (-1, 10_001, True, "0", None):
                    with self.assertRaises(ValueError):
                        case["list"](data_dir, parent.id, invalid)

    def test_lists_tolerate_missing_and_malformed_sections(self) -> None:
        for section, case in SECTION_CASES.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                missing = self._save(data_dir, {"basics": {"name": "Ada"}})
                malformed = self._save(
                    data_dir,
                    {"basics": {"name": "Ada"}, section: {"secret": "hidden"}},
                )
                self.assertEqual(case["list"](data_dir, missing.id, 0)["items"], [])
                result = case["list"](data_dir, malformed.id, 0)
                self.assertEqual(result["total_items"], 0)
                self.assertNotIn("secret", json.dumps(result))

    def test_oversized_raw_section_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {"basics": {"name": "Ada"}, "projects": [None] * 5_001},
            )

            with self.assertRaises(VaultError) as listed:
                list_profile_projects(data_dir, parent.id, 0)
            self.assertEqual(listed.exception.code, "vault_integrity_error")
            with self.assertRaises(ValueError):
                self._update(
                    "projects",
                    data_dir,
                    parent.id,
                    {
                        "kind": "add",
                        "entry": deepcopy(SECTION_CASES["projects"]["entry"]),
                    },
                )

    def test_incomplete_legacy_skill_is_listable_and_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "skills": [{"name": "Legacy", "private": "keep"}],
                },
            )
            listed = list_profile_skills(data_dir, parent.id, 0)
            self.assertEqual(listed["items"][0]["keywords"], [])

            repaired = self._update(
                "skills",
                data_dir,
                parent.id,
                {
                    "kind": "update",
                    "entry_index": 0,
                    "patch": {"keywords": ["Python"]},
                },
            )

            self.assertEqual(repaired["changed_fields"], ["keywords"])
            self.assertEqual(
                latest_profile(data_dir)["profile"]["skills"],
                [{"name": "Legacy", "keywords": ["Python"], "private": "keep"}],
            )

    def test_sparse_updates_adds_and_removes_preserve_unexposed_data(self) -> None:
        receipt_keys = {
            "request_id",
            "profile_version_id",
            "parent_profile_version_id",
            "version_number",
            "created_at_ms",
            "created",
            "section",
            "operation",
            "entry_index",
            "changed_fields",
            "profile_name",
            "section_entries",
            "renderable",
        }
        for section, case in SECTION_CASES.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                original = {
                    "basics": {"name": "Ada", "private": {"keep": True}},
                    section: [deepcopy(case["canonical"]), "malformed-neighbor"],
                    "extension": {"keep": [1, 2]},
                }
                parent = self._save(data_dir, original)
                updated = self._update(
                    section,
                    data_dir,
                    parent.id,
                    {"kind": "update", "entry_index": 0, "patch": case["patch"]},
                )
                self.assertEqual(set(updated), receipt_keys)
                self.assertEqual(updated["section"], section)
                self.assertEqual(updated["operation"], "update")
                self.assertEqual(updated["entry_index"], 0)
                self.assertEqual(updated["changed_fields"], case["changed"])
                self.assertEqual(updated["section_entries"], 1)
                self.assertEqual(updated["parent_profile_version_id"], parent.id)
                stored_version = latest_profile(data_dir)
                self.assertEqual(stored_version["source"], "local_edit")
                stored = stored_version["profile"]
                self.assertEqual(stored[section][0]["private_evidence"], {"source": True})
                self.assertEqual(stored[section][1], "malformed-neighbor")
                self.assertEqual(stored["basics"], original["basics"])
                self.assertEqual(stored["extension"], original["extension"])
                if section == "education":
                    self.assertIs(stored["meta"]["education_curated"], True)

                request_id = str(uuid.uuid4())
                raw_entry = deepcopy(case["entry"])
                required_field = case["fields"][0]
                raw_entry[required_field] = f" {raw_entry[required_field]} "
                add_operation = {"kind": "add", "entry": raw_entry}
                created = self._update(
                    section,
                    data_dir,
                    updated["profile_version_id"],
                    {"kind": "add", "entry": deepcopy(case["entry"])},
                    request_id=request_id,
                )
                replayed = self._update(
                    section,
                    data_dir,
                    updated["profile_version_id"],
                    add_operation,
                    request_id=request_id,
                )
                self.assertTrue(created["created"])
                self.assertFalse(replayed["created"])
                self.assertEqual(created["profile_version_id"], replayed["profile_version_id"])
                self.assertEqual(created["entry_index"], 2)
                self.assertEqual(created["section_entries"], 2)
                self.assertEqual(
                    latest_profile(data_dir)["profile"][section][1],
                    "malformed-neighbor",
                )

                removed = self._update(
                    section,
                    data_dir,
                    created["profile_version_id"],
                    {"kind": "remove", "entry_index": 0},
                )
                self.assertEqual(removed["operation"], "remove")
                self.assertEqual(removed["entry_index"], 0)
                self.assertEqual(removed["section_entries"], 1)
                self.assertEqual(
                    latest_profile(data_dir)["profile"][section][0],
                    "malformed-neighbor",
                )

    def test_exact_retry_concurrency_and_request_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {"basics": {"name": "Ada"}, "projects": [], "education": []},
            )
            request_id = str(uuid.uuid4())
            params = {
                "expected_parent_profile_version_id": parent.id,
                "request_id": request_id,
                "operation": {
                    "kind": "add",
                    "entry": deepcopy(SECTION_CASES["projects"]["entry"]),
                },
            }
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(
                    executor.map(
                        lambda _index: update_profile_projects(data_dir, params),
                        range(4),
                    )
                )
            self.assertEqual(sum(bool(result["created"]) for result in results), 1)
            self.assertEqual(len({result["profile_version_id"] for result in results}), 1)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 2)

            with self.assertRaises(VaultError) as reused:
                update_profile_education(
                    data_dir,
                    {
                        "expected_parent_profile_version_id": parent.id,
                        "request_id": request_id,
                        "operation": {
                            "kind": "add",
                            "entry": deepcopy(SECTION_CASES["education"]["entry"]),
                        },
                    },
                )
            self.assertEqual(reused.exception.code, "profile_update_request_conflict")

            with self.assertRaises(VaultError) as stale:
                self._update(
                    "projects",
                    data_dir,
                    parent.id,
                    {"kind": "add", "entry": deepcopy(SECTION_CASES["projects"]["entry"])},
                )
            self.assertEqual(stale.exception.code, "profile_update_conflict")

    def test_all_section_validation_limits_and_strict_operation_shapes(self) -> None:
        invalid_entries: dict[str, list[dict[str, object]]] = {}
        project = SECTION_CASES["projects"]["entry"]
        invalid_entries["projects"] = [
            {**project, "name": " "},
            {**project, "name": "bad\u0000name"},
            {**project, "name": "x" * 201},
            {**project, "description": "x" * 601},
            {**project, "url": "http://127.0.0.1/private"},
            {**project, "url": "https://example.com/" + "x" * 2_048},
            {**project, "start_date": "x" * 81},
            {**project, "end_date": "x" * 81},
            {**project, "highlights": ["x"] * 9},
            {**project, "highlights": ["x" * 361]},
            {**project, "highlights": ["x" * 360] * 6},
            {**project, "keywords": ["x"] * 17},
            {**project, "keywords": ["x" * 101]},
            {**project, "keywords": ["x" * 100] * 13},
            {**project, "keywords": [1]},
        ]
        education = SECTION_CASES["education"]["entry"]
        invalid_entries["education"] = [
            {**education, "institution": " "},
            {**education, "institution": "x" * 241},
            {**education, "study_type": None, "area": None},
            {**education, "study_type": "x" * 161},
            {**education, "area": "x" * 201},
            {**education, "url": "https://localhost/private"},
            {**education, "url": "https://example.edu/" + "x" * 2_048},
            {**education, "start_date": "x" * 81},
            {**education, "end_date": "x" * 81},
            {**education, "score": "x" * 81},
            {**education, "courses": ["x"] * 13},
            {**education, "courses": ["x" * 161]},
            {**education, "courses": ["x" * 160] * 8},
        ]
        skill = SECTION_CASES["skills"]["entry"]
        invalid_entries["skills"] = [
            {**skill, "name": " "},
            {**skill, "name": "x" * 121},
            {**skill, "level": "x" * 81},
            {**skill, "keywords": []},
            {**skill, "keywords": None},
            {**skill, "keywords": ["x"] * 19},
            {**skill, "keywords": ["x" * 81]},
            {**skill, "keywords": ["x" * 80] * 16},
        ]

        for section, entries in invalid_entries.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                parent = self._save(data_dir, {"basics": {"name": "Ada"}, section: []})
                for index, entry in enumerate(entries):
                    with self.subTest(
                        section=section, invalid=index
                    ), self.assertRaises(ValueError):
                        self._update(
                            section,
                            data_dir,
                            parent.id,
                            {"kind": "add", "entry": entry},
                        )
                missing = deepcopy(SECTION_CASES[section]["entry"])
                missing.pop(next(iter(missing)))
                with self.assertRaises(ValueError):
                    self._update(section, data_dir, parent.id, {"kind": "add", "entry": missing})
                extra = {**SECTION_CASES[section]["entry"], "secret": True}
                with self.assertRaises(ValueError):
                    self._update(section, data_dir, parent.id, {"kind": "add", "entry": extra})

                malformed_operations = (
                    None,
                    {},
                    {"kind": "unknown"},
                    {"kind": "remove", "entry_index": True},
                    {"kind": "remove", "entry_index": 0, "extra": True},
                    {"kind": "update", "entry_index": 0, "patch": {}},
                )
                for operation in malformed_operations:
                    with self.assertRaises(ValueError):
                        self._update(section, data_dir, parent.id, operation)

        patch_cases = (
            ("projects", {"name": None}),
            ("projects", {"name": "Legacy Project"}),
            ("education", {"study_type": None, "area": None}),
            ("skills", {"keywords": None}),
            ("skills", {"keywords": []}),
            ("skills", {"unsupported": "value"}),
        )
        for section, patch in patch_cases:
            with self.subTest(
                section=section, patch=patch
            ), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                parent = self._save(
                    data_dir,
                    {
                        "basics": {"name": "Ada"},
                        section: [deepcopy(SECTION_CASES[section]["canonical"])],
                    },
                )
                with self.assertRaises(ValueError):
                    self._update(
                        section,
                        data_dir,
                        parent.id,
                        {"kind": "update", "entry_index": 0, "patch": patch},
                    )

    def test_rpc_methods_require_exact_params_and_round_trip(self) -> None:
        for section, case in SECTION_CASES.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                parent = self._save(data_dir, {"basics": {"name": "Ada"}, section: []})
                listed = self._request(
                    data_dir,
                    f"profile.{section}.list",
                    {"profile_version_id": parent.id, "offset": 0},
                )
                updated = self._request(
                    data_dir,
                    f"profile.{section}.update",
                    {
                        "expected_parent_profile_version_id": parent.id,
                        "request_id": str(uuid.uuid4()),
                        "operation": {"kind": "add", "entry": deepcopy(case["entry"])},
                    },
                )
                self.assertTrue(listed["ok"])
                self.assertEqual(listed["result"]["items"], [])
                self.assertTrue(updated["ok"])
                self.assertEqual(updated["result"]["section"], section)
                invalid = (
                    (f"profile.{section}.list", {}),
                    (
                        f"profile.{section}.list",
                        {"profile_version_id": parent.id, "offset": 0, "extra": True},
                    ),
                    (
                        f"profile.{section}.update",
                        {
                            "expected_parent_profile_version_id": parent.id,
                            "request_id": str(uuid.uuid4()),
                            "operation": {"kind": "add", "entry": deepcopy(case["entry"])},
                            "extra": True,
                        },
                    ),
                )
                for method, params in invalid:
                    response = self._request(data_dir, method, params)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], "invalid_params")

    def test_receipts_are_immutable_and_tampering_is_detected_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}, "projects": []})
            request_id = str(uuid.uuid4())
            operation = {"kind": "add", "entry": deepcopy(SECTION_CASES["projects"]["entry"])}
            result = self._update(
                "projects", data_dir, parent.id, operation, request_id=request_id
            )
            database = Path(initialize_vault(data_dir).database_path)
            with closing(sqlite3.connect(database)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_section_update_receipts "
                        "SET created_at_ms = created_at_ms + 1"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_section_update_receipts")
                connection.rollback()
                connection.execute("DROP TRIGGER profile_section_update_receipts_no_update")
                connection.execute(
                    "UPDATE profile_section_update_receipts SET changed_fields_json = '[\"name\"]'"
                )
                connection.commit()

            with self.assertRaises(VaultError) as corrupted:
                self._update(
                    "projects", data_dir, parent.id, operation, request_id=request_id
                )
            self.assertEqual(corrupted.exception.code, "vault_integrity_error")
            self.assertEqual(result["profile_version_id"], latest_profile(data_dir)["id"])

    def test_receipt_trigger_rejects_invalid_fields_order_and_add_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = self._save(data_dir, {"basics": {"name": "Ada"}, "projects": []})
            parent = self._update(
                "projects",
                data_dir,
                first.id,
                {"kind": "add", "entry": deepcopy(SECTION_CASES["projects"]["entry"])},
            )
            next_profile = deepcopy(latest_profile(data_dir)["profile"])
            next_profile["basics"]["headline"] = "Latest"
            output = self._save(data_dir, next_profile, source="local_edit")
            latest = latest_profile(data_dir)
            database = Path(initialize_vault(data_dir).database_path)
            insert_sql = """
                INSERT INTO profile_section_update_receipts(
                    request_id, request_fingerprint, section,
                    output_profile_version_id, parent_profile_version_id,
                    operation, entry_index, changed_fields_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            invalid_rows = (
                ("projects", "add", '["description","name"]'),
                ("projects", "add", '["description"]'),
                ("education", "add", '["institution"]'),
                ("skills", "add", '["name"]'),
                ("skills", "update", '["private"]'),
                ("skills", "update", '["name","name"]'),
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for section, operation, changed_fields in invalid_rows:
                    with self.subTest(section=section, fields=changed_fields):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                insert_sql,
                                (
                                    str(uuid.uuid4()),
                                    "a" * 64,
                                    section,
                                    output.id,
                                    parent["profile_version_id"],
                                    operation,
                                    0,
                                    changed_fields,
                                    latest["created_at_ms"],
                                ),
                            )
                        connection.rollback()

    def test_receipt_namespace_rejects_basic_and_work_claimed_outputs(self) -> None:
        claimed_updates = (
            (
                "basics",
                lambda data_dir, parent_id: update_profile_basics(
                    data_dir,
                    {
                        "expected_parent_profile_version_id": parent_id,
                        "request_id": str(uuid.uuid4()),
                        "patch": {"headline": "Engineer"},
                    },
                ),
            ),
            (
                "work",
                lambda data_dir, parent_id: update_profile_work(
                    data_dir,
                    {
                        "expected_parent_profile_version_id": parent_id,
                        "request_id": str(uuid.uuid4()),
                        "operation": {
                            "kind": "add",
                            "entry": {
                                "name": "Analytical Engines",
                                "position": "Engineer",
                                "url": None,
                                "start_date": None,
                                "end_date": None,
                                "summary": None,
                                "highlights": [],
                                "location": None,
                            },
                        },
                    },
                ),
            ),
        )
        insert_sql = """
            INSERT INTO profile_section_update_receipts(
                request_id, request_fingerprint, section,
                output_profile_version_id, parent_profile_version_id,
                operation, entry_index, changed_fields_json, created_at_ms
            ) VALUES (?, ?, 'projects', ?, ?, 'update', 0, '["name"]', ?)
        """
        for receipt_kind, create_claimed_output in claimed_updates:
            with self.subTest(
                receipt_kind=receipt_kind
            ), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                parent = self._save(
                    data_dir,
                    {
                        "basics": {"name": "Ada"},
                        "work": [],
                        "projects": [{"name": "Engine"}],
                    },
                )
                claimed = create_claimed_output(data_dir, parent.id)
                database = Path(initialize_vault(data_dir).database_path)
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "lineage is invalid"):
                        connection.execute(
                            insert_sql,
                            (
                                str(uuid.uuid4()),
                                "a" * 64,
                                claimed["profile_version_id"],
                                parent.id,
                                claimed["created_at_ms"],
                            ),
                        )

    def test_legacy_receipt_guards_reject_section_outputs_and_detect_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}, "projects": []})
            request_id = str(uuid.uuid4())
            operation = {"kind": "add", "entry": deepcopy(SECTION_CASES["projects"]["entry"])}
            result = self._update(
                "projects", data_dir, parent.id, operation, request_id=request_id
            )
            database = Path(initialize_vault(data_dir).database_path)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                common = (
                    str(uuid.uuid4()),
                    "b" * 64,
                    result["profile_version_id"],
                    parent.id,
                    result["created_at_ms"],
                )
                competing_inserts = (
                    """
                    INSERT INTO profile_basic_update_receipts(
                        request_id, request_fingerprint, output_profile_version_id,
                        parent_profile_version_id, changed_fields_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, '["summary"]', ?)
                    """,
                    """
                    INSERT INTO profile_work_update_receipts(
                        request_id, request_fingerprint, output_profile_version_id,
                        parent_profile_version_id, operation, entry_index,
                        changed_fields_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, 'add', 0, '["name","position"]', ?)
                    """,
                    """
                    INSERT INTO profile_restore_receipts(
                        request_id, request_fingerprint, output_profile_version_id,
                        parent_profile_version_id, source_profile_version_id,
                        created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                )
                for index, insert_sql in enumerate(competing_inserts):
                    values = (
                        (*common[:4], parent.id, common[4])
                        if index == 2
                        else (str(uuid.uuid4()), *common[1:])
                    )
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError,
                        "already has a section update receipt",
                    ):
                        connection.execute(insert_sql, values)
                    connection.rollback()

                connection.execute(
                    "DROP TRIGGER profile_basic_update_receipts_reject_section_output"
                )
                connection.execute(competing_inserts[0], common)
                connection.commit()

            with self.assertRaises(VaultError) as corrupted:
                self._update(
                    "projects", data_dir, parent.id, operation, request_id=request_id
                )
            self.assertEqual(corrupted.exception.code, "vault_integrity_error")

    def test_schema_twelve_migration_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            profile_id = str(uuid.uuid4())
            canonical_json = '{"basics":{"name":"Ada"},"skills":["legacy"]}'
            checksum = hashlib.sha256(canonical_json.encode()).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 13):
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
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-v12', 1)
                    """,
                    (profile_id, canonical_json, checksum),
                )
                connection.execute("PRAGMA user_version = 12")
                connection.commit()

            status = initialize_vault(data_dir)

            self.assertEqual(status.schema_version, SCHEMA_VERSION)
            self.assertEqual(latest_profile(data_dir)["id"], profile_id)
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    "SELECT canonical_json, checksum_sha256 FROM profile_versions"
                ).fetchone()
                receipt_count = connection.execute(
                    "SELECT count(*) FROM profile_section_update_receipts"
                ).fetchone()[0]
                migration = connection.execute(
                    """
                    SELECT name, checksum_sha256, app_version
                    FROM schema_migrations WHERE version = 13
                    """
                ).fetchone()
            self.assertEqual(stored, (canonical_json, checksum))
            self.assertEqual(receipt_count, 0)
            self.assertEqual(
                migration,
                (
                    MIGRATION_NAMES[13],
                    hashlib.sha256(MIGRATIONS[13].encode()).hexdigest(),
                    MIGRATION_APP_VERSIONS[13],
                ),
            )


if __name__ == "__main__":
    unittest.main()
