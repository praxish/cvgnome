# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sys
import unittest
from pathlib import Path


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.source_ingest import (  # noqa: E402
    SynthesisLimits,
    synthesize_canonical_profile,
)
from cvgnome_engine.profile import prepare_canonical_profile  # noqa: E402


def _source(name: str, text: str, media_type: str = "text/plain") -> dict[str, object]:
    return {
        "display_name": name,
        "media_type": media_type,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


class SourceSynthesisTests(unittest.TestCase):
    def test_nested_json_resume_is_merged_without_overwriting_confirmed_basics(self) -> None:
        existing = {
            "basics": {
                "name": "Confirmed Name",
                "email": "confirmed@example.test",
                "location": {"city": "Portland"},
            },
            "meta": {"current_focus": "Local-first software"},
        }
        original = deepcopy(existing)
        payload = {
            "envelope": {
                "payload": {
                    "canonical_profile": {
                        "basics": {
                            "name": "Unconfirmed Name",
                            "email": "other@example.test",
                            "phone": "+1 555 0199",
                            "location": {"city": "Seattle", "region": "WA"},
                        },
                        "work": [
                            {
                                "company": "Analytical Engines",
                                "title": "Principal Engineer",
                                "startDate": "2021-01",
                                "highlights": ["Reduced run time by 40 percent."],
                            }
                        ],
                        "education": [
                            {
                                "school": "University of London",
                                "degree": "BSc",
                                "field": "Mathematics",
                            }
                        ],
                        "skills": [
                            {"name": "Data", "keywords": ["Python", "SQL"]}
                        ],
                    }
                }
            }
        }
        source = _source(
            "canonical.json",
            json.dumps(payload),
            "application/json",
        )

        first_profile, first_report = synthesize_canonical_profile(
            existing_profile=existing,
            sources=[source],
        )
        second_profile, second_report = synthesize_canonical_profile(
            existing_profile=existing,
            sources=[source],
        )

        self.assertEqual(first_profile, second_profile)
        self.assertEqual(first_report, second_report)
        self.assertEqual(existing, original)
        self.assertEqual(first_profile["basics"]["name"], "Confirmed Name")
        self.assertEqual(
            first_profile["basics"]["email"], "confirmed@example.test"
        )
        self.assertEqual(first_profile["basics"]["phone"], "+1 555 0199")
        self.assertEqual(first_profile["basics"]["location"]["city"], "Portland")
        self.assertEqual(first_profile["basics"]["location"]["region"], "WA")
        self.assertEqual(first_profile["work"][0]["name"], "Analytical Engines")
        self.assertEqual(first_profile["work"][0]["position"], "Principal Engineer")
        self.assertEqual(first_profile["education"][0]["studyType"], "BSc")
        self.assertEqual(first_profile["skills"][0]["keywords"], ["Python", "SQL"])
        self.assertGreaterEqual(
            first_report["fact_actions"].get("conflict_preserved", 0), 2
        )

        ingest_meta = first_profile["meta"]["source_ingest"]
        self.assertRegex(ingest_meta["synthesis_id"], r"^syn_[0-9a-f]{24}$")
        self.assertTrue(ingest_meta["facts"])
        self.assertTrue(
            all(fact["id"].startswith("fact_") for fact in ingest_meta["facts"])
        )
        self.assertNotIn("generated_at", json.dumps(ingest_meta).casefold())
        self.assertNotIn("timestamp", json.dumps(ingest_meta).casefold())

    def test_explicit_markdown_fields_create_high_confidence_profile_sections(self) -> None:
        markdown = """# Ada Lovelace
Email: ada@example.test
Phone: +1 555 0100
Location: London, England, GB
Headline: Analytics Engineer

Company: Difference Engine Co.
Position: Lead Analyst
Dates: January 2020 - Present
- Built a governed metrics layer that reduced reporting time by 40 percent.

Institution: University of London
Degree: BSc
Field: Mathematics
End Date: 2019

Skills: Python, SQL, Data Modeling
Work Preferences: Remote-first product teams
Impact: Launched a decision system used by 200 customers.
"""

        profile, report = synthesize_canonical_profile(
            existing_profile=None,
            sources=[_source("resume.md", markdown, "text/markdown")],
        )

        self.assertEqual(profile["basics"]["name"], "Ada Lovelace")
        self.assertEqual(profile["basics"]["email"], "ada@example.test")
        self.assertEqual(profile["basics"]["phone"], "+1 555 0100")
        self.assertEqual(profile["basics"]["location"]["city"], "London")
        self.assertEqual(profile["basics"]["label"], "Analytics Engineer")
        self.assertEqual(
            profile["work"][0],
            {
                "name": "Difference Engine Co.",
                "position": "Lead Analyst",
                "startDate": "January 2020",
                "endDate": "Present",
                "highlights": [
                    "Built a governed metrics layer that reduced reporting time by 40 percent."
                ],
            },
        )
        self.assertEqual(
            profile["education"][0],
            {
                "institution": "University of London",
                "studyType": "BSc",
                "area": "Mathematics",
                "endDate": "2019",
            },
        )
        self.assertEqual(
            profile["skills"][0]["keywords"],
            ["Python", "SQL", "Data Modeling"],
        )
        context = profile["meta"]["canonical_context"]
        self.assertIn(
            "Work Preferences: Remote-first product teams",
            context["work_preferences"],
        )
        self.assertIn(
            "Launched a decision system used by 200 customers.",
            context["impact_evidence"],
        )
        self.assertGreaterEqual(report["facts_recorded"], 9)

    def test_provenance_contains_hashes_and_locators_but_not_raw_evidence(self) -> None:
        private_line = "Impact: Reduced PRIVATE_CLIENT processing time by 55 percent."

        profile, _report = synthesize_canonical_profile(
            existing_profile=None,
            sources=[_source("notes.txt", private_line)],
        )

        ingest_meta = profile["meta"]["source_ingest"]
        facts = ingest_meta["facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["locator"], {"kind": "line", "start": 1, "end": 1})
        self.assertRegex(facts[0]["evidence_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(facts[0]["value_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("PRIVATE_CLIENT", json.dumps(ingest_meta))

    def test_source_order_does_not_change_output_and_budgets_are_reported(self) -> None:
        sources = [
            _source("zeta.txt", "Skills: Python, SQL\n" + "z" * 80),
            _source("alpha.txt", "Email: alpha@example.test\n" + "a" * 80),
            _source("duplicate.txt", "Email: alpha@example.test\n" + "a" * 80),
        ]
        limits = SynthesisLimits(
            max_sources=3,
            max_chars_per_source=50,
            max_total_chars=90,
            max_provenance_facts=20,
            max_context_items=10,
        )

        first = synthesize_canonical_profile(
            existing_profile=None,
            sources=sources,
            limits=limits,
        )
        second = synthesize_canonical_profile(
            existing_profile=None,
            sources=list(reversed(sources)),
            limits=limits,
        )

        self.assertEqual(first, second)
        profile, report = first
        self.assertLessEqual(report["characters_used"], 90)
        self.assertIn("source_text_truncated", report["warnings"])
        self.assertIn("duplicate_text_skipped", report["warnings"])
        self.assertEqual(profile["basics"]["email"], "alpha@example.test")

    def test_unstructured_prose_does_not_become_an_asserted_resume_fact(self) -> None:
        prose = (
            "This document describes an organization and mentions many people. "
            "It is not a resume and contains no explicit career fields."
        )

        profile, report = synthesize_canonical_profile(
            existing_profile=None,
            sources=[_source("background.txt", prose)],
        )

        self.assertNotIn("basics", profile)
        self.assertNotIn("work", profile)
        self.assertNotIn("education", profile)
        self.assertNotIn("skills", profile)
        self.assertEqual(report["facts_recorded"], 0)

    def test_provenance_limit_never_commits_an_unprovenanced_fact(self) -> None:
        limits = SynthesisLimits(
            max_sources=5,
            max_chars_per_source=5_000,
            max_total_chars=10_000,
            max_provenance_facts=1,
            max_context_items=10,
        )

        profile, report = synthesize_canonical_profile(
            existing_profile=None,
            sources=[_source("resume.txt", "Email: ada@example.test\nSkills: Python, SQL")],
            limits=limits,
        )

        self.assertEqual(profile["basics"]["email"], "ada@example.test")
        self.assertNotIn("skills", profile)
        self.assertEqual(report["facts_recorded"], 1)
        self.assertGreaterEqual(report["facts_omitted"], 1)
        self.assertIn("provenance_fact_limit_reached", report["warnings"])

    def test_labeled_name_remains_valid_through_the_existing_profile_gate(self) -> None:
        profile, _report = synthesize_canonical_profile(
            existing_profile=None,
            sources=[
                _source(
                    "resume.txt",
                    "Name: Ada Lovelace\nEmail: ada@example.test\nSkills: Python, SQL",
                )
            ],
        )

        prepared, _compaction = prepare_canonical_profile(profile)

        self.assertEqual(prepared["basics"]["name"], "Ada Lovelace")
        self.assertTrue(prepared["meta"]["source_ingest"]["facts"])


if __name__ == "__main__":
    unittest.main()
