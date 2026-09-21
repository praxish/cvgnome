# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.opportunities import (  # noqa: E402
    MATCH_CONTRACT,
    TRACKER_STAGES,
    get_opportunity,
    list_opportunities,
    rematch_opportunity,
    save_and_match_opportunity,
    update_opportunity,
)
from cvgnome_engine.storage import VaultError, initialize_vault, save_profile  # noqa: E402


class OpportunityTests(unittest.TestCase):
    def _profile(self) -> dict[str, object]:
        return {
            "basics": {
                "name": "Ada Lovelace",
                "label": "Analytics Engineer",
                "summary": "Python analytics engineer building durable local systems.",
            },
            "skills": [
                {"name": "Data", "keywords": ["Python", "SQL", "SQLite"]},
            ],
            "work": [
                {
                    "name": "Analytical Engines",
                    "position": "Lead Engineer",
                    "highlights": [
                        "Built deterministic reporting workflows and local data pipelines."
                    ],
                }
            ],
            "meta": {
                "canonical_context": {
                    "work_preferences": "Kubernetes Rust quantum computing",
                    "impact_evidence": ["Led a Snowflake migration for local reporting."],
                }
            },
            "private_notes": "TOPSECRET Kubernetes acquisition plan",
        }

    def _posting(self, **overrides: object) -> dict[str, object]:
        posting: dict[str, object] = {
            "title": "Senior Analytics Engineer",
            "company": "Example Labs",
            "location": "Remote",
            "source_url": "https://EXAMPLE.test/jobs/analytics-1",
            "apply_url": "https://example.test/apply/analytics-1",
            "description": (
                "Build Python and SQL analytics pipelines. "
                "Experience with SQLite, Snowflake, and Kubernetes is useful."
            ),
        }
        posting.update(overrides)
        return posting

    def _list_params(self, **overrides: object) -> dict[str, object]:
        params: dict[str, object] = {
            "query": "",
            "offset": 0,
            "stage": "all",
            "sort": "last_action",
        }
        params.update(overrides)
        return params

    def test_save_is_retry_safe_and_pins_immutable_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            profile = save_profile(data_dir, self._profile())

            first = save_and_match_opportunity(data_dir, self._posting())
            duplicate = save_and_match_opportunity(data_dir, self._posting())

            self.assertEqual(first["opportunity"]["id"], duplicate["opportunity"]["id"])
            self.assertEqual(first["snapshot"]["id"], duplicate["snapshot"]["id"])
            self.assertEqual(first["match"]["id"], duplicate["match"]["id"])
            self.assertEqual(initialize_vault(data_dir).opportunities, 1)
            self.assertEqual(first["opportunity"]["status"], "ready")
            self.assertEqual(first["opportunity"]["tracker_stage"], "tracked")
            self.assertEqual(first["opportunity"]["notes"], "")
            self.assertGreater(first["opportunity"]["tracker_updated_at_ms"], 0)
            self.assertEqual(first["profile_version"]["id"], profile.id)
            self.assertEqual(first["match"]["contract"], MATCH_CONTRACT)
            self.assertIn("python", first["match"]["matched_terms"])
            self.assertIn("snowflake", first["match"]["matched_terms"])
            self.assertIn("kubernetes", first["match"]["terms_to_review"])
            self.assertNotIn(
                "kubernetes",
                [evidence["term"] for evidence in first["match"]["evidence"]],
            )
            self.assertIn(
                "/meta/canonical_context/impact_evidence/0",
                [evidence["profile_path"] for evidence in first["match"]["evidence"]],
            )
            self.assertNotIn("TOPSECRET", json.dumps(first))
            self.assertEqual(first["opportunity"]["source_url"], "https://example.test/jobs/analytics-1")

            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM opportunity_snapshots").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM opportunity_matches").fetchone()[0],
                    1,
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("UPDATE opportunity_snapshots SET title = 'changed'")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM opportunity_snapshots")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("UPDATE opportunity_matches SET signal = 'limited'")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM opportunity_matches")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable pins"):
                    connection.execute(
                        """
                        INSERT INTO opportunity_matches(
                            id, opportunity_id, match_number, snapshot_id,
                            snapshot_checksum_sha256, profile_version_id,
                            profile_checksum_sha256, contract, input_fingerprint,
                            score, signal, result_json, created_at_ms
                        )
                        SELECT ?, opportunity_id, match_number + 1, snapshot_id,
                               ?, profile_version_id, profile_checksum_sha256,
                               'tampered-v1', ?, score, signal, result_json,
                               created_at_ms
                        FROM opportunity_matches LIMIT 1
                        """,
                        (str(uuid.uuid4()), "0" * 64, "1" * 64),
                    )

    def test_tracker_update_is_optimistic_monotonic_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, self._profile())
            saved = save_and_match_opportunity(data_dir, self._posting())
            opportunity = saved["opportunity"]
            original_tracker_time = opportunity["tracker_updated_at_ms"]
            original_technical_time = opportunity["updated_at_ms"]

            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                immutable_counts = (
                    connection.execute("SELECT COUNT(*) FROM opportunity_snapshots").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM opportunity_matches").fetchone()[0],
                )

            with patch(
                "cvgnome_engine.opportunities._utc_now_ms",
                return_value=original_tracker_time,
            ):
                updated = update_opportunity(
                    data_dir,
                    {
                        "opportunity_id": opportunity["id"],
                        "expected_tracker_updated_at_ms": original_tracker_time,
                        "tracker_stage": "to_apply",
                        "notes": "  Follow up Friday.  \r\nBring portfolio.  ",
                    },
                )

            self.assertEqual(updated["opportunity"]["tracker_stage"], "to_apply")
            self.assertEqual(
                updated["opportunity"]["notes"],
                "Follow up Friday.\nBring portfolio.",
            )
            self.assertEqual(
                updated["opportunity"]["tracker_updated_at_ms"],
                original_tracker_time + 1,
            )
            self.assertEqual(updated["opportunity"]["updated_at_ms"], original_technical_time)
            self.assertEqual(updated["snapshot"], saved["snapshot"])
            self.assertEqual(updated["match"], saved["match"])

            no_op = update_opportunity(
                data_dir,
                {
                    "opportunity_id": opportunity["id"],
                    "expected_tracker_updated_at_ms": original_tracker_time + 1,
                    "tracker_stage": "to_apply",
                },
            )
            self.assertEqual(no_op, updated)

            with self.assertRaises(VaultError) as conflict:
                update_opportunity(
                    data_dir,
                    {
                        "opportunity_id": opportunity["id"],
                        "expected_tracker_updated_at_ms": original_tracker_time,
                        "notes": "stale write",
                    },
                )
            self.assertEqual(conflict.exception.code, "opportunity_tracker_conflict")

            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                self.assertEqual(
                    (
                        connection.execute(
                            "SELECT COUNT(*) FROM opportunity_snapshots"
                        ).fetchone()[0],
                        connection.execute(
                            "SELECT COUNT(*) FROM opportunity_matches"
                        ).fetchone()[0],
                    ),
                    immutable_counts,
                )

    def test_tracker_list_filters_searches_and_sorts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, self._profile())
            saved = [
                save_and_match_opportunity(
                    data_dir,
                    self._posting(
                        source_url=f"https://example.test/jobs/{index}",
                        apply_url=None,
                        title=title,
                        description=description,
                    ),
                )
                for index, (title, description) in enumerate(
                    (
                        ("Python Lead", "Lead Python SQL analytics systems."),
                        ("Platform Lead", "Lead Kubernetes platform delivery."),
                        ("Generalist", "Coordinate teams and delivery plans."),
                    )
                )
            ]
            base_time = max(
                item["opportunity"]["tracker_updated_at_ms"] for item in saved
            )
            stages = ("applied", "to_apply", "ignored")
            updated = []
            for index, (detail, stage) in enumerate(zip(saved, stages, strict=True), start=1):
                with patch(
                    "cvgnome_engine.opportunities._utc_now_ms",
                    return_value=base_time + index,
                ):
                    updated.append(
                        update_opportunity(
                            data_dir,
                            {
                                "opportunity_id": detail["opportunity"]["id"],
                                "expected_tracker_updated_at_ms": detail["opportunity"][
                                    "tracker_updated_at_ms"
                                ],
                                "tracker_stage": stage,
                                "notes": (
                                    "ÉCOLE ÜBERPRÜFUNG STRAẞE needle note"
                                    if index == 2
                                    else ""
                                ),
                            },
                        )
                    )

            last_action = list_opportunities(data_dir, self._list_params())
            self.assertEqual(last_action["total"], 3)
            self.assertEqual(
                [item["id"] for item in last_action["items"]],
                [item["opportunity"]["id"] for item in reversed(updated)],
            )
            filtered = list_opportunities(
                data_dir,
                self._list_params(stage="to_apply"),
            )
            self.assertEqual(filtered["total"], 1)
            self.assertNotIn("notes", filtered["items"][0])
            searched = list_opportunities(
                data_dir,
                self._list_params(query="école überprüfung strasse"),
            )
            self.assertEqual(searched["total"], 1)
            self.assertEqual(searched["items"][0]["tracker_stage"], "to_apply")

            by_strength = list_opportunities(
                data_dir,
                self._list_params(sort="strength"),
            )
            scores = [item["match"]["score"] for item in by_strength["items"]]
            self.assertEqual(scores, sorted(scores, reverse=True))
            by_stage = list_opportunities(data_dir, self._list_params(sort="stage"))
            ranks = [TRACKER_STAGES.index(item["tracker_stage"]) for item in by_stage["items"]]
            self.assertEqual(ranks, sorted(ranks))

    def test_same_source_url_appends_changed_snapshot_without_mutating_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, self._profile())
            first = save_and_match_opportunity(data_dir, self._posting())
            second = save_and_match_opportunity(
                data_dir,
                self._posting(
                    title="Principal Analytics Engineer",
                    description="Lead Python analytics and Terraform delivery.",
                ),
            )
            third = save_and_match_opportunity(data_dir, self._posting())

            self.assertEqual(first["opportunity"]["id"], second["opportunity"]["id"])
            self.assertEqual(second["opportunity"]["id"], third["opportunity"]["id"])
            self.assertNotEqual(first["snapshot"]["id"], second["snapshot"]["id"])
            self.assertNotEqual(first["snapshot"]["id"], third["snapshot"]["id"])
            self.assertNotEqual(second["snapshot"]["id"], third["snapshot"]["id"])
            self.assertNotEqual(first["match"]["id"], second["match"]["id"])
            self.assertEqual(second["opportunity"]["title"], "Principal Analytics Engineer")
            self.assertEqual(third["opportunity"]["title"], "Senior Analytics Engineer")
            self.assertEqual(third["snapshot"]["checksum_sha256"], first["snapshot"]["checksum_sha256"])
            self.assertEqual(get_opportunity(data_dir, first["opportunity"]["id"]), third)

            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                snapshots = connection.execute(
                    """
                    SELECT snapshot_number, title, description
                    FROM opportunity_snapshots ORDER BY snapshot_number
                    """
                ).fetchall()
                self.assertEqual([row[0] for row in snapshots], [1, 2, 3])
                self.assertEqual(snapshots[0][1], "Senior Analytics Engineer")
                self.assertIn("Kubernetes", snapshots[0][2])
                self.assertEqual(snapshots[2][1], "Senior Analytics Engineer")
                self.assertIn("Kubernetes", snapshots[2][2])
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM opportunity_matches").fetchone()[0],
                    3,
                )

    def test_distinct_urls_create_distinct_matches_for_the_same_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, self._profile())
            first = save_and_match_opportunity(
                data_dir,
                self._posting(
                    source_url="https://example.test/jobs/one",
                    apply_url=None,
                ),
            )
            second = save_and_match_opportunity(
                data_dir,
                self._posting(
                    source_url="https://example.test/jobs/two",
                    apply_url=None,
                ),
            )

            self.assertNotEqual(first["opportunity"]["id"], second["opportunity"]["id"])
            self.assertNotEqual(first["snapshot"]["id"], second["snapshot"]["id"])
            self.assertNotEqual(first["match"]["id"], second["match"]["id"])
            self.assertEqual(list_opportunities(data_dir, self._list_params())["total"], 2)

    def test_rematch_uses_latest_profile_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first_profile = save_profile(data_dir, self._profile())
            saved = save_and_match_opportunity(data_dir, self._posting())
            second_profile = save_profile(
                data_dir,
                {
                    "basics": {"name": "Ada Lovelace", "summary": "Kubernetes platform lead"},
                    "skills": [{"name": "Platform", "keywords": ["Kubernetes", "Terraform"]}],
                },
            )

            rematched = rematch_opportunity(data_dir, saved["opportunity"]["id"])
            retried = rematch_opportunity(data_dir, saved["opportunity"]["id"])

            self.assertEqual(saved["profile_version"]["id"], first_profile.id)
            self.assertEqual(rematched["profile_version"]["id"], second_profile.id)
            self.assertEqual(rematched["match"]["id"], retried["match"]["id"])
            self.assertIn("kubernetes", rematched["match"]["matched_terms"])
            with closing(sqlite3.connect(data_dir / "cvgnome.sqlite3")) as connection:
                pinned = connection.execute(
                    """
                    SELECT profile_version_id, snapshot_checksum_sha256,
                           profile_checksum_sha256, contract
                    FROM opportunity_matches ORDER BY created_at_ms, id
                    """
                ).fetchall()
                self.assertEqual(len(pinned), 2)
                self.assertEqual({row[0] for row in pinned}, {first_profile.id, second_profile.id})
                self.assertEqual({row[3] for row in pinned}, {MATCH_CONTRACT})
                self.assertEqual(len({row[1] for row in pinned}), 1)

    def test_list_search_escapes_wildcards_and_omits_private_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, self._profile())
            percent = save_and_match_opportunity(
                data_dir,
                self._posting(
                    source_url="https://example.test/jobs/percent",
                    company="100% Local",
                    description="Python systems for literal percent searches.",
                ),
            )
            save_and_match_opportunity(
                data_dir,
                self._posting(
                    source_url="https://example.test/jobs/underscore",
                    company="Other_Labs",
                    description="SQL reporting.",
                ),
            )
            unicode_company = save_and_match_opportunity(
                data_dir,
                self._posting(
                    source_url="https://example.test/jobs/unicode",
                    company="École Labs",
                    description="Analytics systems for universities.",
                ),
            )

            result = list_opportunities(data_dir, self._list_params(query="%"))

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["id"], percent["opportunity"]["id"])
            serialized = json.dumps(result)
            self.assertNotIn("description", serialized)
            self.assertNotIn("evidence", serialized)
            self.assertNotIn("source_url", serialized)
            self.assertNotIn("notes", result["items"][0])
            self.assertEqual(
                list_opportunities(data_dir, self._list_params(query="_"))["total"],
                1,
            )
            unicode_result = list_opportunities(
                data_dir,
                self._list_params(query="éCOLE"),
            )
            self.assertEqual(unicode_result["total"], 1)
            self.assertEqual(unicode_result["items"][0]["id"], unicode_company["opportunity"]["id"])

    def test_known_canonical_aliases_are_used_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(
                data_dir,
                {
                    "basics": {"name": "Ada"},
                    "work": [
                        {
                            "employer": "Example",
                            "role": "Architect",
                            "description": "Designed distributed telemetry systems.",
                        }
                    ],
                    "certifications": [
                        {"name": "Snowflake Architect", "issuer": "Snowflake"}
                    ],
                    "private_notes": "TOPSECRET Kubernetes acquisition",
                },
            )
            detail = save_and_match_opportunity(
                data_dir,
                {
                    "title": "Telemetry Architect",
                    "description": "Lead distributed telemetry on Snowflake and Kubernetes.",
                },
            )

            self.assertIn("telemetry", detail["match"]["matched_terms"])
            self.assertIn("snowflake", detail["match"]["matched_terms"])
            self.assertIn("kubernetes", detail["match"]["terms_to_review"])
            self.assertNotIn("TOPSECRET", json.dumps(detail))

    def test_input_bounds_https_only_and_domain_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with self.assertRaisesRegex(VaultError, "canonical profile"):
                save_and_match_opportunity(data_dir, self._posting())
            save_profile(data_dir, self._profile())

            invalid_postings = (
                self._posting(title=""),
                self._posting(description=""),
                self._posting(description="é" * 16_001),
                self._posting(description='"' * 31_999),
                self._posting(source_url="http://example.test/job"),
                self._posting(source_url="https://person:secret@example.test/job"),
                self._posting(apply_url="https://example.test/has a space"),
            )
            for posting in invalid_postings:
                with self.subTest(posting=posting):
                    with self.assertRaises(ValueError):
                        save_and_match_opportunity(data_dir, posting)

            with self.assertRaisesRegex(ValueError, "canonical lowercase UUID"):
                get_opportunity(data_dir, "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA")
            with self.assertRaisesRegex(VaultError, "no longer exists"):
                get_opportunity(data_dir, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                list_opportunities(data_dir, {**self._list_params(), "limit": 21})
            with self.assertRaisesRegex(ValueError, "offset"):
                list_opportunities(data_dir, self._list_params(offset=True))
            with self.assertRaisesRegex(ValueError, "exactly"):
                list_opportunities(data_dir, {})
            with self.assertRaisesRegex(ValueError, "query"):
                list_opportunities(data_dir, self._list_params(query=None))
            with self.assertRaisesRegex(ValueError, "stage"):
                list_opportunities(data_dir, self._list_params(stage="someday"))
            with self.assertRaisesRegex(ValueError, "sort"):
                list_opportunities(data_dir, self._list_params(sort="title"))
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                save_and_match_opportunity(
                    data_dir,
                    {**self._posting(), "raw_source_payload": "must not be accepted"},
                )
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                list_opportunities(data_dir, {"private_query": "must not be accepted"})

            minimal = save_and_match_opportunity(
                data_dir,
                {
                    "title": "Local Engineer",
                    "description": "Build Python systems.",
                    "company": None,
                    "location": None,
                    "source_url": None,
                    "apply_url": None,
                },
            )
            self.assertEqual(minimal["opportunity"]["company"], "")
            self.assertEqual(minimal["opportunity"]["location"], "")

            tracker_time = minimal["opportunity"]["tracker_updated_at_ms"]
            invalid_updates = (
                {
                    "opportunity_id": minimal["opportunity"]["id"],
                    "expected_tracker_updated_at_ms": tracker_time,
                },
                {
                    "opportunity_id": minimal["opportunity"]["id"],
                    "expected_tracker_updated_at_ms": True,
                    "notes": "hello",
                },
                {
                    "opportunity_id": minimal["opportunity"]["id"],
                    "expected_tracker_updated_at_ms": tracker_time,
                    "tracker_stage": "someday",
                },
                {
                    "opportunity_id": minimal["opportunity"]["id"],
                    "expected_tracker_updated_at_ms": tracker_time,
                    "notes": "x" * 4_001,
                },
                {
                    "opportunity_id": minimal["opportunity"]["id"],
                    "expected_tracker_updated_at_ms": tracker_time,
                    "notes": "valid",
                    "private_payload": "not accepted",
                },
            )
            for invalid in invalid_updates:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        update_opportunity(data_dir, invalid)

    def test_concurrent_retry_creates_one_snapshot_and_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(data_dir, self._profile())
            with ThreadPoolExecutor(max_workers=4) as executor:
                details = list(
                    executor.map(
                        lambda _: save_and_match_opportunity(data_dir, self._posting()),
                        range(4),
                    )
                )

            self.assertEqual(len({item["opportunity"]["id"] for item in details}), 1)
            self.assertEqual(len({item["snapshot"]["id"] for item in details}), 1)
            self.assertEqual(len({item["match"]["id"] for item in details}), 1)

    def test_sparse_generic_overlap_never_claims_strong_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(
                data_dir,
                {"basics": {"name": "Ada", "label": "Engineer"}},
            )
            detail = save_and_match_opportunity(
                data_dir,
                {
                    "title": "Engineer",
                    "description": (
                        "Must have ten years experience. The successful candidate will join our team."
                    ),
                },
            )

            self.assertEqual(detail["match"]["matched_terms"], ["engineer"])
            self.assertLessEqual(detail["match"]["score"], 29)
            self.assertEqual(detail["match"]["signal"], "limited")

    def test_evidence_receipts_remove_controls_from_profile_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(
                data_dir,
                {
                    "basics": {
                        "name": "Ada",
                        "summary": "Python\u0000 systems and Rust\u202e delivery",
                    }
                },
            )
            detail = save_and_match_opportunity(
                data_dir,
                {
                    "title": "Python Engineer",
                    "description": "Build Python and Rust delivery systems.",
                },
            )

            snippets = [item["snippet"] for item in detail["match"]["evidence"]]
            self.assertTrue(snippets)
            self.assertTrue(
                all(
                    not unicodedata.category(character).startswith("C")
                    for snippet in snippets
                    for character in snippet
                )
            )

    def test_detail_payload_is_bounded_below_native_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            save_profile(
                data_dir,
                {
                    "basics": {"name": "Ada", "summary": "é" * 100_000},
                    "skills": [{"name": "Data", "keywords": ["Python", "SQL"]}],
                },
            )
            detail = save_and_match_opportunity(
                data_dir,
                self._posting(description="x" * 31_999),
            )
            encoded = json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLess(len(encoded), 64 * 1024)


if __name__ == "__main__":
    unittest.main()
