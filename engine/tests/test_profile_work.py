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
from cvgnome_engine.profile_work import (  # noqa: E402
    WORK_HIGHLIGHT_LIMIT,
    WORK_LIST_RESPONSE_LIMIT_BYTES,
    list_profile_work,
    update_profile_work,
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


class ProfileWorkTests(unittest.TestCase):
    _EMPTY_ENTRY = {
        "name": "Analytical Engines",
        "position": "Engineer",
        "url": None,
        "start_date": None,
        "end_date": None,
        "summary": None,
        "highlights": [],
        "location": None,
    }

    def _save(
        self,
        data_dir: Path,
        profile: dict[str, object],
        *,
        source: str = "file_import:test",
    ):
        return save_profile(data_dir, profile, source=source)

    def _update(
        self,
        data_dir: Path,
        parent_id: str,
        operation: object,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return update_profile_work(
            data_dir,
            {
                "expected_parent_profile_version_id": parent_id,
                "request_id": request_id or str(uuid.uuid4()),
                "operation": operation,
            },
        )

    def _request(
        self,
        data_dir: Path,
        method: str,
        params: dict[str, object],
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

    def test_list_is_pinned_paged_allowlisted_and_uses_raw_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            work: list[object] = ["malformed"]
            for index in range(22):
                work.append(
                    {
                        "name": f"Employer {index}",
                        "position": f"Role {index}",
                        "url": (
                            "http://127.0.0.1/private"
                            if index == 0
                            else "https://example.com/work"
                        ),
                        "startDate": "2020-01",
                        "endDate": None,
                        "summary": "S" * 5_000,
                        "highlights": ["H" * 500] * 20,
                        "location": "London",
                        "private_notes": f"secret-{index}",
                    }
                )
                if index == 9:
                    work.append(91)
            saved = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": work,
                    "meta": {"source_path": "/private/source.pdf"},
                },
            )

            first = list_profile_work(data_dir, saved.id, 0)
            second = list_profile_work(data_dir, saved.id, 20)

            self.assertEqual(
                set(first),
                {
                    "profile_version_id",
                    "version_number",
                    "total_items",
                    "offset",
                    "limit",
                    "next_offset",
                    "items",
                },
            )
            self.assertEqual(first["profile_version_id"], saved.id)
            self.assertEqual(first["total_items"], 22)
            self.assertEqual(first["limit"], 20)
            self.assertEqual(first["next_offset"], 20)
            self.assertEqual(len(first["items"]), 20)
            self.assertEqual(first["items"][0]["entry_index"], 1)
            self.assertEqual(first["items"][10]["entry_index"], 12)
            self.assertEqual(second["items"][0]["entry_index"], 22)
            self.assertIsNone(second["next_offset"])
            expected_keys = {
                "entry_index",
                "name",
                "position",
                "url",
                "start_date",
                "end_date",
                "summary",
                "highlights",
                "location",
            }
            self.assertTrue(all(set(item) == expected_keys for item in first["items"]))
            self.assertIsNone(first["items"][0]["url"])
            self.assertEqual(len(first["items"][0]["summary"]), 1_000)
            self.assertLessEqual(
                len(first["items"][0]["highlights"]),
                WORK_HIGHLIGHT_LIMIT,
            )
            self.assertEqual(
                sum(len(value) for value in first["items"][0]["highlights"]),
                1_800,
            )
            encoded = json.dumps(first, ensure_ascii=False).encode("utf-8")
            self.assertLessEqual(len(encoded), WORK_LIST_RESPONSE_LIMIT_BYTES)
            self.assertNotIn("private_notes", encoded.decode())
            self.assertNotIn("source_path", encoded.decode())
            self.assertNotIn("secret-", encoded.decode())

            for invalid in (-1, 10_001, True, 1.5, "0", None):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        list_profile_work(data_dir, saved.id, invalid)

    def test_list_tolerates_missing_or_malformed_work_without_exposing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            missing = self._save(data_dir, {"basics": {"name": "Ada"}})
            malformed = self._save(
                data_dir,
                {"basics": {"name": "Ada"}, "work": {"secret": "hidden"}},
            )
            self.assertEqual(list_profile_work(data_dir, missing.id, 0)["items"], [])
            result = list_profile_work(data_dir, malformed.id, 0)
            self.assertEqual(result["total_items"], 0)
            self.assertNotIn("secret", json.dumps(result))

    def test_list_urls_match_native_public_network_policy(self) -> None:
        unsafe_urls = (
            "https://example.com/a\\b",
            "https://localhost.localdomain/work",
            "https://child.localhost.localdomain/work",
            "https://service.local/work",
            "https://192.0.0.9/work",
            "https://0x7f.1/work",
            "https://example.8/work",
            "https://100.64.0.1/work",
            "https://192.0.2.1/work",
            "https://198.18.0.1/work",
            "https://198.51.100.1/work",
            "https://203.0.113.1/work",
            "https://224.0.0.1/work",
            "https://240.0.0.1/work",
            "https://[::ffff:127.0.0.1]/work",
            "https://[64:ff9b:1::1]/work",
            "https://[100::1]/work",
            "https://[2001::1]/work",
            "https://[2001:db8::1]/work",
            "https://[2002::1]/work",
            "https://[3fff::1]/work",
            "https://[fc00::1]/work",
            "https://[fe80::1]/work",
        )
        safe_urls = (
            "https://example.com/work",
            "https://8.8.8.8/work",
            "https://8.8/work",
            "https://0x08080808/work",
            "https://[64:ff9b::1]/work",
            "https://[2001:1::1]/work",
            "https://[2001:1::2]/work",
            "https://[2001:3::1]/work",
            "https://[2001:4:112::1]/work",
            "https://[2001:20::1]/work",
            "https://[2001:30::1]/work",
            "https://[2606:4700:4700::1111]/work",
        )
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            urls = (*unsafe_urls, *safe_urls)
            saved = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {
                            "name": f"Employer {index}",
                            "position": "Engineer",
                            "url": url,
                        }
                        for index, url in enumerate(urls)
                    ],
                },
            )

            listed = list_profile_work(data_dir, saved.id, 0)
            second_page = list_profile_work(data_dir, saved.id, 20)
            projected_urls = [
                item["url"] for item in [*listed["items"], *second_page["items"]]
            ]

            self.assertEqual(projected_urls[: len(unsafe_urls)], [None] * len(unsafe_urls))
            self.assertEqual(projected_urls[len(unsafe_urls) :], list(safe_urls))

    def test_unicode_page_budget_advances_without_skipping_raw_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            work = [
                {
                    "name": "😀" * 240,
                    "position": "界" * 240,
                    "url": "https://example.com/" + "a" * 2_000,
                    "startDate": "2" * 80,
                    "endDate": "3" * 80,
                    "summary": "🚀" * 1_000,
                    "highlights": ["🧪" * 360] * 5,
                    "location": "市" * 200,
                }
                for _index in range(20)
            ]
            saved = self._save(
                data_dir,
                {"basics": {"name": "Ada"}, "work": work},
            )

            first = list_profile_work(data_dir, saved.id, 0)
            encoded = json.dumps(
                first,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            self.assertLessEqual(len(encoded), WORK_LIST_RESPONSE_LIMIT_BYTES)
            self.assertGreater(len(first["items"]), 0)
            self.assertLess(len(first["items"]), 20)
            self.assertEqual(first["next_offset"], len(first["items"]))
            second = list_profile_work(data_dir, saved.id, first["next_offset"])
            self.assertEqual(
                second["items"][0]["entry_index"],
                first["next_offset"],
            )

    def test_legacy_dictionary_entries_are_listed_repairable_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {},
                        {"name": "Legacy Employer", "private": "keep"},
                        "non-dictionary",
                    ],
                },
            )
            listed = list_profile_work(data_dir, parent.id, 0)
            self.assertEqual(listed["total_items"], 2)
            self.assertEqual(
                [item["entry_index"] for item in listed["items"]],
                [0, 1],
            )
            self.assertEqual(listed["items"][0]["name"], "")
            self.assertEqual(listed["items"][0]["position"], "")
            self.assertEqual(listed["items"][1]["name"], "Legacy Employer")
            self.assertEqual(listed["items"][1]["position"], "")

            repaired = self._update(
                data_dir,
                parent.id,
                {
                    "kind": "update",
                    "entry_index": 1,
                    "patch": {"position": "Engineer"},
                },
            )
            self.assertEqual(repaired["changed_fields"], ["position"])
            self.assertEqual(repaired["work_entries"], 2)
            self.assertEqual(
                latest_profile(data_dir)["profile"]["work"][1]["private"],
                "keep",
            )

            removed = self._update(
                data_dir,
                repaired["profile_version_id"],
                {"kind": "remove", "entry_index": 0},
            )
            self.assertEqual(removed["changed_fields"], ["name", "position"])
            self.assertEqual(removed["work_entries"], 1)
            self.assertEqual(
                latest_profile(data_dir)["profile"]["work"],
                [
                    {
                        "name": "Legacy Employer",
                        "position": "Engineer",
                        "private": "keep",
                    },
                    "non-dictionary",
                ],
            )

    def test_add_is_atomic_idempotent_and_preserves_the_raw_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            original = {
                "basics": {"name": "Ada", "private": {"keep": True}},
                "work": [
                    {"name": "First", "position": "Engineer", "private": "keep"},
                    "malformed-preserved",
                ],
                "meta": {"source": "/private/resume.pdf"},
            }
            parent = self._save(data_dir, original)
            request_id = str(uuid.uuid4())
            operation = {
                "kind": "add",
                "entry": {
                    "name": " Analytical Engines ",
                    "position": " Senior Engineer ",
                    "url": "https://example.com/careers",
                    "start_date": " 2024-01 ",
                    "end_date": None,
                    "summary": " First line\r\nSecond line ",
                    "highlights": [" Built safe storage ", "Shipped locally"],
                    "location": " London ",
                },
            }
            created = self._update(
                data_dir,
                parent.id,
                operation,
                request_id=request_id,
            )
            replayed = self._update(
                data_dir,
                parent.id,
                {
                    "kind": "add",
                    "entry": {
                        **operation["entry"],
                        "name": "Analytical Engines",
                        "position": "Senior Engineer",
                        "start_date": "2024-01",
                        "summary": "First line\nSecond line",
                        "highlights": ["Built safe storage", "Shipped locally"],
                        "location": "London",
                    },
                },
                request_id=request_id,
            )

            self.assertTrue(created["created"])
            self.assertFalse(replayed["created"])
            self.assertEqual(created["profile_version_id"], replayed["profile_version_id"])
            self.assertEqual(created["operation"], "add")
            self.assertEqual(created["entry_index"], 2)
            self.assertEqual(created["work_entries"], 2)
            self.assertEqual(
                created["changed_fields"],
                [
                    "name",
                    "position",
                    "url",
                    "start_date",
                    "summary",
                    "highlights",
                    "location",
                ],
            )
            stored = latest_profile(data_dir)
            self.assertEqual(stored["source"], "local_edit")
            self.assertEqual(stored["profile"]["work"][:2], original["work"])
            self.assertEqual(stored["profile"]["basics"], original["basics"])
            self.assertEqual(stored["profile"]["meta"], original["meta"])
            self.assertEqual(
                stored["profile"]["work"][2]["summary"],
                "First line\nSecond line",
            )
            self.assertEqual(
                list_profile_work(data_dir, parent.id, 0)["total_items"],
                1,
            )

            with self.assertRaises(VaultError) as conflict:
                self._update(
                    data_dir,
                    parent.id,
                    {
                        **operation,
                        "entry": {**operation["entry"], "position": "Different"},
                    },
                    request_id=request_id,
                )
            self.assertEqual(conflict.exception.code, "profile_update_request_conflict")

    def test_update_is_sparse_clears_nullable_fields_and_preserves_hidden_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {
                            "name": "Employer",
                            "position": "Engineer",
                            "url": "https://example.com",
                            "startDate": "2020",
                            "endDate": "2022",
                            "summary": "Old",
                            "highlights": ["One"],
                            "location": "London",
                            "private_evidence": {"source": "keep"},
                        }
                    ],
                    "extension": {"keep": [1, 2]},
                },
            )
            result = self._update(
                data_dir,
                parent.id,
                {
                    "kind": "update",
                    "entry_index": 0,
                    "patch": {
                        "position": "Principal Engineer",
                        "url": None,
                        "end_date": None,
                        "summary": "New",
                        "highlights": [],
                    },
                },
            )

            self.assertEqual(result["operation"], "update")
            self.assertEqual(result["entry_index"], 0)
            self.assertEqual(
                result["changed_fields"],
                ["position", "url", "end_date", "summary", "highlights"],
            )
            entry = latest_profile(data_dir)["profile"]["work"][0]
            self.assertEqual(entry["name"], "Employer")
            self.assertEqual(entry["startDate"], "2020")
            self.assertEqual(entry["location"], "London")
            self.assertEqual(entry["private_evidence"], {"source": "keep"})
            self.assertNotIn("url", entry)
            self.assertNotIn("endDate", entry)
            self.assertEqual(entry["highlights"], [])
            self.assertEqual(
                latest_profile(data_dir)["profile"]["extension"],
                {"keep": [1, 2]},
            )

    def test_remove_deletes_only_the_exact_raw_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            original_work = [
                {"name": "First", "position": "One"},
                "malformed",
                {
                    "name": "Second",
                    "position": "Two",
                    "summary": None,
                    "private": "keep elsewhere",
                },
                {"name": "Third", "position": "Three"},
            ]
            parent = self._save(
                data_dir,
                {"basics": {"name": "Ada"}, "work": original_work},
            )
            result = self._update(
                data_dir,
                parent.id,
                {"kind": "remove", "entry_index": 2},
            )

            self.assertEqual(result["operation"], "remove")
            self.assertEqual(result["entry_index"], 2)
            self.assertEqual(
                result["changed_fields"],
                ["name", "position", "summary"],
            )
            self.assertEqual(result["work_entries"], 2)
            self.assertEqual(
                latest_profile(data_dir)["profile"]["work"],
                [original_work[0], original_work[1], original_work[3]],
            )

    def test_noops_bad_targets_nameless_and_malformed_arrays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {"name": "Employer", "position": "Engineer"},
                        "not-a-dict",
                        {"name": "Needs repair"},
                    ],
                },
            )
            invalid_operations = (
                {
                    "kind": "update",
                    "entry_index": 0,
                    "patch": {"position": "Engineer"},
                },
                {"kind": "update", "entry_index": 0, "patch": {"name": None}},
                {"kind": "update", "entry_index": 0, "patch": {"position": " "}},
                {"kind": "update", "entry_index": 1, "patch": {"name": "X"}},
                {"kind": "update", "entry_index": 2, "patch": {"name": "X"}},
                {"kind": "remove", "entry_index": 1},
                {"kind": "remove", "entry_index": 99},
                {"kind": "remove", "entry_index": True},
            )
            for operation in invalid_operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(ValueError):
                        self._update(data_dir, parent.id, operation)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)

            malformed = self._save(
                data_dir,
                {"basics": {"name": "Ada"}, "work": {"not": "a list"}},
            )
            with self.assertRaisesRegex(ValueError, "malformed"):
                self._update(
                    data_dir,
                    malformed.id,
                    {"kind": "add", "entry": self._EMPTY_ENTRY},
                )

            nameless = self._save(
                data_dir,
                {"basics": {"label": "Needs repair"}, "work": []},
            )
            with self.assertRaisesRegex(ValueError, "non-empty name"):
                self._update(
                    data_dir,
                    nameless.id,
                    {"kind": "add", "entry": self._EMPTY_ENTRY},
                )

    def test_input_validation_bounds_urls_and_highlights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}, "work": []})

            def add(entry_patch: dict[str, object]) -> None:
                self._update(
                    data_dir,
                    parent.id,
                    {
                        "kind": "add",
                        "entry": {**self._EMPTY_ENTRY, **entry_patch},
                    },
                )

            invalid_entries = (
                {"name": ""},
                {"position": None},
                {"name": "bad\u0000name"},
                {"position": "x" * 241},
                {"start_date": "x" * 81},
                {"end_date": "😀" * 81},
                {"summary": "x" * 1_001},
                {"location": "x" * 201},
                {"url": "https://user:secret@example.com"},
                {"url": "http://127.0.0.1/private"},
                {"url": "https://service.local/path"},
                {"url": "https://example.com/a\\b"},
                {"url": "https://localhost.localdomain/work"},
                {"url": "https://192.0.0.9/work"},
                {"url": "https://0x7f.1/work"},
                {"url": "https://example.8/work"},
                {"url": "https://100.64.0.1/work"},
                {"url": "https://[64:ff9b:1::1]/work"},
                {"url": "https://[2001:db8::1]/work"},
                {"url": "file:///private/resume"},
                {"highlights": "not-a-list"},
                {"highlights": None},
                {"highlights": ["valid", 5]},
                {"highlights": ["x"] * (WORK_HIGHLIGHT_LIMIT + 1)},
                {"highlights": ["x" * 361]},
                {"highlights": ["x" * 301] * 6},
            )
            for entry_patch in invalid_entries:
                with self.subTest(entry_patch=entry_patch):
                    with self.assertRaises(ValueError):
                        add(entry_patch)

            with self.assertRaises(ValueError):
                self._update(
                    data_dir,
                    parent.id,
                    {"kind": "add", "entry": {"name": "Incomplete"}},
                )
            self.assertEqual(initialize_vault(data_dir).profile_versions, 1)

            accepted = self._update(
                data_dir,
                parent.id,
                {
                    "kind": "add",
                    "entry": {
                        **self._EMPTY_ENTRY,
                        "url": "https://8.8/work",
                    },
                },
            )
            self.assertEqual(
                list_profile_work(data_dir, accepted["profile_version_id"], 0)["items"][0][
                    "url"
                ],
                "https://8.8/work",
            )

    def test_stale_parent_and_concurrent_retries_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}, "work": []})
            params = {
                "expected_parent_profile_version_id": parent.id,
                "request_id": str(uuid.uuid4()),
                "operation": {"kind": "add", "entry": self._EMPTY_ENTRY},
            }
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(
                    executor.map(
                        lambda _index: update_profile_work(data_dir, params),
                        range(6),
                    )
                )
            self.assertEqual(sum(result["created"] for result in results), 1)
            self.assertEqual(len({result["profile_version_id"] for result in results}), 1)
            self.assertTrue(
                all(result["changed_fields"] == ["name", "position"] for result in results)
            )
            self.assertEqual(initialize_vault(data_dir).profile_versions, 2)

            output_id = results[0]["profile_version_id"]
            self._update(
                data_dir,
                output_id,
                {
                    "kind": "update",
                    "entry_index": 0,
                    "patch": {"position": "Principal Engineer"},
                },
            )
            late_retry = update_profile_work(data_dir, params)
            self.assertFalse(late_retry["created"])
            self.assertEqual(late_retry["profile_version_id"], output_id)
            self.assertEqual(initialize_vault(data_dir).profile_versions, 3)

            with self.assertRaises(VaultError) as conflict:
                self._update(
                    data_dir,
                    parent.id,
                    {"kind": "add", "entry": {**self._EMPTY_ENTRY, "name": "Other"}},
                )
            self.assertEqual(conflict.exception.code, "profile_update_conflict")

    def test_rpc_requires_exact_params_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}, "work": []})
            listed = self._request(
                data_dir,
                "profile.work.list",
                {"profile_version_id": parent.id, "offset": 0},
            )
            updated = self._request(
                data_dir,
                "profile.work.update",
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(uuid.uuid4()),
                    "operation": {"kind": "add", "entry": self._EMPTY_ENTRY},
                },
            )
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["result"]["items"], [])
            self.assertTrue(updated["ok"])
            self.assertTrue(updated["result"]["created"])

            invalid = (
                ("profile.work.list", {}),
                (
                    "profile.work.list",
                    {"profile_version_id": parent.id, "offset": 0, "extra": True},
                ),
                (
                    "profile.work.update",
                    {
                        "expected_parent_profile_version_id": parent.id,
                        "request_id": str(uuid.uuid4()),
                        "operation": {"kind": "add", "entry": self._EMPTY_ENTRY},
                        "extra": True,
                    },
                ),
            )
            for method, params in invalid:
                response = self._request(data_dir, method, params)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_params")

    def test_receipts_are_durable_immutable_and_reject_invalid_direct_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}, "work": []})
            result = self._update(
                data_dir,
                parent.id,
                {"kind": "add", "entry": self._EMPTY_ENTRY},
            )
            database_path = Path(initialize_vault(data_dir).database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                receipt = connection.execute(
                    "SELECT * FROM profile_work_update_receipts"
                ).fetchone()
                self.assertIsNotNone(receipt)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_work_update_receipts SET created_at_ms = created_at_ms + 1"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_work_update_receipts")
                connection.rollback()

            eligible = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {"name": "Analytical Engines", "position": "Engineer"},
                        {"name": "Other", "position": "Lead"},
                    ],
                },
                source="local_edit",
            )
            latest = latest_profile(data_dir)
            insert_sql = """
                INSERT INTO profile_work_update_receipts(
                    request_id, request_fingerprint, output_profile_version_id,
                    parent_profile_version_id, operation, entry_index,
                    changed_fields_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            invalid_rows = (
                ("add", 1, '["position","name"]'),
                ("add", 1, '["name","name"]'),
                ("add", 1, '["name"]'),
                ("add", 1, '["position"]'),
                ("add", 1, '["private"]'),
            )
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for operation, entry_index, changed_json in invalid_rows:
                    with self.subTest(changed_json=changed_json):
                        with self.assertRaisesRegex(
                            sqlite3.IntegrityError,
                            "lineage is invalid",
                        ):
                            connection.execute(
                                insert_sql,
                                (
                                    str(uuid.uuid4()),
                                    "a" * 64,
                                    eligible.id,
                                    result["profile_version_id"],
                                    operation,
                                    entry_index,
                                    changed_json,
                                    latest["created_at_ms"],
                                ),
                            )
                        connection.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        insert_sql,
                        (
                            str(uuid.uuid4()),
                            "b" * 64,
                            eligible.id,
                            result["profile_version_id"],
                            "invalid",
                            1,
                            '["name"]',
                            latest["created_at_ms"],
                        ),
                    )

    def test_populated_schema_ten_migration_preserves_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database = data_dir / "cvgnome.sqlite3"
            first_id = str(uuid.uuid4())
            second_id = str(uuid.uuid4())
            first_json = '{"basics":{"name":"Ada"},"work":["malformed"]}'
            second_json = (
                '{"basics":{"name":"Ada"},"work":'
                '["malformed",{"name":"Employer","position":"Engineer",'
                '"private":"keep"}]}'
            )
            first_checksum = hashlib.sha256(first_json.encode()).hexdigest()
            second_checksum = hashlib.sha256(second_json.encode()).hexdigest()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for version in range(1, 11):
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
                    ) VALUES (?, 1, NULL, ?, ?, 'legacy-v10', 1)
                    """,
                    (first_id, first_json, first_checksum),
                )
                connection.execute(
                    """
                    INSERT INTO profile_versions(
                        id, version_number, parent_version_id, canonical_json,
                        checksum_sha256, source, created_at_ms
                    ) VALUES (?, 2, ?, ?, ?, 'legacy-v10', 2)
                    """,
                    (second_id, first_id, second_json, second_checksum),
                )
                connection.execute("PRAGMA user_version = 10")
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
                    "SELECT count(*) FROM profile_work_update_receipts"
                ).fetchone()[0]
                migration = connection.execute(
                    """
                    SELECT name, checksum_sha256, app_version
                    FROM schema_migrations WHERE version = 11
                    """
                ).fetchone()
            self.assertEqual(
                profiles,
                [
                    (first_id, 1, None, first_json, first_checksum, "legacy-v10", 1),
                    (
                        second_id,
                        2,
                        first_id,
                        second_json,
                        second_checksum,
                        "legacy-v10",
                        2,
                    ),
                ],
            )
            self.assertEqual(receipt_count, 0)
            self.assertEqual(
                migration,
                (
                    MIGRATION_NAMES[11],
                    hashlib.sha256(MIGRATIONS[11].encode()).hexdigest(),
                    MIGRATION_APP_VERSIONS[11],
                ),
            )


if __name__ == "__main__":
    unittest.main()
