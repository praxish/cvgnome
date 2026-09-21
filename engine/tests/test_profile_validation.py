# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.profile import (
    CanonicalProfileValidationError,
    prepare_canonical_profile,
    validate_canonical_profile,
)
from cvgnome_engine.profile.validation import (
    MAX_CONTAINER_ITEMS,
    MAX_KEY_CHARS,
    MAX_PROFILE_DEPTH,
    MAX_PROFILE_NODES,
    MAX_STRING_CHARS,
)


class CanonicalProfileValidationTests(unittest.TestCase):
    def assert_error_code(self, payload: object, code: str) -> None:
        with self.assertRaises(CanonicalProfileValidationError) as raised:
            validate_canonical_profile(payload)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(
            raised.exception.as_dict(),
            {"code": code, "message": str(raised.exception)},
        )

    def test_requires_top_level_object(self) -> None:
        self.assert_error_code([], "canonical_profile.not_object")

    def test_rejects_non_finite_and_cyclic_values(self) -> None:
        self.assert_error_code(
            {"score": math.nan}, "canonical_profile.not_finite_json"
        )
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        self.assert_error_code(cyclic, "canonical_profile.not_finite_json")

    def test_rejects_excessive_depth_and_nodes(self) -> None:
        payload: dict[str, object] = {}
        cursor = payload
        for _ in range(MAX_PROFILE_DEPTH + 1):
            nested: dict[str, object] = {}
            cursor["nested"] = nested
            cursor = nested
        self.assert_error_code(payload, "canonical_profile.too_deep")

        many_nodes = {
            f"items_{index}": [None] * MAX_CONTAINER_ITEMS
            for index in range((MAX_PROFILE_NODES // MAX_CONTAINER_ITEMS) + 1)
        }
        self.assert_error_code(many_nodes, "canonical_profile.too_many_values")

    def test_rejects_oversized_strings_keys_and_containers(self) -> None:
        self.assert_error_code(
            {"summary": "x" * (MAX_STRING_CHARS + 1)},
            "canonical_profile.string_too_long",
        )
        self.assert_error_code(
            {"k" * (MAX_KEY_CHARS + 1): True},
            "canonical_profile.key_too_long",
        )
        self.assert_error_code(
            {"items": [None] * (MAX_CONTAINER_ITEMS + 1)},
            "canonical_profile.list_too_large",
        )

    def test_rejects_non_string_keys_before_json_coercion(self) -> None:
        self.assert_error_code(
            {"valid": True, 7: "coercible by json.dumps"},
            "canonical_profile.key_not_string",
        )

    def test_prepare_returns_plain_copies_and_report(self) -> None:
        source = {
            "skills": [
                {"name": "Analysis", "keywords": ["SQL"]},
                {"name": "Analysis", "keywords": ["Python"]},
            ]
        }
        prepared, report = prepare_canonical_profile(source)

        self.assertIsInstance(prepared, dict)
        self.assertIsInstance(report, dict)
        self.assertIsNot(prepared, source)
        self.assertEqual(source["skills"][0]["keywords"], ["SQL"])
        self.assertEqual(prepared["skills"][0]["keywords"], ["SQL", "Python"])
        self.assertTrue(report["changed"])


if __name__ == "__main__":
    unittest.main()
