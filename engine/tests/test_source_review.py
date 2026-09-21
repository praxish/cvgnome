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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvgnome_engine.cli import _handle_request
from cvgnome_engine.profile_sources import preview_profile_sources, commit_profile_sources, discard_profile_sources
from cvgnome_engine.profile_versions import update_profile_basics
from cvgnome_engine.source_review import apply_source_review_decisions, list_source_review_items
from cvgnome_engine.storage import initialize_vault, latest_profile, save_profile, reset_profile_source_retention, VaultError
from cvgnome_engine.workspace_reset import reset_workspace, RESET_EXPECTED_FIELDS
import cvgnome_engine.workspace_reset as reset_module

BASE = {"basics": {"name": "Ada Lovelace", "label": "Engineer", "email": "ada@example.test",
                   "private_marker": "must-preserve-hidden"},
        "work": [{"name": "Engine Works", "position": "Engineer", "summary": "Built engines."}],
        "meta": {"private_note": "must-not-display"}}


class SourceReviewTests(unittest.TestCase):
    def stage(self, data_dir, profiles):
        scan = str(uuid.uuid4())
        sources = []
        for ordinal, profile in enumerate(profiles):
            content = profile if isinstance(profile, bytes) else json.dumps(profile).encode()
            relative = f"imports/staging/{scan}/{ordinal:04d}.json"
            path = data_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            sources.append({"ordinal": ordinal, "managed_relative_path": relative,
                            "display_name": f"source-{ordinal}.json", "format": "json",
                            "byte_size": len(content), "checksum_sha256": hashlib.sha256(content).hexdigest()})
        return {"scan_id": scan, "sources": sources,
                "file_counts": {"discovered": len(sources), "staged": len(sources), "skipped": 0},
                "scan_issues": {}}

    def imported(self, data_dir, *, profiles=None, initial=True):
        if initial:
            save_profile(data_dir, BASE, source="test")
        incoming = deepcopy(BASE)
        incoming["basics"]["email"] = "source@example.test"
        incoming["basics"]["label"] = "Researcher"
        params = self.stage(data_dir, profiles or [incoming, incoming])
        preview = preview_profile_sources(data_dir, params)
        receipt = commit_profile_sources(data_dir, params["scan_id"])
        page = self.page(data_dir)
        return preview, receipt, page

    def page(self, data_dir, scope="inbox"):
        return list_source_review_items(data_dir, scope=scope, limit=10, offset=0)

    def request(self, page, selected):
        return {"request_id": str(uuid.uuid4()),
                "expected_parent_profile_version_id": page["current_profile_version_id"],
                "decisions": [{"item_id": item["id"], "expected_state": item["state"],
                               "expected_revision": item["state_revision"], "action": action}
                              for item, action in selected]}

    def test_import_checks_are_atomic_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview, receipt, page = self.imported(root)
            self.assertEqual(preview["source_review_count"], 3)
            self.assertEqual(receipt["source_review_count"], 3)
            self.assertEqual(page["counts"], {"inbox": 3, "deferred": 0, "history": 0})
            self.assertEqual(latest_profile(root)["profile"]["basics"]["email"], "ada@example.test")
            encoded = json.dumps(page)
            for forbidden in ("must-not-display", "must-preserve-hidden", "sha256", "source_ordinal", "imports/staging", "relative_path"):
                self.assertNotIn(forbidden, encoded)
            duplicate = next(item for item in page["items"] if item["kind"] == "duplicate_document")
            self.assertEqual(len(duplicate["evidence"]), 2)
            self.assertTrue(all(e["excerpt"] == "" for e in duplicate["evidence"]))
            self.assertEqual(commit_profile_sources(root, receipt["scan_id"]), receipt)
            self.assertEqual(initialize_vault(root).source_review_inbox_items, 3)

    def test_queued_changes_make_one_version_and_exact_concurrent_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            request = self.request(page, [(item, "use_source" if item["field"] else "acknowledge_duplicate") for item in page["items"]])
            before = initialize_vault(root)
            with ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(lambda _: apply_source_review_decisions(root, request), range(3)))
            self.assertEqual(sum(result["created"] for result in results), 1)
            self.assertEqual(len({result["profile_version_id"] for result in results}), 1)
            self.assertTrue(results[0]["profile_changed"])
            self.assertEqual(initialize_vault(root).profile_versions, before.profile_versions + 1)
            current = latest_profile(root)["profile"]
            self.assertEqual(current["basics"]["email"], "source@example.test")
            self.assertEqual(current["basics"]["label"], "Researcher")
            self.assertEqual(current["basics"]["private_marker"], "must-preserve-hidden")
            self.assertEqual(current["meta"]["private_note"], "must-not-display")
            self.assertEqual(self.page(root, "history")["counts"]["history"], 3)
            changed = deepcopy(request)
            changed["decisions"][0]["action"] = "defer"
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, changed)
            self.assertEqual(failure.exception.code, "source_review_request_conflict")

    def test_keep_and_organize_do_not_create_profile_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            before = initialize_vault(root).profile_versions
            item = next(item for item in page["items"] if item["field"] == "email")
            deferred = apply_source_review_decisions(root, self.request(page, [(item, "defer")]))
            self.assertFalse(deferred["profile_changed"])
            page = self.page(root, "deferred")
            self.assertEqual(page["items"][0]["state_revision"], 1)
            apply_source_review_decisions(root, self.request(page, [(page["items"][0], "reopen")]))
            page = self.page(root)
            item = next(item for item in page["items"] if item["field"] == "email")
            applied = apply_source_review_decisions(root, self.request(page, [(item, "keep_profile")]))
            self.assertFalse(applied["profile_changed"])
            self.assertEqual(initialize_vault(root).profile_versions, before)
            self.assertEqual(self.page(root, "history")["items"][0]["resolution"], "keep_profile")

    def test_one_stale_item_rejects_entire_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            items = [item for item in page["items"] if item["field"]]
            request = self.request(page, [(item, "use_source") for item in items])
            apply_source_review_decisions(root, self.request(page, [(items[-1], "defer")]))
            before = initialize_vault(root).profile_versions
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, request)
            self.assertEqual(failure.exception.code, "source_review_state_conflict")
            self.assertEqual(initialize_vault(root).profile_versions, before)
            self.assertEqual(latest_profile(root)["profile"]["basics"]["email"], "ada@example.test")
            self.assertEqual(self.page(root, "history")["total_items"], 0)

    def test_stale_parent_and_changed_target_reject_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            item = next(item for item in page["items"] if item["field"] == "email")
            request = self.request(page, [(item, "use_source")])
            update_profile_basics(root, {"request_id": str(uuid.uuid4()),
                "expected_parent_profile_version_id": page["current_profile_version_id"],
                "patch": {"email": "edited@example.test"}})
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, request)
            self.assertEqual(failure.exception.code, "source_review_profile_conflict")
            request["expected_parent_profile_version_id"] = latest_profile(root)["id"]
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, request)
            self.assertEqual(failure.exception.code, "source_review_field_conflict")

    def test_two_incoming_values_for_one_field_fail_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = deepcopy(BASE), deepcopy(BASE)
            a["basics"]["email"] = "one@example.test"
            b["basics"]["email"] = "two@example.test"
            _, _, page = self.imported(root, profiles=[a, b])
            items = [item for item in page["items"] if item["field"] == "email"]
            self.assertEqual(len(items), 2)
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, self.request(page, [(item, "use_source") for item in items]))
            self.assertEqual(failure.exception.code, "source_review_field_conflict")
            self.assertEqual(self.page(root)["counts"]["history"], 0)

    def test_bootstrap_conflict_compares_saved_baseline_and_discard_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = deepcopy(BASE)
            other["basics"]["email"] = "other@example.test"
            staged = self.stage(root, [BASE, other, other])
            preview = preview_profile_sources(root, staged)
            self.assertGreaterEqual(preview["source_review_count"], 2)
            self.assertEqual(initialize_vault(root).source_review_inbox_items, 0)
            discard_profile_sources(root, staged["scan_id"])
            self.assertEqual(initialize_vault(root).source_review_inbox_items, 0)
            _, _, page = self.imported(root, profiles=[BASE, other, other], initial=False)
            item = next(item for item in page["items"] if item["field"] == "email")
            self.assertEqual(item["previous_value"], latest_profile(root)["profile"]["basics"]["email"])
            apply_source_review_decisions(root, self.request(page, [(item, "use_source")]))
            self.assertEqual(latest_profile(root)["profile"]["basics"]["email"], "other@example.test")

    def test_bad_scalar_candidates_do_not_break_import_or_claim_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = deepcopy(BASE)
            bad["basics"].update({"email": "not an email", "url": "javascript:alert(1)", "name": "x" * 300})
            _, _, page = self.imported(root, profiles=[bad])
            self.assertFalse(any(item["field"] in {"email", "url", "name"} for item in page["items"]))
            self.assertFalse(any(item["kind"] == "duplicate_document" for item in page["items"]))

    def test_import_reset_archives_items_but_exact_retry_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            request = self.request(page, [(page["items"][0], "defer")])
            first = apply_source_review_decisions(root, request)
            reset_profile_source_retention(root, expected_generation=1, request_id=str(uuid.uuid4()))
            retry = apply_source_review_decisions(root, request)
            self.assertFalse(retry["created"])
            self.assertEqual({**retry, "created": True}, first)
            self.assertEqual(self.page(root)["total_items"], 0)
            history = self.page(root, "history")
            self.assertTrue(all(item["is_previous_import_set"] for item in history["items"]))
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, self.request(history, [(history["items"][1], "defer")]))
            self.assertEqual(failure.exception.code, "source_review_generation_conflict")

    def test_tampered_retained_file_rejects_whole_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            with closing(sqlite3.connect(root / "cvgnome.sqlite3")) as connection:
                relative = connection.execute("SELECT relative_path FROM sources LIMIT 1").fetchone()[0]
            (root / relative).write_bytes(b"tampered")
            item = next(item for item in page["items"] if item["field"] == "email")
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, self.request(page, [(item, "use_source")]))
            self.assertEqual(failure.exception.code, "source_review_evidence_missing")
            self.assertEqual(self.page(root)["counts"]["history"], 0)

    def test_workspace_reset_clears_checks_preserves_spend_and_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.imported(root)
            (root / "provider-spend.json").write_text("sentinel")
            status = initialize_vault(root).to_dict()
            request = {"request_id": str(uuid.uuid4()), "expected": {key: status[key] for key in RESET_EXPECTED_FIELDS}}
            first = reset_workspace(root, request)
            self.assertEqual(first.source_review_inbox_items, 0)
            self.assertEqual((root / "provider-spend.json").read_text(), "sentinel")
            self.assertEqual(self.page(root)["counts"], {"inbox": 0, "deferred": 0, "history": 0})
            self.assertFalse(reset_workspace(root, request).created)

    def test_rpc_rejects_extra_fields_and_wrong_action_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for method, params in (("profile.source_review.list", {"scope": "inbox", "limit": 10, "offset": 0, "extra": True}),
                                   ("profile.source_review.apply", {"request_id": str(uuid.uuid4()), "expected_parent_profile_version_id": str(uuid.uuid4()), "decisions": []})):
                response = _handle_request(root, {"protocol_version": 1, "id": "test", "method": method, "params": params})
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_params")

    def test_source_records_and_decision_receipts_are_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            apply_source_review_decisions(root, self.request(page, [(page["items"][0], "defer")]))
            with closing(sqlite3.connect(root / "cvgnome.sqlite3")) as connection:
                for table in ("source_review_items", "source_review_receipts", "source_review_events"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(f"DELETE FROM {table}")

    def test_forged_scan_proposal_rejected_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_profile(root, BASE, source="test")
            incoming = deepcopy(BASE)
            incoming["basics"]["email"] = "source@example.test"
            params = self.stage(root, [incoming])
            preview_profile_sources(root, params)
            with closing(sqlite3.connect(root / "cvgnome.sqlite3")) as connection:
                checks = json.loads(connection.execute("SELECT source_review_json FROM profile_source_scans").fetchone()[0])
                checks[0]["proposed_value"] = "forged@example.test"
                checks[0]["evidence"][0]["excerpt"] = "forged@example.test"
                connection.execute("UPDATE profile_source_scans SET source_review_json=?", (json.dumps(checks),))
                connection.commit()
            with self.assertRaises(VaultError) as failure:
                commit_profile_sources(root, params["scan_id"])
            self.assertEqual(failure.exception.code, "vault_integrity_error")
            self.assertEqual(initialize_vault(root).profile_versions, 1)
            self.assertEqual(self.page(root)["total_items"], 0)

    def test_forged_material_with_recomputed_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            item = next(item for item in page["items"] if item["field"] == "email")
            with closing(sqlite3.connect(root / "cvgnome.sqlite3")) as connection:
                check = json.loads(connection.execute("SELECT material_json FROM source_review_items WHERE id=?", (item["id"],)).fetchone()[0])
                check["proposed_value"] = "forged@example.test"
                check["evidence"][0]["excerpt"] = "forged@example.test"
                encoded = json.dumps(check, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                connection.execute("DROP TRIGGER source_review_items_immutable_update")
                connection.execute("UPDATE source_review_items SET material_json=?, checksum_sha256=? WHERE id=?",
                                   (encoded, hashlib.sha256(encoded.encode()).hexdigest(), item["id"]))
                connection.commit()
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, self.request(page, [(item, "use_source")]))
            self.assertEqual(failure.exception.code, "vault_integrity_error")
            self.assertEqual(latest_profile(root)["profile"]["basics"]["email"], "ada@example.test")

    def test_replay_rejects_altered_output_even_with_valid_profile_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, page = self.imported(root)
            item = next(item for item in page["items"] if item["field"] == "email")
            request = self.request(page, [(item, "use_source")])
            receipt = apply_source_review_decisions(root, request)
            with closing(sqlite3.connect(root / "cvgnome.sqlite3")) as connection:
                triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='profile_versions'").fetchall()
                for (name,) in triggers:
                    connection.execute(f'DROP TRIGGER "{name}"')
                profile = latest_profile(root)["profile"]
                profile["basics"]["email"] = "forged@example.test"
                encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                connection.execute("UPDATE profile_versions SET canonical_json=?, checksum_sha256=? WHERE id=?",
                                   (encoded, hashlib.sha256(encoded.encode()).hexdigest(), receipt["profile_version_id"]))
                connection.commit()
            with self.assertRaises(VaultError) as failure:
                apply_source_review_decisions(root, request)
            self.assertEqual(failure.exception.code, "vault_integrity_error")

    def test_schema_seventeen_upgrade_preserves_profile_and_v2_reset_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_profile(root, BASE, source="test")
            before = latest_profile(root)
            from migration_fixtures import remove_schema_nineteen
            with closing(sqlite3.connect(root / "cvgnome.sqlite3")) as connection:
                remove_schema_nineteen(connection)
                connection.execute("DROP TABLE source_review_events")
                connection.execute("DROP TABLE source_review_receipts")
                connection.execute("DROP TABLE source_review_items")
                connection.execute("ALTER TABLE profile_source_scans DROP COLUMN source_review_json")
                connection.execute("DELETE FROM schema_migrations WHERE version=18")
                connection.execute("PRAGMA user_version=17")
                connection.commit()
            self.assertEqual(initialize_vault(root).schema_version, 19)
            self.assertEqual(latest_profile(root), before)
            self.assertEqual(self.page(root)["total_items"], 0)
            status = initialize_vault(root).to_dict()
            request = {"request_id": str(uuid.uuid4()), "expected": {key: status[key] for key in RESET_EXPECTED_FIELDS}}
            result = reset_workspace(root, request).to_dict()
            receipt_path = root / reset_module.RESET_RECEIPT_FILENAME
            old_receipt = json.loads(receipt_path.read_text())
            old_receipt["contract_version"] = 2
            old_receipt["expected"] = {key: value for key, value in old_receipt["expected"].items() if key in reset_module.V2_RESET_EXPECTED_FIELDS}
            old_receipt["result"] = {key: value for key, value in old_receipt["result"].items() if not key.startswith("source_review_") and key != "memory_count"}
            old_receipt["result"]["schema_version"] = 17
            receipt_path.write_text(json.dumps(old_receipt))
            retried = reset_workspace(root, request).to_dict()
            self.assertFalse(retried["created"])
            self.assertEqual(retried["source_review_history_items"], 0)
            self.assertEqual(retried["reset_at_ms"], result["reset_at_ms"])

    def test_old_v2_pending_reset_journal_recovers_to_current_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.imported(root)
            status = initialize_vault(root).to_dict()
            journal = {"contract_version": 2, "phase": "prepared", "request_id": str(uuid.uuid4()),
                       "reset_at_ms": 1_700_000_000_000,
                       "expected": {key: status[key] for key in reset_module.V2_RESET_EXPECTED_FIELDS}}
            reset_module._write_state_file(root / reset_module.RESET_JOURNAL_FILENAME, journal)
            current = initialize_vault(root)
            self.assertEqual(current.profile_versions, 0)
            self.assertEqual(current.source_review_inbox_items, 0)


if __name__ == "__main__":
    unittest.main()
