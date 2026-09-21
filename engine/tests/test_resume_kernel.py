# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import importlib.util
import sys
import unittest
from pathlib import Path
from zipfile import ZipFile


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.legacy_compatibility import LEGACY_PROFILE_HOSTS, LEGACY_PROFILE_NETWORKS
from cvgnome_engine.resume import (  # noqa: E402
    derive_baseline_resume,
    render_docx_bytes,
    render_pdf_bytes,
)


SAMPLE_PROFILE = {
    "basics": {
        "name": "Ada Lovelace",
        "label": "Analytics Engineer",
        "email": "ada@example.test",
        "phone": "+1 555 0100",
        "location": {"city": "London", "countryCode": "GB"},
        "profiles": [
            {
                "network": "GitHub",
                "username": "ada",
                "url": "https://github.example/ada",
                "private_note": "must not leak",
            }
        ],
    },
    "work": [
        {
            "company": "Earlier Systems",
            "title": "Analyst",
            "startDate": "2018/1",
            "endDate": "2020/2",
            "highlights": ["Built a governed metrics layer."],
            "source_quote": "private",
        },
        {
            "name": "Current Systems",
            "position": "Lead Engineer",
            "startDate": "2022-03",
            "summary": "Leads the local analytics platform.",
            "highlights": [
                "Shipped deterministic exports.",
                "Shipped deterministic exports.",
                "**May 2022 - Present**",
                "Reduced processing time by 40 percent.",
            ],
        },
    ],
    "education": [
        {
            "institution": "University of London",
            "studyType": "BSc",
            "area": "Mathematics",
            "endDate": "2017",
            "editorial_note": "private",
        }
    ],
    "skills": [
        {"name": "Data", "keywords": ["Python", "SQL", "Python"]},
    ],
    "meta": {
        "canonical_context": {
            "career_narrative": "Builds trustworthy data products and decision systems."
        },
        "private_evidence": [{"source": "do not publish"}],
    },
    "workflow_state": {"internal": True},
}


class ResumeDerivationTests(unittest.TestCase):
    def test_derivation_is_public_deterministic_and_does_not_mutate_input(self) -> None:
        source = deepcopy(SAMPLE_PROFILE)

        first = derive_baseline_resume(source)
        second = derive_baseline_resume(source)

        self.assertEqual(first, second)
        self.assertEqual(source, SAMPLE_PROFILE)
        self.assertNotIn("meta", first)
        self.assertNotIn("workflow_state", first)
        self.assertNotIn("source_quote", first["work"][1])
        self.assertNotIn("private_note", first["basics"]["profiles"][0])
        self.assertEqual(first["basics"]["summary"], "Builds trustworthy data products and decision systems.")
        self.assertEqual([item["name"] for item in first["work"]], ["Current Systems", "Earlier Systems"])
        self.assertEqual(
            first["work"][0]["highlights"],
            ["Shipped deterministic exports.", "Reduced processing time by 40 percent."],
        )
        self.assertEqual(first["skills"][0]["keywords"], ["Python", "SQL"])

    def test_derivation_compacts_and_limits_roles(self) -> None:
        profile = {
            "work": [
                {
                    "name": f"Company {index}",
                    "position": "Engineer",
                    "startDate": f"{2025 - index}-01",
                    "highlights": [f"Impact {value}" for value in range(7)],
                }
                for index in range(12)
            ]
        }

        resume = derive_baseline_resume(profile)

        self.assertEqual(len(resume["work"]), 8)
        self.assertTrue(all(len(item["highlights"]) == 4 for item in resume["work"]))

    def test_invalid_top_level_shape_is_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "dictionary"):
            derive_baseline_resume([])  # type: ignore[arg-type]

    def test_unsafe_and_internal_urls_are_not_public(self) -> None:
        resume = derive_baseline_resume(
            {
                "basics": {
                    "name": "Private Links",
                    "url": "javascript:alert(1)",
                    "profiles": [
                        {"network": "CVGnome", "url": "https://example.test/current"},
                        {"network": "Current Host", "url": "https://cvgnome.com/profile"},
                        {"network": LEGACY_PROFILE_NETWORKS[0], "url": "https://example.test/legacy"},
                        {
                            "network": "Legacy Host",
                            "url": f"https://{LEGACY_PROFILE_HOSTS[0]}/me",
                        },
                        {"network": "Bad", "url": "https://user:secret@example.test/profile"},
                        {"network": "Good", "url": "https://example.test/profile"},
                    ],
                },
                "skills": [{"name": "Safety", "keywords": ["URL validation"]}],
            }
        )

        self.assertNotIn("url", resume["basics"])
        self.assertEqual(
            resume["basics"]["profiles"],
            [
                {"network": "Current Host"},
                {"network": "Legacy Host"},
                {"network": "Bad"},
                {"network": "Good", "url": "https://example.test/profile"},
            ],
        )

    def test_work_window_is_anchored_to_profile_content(self) -> None:
        resume = derive_baseline_resume(
            {
                "work": [
                    {"name": "Newest", "position": "Lead", "endDate": "2020"},
                    {"name": "Boundary", "position": "Analyst", "endDate": "2005"},
                    {"name": "Too Old", "position": "Intern", "endDate": "2004"},
                ]
            }
        )
        self.assertEqual([item["name"] for item in resume["work"]], ["Newest", "Boundary"])


