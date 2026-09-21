# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from typing import Any


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.profile_sources import (  # noqa: E402
    commit_profile_sources,
    preview_profile_sources,
)
from cvgnome_engine.cli import _handle_request  # noqa: E402
from cvgnome_engine.review_inbox import (  # noqa: E402
    apply_profile_review_item,
    list_profile_review_items,
    partition_review_candidates,
    transition_profile_review_item,
)
from cvgnome_engine.source_ingest import synthesize_canonical_profile  # noqa: E402
from cvgnome_engine.storage import (  # noqa: E402
    VaultError,
    import_canonical_profile_source,
    initialize_vault,
    latest_profile,
    reset_profile_source_retention,
    save_profile,
)
from cvgnome_engine.workspace_reset import reset_workspace  # noqa: E402
from migration_fixtures import remove_schema_nineteen


BASE_PROFILE = {
    "basics": {
        "name": "Ada Lovelace",
        "label": "Analytics Engineer",
        "email": "ada-private@example.test",
    },
    "work": [
        {
            "name": "Analytical Engines",
            "position": "Lead Engineer",
            "summary": "Built reliable data systems.",
        }
    ],
    "education": [
        {
            "institution": "University of London",
            "studyType": "BSc",
            "area": "Mathematics",
        }
    ],
    "projects": [
        {
            "name": "Existing Engine",
            "description": "Original description.",
        }
    ],
    "skills": [{"name": "Data", "keywords": ["Python"]}],
}

FOLLOWUP_PROFILE = {
    "basics": {
        "name": "Ada Lovelace",
        "email": "do-not-leak@example.test",
    },
    "work": [
        {
            "name": "New Research Group",
            "position": "Advisor",
            "summary": "Guided a new analytical program.",
        }
    ],
    "education": [
        {
            "institution": "University of London",
            "studyType": "BSc",
            "area": "Mathematics",
            "score": "First-class honours",
        },
        {
            "institution": "Oxford",
            "studyType": "Certificate",
            "area": "Computing",
        },
    ],
    "projects": [
        {
            "name": "Existing Engine",
            "description": "Original description.",
            "keywords": ["Algorithms"],
        },
        {
            "name": "New Engine",
            "description": "A proposed project entry.",
        },
    ],
    "skills": [
        {"name": "Data", "keywords": ["Python", "SQL"]},
        {"name": "Leadership", "keywords": ["Coaching"]},
    ],
}


