# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvgnome_engine.cli import _handle_request
from cvgnome_engine.memories import get_memory, list_memories, propose_memory_project, save_memory
from cvgnome_engine.review_inbox import apply_profile_review_item, list_profile_review_items, transition_profile_review_item
from cvgnome_engine.storage import VaultError, initialize_vault, latest_profile, reset_profile_source_retention, save_profile
from cvgnome_engine.workspace_reset import RESET_EXPECTED_FIELDS, reset_workspace
import cvgnome_engine.storage as storage
import cvgnome_engine.workspace_reset as reset_module
from migration_fixtures import remove_schema_nineteen

BASE = {"basics": {"name": "Ada Lovelace", "label": "Engineer"},
        "meta": {"private_note": "Never copy this into a memory response"}}
STORY = "  In spring (perhaps 2021), I rebuilt the handoff.\r\n\tThe savings might have been 20%, but I need to verify that.  "


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        save_profile(self.root, BASE, source="test")

    def capture(self, **changes):
        return {"request_id": str(uuid.uuid4()),
                "expected_parent_profile_version_id": latest_profile(self.root)["id"],
                "expected_retention_generation": initialize_vault(self.root).source_retention_generation,
                "title": "Handoff story", "narrative": STORY, **changes}

    def proposal(self, memory_id, **changes):
        return {"request_id": str(uuid.uuid4()), "memory_id": memory_id,
                "expected_parent_profile_version_id": latest_profile(self.root)["id"],
                "expected_retention_generation": initialize_vault(self.root).source_retention_generation,
                "name": "Team handoff", "description": "Rebuilt the team handoff process.", **changes}

    def page(self, scope="current", offset=0, limit=10):
        return list_memories(self.root, {"scope": scope, "offset": offset, "limit": limit})

    def apply_request(self, item):
        return {"request_id": str(uuid.uuid4()), "review_item_id": item["id"],
                "expected_parent_profile_version_id": latest_profile(self.root)["id"],
                "expected_state": "inbox", "expected_state_revision": item["state_revision"],
                "candidate": item["candidate"]}

    def reset_request(self):
        status = initialize_vault(self.root).to_dict()
        return {"request_id": str(uuid.uuid4()), "expected": {key: status[key] for key in RESET_EXPECTED_FIELDS}}

    def test_capture_preserves_exact_original_without_profile_or_import_mutation(self):
        profile = latest_profile(self.root)
        request = self.capture(title="  A career story  ")
        saved = save_memory(self.root, request)
        detail = get_memory(self.root, {"memory_id": saved["memory_id"]})
        self.assertEqual(detail["narrative"], STORY)
        self.assertEqual(detail["title"], "A career story")
        self.assertEqual(detail["character_count"], len(STORY))
        self.assertEqual(latest_profile(self.root), profile)
        status = initialize_vault(self.root)
        self.assertEqual((status.memory_count, status.profile_versions, status.source_imports, status.review_inbox_items), (1, 1, 0, 0))
        self.assertEqual(set(saved), {"request_id", "memory_id", "created", "created_at_ms", "profile_version_id", "retention_generation"})
        self.assertNotIn("Never copy", json.dumps(detail))
        self.assertNotIn("checksum", json.dumps(detail))

    def test_save_exact_concurrent_retry_and_conflict(self):
        request = self.capture()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: save_memory(self.root, request), range(4)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["memory_id"] for result in results}), 1)
        with self.assertRaises(VaultError) as error:
            save_memory(self.root, {**request, "narrative": "Different story"})
        self.assertEqual(error.exception.code, "memory_request_conflict")

    def test_explicit_project_draft_has_no_invented_facts_and_apply_is_review_gated(self):
        before = latest_profile(self.root)
        saved = save_memory(self.root, self.capture())
        request = self.proposal(saved["memory_id"])
        proposal = propose_memory_project(self.root, request)
        self.assertTrue(proposal["created"])
        self.assertFalse(propose_memory_project(self.root, request)["created"])
        self.assertEqual(latest_profile(self.root), before)
        page = list_profile_review_items(self.root, scope="inbox", offset=0)
        item = page["items"][0]
        self.assertEqual(item["id"], proposal["review_item_id"])
        self.assertEqual(item["candidate"], {"name": "Team handoff", "description": request["description"],
                                              "url": None, "start_date": None, "end_date": None, "highlights": [], "keywords": []})
        self.assertEqual(item["evidence"][0]["location_label"], "Original memory")
        self.assertIn("perhaps 2021", item["evidence"][0]["excerpt"])
        self.assertNotIn("20%", json.dumps(item["candidate"]))
        apply_request = self.apply_request(item)
        apply_request["candidate"]["description"] = "Reviewed: rebuilt the team handoff."
        receipt = apply_profile_review_item(self.root, apply_request)
        self.assertEqual(receipt["version_number"], 2)
        self.assertFalse(apply_profile_review_item(self.root, apply_request)["created"])
        self.assertFalse(propose_memory_project(self.root, request)["created"])
        self.assertEqual(latest_profile(self.root)["profile"]["projects"][0]["description"], apply_request["candidate"]["description"])
        self.assertEqual(get_memory(self.root, {"memory_id": saved["memory_id"]})["narrative"], STORY)
        self.assertEqual(list_profile_review_items(self.root, scope="history", offset=0)["items"][0]["state"], "applied")

    def test_each_memory_has_one_proposal_with_concurrent_exact_retry(self):
        saved = save_memory(self.root, self.capture())
        request = self.proposal(saved["memory_id"])
        with ThreadPoolExecutor(max_workers=3) as executor:
            receipts = list(executor.map(lambda _: propose_memory_project(self.root, request), range(3)))
        self.assertEqual(sum(row["created"] for row in receipts), 1)
        with self.assertRaises(VaultError) as duplicate:
            propose_memory_project(self.root, {**request, "request_id": str(uuid.uuid4())})
        self.assertEqual(duplicate.exception.code, "memory_project_exists")
        with self.assertRaises(VaultError) as conflict:
            propose_memory_project(self.root, {**request, "description": "Different"})
        self.assertEqual(conflict.exception.code, "memory_request_conflict")

    def test_defer_reject_reopen_use_existing_review_state_and_original_survives(self):
        saved = save_memory(self.root, self.capture())
        proposal = propose_memory_project(self.root, self.proposal(saved["memory_id"]))
        state, revision = "inbox", 0
        for action, expected in (("defer", "deferred"), ("reject", "rejected"), ("reopen", "inbox")):
            request = {"review_item_id": proposal["review_item_id"], "request_id": str(uuid.uuid4()),
                       "action": action, "expected_state": state, "expected_state_revision": revision}
            result = transition_profile_review_item(self.root, **request)
            self.assertEqual(result["state"], expected)
            self.assertFalse(transition_profile_review_item(self.root, **request)["created"])
            state, revision = result["state"], result["state_revision"]
        self.assertEqual(get_memory(self.root, {"memory_id": saved["memory_id"]})["narrative"], STORY)
        self.assertEqual(initialize_vault(self.root).profile_versions, 1)

    def test_generation_reset_archives_memories_and_suggestions_without_deleting_original(self):
        save_request = self.capture()
        saved = save_memory(self.root, save_request)
        proposal_request = self.proposal(saved["memory_id"])
        proposal = propose_memory_project(self.root, proposal_request)
        reset_profile_source_retention(self.root, expected_generation=1, request_id=str(uuid.uuid4()))
        self.assertEqual(self.page()["total_items"], 0)
        self.assertEqual(self.page("history")["items"][0]["id"], saved["memory_id"])
        detail = get_memory(self.root, {"memory_id": saved["memory_id"]})
        self.assertTrue(detail["is_previous_import_set"])
        self.assertEqual(detail["narrative"], STORY)
        self.assertFalse(save_memory(self.root, save_request)["created"])
        self.assertFalse(propose_memory_project(self.root, proposal_request)["created"])
        with self.assertRaises(VaultError) as archived:
            propose_memory_project(self.root, self.proposal(saved["memory_id"]))
        self.assertEqual(archived.exception.code, "memory_stale_generation")
        item = list_profile_review_items(self.root, scope="history", offset=0)["items"][0]
        self.assertEqual(item["id"], proposal["review_item_id"])
        with self.assertRaises(VaultError) as apply_archived:
            apply_profile_review_item(self.root, self.apply_request(item))
        self.assertEqual(apply_archived.exception.code, "review_item_stale_generation")

    def test_stale_profile_and_retention_pins_fail_without_new_memory(self):
        request = self.capture()
        save_profile(self.root, {"basics": {"name": "Ada", "label": "Updated"}}, source="test")
        with self.assertRaises(VaultError) as parent:
            save_memory(self.root, request)
        self.assertEqual(parent.exception.code, "memory_profile_conflict")
        request = self.capture()
        reset_profile_source_retention(self.root, expected_generation=1, request_id=str(uuid.uuid4()))
        with self.assertRaises(VaultError) as generation:
            save_memory(self.root, request)
        self.assertEqual(generation.exception.code, "memory_stale_generation")
        self.assertEqual(initialize_vault(self.root).memory_count, 0)

    def test_bounded_unicode_and_original_whitespace_policy(self):
        for invalid in ("", " \r\n\t ", "x" * 12001, "bad\u202estory", "bad\x00story", "\ue000", "\ud800"):
            with self.subTest(invalid=repr(invalid[:15])), self.assertRaises(ValueError):
                save_memory(self.root, self.capture(narrative=invalid))
        saved = save_memory(self.root, self.capture(narrative="🧠" * 12000, title=None))
        detail = get_memory(self.root, {"memory_id": saved["memory_id"]})
        self.assertEqual(detail["character_count"], 12000)
        self.assertLess(len(json.dumps(detail, ensure_ascii=False).encode()), 52 * 1024)
        with self.assertRaises(ValueError):
            save_memory(self.root, self.capture(title="x" * 161))
        with self.assertRaises(ValueError):
            propose_memory_project(self.root, self.proposal(saved["memory_id"], description="x" * 601))

    def test_paging_is_bounded_and_does_not_expose_original_or_internal_lineage(self):
        for index in range(12):
            save_memory(self.root, self.capture(title=f"Story {index}", narrative="A" * 1000))
        page = self.page(limit=3)
        self.assertEqual((page["total_items"], page["next_offset"], len(page["items"])), (12, 3, 3))
        self.assertTrue(all(len(row["excerpt"]) == 240 for row in page["items"]))
        self.assertNotIn("narrative", json.dumps(page))
        self.assertNotIn("checksum", json.dumps(page))
        self.assertEqual(self.page(offset=9)["next_offset"], None)
        for params in ({"scope": "current", "limit": True, "offset": 0},
                       {"scope": "current", "limit": 11, "offset": 0},
                       {"scope": "current", "limit": 10, "offset": 10001}):
            with self.assertRaises(ValueError):
                list_memories(self.root, params)

    def test_immutable_memory_and_receipt_and_candidate_triggers(self):
        saved = save_memory(self.root, self.capture())
        propose_memory_project(self.root, self.proposal(saved["memory_id"]))
        with closing(sqlite3.connect(self.root / "cvgnome.sqlite3")) as connection:
            for sql in ("UPDATE memories SET narrative='changed'", "DELETE FROM memories",
                        "DELETE FROM memory_project_receipts", "UPDATE memory_project_receipts SET created_at_ms=1",
                        "UPDATE profile_review_items SET memory_id=NULL", "DELETE FROM profile_review_items"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)
                connection.rollback()

    def test_tampered_original_blocks_detail_proposal_and_apply(self):
        saved = save_memory(self.root, self.capture())
        request = self.proposal(saved["memory_id"])
        propose_memory_project(self.root, request)
        item = list_profile_review_items(self.root, scope="inbox", offset=0)["items"][0]
        with closing(sqlite3.connect(self.root / "cvgnome.sqlite3")) as connection:
            connection.execute("DROP TRIGGER memories_no_update")
            connection.execute("UPDATE memories SET narrative='Forged original'")
            connection.commit()
        for operation in (lambda: get_memory(self.root, {"memory_id": saved["memory_id"]}),
                          lambda: propose_memory_project(self.root, request),
                          lambda: apply_profile_review_item(self.root, self.apply_request(item)),
                          lambda: list_profile_review_items(self.root, scope="inbox", offset=0)):
            with self.assertRaises(VaultError) as integrity:
                operation()
            self.assertEqual(integrity.exception.code, "vault_integrity_error")

    def test_workspace_reset_counts_guard_against_new_memories_and_clear_all_memory_tables(self):
        stale = self.reset_request()
        saved = save_memory(self.root, self.capture())
        with self.assertRaises(VaultError) as changed:
            reset_workspace(self.root, stale)
        self.assertEqual(changed.exception.code, "workspace_reset_conflict")
        propose_memory_project(self.root, self.proposal(saved["memory_id"]))
        request = self.reset_request()
        receipt = reset_workspace(self.root, request).to_dict()
        self.assertEqual(receipt["memory_count"], 0)
        self.assertFalse(reset_workspace(self.root, request).created)
        self.assertEqual(initialize_vault(self.root).memory_count, 0)
        self.assertEqual(self.page()["total_items"], 0)
        with self.assertRaises(VaultError):
            get_memory(self.root, {"memory_id": saved["memory_id"]})

    def test_legacy_v3_reset_receipt_and_pending_journal_recover_memory_count(self):
        request = self.reset_request()
        receipt = reset_workspace(self.root, request).to_dict()
        path = self.root / reset_module.RESET_RECEIPT_FILENAME
        legacy = json.loads(path.read_text())
        legacy["contract_version"] = 3
        legacy["expected"].pop("memory_count")
        legacy["result"].pop("memory_count")
        legacy["result"]["schema_version"] = 18
        path.write_text(json.dumps(legacy))
        retry = reset_workspace(self.root, request).to_dict()
        self.assertFalse(retry["created"])
        self.assertEqual(retry["reset_at_ms"], receipt["reset_at_ms"])
        self.assertEqual(retry["memory_count"], 0)
        save_profile(self.root, BASE, source="test")
        saved = save_memory(self.root, self.capture())
        journal = {"contract_version": 3, "phase": "prepared", "request_id": str(uuid.uuid4()),
                   "reset_at_ms": 1_700_000_000_000,
                   "expected": {key: initialize_vault(self.root).to_dict()[key] for key in reset_module.V3_RESET_EXPECTED_FIELDS}}
        reset_module._write_state_file(self.root / reset_module.RESET_JOURNAL_FILENAME, journal)
        self.assertEqual(initialize_vault(self.root).memory_count, 0)
        with self.assertRaises(VaultError):
            get_memory(self.root, {"memory_id": saved["memory_id"]})

    def test_rpc_contract_rejects_extras_and_has_no_provider_calls(self):
        request = self.capture()
        response = _handle_request(self.root, {"protocol_version": 1, "id": "memory", "method": "memory.save", "params": request})
        self.assertTrue(response["ok"], response)
        invalid = _handle_request(self.root, {"protocol_version": 1, "id": "memory", "method": "memory.save", "params": {**request, "api_key": "never-send"}})
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"]["code"], "invalid_params")
        self.assertNotIn("never-send", json.dumps(invalid))

    def test_schema18_populated_review_migration_preserves_apply_and_transition_retries(self):
        import test_review_inbox
        test_review_inbox.ReviewInboxTests()._create_items(self.root)
        page = list_profile_review_items(self.root, scope="inbox", offset=0)
        first, second, third = page["items"][:3]
        apply_request = self.apply_request(first)
        applied = apply_profile_review_item(self.root, apply_request)
        transitions = []
        for item, action in ((second, "defer"), (third, "reject")):
            transition = {"review_item_id": item["id"], "request_id": str(uuid.uuid4()), "action": action,
                          "expected_state": "inbox", "expected_state_revision": 0}
            transition_profile_review_item(self.root, **transition)
            transitions.append(transition)
        profile = latest_profile(self.root)
        with closing(sqlite3.connect(self.root / "cvgnome.sqlite3")) as connection:
            remove_schema_nineteen(connection)
            connection.commit()
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone())
            self.assertNotIn("memory_id", [row[1] for row in connection.execute("PRAGMA table_info(profile_review_items)")])
        status = initialize_vault(self.root)
        self.assertEqual(status.schema_version, 19)
        self.assertEqual(latest_profile(self.root), profile)
        retry = apply_profile_review_item(self.root, apply_request)
        self.assertFalse(retry["created"])
        self.assertEqual(retry["profile_version_id"], applied["profile_version_id"])
        for request in transitions:
            self.assertFalse(transition_profile_review_item(self.root, **request)["created"])
        self.assertEqual(status.review_deferred_items, 1)
        self.assertEqual(status.review_history_items, 2)
        self.assertEqual(status.memory_count, 0)
        with closing(storage._connect(self.root)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_schema19_upgrade_rolls_back_and_restores_foreign_keys(self):
        with closing(sqlite3.connect(self.root / "cvgnome.sqlite3")) as connection:
            remove_schema_nineteen(connection)
            connection.commit()
        with closing(storage._connect(self.root)) as connection:
            with patch.object(storage, "_verify_vault", side_effect=RuntimeError("injected verification failure")):
                with self.assertRaises(RuntimeError):
                    storage._apply_migrations(connection)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone())
        self.assertEqual(initialize_vault(self.root).schema_version, 19)


if __name__ == "__main__":
    unittest.main()
