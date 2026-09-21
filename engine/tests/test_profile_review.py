# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import unittest
import unicodedata

from cvgnome_engine.profile_review import (
    PROFILE_REVIEW_RESPONSE_LIMIT_BYTES,
    PROFILE_REVIEW_SECTION_ITEM_LIMIT,
    PROFILE_REVIEW_SUMMARY_LIMIT_BYTES,
    SECTION_LABELS,
    build_profile_review_section,
    build_profile_review_summary,
)


class ProfileReviewTests(unittest.TestCase):
    def _version(self, profile: dict[str, object], *, source: str = "local_edit") -> dict[str, object]:
        return {
            "id": "64aa9547-6ff7-42a9-a03d-c29c3605ce80",
            "version_number": 3,
            "created_at_ms": 1_800_000_000_000,
            "source": source,
            "profile": profile,
        }

    def test_summary_is_allowlisted_and_keeps_private_profile_data_out(self) -> None:
        version = self._version(
            {
                "basics": {
                    "name": "Ada Lovelace",
                    "label": "Analytical engine pioneer",
                    "email": "ada@example.test",
                    "location": {"city": "London", "region": "England"},
                    "profiles": [
                        {
                            "network": "GitHub",
                            "username": "ada",
                            "url": "https://example.test/ada",
                            "private_token": "PROFILE SECRET",
                        }
                    ],
                    "unknown_private": "BASICS SECRET",
                },
                "work": [{"name": "Difference Engine", "position": "Programmer"}],
                "projects": [{"name": "Notes", "private": "PROJECT SECRET"}],
                "meta": {
                    "source_ingest": {"filename": "secret-resume.pdf", "hash": "HASH SECRET"},
                    "dossier": {"claims": [{"text": "DOSSIER SECRET"}]},
                    "canonical_context": {"career_narrative": "CONTEXT SECRET"},
                },
                "unknown": "TOP SECRET",
            },
            source="source_scan:11111111-1111-4111-8111-111111111111",
        )

        result = build_profile_review_summary(version)

        self.assertEqual(result["name"], "Ada Lovelace")
        self.assertEqual(result["source_kind"], "source_documents")
        self.assertEqual(result["contacts"][0]["value"], "ada@example.test")
        self.assertEqual(
            [section["key"] for section in result["sections"]],
            [key for key, _label in SECTION_LABELS],
        )
        encoded = json.dumps(result, ensure_ascii=False)
        for secret in (
            "PROFILE SECRET",
            "BASICS SECRET",
            "PROJECT SECRET",
            "secret-resume.pdf",
            "HASH SECRET",
            "DOSSIER SECRET",
            "CONTEXT SECRET",
            "TOP SECRET",
        ):
            self.assertNotIn(secret, encoded)

    def test_sections_page_after_sanitizing_and_remain_version_pinned(self) -> None:
        work: list[object] = [None, "bad"]
        for index in range(13):
            work.append(
                {
                    "name": f"Organization {index}",
                    "position": f"Role {index}",
                    "startDate": str(2000 + index),
                    "summary": f"Summary {index}",
                    "highlights": [f"Impact {index}"],
                    "private": f"SECRET {index}",
                }
            )
        version = self._version({"basics": {"name": "Ada"}, "work": work})

        first = build_profile_review_section(version, section="work", offset=0)
        second = build_profile_review_section(version, section="work", offset=10)

        self.assertEqual(first["profile_version_id"], version["id"])
        self.assertEqual(first["total_items"], 13)
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(first["next_offset"], 10)
        self.assertEqual([item["ordinal"] for item in first["items"]], list(range(10)))
        self.assertEqual(second["total_items"], 13)
        self.assertEqual([item["ordinal"] for item in second["items"]], [10, 11, 12])
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("SECRET", json.dumps([first, second]))

    def test_adversarial_unicode_page_stays_below_native_budget(self) -> None:
        emoji = "\U0001f9d9" * 100_000
        work = [
            {
                "name": emoji,
                "position": emoji,
                "location": emoji,
                "summary": emoji,
                "highlights": [emoji] * 8,
            }
            for _index in range(10)
        ]
        version = self._version({"basics": {"name": "Ada"}, "work": work})

        result = build_profile_review_section(version, section="work", offset=0)
        encoded = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        self.assertLessEqual(len(encoded), PROFILE_REVIEW_RESPONSE_LIMIT_BYTES)
        self.assertGreater(len(result["items"]), 0)
        for item in result["items"]:
            self.assertLessEqual(len(item["title"]), 240)
            self.assertLessEqual(len(item["subtitle"] or ""), 240)
            self.assertLessEqual(len(item["location"] or ""), 200)
            self.assertLessEqual(len(item["summary"] or ""), 700)
            self.assertTrue(all(len(value) <= 360 for value in item["highlights"]))

    def test_adversarial_unicode_summary_prunes_contacts_to_native_budget(self) -> None:
        emoji = "\U0001f9d9"
        long_url = f"https://example.test/{emoji * 600}"
        version = self._version(
            {
                "basics": {
                    "name": emoji * 160,
                    "label": emoji * 200,
                    "summary": emoji * 700,
                    "email": emoji * 500,
                    "phone": emoji * 500,
                    "location": {
                        "city": emoji * 160,
                        "region": emoji * 160,
                        "countryCode": emoji * 60,
                    },
                    "url": long_url,
                    "profiles": [
                        {"network": emoji * 80, "url": long_url}
                        for _index in range(20)
                    ],
                }
            }
        )

        result = build_profile_review_summary(version)
        encoded = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        self.assertLessEqual(len(encoded), PROFILE_REVIEW_SUMMARY_LIMIT_BYTES)
        self.assertLess(len(result["contacts"]), 12)
        self.assertEqual(result["contacts"][0]["kind"], "email")

    def test_private_only_reference_entries_do_not_create_ghost_cards(self) -> None:
        version = self._version(
            {
                "basics": {"name": "Ada"},
                "references": [
                    {"source_path": "/private/reference.txt", "hash": "SECRET"},
                    {"reference": "Available on request"},
                ],
            }
        )

        summary = build_profile_review_summary(version)
        page = build_profile_review_section(version, section="references", offset=0)

        reference_summary = next(
            section for section in summary["sections"] if section["key"] == "references"
        )
        self.assertEqual(reference_summary["count"], 1)
        self.assertEqual(page["total_items"], 1)
        self.assertEqual(page["items"][0]["title"], "Reference")
        self.assertNotIn("private", json.dumps([summary, page]).lower())
        self.assertNotIn("SECRET", json.dumps([summary, page]))

    def test_invalid_section_and_offset_are_rejected(self) -> None:
        version = self._version({"basics": {"name": "Ada"}})
        for section, offset in (("private", 0), ("work", -1), ("work", True), ("work", 10_001)):
            with self.subTest(section=section, offset=offset):
                with self.assertRaises(ValueError):
                    build_profile_review_section(version, section=section, offset=offset)

    def test_known_fields_drop_control_characters_before_native_validation(self) -> None:
        version = self._version(
            {
                "basics": {"name": "Ada\u0000 Lovelace"},
                "work": [{"name": "Engine\u0007 Lab", "position": "Programmer"}],
            }
        )

        summary = build_profile_review_summary(version)
        page = build_profile_review_section(version, section="work", offset=0)

        self.assertEqual(summary["name"], "Ada Lovelace")
        self.assertEqual(page["items"][0]["subtitle"], "Engine Lab")
        self.assertFalse(
            any(
                unicodedata.category(character).startswith("C")
                for character in json.dumps([summary, page], ensure_ascii=False)
            )
        )

    def test_section_totals_share_the_native_collection_cap(self) -> None:
        version = self._version(
            {
                "basics": {"name": "Ada"},
                "work": [
                    {"name": f"Engine {index}", "position": "Programmer"}
                    for index in range(PROFILE_REVIEW_SECTION_ITEM_LIMIT + 1)
                ],
            }
        )

        summary = build_profile_review_summary(version)
        final_page = build_profile_review_section(
            version,
            section="work",
            offset=PROFILE_REVIEW_SECTION_ITEM_LIMIT - 1,
        )

        self.assertEqual(summary["sections"][0]["count"], PROFILE_REVIEW_SECTION_ITEM_LIMIT)
        self.assertEqual(final_page["total_items"], PROFILE_REVIEW_SECTION_ITEM_LIMIT)
        self.assertEqual(len(final_page["items"]), 1)
        self.assertIsNone(final_page["next_offset"])

    def test_final_profile_links_are_reclassified_after_unicode_sanitizing(self) -> None:
        version = self._version(
            {
                "basics": {
                    "name": "Ada",
                    "url": "https://cv\u200bgnome.com/profile?token=WEBSITE_SECRET",
                    "profiles": [
                        {
                            "network": "CV\u200bGnome",
                            "username": "INTERNAL_PROFILE_SECRET",
                        },
                        {
                            "network": "GitHub",
                            "username": "ada",
                            "url": "https://example.test/ada",
                        },
                    ],
                }
            }
        )

        result = build_profile_review_summary(version)
        encoded = json.dumps(result, ensure_ascii=False)

        self.assertNotIn("WEBSITE_SECRET", encoded)
        self.assertNotIn("INTERNAL_PROFILE_SECRET", encoded)
        self.assertEqual(
            result["contacts"],
            [{"kind": "profile", "label": "GitHub", "value": "https://example.test/ada"}],
        )

    def test_multilingual_limits_and_full_review_material_are_preserved(self) -> None:
        name = "界" * 160
        summary = "界" * 700
        highlights = [f"Impact {index}" for index in range(8)]
        version = self._version(
            {
                "basics": {"name": name},
                "work": [
                    {
                        "name": "Analytical Engine",
                        "position": "Programmer",
                        "summary": summary,
                        "highlights": highlights,
                    }
                ],
                "projects": [
                    {
                        "name": "Notes",
                        "description": summary,
                        "highlights": highlights,
                    }
                ],
            }
        )

        overview = build_profile_review_summary(version)
        work = build_profile_review_section(version, section="work", offset=0)
        projects = build_profile_review_section(version, section="projects", offset=0)

        self.assertEqual(overview["name"], name)
        self.assertEqual(work["items"][0]["summary"], summary)
        self.assertEqual(work["items"][0]["highlights"], highlights)
        self.assertEqual(projects["items"][0]["summary"], summary)
        self.assertEqual(projects["items"][0]["highlights"], highlights)

    def test_start_only_dates_do_not_infer_present(self) -> None:
        version = self._version(
            {
                "basics": {"name": "Ada"},
                "work": [
                    {"name": "Engine A", "position": "Programmer", "startDate": "2020"},
                    {
                        "name": "Engine B",
                        "position": "Programmer",
                        "startDate": "2021",
                        "endDate": "Present",
                    },
                ],
            }
        )

        page = build_profile_review_section(version, section="work", offset=0)

        self.assertEqual(page["items"][0]["date_range"], "2020")
        self.assertEqual(page["items"][1]["date_range"], "2021 – Present")


if __name__ == "__main__":
    unittest.main()
