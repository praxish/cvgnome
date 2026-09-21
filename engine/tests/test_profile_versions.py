# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.cli import _handle_request  # noqa: E402
import cvgnome_engine.profile_versions as profile_versions_module  # noqa: E402
from cvgnome_engine.profile_versions import (  # noqa: E402
    diff_profile_versions,
    get_profile_basics,
    list_profile_versions,
    update_profile_basics,
)
from cvgnome_engine.storage import (  # noqa: E402
    VaultError,
    initialize_vault,
    latest_profile,
    save_profile,
)


class ProfileVersionTests(unittest.TestCase):
    def _save(
        self,
        data_dir: Path,
        profile: dict[str, object],
        *,
        source: str = "file_import:test",
    ):
        return save_profile(data_dir, profile, source=source)

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

    def test_version_list_is_fixed_paged_newest_first_and_public(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            empty = list_profile_versions(data_dir, 0)
            self.assertEqual(
                empty,
                {
                    "current_profile_version_id": None,
                    "total_items": 0,
                    "offset": 0,
                    "limit": 20,
                    "next_offset": None,
                    "items": [],
                },
            )
            saved = []
            for number in range(22):
                saved.append(
                    self._save(
                        data_dir,
                        {
                            "basics": {
                                "name": f"Person {number:02d}",
                                "label": f"Headline {number:02d}",
                                "private_token": f"never-return-{number}",
                            },
                            "meta": {"private_path": f"/secret/{number}"},
                        },
                        source=("source_scan:test" if number == 21 else "file_import:test"),
                    )
                )

            first = list_profile_versions(data_dir, 0)
            second = list_profile_versions(data_dir, 20)

            self.assertEqual(first["current_profile_version_id"], saved[-1].id)
            self.assertEqual(first["total_items"], 22)
            self.assertEqual(first["next_offset"], 20)
            self.assertEqual(len(first["items"]), 20)
            self.assertEqual(first["items"][0]["profile_version_id"], saved[-1].id)
            self.assertEqual(first["items"][0]["source_kind"], "source_documents")
            self.assertEqual(first["items"][-1]["version_number"], 3)
            self.assertEqual(second["next_offset"], None)
            self.assertEqual(
                [item["version_number"] for item in second["items"]],
                [2, 1],
            )
            serialized = json.dumps(first)
            self.assertNotIn("private_token", serialized)
            self.assertNotIn("private_path", serialized)
            self.assertNotIn("checksum", serialized)

            for invalid in (-1, 10_001, True, 1.5, "0", None):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        list_profile_versions(data_dir, invalid)

    def test_version_list_uses_one_read_snapshot_during_concurrent_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            original = self._save(data_dir, {"basics": {"name": "Original"}})
            page_query_started = threading.Event()
            writer_finished = threading.Event()
            original_connect = profile_versions_module._connect

            def traced_connect(path: Path) -> sqlite3.Connection:
                connection = original_connect(path)

                def trace(statement: str) -> None:
                    normalized = " ".join(statement.split())
                    if (
                        normalized.startswith("SELECT id, version_number")
                        and "ORDER BY version_number DESC" in normalized
                        and "LIMIT" in normalized
                    ):
                        page_query_started.set()
                        writer_finished.wait(5)

                connection.set_trace_callback(trace)
                return connection

            def append_version() -> None:
                if not page_query_started.wait(5):
                    return
                self._save(data_dir, {"basics": {"name": "Concurrent"}})
                writer_finished.set()

            with ThreadPoolExecutor(max_workers=1) as executor:
                writer = executor.submit(append_version)
                with mock.patch.object(
                    profile_versions_module,
                    "_connect",
                    side_effect=traced_connect,
                ):
                    result = list_profile_versions(data_dir, 0)
                writer.result(timeout=5)

            self.assertTrue(page_query_started.is_set())
            self.assertTrue(writer_finished.is_set())
            self.assertEqual(result["total_items"], 1)
            self.assertEqual(result["current_profile_version_id"], original.id)
            self.assertEqual(
                [item["profile_version_id"] for item in result["items"]],
                [original.id],
            )
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 2)

    def test_basics_get_is_version_pinned_bounded_and_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            saved = self._save(
                data_dir,
                {
                    "basics": {
                        "name": "Ada Lovelace",
                        "label": "Analytical Engineer",
                        "summary": "Builds useful systems.",
                        "email": "ada@example.com",
                        "phone": "+1 555 123 4567",
                        "url": "https://example.com/ada",
                        "location": {
                            "city": "London",
                            "region": "Greater London",
                            "countryCode": "gb",
                            "coordinates": "private coordinates",
                        },
                        "profiles": [{"network": "Secret", "username": "private"}],
                        "hidden": "do not project",
                    },
                    "meta": {"source_path": "/private/resume.docx"},
                },
            )

            result = get_profile_basics(data_dir, saved.id)

            self.assertEqual(
                set(result),
                {
                    "profile_version_id",
                    "version_number",
                    "created_at_ms",
                    "renderable",
                    "name",
                    "headline",
                    "summary",
                    "email",
                    "phone",
                    "url",
                    "location",
                },
            )
            self.assertEqual(result["name"], "Ada Lovelace")
            self.assertEqual(result["location"]["country_code"], "GB")
            self.assertNotIn("hidden", json.dumps(result))
            self.assertNotIn("coordinates", json.dumps(result))
            self.assertNotIn("profiles", json.dumps(result))
            self.assertNotIn("source_path", json.dumps(result))

    def test_basic_update_is_atomic_idempotent_and_preserves_hidden_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            original_profile = {
                "basics": {
                    "name": "Ada Lovelace",
                    "label": "Engineer",
                    "summary": "Original summary",
                    "email": "ada@example.com",
                    "hidden_identity": {"private": "keep me"},
                    "location": {
                        "city": "London",
                        "region": "London",
                        "countryCode": "GB",
                        "coordinates": {"lat": 51.5, "lng": -0.1},
                    },
                },
                "work": [{"name": "Babbage", "position": "Engineer"}],
                "meta": {"private_source": "/vault/raw/resume.pdf"},
                "extension": {"unknown": [1, 2, 3]},
            }
            parent = self._save(data_dir, original_profile)
            request_id = str(uuid.uuid4())
            params = {
                "expected_parent_profile_version_id": parent.id,
                "request_id": request_id,
                "patch": {
                    "headline": "Principal Engineer",
                    "email": None,
                    "location": {"region": "England", "country_code": "us"},
                },
            }

            created = update_profile_basics(data_dir, params)
            replayed = update_profile_basics(
                data_dir,
                {
                    **params,
                    "patch": {
                        "headline": " Principal Engineer ",
                        "email": None,
                        "location": {"region": "England", "country_code": "US"},
                    },
                },
            )

            self.assertTrue(created["created"])
            self.assertFalse(replayed["created"])
            self.assertEqual(created["profile_version_id"], replayed["profile_version_id"])
            self.assertEqual(created["changed_fields"], replayed["changed_fields"])
            self.assertEqual(
                created["changed_fields"],
                ["headline", "email", "location.region", "location.country_code"],
            )
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 2)
            stored = latest_profile(data_dir)
            self.assertEqual(stored["source"], "local_edit")
            self.assertEqual(
                list_profile_versions(data_dir, 0)["items"][0][
                    "parent_profile_version_id"
                ],
                parent.id,
            )
            self.assertNotIn("email", stored["profile"]["basics"])
            self.assertEqual(
                stored["profile"]["basics"]["hidden_identity"],
                original_profile["basics"]["hidden_identity"],
            )
            self.assertEqual(
                stored["profile"]["basics"]["location"]["coordinates"],
                original_profile["basics"]["location"]["coordinates"],
            )
            self.assertEqual(stored["profile"]["work"], original_profile["work"])
            self.assertEqual(stored["profile"]["meta"], original_profile["meta"])
            self.assertEqual(stored["profile"]["extension"], original_profile["extension"])

            with self.assertRaisesRegex(VaultError, "different inputs") as conflict:
                update_profile_basics(
                    data_dir,
                    {
                        **params,
                        "patch": {"summary": "Different reuse"},
                    },
                )
            self.assertEqual(conflict.exception.code, "profile_update_request_conflict")

    def test_concurrent_exact_retries_append_only_one_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}})
            params = {
                "expected_parent_profile_version_id": parent.id,
                "request_id": str(uuid.uuid4()),
                "patch": {"summary": "A safely retried edit."},
            }
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(
                    executor.map(
                        lambda _index: update_profile_basics(data_dir, params),
                        range(6),
                    )
                )

            self.assertEqual(sum(result["created"] for result in results), 1)
            self.assertEqual(len({result["profile_version_id"] for result in results}), 1)
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 2)

    def test_stale_parent_noop_and_adversarial_patches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {
                    "basics": {
                        "name": "Ada",
                        "label": "Engineer",
                        "url": "https://example.com",
                    }
                },
            )

            def update(patch: object, *, expected: str | None = None):
                return update_profile_basics(
                    data_dir,
                    {
                        "expected_parent_profile_version_id": expected or parent.id,
                        "request_id": str(uuid.uuid4()),
                        "patch": patch,
                    },
                )

            with self.assertRaisesRegex(ValueError, "semantic change"):
                update({"headline": "Engineer"})
            for patch in (
                {"name": None},
                {"name": "   "},
                {"name": "Ada\n"},
                {"summary": "bad\u0000control"},
                {"headline": "x" * 201},
                {"summary": "😀" * 4_001},
                {"url": "https://user:secret@example.com"},
                {"url": "http://127.0.0.1/private"},
                {"url": "https://service.local/path"},
                {"email": "not-an-email"},
                {"email": "a@example.com/path"},
                {"email": "a@example.com:443"},
                {"email": "a@-bad.example"},
                {"email": "a@bad-.example"},
                {"email": "a@exa_mple.com"},
                {"phone": "call-me"},
                {"location": None},
                {"location": {}},
                {"location": {"country_code": "USA"}},
                {"location": {"postal_code": "12345"}},
                {"unknown": "value"},
                {},
            ):
                with self.subTest(patch=patch):
                    with self.assertRaises(ValueError):
                        update(patch)

            current = update({"summary": "New current state"})
            with self.assertRaises(VaultError) as conflict:
                update({"summary": "Stale overwrite"}, expected=parent.id)
            self.assertEqual(conflict.exception.code, "profile_update_conflict")
            self.assertEqual(latest_profile(data_dir)["id"], current["profile_version_id"])
            self.assertEqual(list_profile_versions(data_dir, 0)["total_items"], 2)

    def test_summary_normalizes_plain_multiline_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}})
            result = update_profile_basics(
                data_dir,
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(uuid.uuid4()),
                    "patch": {"summary": " First line\r\nSecond\tline\rThird line "},
                },
            )
            basics = get_profile_basics(data_dir, result["profile_version_id"])
            self.assertEqual(
                basics["summary"],
                "First line\nSecond line\nThird line",
            )
            self.assertEqual(
                latest_profile(data_dir)["profile"]["basics"]["summary"],
                "First line\nSecond line\nThird line",
            )

    def test_diff_is_public_bounded_and_counts_basic_and_section_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            before = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada", "label": "Engineer"},
                    "work": [
                        {
                            "name": "Babbage",
                            "position": "Engineer",
                            "summary": "Built a public engine.",
                            "private_claim": "do not return",
                        }
                    ],
                    "meta": {"secret": "before-private"},
                },
            )
            after = self._save(
                data_dir,
                {
                    "basics": {"name": "Ada", "label": "Principal Engineer"},
                    "work": [
                        {
                            "name": "Babbage",
                            "position": "Engineer",
                            "summary": "Built a different public engine.",
                            "private_claim": "still do not return",
                        }
                    ],
                    "meta": {"secret": "after-private"},
                },
                source="local_edit",
            )

            result = diff_profile_versions(data_dir, before.id, after.id)

            self.assertEqual(result["from_version"]["profile_version_id"], before.id)
            self.assertEqual(result["to_version"]["profile_version_id"], after.id)
            self.assertEqual(result["basic_changes"][0]["field"], "headline")
            self.assertEqual(len(result["section_changes"]), 1)
            self.assertTrue(result["section_changes"][0]["content_changed"])
            self.assertEqual(result["section_changes"][0]["before_count"], 1)
            self.assertEqual(result["section_changes"][0]["after_count"], 1)
            self.assertEqual(result["total_changes"], 2)
            serialized = json.dumps(result)
            self.assertNotIn("private_claim", serialized)
            self.assertNotIn("before-private", serialized)
            self.assertNotIn("after-private", serialized)
            self.assertNotIn("Built a public engine", serialized)

            unchanged = diff_profile_versions(data_dir, after.id, after.id)
            self.assertEqual(unchanged["basic_changes"], [])
            self.assertEqual(unchanged["section_changes"], [])
            self.assertEqual(unchanged["total_changes"], 0)

    def test_nameless_import_can_be_repaired_by_setting_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {"basics": {"label": "Needs a name"}, "meta": {"secret": "keep"}},
            )
            basics = get_profile_basics(data_dir, parent.id)
            self.assertEqual(basics["name"], "")

            repaired = update_profile_basics(
                data_dir,
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(uuid.uuid4()),
                    "patch": {"name": "Ada Lovelace"},
                },
            )

            self.assertEqual(repaired["profile_name"], "Ada Lovelace")
            self.assertIsInstance(repaired["renderable"], bool)
            self.assertEqual(latest_profile(data_dir)["profile"]["meta"]["secret"], "keep")

    def test_rpc_methods_require_exact_params_and_review_exact_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            saved = self._save(
                data_dir,
                {"basics": {"name": "Ada", "label": "Engineer"}},
            )
            review = self._request(data_dir, "profile.review", {})
            pinned = self._request(
                data_dir,
                "profile.review.version",
                {"profile_version_id": saved.id},
            )
            self.assertTrue(review["ok"])
            self.assertEqual(review["result"], pinned["result"])

            invalid_requests = (
                ("profile.versions.list", {}),
                ("profile.versions.list", {"offset": 0, "extra": True}),
                (
                    "profile.review.version",
                    {"profile_version_id": saved.id, "extra": True},
                ),
                (
                    "profile.basics.get",
                    {"profile_version_id": saved.id, "extra": True},
                ),
                (
                    "profile.versions.diff",
                    {
                        "from_profile_version_id": saved.id,
                        "to_profile_version_id": saved.id,
                        "extra": True,
                    },
                ),
                (
                    "profile.basics.update",
                    {
                        "expected_parent_profile_version_id": saved.id,
                        "request_id": str(uuid.uuid4()),
                        "patch": {"headline": "New"},
                        "extra": True,
                    },
                ),
            )
            for method, params in invalid_requests:
                with self.subTest(method=method, params=params):
                    response = self._request(data_dir, method, params)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], "invalid_params")

    def test_rpc_profile_history_update_and_diff_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(
                data_dir,
                {"basics": {"name": "Ada", "label": "Engineer"}},
            )
            listed = self._request(data_dir, "profile.versions.list", {"offset": 0})
            basics = self._request(
                data_dir,
                "profile.basics.get",
                {"profile_version_id": parent.id},
            )
            request_id = str(uuid.uuid4())
            updated = self._request(
                data_dir,
                "profile.basics.update",
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": request_id,
                    "patch": {"headline": "Principal Engineer"},
                },
            )
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["result"]["items"][0]["profile_version_id"], parent.id)
            self.assertTrue(basics["ok"])
            self.assertEqual(basics["result"]["name"], "Ada")
            self.assertTrue(updated["ok"])
            self.assertTrue(updated["result"]["created"])

            output_id = updated["result"]["profile_version_id"]
            difference = self._request(
                data_dir,
                "profile.versions.diff",
                {
                    "from_profile_version_id": parent.id,
                    "to_profile_version_id": output_id,
                },
            )
            review = self._request(
                data_dir,
                "profile.review.version",
                {"profile_version_id": output_id},
            )
            self.assertTrue(difference["ok"])
            self.assertEqual(difference["result"]["total_changes"], 1)
            self.assertEqual(
                difference["result"]["basic_changes"][0]["field"],
                "headline",
            )
            self.assertTrue(review["ok"])
            self.assertEqual(review["result"]["profile_version_id"], output_id)

    def test_receipts_are_durable_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}})
            result = update_profile_basics(
                data_dir,
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(uuid.uuid4()),
                    "patch": {"summary": "Durable receipt"},
                },
            )
            database_path = initialize_vault(data_dir).database_path
            with closing(sqlite3.connect(database_path)) as connection:
                receipt = connection.execute(
                    "SELECT * FROM profile_basic_update_receipts"
                ).fetchone()
                self.assertIsNotNone(receipt)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_basic_update_receipts SET created_at_ms = created_at_ms + 1"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_basic_update_receipts")
            replay = update_profile_basics(
                data_dir,
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": result["request_id"],
                    "patch": {"summary": "Durable receipt"},
                },
            )
            self.assertFalse(replay["created"])
            self.assertEqual(replay["profile_version_id"], result["profile_version_id"])

    def test_receipt_table_rejects_invalid_changed_fields_and_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            parent = self._save(data_dir, {"basics": {"name": "Ada"}})
            edited = update_profile_basics(
                data_dir,
                {
                    "expected_parent_profile_version_id": parent.id,
                    "request_id": str(uuid.uuid4()),
                    "patch": {"headline": "Engineer"},
                },
            )
            local_output = self._save(
                data_dir,
                {"basics": {"name": "Ada", "label": "Principal Engineer"}},
                source="local_edit",
            )
            local_created_at_ms = latest_profile(data_dir)["created_at_ms"]
            database_path = initialize_vault(data_dir).database_path
            with closing(sqlite3.connect(database_path)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "lineage is invalid"):
                    connection.execute(
                        """
                        INSERT INTO profile_basic_update_receipts(
                            request_id, request_fingerprint, output_profile_version_id,
                            parent_profile_version_id, changed_fields_json, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            "a" * 64,
                            local_output.id,
                            edited["profile_version_id"],
                            '["private.hidden"]',
                            local_created_at_ms,
                        ),
                    )
                connection.rollback()

            structured_output = self._save(
                data_dir,
                {"basics": {"name": "Ada", "label": "Imported Engineer"}},
                source="file_import:test",
            )
            structured_created_at_ms = latest_profile(data_dir)["created_at_ms"]
            with closing(sqlite3.connect(database_path)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "lineage is invalid"):
                    connection.execute(
                        """
                        INSERT INTO profile_basic_update_receipts(
                            request_id, request_fingerprint, output_profile_version_id,
                            parent_profile_version_id, changed_fields_json, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            "b" * 64,
                            structured_output.id,
                            local_output.id,
                            '["headline"]',
                            structured_created_at_ms,
                        ),
                    )
                connection.rollback()

            with closing(sqlite3.connect(database_path)) as connection:
                receipt_count = connection.execute(
                    "SELECT count(*) FROM profile_basic_update_receipts"
                ).fetchone()[0]
            self.assertEqual(receipt_count, 1)


if __name__ == "__main__":
    unittest.main()
