# SPDX-License-Identifier: MPL-2.0
"""Pure canonical-profile preparation for the local CVGnome engine.

The public contract deliberately uses ordinary dictionaries so it can sit behind
the JSON-lines engine protocol without coupling the domain layer to SQLite, Tauri,
or a validation framework.
"""

from .compaction import canonical_profile_metrics, compact_canonical_profile
from .validation import (
    CanonicalProfileValidationError,
    prepare_canonical_profile,
    validate_canonical_profile,
)

__all__ = [
    "CanonicalProfileValidationError",
    "canonical_profile_metrics",
    "compact_canonical_profile",
    "prepare_canonical_profile",
    "validate_canonical_profile",
]