class ReviewInboxTests(unittest.TestCase):
    def _stage_json(self, data_dir: Path, profile: dict[str, Any]) -> dict[str, Any]:
        scan_id = str(uuid.uuid4())
        content = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
        staged = data_dir / "imports" / "staging" / scan_id / "0000.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        return {
            "scan_id": scan_id,
            "sources": [
                {
                    "ordinal": 0,
                    "managed_relative_path": (
                        f"imports/staging/{scan_id}/0000.json"
                    ),
                    "display_name": "follow-up-profile.json",
                    "format": "json",
                    "byte_size": len(content),
                    "checksum_sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
            "file_counts": {"discovered": 1, "staged": 1, "skipped": 0},
            "scan_issues": {},
        }

    def _create_items(self, data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        save_profile(data_dir, BASE_PROFILE, source="test")
        params = self._stage_json(data_dir, FOLLOWUP_PROFILE)
        preview = preview_profile_sources(data_dir, params)
        self.assertEqual(preview["review_candidate_count"], 6)
        receipt = commit_profile_sources(data_dir, params["scan_id"])
        self.assertEqual(receipt["review_item_count"], 6)
        return preview, receipt

    def test_followup_import_quarantines_only_target_section_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _preview, receipt = self._create_items(data_dir)

            current = latest_profile(data_dir)
            self.assertIsNotNone(current)
            profile = current["profile"]
            self.assertEqual(profile["education"], BASE_PROFILE["education"])
            self.assertEqual(profile["projects"], BASE_PROFILE["projects"])
            self.assertEqual(profile["skills"], BASE_PROFILE["skills"])
            self.assertEqual(len(profile["work"]), 2)

            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            self.assertEqual(page["total_items"], 6)
            self.assertEqual(page["counts"], {"inbox": 6, "deferred": 0, "history": 0})
            self.assertIsNone(page["next_offset"])
            self.assertEqual(
                {(item["candidate_kind"], item["operation_kind"]) for item in page["items"]},
                {
                    ("education", "add"),
                    ("education", "update"),
                    ("projects", "add"),
                    ("projects", "update"),
                    ("skills", "add"),
                    ("skills", "update"),
                },
            )
            for item in page["items"]:
                self.assertEqual(item["state"], "inbox")
                self.assertEqual(item["state_revision"], 0)
                self.assertFalse(item["is_previous_import_set"])
                self.assertTrue(item["title"])
                self.assertTrue(item["changed_fields"])
                self.assertGreaterEqual(len(item["evidence"]), 1)
                self.assertLessEqual(len(item["evidence"]), 3)
                evidence = item["evidence"][0]
                self.assertEqual(evidence["display_name"], "follow-up-profile.json")
                self.assertEqual(evidence["source_format"], "json")
                self.assertEqual(evidence["location_label"], "Structured profile field")
                self.assertTrue(evidence["excerpt"])
            serialized = json.dumps(page, ensure_ascii=False)
            self.assertNotIn("do-not-leak@example.test", serialized)
            self.assertEqual(
                page["current_profile_version_id"], receipt["profile_version_id"]
            )
            self.assertNotIn("candidate_checksum_sha256", serialized)

            retry = commit_profile_sources(data_dir, receipt["scan_id"])
            self.assertEqual(retry, receipt)
            self.assertEqual(retry["review_item_count"], 6)

    def test_transitions_are_pinned_idempotent_and_history_is_reopenable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            item = list_profile_review_items(data_dir, scope="inbox", offset=0)["items"][0]
            defer_request = str(uuid.uuid4())
            deferred = transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=defer_request,
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )
            self.assertEqual(deferred["previous_state"], "inbox")
            self.assertEqual(deferred["state"], "deferred")
            self.assertEqual(deferred["state_revision"], 1)
            self.assertTrue(deferred["created"])

            retry = transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=defer_request,
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )
            self.assertFalse(retry["created"])
            self.assertEqual(retry["created_at_ms"], deferred["created_at_ms"])
            with self.assertRaises(VaultError) as reused:
                transition_profile_review_item(
                    data_dir,
                    review_item_id=item["id"],
                    request_id=defer_request,
                    action="reject",
                    expected_state="deferred",
                    expected_state_revision=1,
                )
            self.assertEqual(reused.exception.code, "review_item_request_conflict")
            with self.assertRaises(VaultError) as stale:
                transition_profile_review_item(
                    data_dir,
                    review_item_id=item["id"],
                    request_id=str(uuid.uuid4()),
                    action="reject",
                    expected_state="inbox",
                    expected_state_revision=0,
                )
            self.assertEqual(stale.exception.code, "review_item_state_conflict")

            rejected = transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=str(uuid.uuid4()),
                action="reject",
                expected_state="deferred",
                expected_state_revision=1,
            )
            self.assertEqual(rejected["state"], "rejected")
            history = list_profile_review_items(data_dir, scope="history", offset=0)
            self.assertEqual(history["total_items"], 1)
            reopened = transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=str(uuid.uuid4()),
                action="reopen",
                expected_state="rejected",
                expected_state_revision=2,
            )
            self.assertEqual(reopened["state"], "inbox")
            self.assertEqual(reopened["state_revision"], 3)
            status = initialize_vault(data_dir)
            self.assertEqual(status.review_inbox_items, 6)
            self.assertEqual(status.review_deferred_items, 0)
            self.assertEqual(status.review_history_items, 0)

    def test_concurrent_exact_transition_records_one_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            item = list_profile_review_items(data_dir, scope="inbox", offset=0)["items"][0]
            request_id = str(uuid.uuid4())

            def defer_once(_index: int) -> dict[str, Any]:
                return transition_profile_review_item(
                    data_dir,
                    review_item_id=item["id"],
                    request_id=request_id,
                    action="defer",
                    expected_state="inbox",
                    expected_state_revision=0,
                )

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(defer_once, range(4)))
            self.assertEqual(sum(bool(result["created"]) for result in results), 1)
            self.assertEqual({result["state"] for result in results}, {"deferred"})
            self.assertEqual({result["state_revision"] for result in results}, {1})
            with closing(sqlite3.connect(initialize_vault(data_dir).database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_item_events"
                    ).fetchone()[0],
                    1,
                )

    def test_retention_reset_moves_active_items_to_history_without_erasing_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            item = list_profile_review_items(data_dir, scope="inbox", offset=0)["items"][0]
            request_id = str(uuid.uuid4())
            receipt = transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=request_id,
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )
            reset_profile_source_retention(
                data_dir,
                expected_generation=1,
                request_id=str(uuid.uuid4()),
            )
            status = initialize_vault(data_dir)
            self.assertEqual(status.review_inbox_items, 0)
            self.assertEqual(status.review_deferred_items, 0)
            self.assertEqual(status.review_history_items, 6)
            history = list_profile_review_items(data_dir, scope="history", offset=0)
            self.assertEqual(history["total_items"], 6)
            self.assertTrue(all(item["is_previous_import_set"] for item in history["items"]))
            with self.assertRaises(VaultError) as stale_generation:
                transition_profile_review_item(
                    data_dir,
                    review_item_id=item["id"],
                    request_id=str(uuid.uuid4()),
                    action="reopen",
                    expected_state="deferred",
                    expected_state_revision=1,
                )
            self.assertEqual(
                stale_generation.exception.code,
                "review_item_stale_generation",
            )
            exact_retry = transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=request_id,
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )
            self.assertFalse(exact_retry["created"])
            self.assertEqual(exact_retry["created_at_ms"], receipt["created_at_ms"])
            with closing(sqlite3.connect(status.database_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM profile_review_items").fetchone()[0],
                    6,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_item_events"
                    ).fetchone()[0],
                    1,
                )

    def test_first_and_nonrenderable_bootstrap_imports_do_not_create_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_dir = Path(directory) / "first"
            params = self._stage_json(first_dir, BASE_PROFILE)
            preview = preview_profile_sources(first_dir, params)
            self.assertEqual(preview["review_candidate_count"], 0)
            receipt = commit_profile_sources(first_dir, params["scan_id"])
            self.assertEqual(receipt["review_item_count"], 0)

            bootstrap_dir = Path(directory) / "bootstrap"
            save_profile(
                bootstrap_dir,
                {"basics": {"name": "Ada Lovelace"}},
                source="manual_start",
            )
            params = self._stage_json(
                bootstrap_dir,
                {
                    "education": [
                        {
                            "institution": "University of London",
                            "studyType": "BSc",
                            "area": "Mathematics",
                        }
                    ]
                },
            )
            preview = preview_profile_sources(bootstrap_dir, params)
            self.assertEqual(preview["review_candidate_count"], 0)
            receipt = commit_profile_sources(bootstrap_dir, params["scan_id"])
            self.assertEqual(receipt["review_item_count"], 0)
            self.assertEqual(len(latest_profile(bootstrap_dir)["profile"]["education"]), 1)

    def test_canonical_import_never_creates_review_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, BASE_PROFILE, source="test")
            scan_id = str(uuid.uuid4())
            profile = {**BASE_PROFILE, "skills": [{"name": "Data", "keywords": ["Rust"]}]}
            content = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
            staged = data_dir / "imports" / "staging" / scan_id / "0000.json"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
            import_canonical_profile_source(
                data_dir,
                {
                    "scan_id": scan_id,
                    "managed_relative_path": f"imports/staging/{scan_id}/0000.json",
                    "display_name": "canonical.json",
                    "raw_sha256": hashlib.sha256(content).hexdigest(),
                    "raw_bytes": len(content),
                },
            )
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            self.assertEqual(page["total_items"], 0)
            self.assertEqual(initialize_vault(data_dir).review_inbox_items, 0)

    def test_review_rows_and_events_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            item = list_profile_review_items(data_dir, scope="inbox", offset=0)["items"][0]
            transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=str(uuid.uuid4()),
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )
            database_path = initialize_vault(data_dir).database_path
            with closing(sqlite3.connect(database_path)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_review_items SET candidate_kind = 'skills' WHERE id = ?",
                        (item["id"],),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_review_item_events")

    def test_rpc_list_and_transition_use_strict_parameter_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            listed = _handle_request(
                data_dir,
                {
                    "protocol_version": 1,
                    "id": "list-review",
                    "method": "review.inbox.list",
                    "params": {"scope": "inbox", "offset": 0},
                },
            )
            self.assertTrue(listed["ok"])
            item = listed["result"]["items"][0]
            transitioned = _handle_request(
                data_dir,
                {
                    "protocol_version": 1,
                    "id": "defer-review",
                    "method": "review.inbox.transition",
                    "params": {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "action": "defer",
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                    },
                },
            )
            self.assertTrue(transitioned["ok"])
            self.assertEqual(transitioned["result"]["state"], "deferred")
            invalid = _handle_request(
                data_dir,
                {
                    "protocol_version": 1,
                    "id": "invalid-list",
                    "method": "review.inbox.list",
                    "params": {"scope": "inbox", "offset": 0, "limit": 100},
                },
            )
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["error"]["code"], "invalid_params")

    def test_list_is_fixed_paged_with_stable_aggregate_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, BASE_PROFILE, source="test")
            params = self._stage_json(
                data_dir,
                {
                    "skills": [
                        {"name": f"Skill group {index}", "keywords": [f"Skill {index}"]}
                        for index in range(12)
                    ]
                },
            )
            preview = preview_profile_sources(data_dir, params)
            self.assertEqual(preview["review_candidate_count"], 12)
            commit_profile_sources(data_dir, params["scan_id"])
            first = list_profile_review_items(data_dir, scope="inbox", offset=0)
            self.assertEqual(first["limit"], 10)
            self.assertEqual(first["total_items"], 12)
            self.assertEqual(first["next_offset"], 10)
            self.assertEqual(len(first["items"]), 10)
            second = list_profile_review_items(data_dir, scope="inbox", offset=10)
            self.assertEqual(second["total_items"], 12)
            self.assertIsNone(second["next_offset"])
            self.assertEqual(len(second["items"]), 2)
            self.assertEqual(first["counts"], second["counts"])
            self.assertEqual(first["counts"], {"inbox": 12, "deferred": 0, "history": 0})

    def test_same_raw_hash_distinct_extractions_keep_exact_evidence_identity(self) -> None:
        raw_sha = "a" * 64
        education_text = (
            "## Education\n"
            "Institution: Oxford\n"
            "Degree: Certificate\n"
            "Area: Computing\n"
        )
        skills_text = "## Skills\nSkills: COBOL, Compilers\n"
        sources = [
            {
                "display_name": "same-bytes.txt",
                "media_type": "text/plain",
                "sha256": raw_sha,
                "text": education_text,
                "origin_ordinal": 0,
            },
            {
                "display_name": "same-bytes.csv",
                "media_type": "text/csv",
                "sha256": raw_sha,
                "text": skills_text,
                "origin_ordinal": 1,
            },
        ]
        draft, _report = synthesize_canonical_profile(
            existing_profile=BASE_PROFILE,
            sources=sources,
        )
        source_manifest = draft["meta"]["source_ingest"]["sources"]
        self.assertEqual(len({source["source_id"] for source in source_manifest}), 2)
        _profile, candidates = partition_review_candidates(
            existing_profile=BASE_PROFILE,
            draft_profile=draft,
            synthesis_sources=sources,
        )
        by_kind = {candidate["candidate_kind"]: candidate for candidate in candidates}
        self.assertEqual(by_kind["education"]["evidence"][0]["source_ordinal"], 0)
        self.assertIn("Oxford", by_kind["education"]["evidence"][0]["excerpt"])
        self.assertEqual(by_kind["skills"]["evidence"][0]["source_ordinal"], 1)
        self.assertIn("COBOL", by_kind["skills"]["evidence"][0]["excerpt"])

    def test_workspace_reset_returns_to_pristine_review_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
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
            result = reset_workspace(
                data_dir,
                {"request_id": str(uuid.uuid4()), "expected": expected},
            ).to_dict()
            self.assertEqual(result["review_inbox_items"], 0)
            self.assertEqual(result["review_deferred_items"], 0)
            self.assertEqual(result["review_history_items"], 0)
            pristine = initialize_vault(data_dir)
            self.assertEqual(pristine.review_inbox_items, 0)
            self.assertEqual(pristine.review_deferred_items, 0)
            self.assertEqual(pristine.review_history_items, 0)
            with closing(sqlite3.connect(pristine.database_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM profile_review_items").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_item_events"
                    ).fetchone()[0],
                    0,
                )

    def test_review_transition_invalidates_a_confirmed_reset_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
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
            item = list_profile_review_items(data_dir, scope="inbox", offset=0)[
                "items"
            ][0]
            transition_profile_review_item(
                data_dir,
                review_item_id=item["id"],
                request_id=str(uuid.uuid4()),
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )

            with self.assertRaises(VaultError) as conflict:
                reset_workspace(
                    data_dir,
                    {"request_id": str(uuid.uuid4()), "expected": expected},
                )

            self.assertEqual(conflict.exception.code, "workspace_reset_conflict")
            post_conflict = initialize_vault(data_dir)
            self.assertEqual(post_conflict.review_inbox_items, 5)
            self.assertEqual(post_conflict.review_deferred_items, 1)
            self.assertEqual(post_conflict.review_history_items, 0)
            self.assertEqual(post_conflict.profile_versions, status.profile_versions)

    def test_apply_original_and_edited_candidates_are_atomic_and_retry_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            item = next(
                value
                for value in page["items"]
                if value["candidate_kind"] == "projects"
                and value["operation_kind"] == "add"
            )
            candidate = {**item["candidate"], "name": "Edited New Engine"}
            request_id = str(uuid.uuid4())
            params = {
                "review_item_id": item["id"],
                "request_id": request_id,
                "expected_state": "inbox",
                "expected_state_revision": item["state_revision"],
                "expected_parent_profile_version_id": page[
                    "current_profile_version_id"
                ],
                "candidate": candidate,
            }
            before_status = initialize_vault(data_dir)
            receipt = apply_profile_review_item(data_dir, params)
            self.assertTrue(receipt["created"])
            self.assertEqual(receipt["previous_state"], "inbox")
            self.assertEqual(receipt["state"], "applied")
            self.assertEqual(receipt["state_revision"], 1)
            self.assertEqual(receipt["parent_profile_version_id"], page["current_profile_version_id"])
            self.assertEqual(receipt["candidate_kind"], "projects")
            self.assertEqual(receipt["operation_kind"], "add")
            self.assertEqual(receipt["changed_fields"], ["name", "description"])
            self.assertEqual(initialize_vault(data_dir).profile_versions, before_status.profile_versions + 1)
            current = latest_profile(data_dir)
            self.assertEqual(current["id"], receipt["profile_version_id"])
            self.assertEqual(
                current["profile"]["projects"][receipt["entry_index"]]["name"],
                "Edited New Engine",
            )

            retry = apply_profile_review_item(data_dir, params)
            self.assertFalse(retry["created"])
            self.assertEqual(
                {key: value for key, value in retry.items() if key != "created"},
                {key: value for key, value in receipt.items() if key != "created"},
            )
            self.assertEqual(initialize_vault(data_dir).profile_versions, before_status.profile_versions + 1)
            history = list_profile_review_items(data_dir, scope="history", offset=0)
            applied = next(value for value in history["items"] if value["id"] == item["id"])
            self.assertEqual(applied["state"], "applied")
            self.assertEqual(applied["state_revision"], 1)
            self.assertEqual(applied["candidate"], candidate)
            self.assertEqual(applied["title"], "Edited New Engine")
            self.assertEqual(history["counts"], {"inbox": 5, "deferred": 0, "history": 1})
            with closing(sqlite3.connect(before_status.database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_apply_receipts"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_item_events WHERE review_item_id = ?",
                        (item["id"],),
                    ).fetchone()[0],
                    0,
                )

    def test_apply_fails_closed_for_stale_state_profile_generation_and_request_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            state_dir = root / "state"
            self._create_items(state_dir)
            page = list_profile_review_items(state_dir, scope="inbox", offset=0)
            item = page["items"][0]
            transition_profile_review_item(
                state_dir,
                review_item_id=item["id"],
                request_id=str(uuid.uuid4()),
                action="defer",
                expected_state="inbox",
                expected_state_revision=0,
            )
            with self.assertRaises(VaultError) as stale_state:
                apply_profile_review_item(
                    state_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                    },
                )
            self.assertEqual(stale_state.exception.code, "review_item_state_conflict")

            profile_dir = root / "profile"
            self._create_items(profile_dir)
            page = list_profile_review_items(profile_dir, scope="inbox", offset=0)
            item = page["items"][0]
            changed = deepcopy(latest_profile(profile_dir)["profile"])
            changed["basics"]["label"] = "Changed elsewhere"
            save_profile(profile_dir, changed, source="local_edit")
            with self.assertRaises(VaultError) as stale_profile:
                apply_profile_review_item(
                    profile_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                    },
                )
            self.assertEqual(stale_profile.exception.code, "review_item_profile_conflict")

            generation_dir = root / "generation"
            self._create_items(generation_dir)
            page = list_profile_review_items(generation_dir, scope="inbox", offset=0)
            item = page["items"][0]
            reset_profile_source_retention(
                generation_dir,
                expected_generation=1,
                request_id=str(uuid.uuid4()),
            )
            with self.assertRaises(VaultError) as stale_generation:
                apply_profile_review_item(
                    generation_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                    },
                )
            self.assertEqual(stale_generation.exception.code, "review_item_stale_generation")

            reuse_dir = root / "reuse"
            self._create_items(reuse_dir)
            page = list_profile_review_items(reuse_dir, scope="inbox", offset=0)
            item = next(value for value in page["items"] if value["operation_kind"] == "add")
            request_id = str(uuid.uuid4())
            params = {
                "review_item_id": item["id"],
                "request_id": request_id,
                "expected_state": "inbox",
                "expected_state_revision": 0,
                "expected_parent_profile_version_id": page["current_profile_version_id"],
                "candidate": item["candidate"],
            }
            receipt = apply_profile_review_item(reuse_dir, params)
            changed_candidate = deepcopy(item["candidate"])
            identity_field = "institution" if item["candidate_kind"] == "education" else "name"
            changed_candidate[identity_field] += " changed"
            with self.assertRaises(VaultError) as reused:
                apply_profile_review_item(
                    reuse_dir, {**params, "candidate": changed_candidate}
                )
            self.assertEqual(reused.exception.code, "review_item_apply_request_conflict")
            exact_retry = apply_profile_review_item(reuse_dir, params)
            self.assertFalse(exact_retry["created"])
            self.assertEqual(exact_retry["profile_version_id"], receipt["profile_version_id"])

    def test_apply_rejects_invalid_noop_target_conflict_and_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            invalid_dir = root / "invalid"
            self._create_items(invalid_dir)
            page = list_profile_review_items(invalid_dir, scope="inbox", offset=0)
            item = page["items"][0]
            invalid_candidate = deepcopy(item["candidate"])
            invalid_candidate.pop(next(iter(invalid_candidate)))
            with self.assertRaises(ValueError):
                apply_profile_review_item(
                    invalid_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": invalid_candidate,
                    },
                )

            noop_dir = root / "noop"
            self._create_items(noop_dir)
            page = list_profile_review_items(noop_dir, scope="inbox", offset=0)
            item = next(
                value
                for value in page["items"]
                if value["candidate_kind"] == "projects"
                and value["operation_kind"] == "update"
            )
            no_change = {
                "name": "Existing Engine",
                "description": "Original description.",
                "url": None,
                "start_date": None,
                "end_date": None,
                "highlights": [],
                "keywords": [],
            }
            with self.assertRaises(VaultError) as noop:
                apply_profile_review_item(
                    noop_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": no_change,
                    },
                )
            self.assertEqual(noop.exception.code, "review_item_apply_noop")
            duplicate_item = next(
                value
                for value in page["items"]
                if value["candidate_kind"] == "projects"
                and value["operation_kind"] == "add"
            )
            duplicate_candidate = {
                "name": "Existing Engine",
                "description": "Original description.",
                "url": None,
                "start_date": None,
                "end_date": None,
                "highlights": [],
                "keywords": [],
            }
            with self.assertRaises(VaultError) as duplicate:
                apply_profile_review_item(
                    noop_dir,
                    {
                        "review_item_id": duplicate_item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": duplicate_candidate,
                    },
                )
            self.assertEqual(duplicate.exception.code, "review_item_apply_noop")

            target_dir = root / "target"
            self._create_items(target_dir)
            page = list_profile_review_items(target_dir, scope="inbox", offset=0)
            item = next(value for value in page["items"] if value["operation_kind"] == "update")
            changed = deepcopy(latest_profile(target_dir)["profile"])
            section = item["candidate_kind"]
            changed[section][0]["_changed_elsewhere"] = True
            save_profile(target_dir, changed, source="local_edit")
            refreshed = list_profile_review_items(target_dir, scope="inbox", offset=0)
            with self.assertRaises(VaultError) as target_conflict:
                apply_profile_review_item(
                    target_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": refreshed[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                    },
                )
            self.assertEqual(target_conflict.exception.code, "review_item_apply_conflict")

            evidence_dir = root / "evidence"
            self._create_items(evidence_dir)
            page = list_profile_review_items(evidence_dir, scope="inbox", offset=0)
            item = page["items"][0]
            status = initialize_vault(evidence_dir)
            with closing(sqlite3.connect(status.database_path)) as connection:
                relative_path = connection.execute(
                    """
                    SELECT source.relative_path
                    FROM profile_review_items AS review
                    JOIN profile_version_sources AS link
                      ON link.import_id = review.import_id
                     AND link.ordinal = review.primary_source_ordinal
                    JOIN sources AS source ON source.id = link.source_id
                    WHERE review.id = ?
                    """,
                    (item["id"],),
                ).fetchone()[0]
            (evidence_dir / relative_path).unlink()
            with self.assertRaises(VaultError) as evidence_loss:
                apply_profile_review_item(
                    evidence_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                    },
                )
            self.assertEqual(evidence_loss.exception.code, "review_item_evidence_invalid")
            self.assertEqual(initialize_vault(evidence_dir).profile_versions, 2)

    def test_concurrent_exact_apply_creates_one_profile_version_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            item = next(value for value in page["items"] if value["operation_kind"] == "add")
            params = {
                "review_item_id": item["id"],
                "request_id": str(uuid.uuid4()),
                "expected_state": "inbox",
                "expected_state_revision": 0,
                "expected_parent_profile_version_id": page["current_profile_version_id"],
                "candidate": item["candidate"],
            }

            def apply_once(_index: int) -> dict[str, Any]:
                return apply_profile_review_item(data_dir, params)

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(apply_once, range(4)))
            self.assertEqual(sum(result["created"] for result in results), 1)
            self.assertEqual({result["profile_version_id"] for result in results}, {results[0]["profile_version_id"]})
            status = initialize_vault(data_dir)
            self.assertEqual(status.profile_versions, 3)
            with closing(sqlite3.connect(status.database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_apply_receipts"
                    ).fetchone()[0],
                    1,
                )

    def test_apply_update_preserves_target_unknown_fields_and_retry_survives_later_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            base = deepcopy(BASE_PROFILE)
            base["projects"][0]["target_private"] = {"keep": True}
            followup = deepcopy(FOLLOWUP_PROFILE)
            followup["projects"][0]["source_private"] = {"new": True}
            save_profile(data_dir, base, source="test")
            staged = self._stage_json(data_dir, followup)
            preview_profile_sources(data_dir, staged)
            commit_profile_sources(data_dir, staged["scan_id"])
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            item = next(
                value
                for value in page["items"]
                if value["candidate_kind"] == "projects"
                and value["operation_kind"] == "update"
            )
            edited = deepcopy(item["candidate"])
            edited["description"] = "Edited before applying."
            params = {
                "review_item_id": item["id"],
                "request_id": str(uuid.uuid4()),
                "expected_state": "inbox",
                "expected_state_revision": 0,
                "expected_parent_profile_version_id": page["current_profile_version_id"],
                "candidate": edited,
            }
            receipt = apply_profile_review_item(data_dir, params)
            self.assertEqual(receipt["operation_kind"], "update")
            self.assertEqual(receipt["entry_index"], 0)
            self.assertEqual(receipt["changed_fields"], ["description", "keywords"])
            applied_entry = latest_profile(data_dir)["profile"]["projects"][0]
            self.assertEqual(applied_entry["description"], "Edited before applying.")
            self.assertEqual(applied_entry["keywords"], ["Algorithms"])
            self.assertEqual(applied_entry["target_private"], {"keep": True})
            self.assertEqual(applied_entry["source_private"], {"new": True})

            later = deepcopy(latest_profile(data_dir)["profile"])
            later["basics"]["label"] = "Advanced after apply"
            later_version = save_profile(data_dir, later, source="local_edit")
            retry = apply_profile_review_item(data_dir, params)
            self.assertFalse(retry["created"])
            self.assertEqual(retry["profile_version_id"], receipt["profile_version_id"])
            self.assertEqual(latest_profile(data_dir)["id"], later_version.id)

    def test_apply_add_preserves_unexposed_proposal_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, BASE_PROFILE, source="test")
            staged = self._stage_json(
                data_dir,
                {
                    "projects": [
                        {
                            "name": "Private provenance project",
                            "description": "Visible source description.",
                            "source_private": {"retained": True, "ordinal": 4},
                        }
                    ]
                },
            )
            preview_profile_sources(data_dir, staged)
            commit_profile_sources(data_dir, staged["scan_id"])
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            item = page["items"][0]
            self.assertNotIn("source_private", item["candidate"])
            edited = deepcopy(item["candidate"])
            edited["description"] = "User-edited description."
            receipt = apply_profile_review_item(
                data_dir,
                {
                    "review_item_id": item["id"],
                    "request_id": str(uuid.uuid4()),
                    "expected_state": "inbox",
                    "expected_state_revision": 0,
                    "expected_parent_profile_version_id": page[
                        "current_profile_version_id"
                    ],
                    "candidate": edited,
                },
            )
            entry = latest_profile(data_dir)["profile"]["projects"][
                receipt["entry_index"]
            ]
            self.assertEqual(entry["description"], "User-edited description.")
            self.assertEqual(
                entry["source_private"], {"retained": True, "ordinal": 4}
            )

    def test_apply_receipts_are_immutable_and_workspace_reset_clears_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            item = next(value for value in page["items"] if value["operation_kind"] == "add")
            apply_profile_review_item(
                data_dir,
                {
                    "review_item_id": item["id"],
                    "request_id": str(uuid.uuid4()),
                    "expected_state": "inbox",
                    "expected_state_revision": 0,
                    "expected_parent_profile_version_id": page[
                        "current_profile_version_id"
                    ],
                    "candidate": item["candidate"],
                },
            )
            status = initialize_vault(data_dir)
            with closing(sqlite3.connect(status.database_path)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE profile_review_apply_receipts SET entry_index = 2"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM profile_review_apply_receipts")
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
            reset_workspace(
                data_dir, {"request_id": str(uuid.uuid4()), "expected": expected}
            )
            pristine = initialize_vault(data_dir)
            with closing(sqlite3.connect(pristine.database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_apply_receipts"
                    ).fetchone()[0],
                    0,
                )

    def test_apply_detects_tampered_retained_source_and_rpc_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            item = page["items"][0]
            response = _handle_request(
                data_dir,
                {
                    "protocol_version": 1,
                    "id": "apply-review",
                    "method": "review.inbox.apply",
                    "params": {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                        "unsupported": True,
                    },
                },
            )
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "invalid_params")

            status = initialize_vault(data_dir)
            with closing(sqlite3.connect(status.database_path)) as connection:
                relative_path = connection.execute(
                    """
                    SELECT source.relative_path
                    FROM profile_review_items AS review
                    JOIN profile_version_sources AS link
                      ON link.import_id = review.import_id
                     AND link.ordinal = review.primary_source_ordinal
                    JOIN sources AS source ON source.id = link.source_id
                    WHERE review.id = ?
                    """,
                    (item["id"],),
                ).fetchone()[0]
            source_path = data_dir / relative_path
            original = source_path.read_bytes()
            source_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            with self.assertRaises(VaultError) as tampered:
                apply_profile_review_item(
                    data_dir,
                    {
                        "review_item_id": item["id"],
                        "request_id": str(uuid.uuid4()),
                        "expected_state": "inbox",
                        "expected_state_revision": 0,
                        "expected_parent_profile_version_id": page[
                            "current_profile_version_id"
                        ],
                        "candidate": item["candidate"],
                    },
                )
            self.assertEqual(tampered.exception.code, "review_item_evidence_invalid")

    def test_schema_sixteen_upgrade_preserves_pending_review_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._create_items(data_dir)
            database_path = Path(initialize_vault(data_dir).database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                remove_schema_nineteen(connection)
                external_triggers = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'trigger'
                      AND sql LIKE '%profile_review_apply_receipts%'
                    """
                ).fetchall()
                for (trigger_name,) in external_triggers:
                    connection.execute(f'DROP TRIGGER "{trigger_name}"')
                connection.execute("DROP TABLE profile_review_apply_receipts")
                connection.execute("DROP TABLE source_review_events")
                connection.execute("DROP TABLE source_review_receipts")
                connection.execute("DROP TABLE source_review_items")
                connection.execute("ALTER TABLE profile_source_scans DROP COLUMN source_review_json")
                connection.execute("DELETE FROM schema_migrations WHERE version = 18")
                connection.execute("DELETE FROM schema_migrations WHERE version = 17")
                connection.execute("PRAGMA user_version = 16")
                connection.commit()

            upgraded = initialize_vault(data_dir)
            self.assertEqual(upgraded.schema_version, 19)
            self.assertEqual(upgraded.profile_versions, 2)
            self.assertEqual(upgraded.review_inbox_items, 6)
            page = list_profile_review_items(data_dir, scope="inbox", offset=0)
            self.assertEqual(page["total_items"], 6)
            self.assertTrue(all(item["state"] == "inbox" for item in page["items"]))
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM profile_review_apply_receipts"
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