@unittest.skipUnless(importlib.util.find_spec("docx"), "python-docx is not installed")
class DocxRenderingTests(unittest.TestCase):
    def test_docx_is_valid_searchable_and_byte_deterministic(self) -> None:
        from docx import Document

        first = render_docx_bytes(SAMPLE_PROFILE)
        second = render_docx_bytes(SAMPLE_PROFILE)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(b"PK"))
        document = Document(BytesIO(first))
        self.assertEqual(document.core_properties.author, "CVGnome")
        self.assertEqual(document.core_properties.last_modified_by, "CVGnome")
        with ZipFile(BytesIO(first)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("Ada Lovelace", document_xml)
            self.assertIn("Current Systems", document_xml)
            self.assertNotIn("private_evidence", document_xml)
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_render_requires_named_substantive_resume(self) -> None:
        with self.assertRaisesRegex(ValueError, "basics.name"):
            render_docx_bytes({"skills": [{"name": "Python"}]})
        with self.assertRaisesRegex(ValueError, "summary or"):
            render_docx_bytes({"basics": {"name": "Only A Name"}})


@unittest.skipUnless(importlib.util.find_spec("reportlab"), "ReportLab is not installed")
class PdfRenderingTests(unittest.TestCase):
    def test_pdf_is_valid_and_byte_deterministic(self) -> None:
        from pypdf import PdfReader

        first = render_pdf_bytes(SAMPLE_PROFILE)
        second = render_pdf_bytes(SAMPLE_PROFILE)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", first[-64:])
        self.assertEqual(PdfReader(BytesIO(first)).metadata.creator, "CVGnome")

    def test_pdf_preserves_supported_name_and_rejects_missing_glyph(self) -> None:
        supported = deepcopy(SAMPLE_PROFILE)
        supported["basics"]["name"] = "Zoë García"
        self.assertTrue(render_pdf_bytes(supported).startswith(b"%PDF-"))

        unsupported = deepcopy(SAMPLE_PROFILE)
        unsupported["basics"]["name"] = "Rocket 🚀"
        with self.assertRaisesRegex(ValueError, "U\\+1F680"):
            render_pdf_bytes(unsupported)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed")
    def test_location_does_not_duplicate_region(self) -> None:
        from pypdf import PdfReader

        profile = deepcopy(SAMPLE_PROFILE)
        profile["basics"]["location"] = {"city": "Port Angeles, WA", "region": "WA"}
        text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(BytesIO(render_pdf_bytes(profile))).pages
        )
        self.assertIn("Port Angeles, WA", text)
        self.assertNotIn("Port Angeles, WA, WA", text)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed")
    def test_pdf_text_is_searchable(self) -> None:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(render_pdf_bytes(SAMPLE_PROFILE)))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Ada Lovelace", text)
        self.assertIn("Current Systems", text)
        self.assertNotIn("private_evidence", text)


if __name__ == "__main__":
    unittest.main()
