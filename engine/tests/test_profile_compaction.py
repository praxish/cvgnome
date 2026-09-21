# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.profile import compact_canonical_profile
from cvgnome_engine.profile.education import parse_legacy_education_summary
from cvgnome_engine.profile.provenance import strip_public_provenance_suffix
from cvgnome_engine.profile.work import work_entries_can_merge


class CanonicalProfileCompactionTests(unittest.TestCase):
    def test_skills_compaction_preserves_a_normalized_level(self) -> None:
        profile = {
            "skills": [
                {"name": "Engineering", "keywords": ["Python"]},
                {
                    "name": " Engineering ",
                    "level": " Advanced ",
                    "keywords": ["SQLite"],
                },
            ]
        }

        compacted, _report = compact_canonical_profile(profile)

        self.assertEqual(
            compacted["skills"],
            [
                {
                    "name": "Engineering",
                    "level": "Advanced",
                    "keywords": ["Python", "SQLite"],
                }
            ],
        )

    def test_skills_compaction_drops_non_text_levels(self) -> None:
        profile = {
            "skills": [
                {
                    "name": "Engineering",
                    "level": {"private": "do not stringify"},
                    "keywords": ["Python"],
                }
            ]
        }

        compacted, _report = compact_canonical_profile(profile)

        self.assertEqual(
            compacted["skills"],
            [{"name": "Engineering", "keywords": ["Python"]}],
        )
        self.assertNotIn("private", json.dumps(compacted))

    def test_merges_duplicate_skills_and_work_without_mutating_input(self) -> None:
        profile = {
            "skills": [
                {
                    "name": "People Analytics",
                    "keywords": ["people analytics", "workforce analysis"],
                },
                {
                    "name": "People Analytics",
                    "keywords": ["People Analytics", "employee data analysis"],
                },
            ],
            "work": [
                {
                    "name": "Kantar",
                    "position": "People Analytics Lead",
                    "startDate": "2019",
                    "endDate": "2024",
                    "highlights": ["Measured diversity program impacts."],
                },
                {
                    "name": "Kantar",
                    "position": "People Analytics Lead",
                    "startDate": "2019",
                    "endDate": "2024",
                    "highlights": ["Built executive-ready narratives."],
                },
            ],
        }
        original = copy.deepcopy(profile)

        compacted, report = compact_canonical_profile(profile)

        self.assertEqual(profile, original)
        self.assertEqual(report["skill_groups_merged"], 1)
        self.assertEqual(report["work_entries_merged"], 1)
        self.assertEqual(
            compacted["skills"],
            [
                {
                    "name": "People Analytics",
                    "keywords": [
                        "people analytics",
                        "workforce analysis",
                        "employee data analysis",
                    ],
                }
            ],
        )
        self.assertEqual(len(compacted["work"]), 1)

    def test_strips_public_provenance_but_preserves_meta(self) -> None:
        profile = {
            "work": [
                {
                    "name": "Example",
                    "summary": "Built research systems as described by the candidate.",
                    "highlights": [
                        "Improved delivery by 15% (reported in source materials).",
                        "Candidate reports that exact dates were unavailable.",
                    ],
                }
            ],
            "meta": {
                "canonical_context": {
                    "open_questions": [
                        "Verify the metric (reported in source materials)."
                    ]
                }
            },
        }

        compacted, report = compact_canonical_profile(profile)

        self.assertEqual(compacted["work"][0]["summary"], "Built research systems.")
        self.assertEqual(
            compacted["work"][0]["highlights"], ["Improved delivery by 15%."]
        )
        self.assertEqual(
            compacted["meta"]["canonical_context"]["open_questions"],
            ["Verify the metric (reported in source materials)."],
        )
        self.assertEqual(report["public_provenance_annotations_removed"], 3)

    def test_cleans_legacy_education_and_merges_semantic_duplicates(self) -> None:
        profile = {
            "education": [
                {
                    "institution": "",
                    "summary": "Sentinel College: B.A., Economics",
                },
                {
                    "institution": "Sentinel College",
                    "studyType": "B.A.",
                    "area": "Economics",
                    "notes": "Source commentary",
                },
            ]
        }

        compacted, report = compact_canonical_profile(profile)

        self.assertEqual(
            compacted["education"],
            [
                {
                    "institution": "Sentinel College",
                    "studyType": "B.A",
                    "area": "Economics",
                }
            ],
        )
        self.assertEqual(report["education_entries_merged"], 1)
        self.assertEqual(
            parse_legacy_education_summary("A sentence without a credential"), {}
        )

    def test_work_matching_requires_compatible_location_and_dates(self) -> None:
        base = {
            "name": "Praxish",
            "position": "Principal",
            "location": "Port Angeles, WA (Remote)",
            "startDate": "2024-10",
            "endDate": "Present",
        }
        alias = {
            "name": "praxish.com",
            "position": "Principal",
            "location": "Port Angeles, WA",
            "startDate": "Oct 2024",
            "endDate": "Current",
        }
        different_location = {**alias, "location": "Seattle, WA"}

        self.assertTrue(work_entries_can_merge(base, alias))
        self.assertFalse(work_entries_can_merge(base, different_location))

    def test_suffix_sanitizer_is_conservative(self) -> None:
        self.assertEqual(
            strip_public_provenance_suffix(
                "Reported approximately 12% growth based on survey estimates."
            ),
            "Reported approximately 12% growth based on survey estimates.",
        )
        self.assertEqual(
            strip_public_provenance_suffix(
                "Led client programs with near-100% satisfaction reported in source material."
            ),
            "Led client programs with near-100% satisfaction.",
        )

    def test_compaction_is_idempotent(self) -> None:
        profile = {
            "skills": [
                {"name": "Tools", "keywords": ["SQL", "SQL", "Python"]}
            ],
            "projects": [
                {"name": f"Project {index}", "startDate": "2024"}
                for index in range(36)
            ],
        }

        once, first_report = compact_canonical_profile(profile)
        twice, second_report = compact_canonical_profile(once)

        self.assertEqual(once, twice)
        self.assertTrue(first_report["changed"])
        self.assertFalse(second_report["changed"])
        self.assertEqual(len(once["projects"]), 32)


if __name__ == "__main__":
    unittest.main()
