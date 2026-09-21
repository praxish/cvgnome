# SPDX-License-Identifier: MPL-2.0
"""Offline source parsing and deterministic canonical-profile synthesis.

The package exposes two deliberately small integration surfaces:

``parse_structured_source(path, ...)``
    Verify a staged PDF or DOCX by byte size and SHA-256, parse it in a
    resource-bounded child process, and return one JSON-safe source dictionary.
    ``parser_child_main`` is callable so a frozen sidecar entry point can route
    a private ``source-parser-child`` command to the same isolated worker.

``synthesize_canonical_profile(existing_profile=..., sources=...)``
    Merge bounded source text into a deterministic JSON Resume-like profile.
    Existing values win, extracted facts carry hash-based provenance under
    ``meta.source_ingest``, and the function returns ``(profile, report)``.

Neither API performs persistence, network access, model calls, or clock reads.
Callers remain responsible for staging selected files and passing the returned
profile through :func:`cvgnome_engine.profile.prepare_canonical_profile`
before committing an immutable version.
"""

from __future__ import annotations

from typing import BinaryIO

from .parser import (
    ParserLimits,
    ParserResourceLimits,
    SourceParserError,
    default_parser_child_command,
    parse_structured_source,
    source_file_identity,
)
from .synthesis import SynthesisLimits, synthesize_canonical_profile


def parser_child_main(
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
) -> int:
    """Run the private parser worker in the current process.

    A packaged sidecar should route its private child-only command here. Normal
    application code should call :func:`parse_structured_source` instead.
    """

    from .parser_child import child_main

    return child_main(input_stream=input_stream, output_stream=output_stream)


__all__ = [
    "ParserLimits",
    "ParserResourceLimits",
    "SourceParserError",
    "SynthesisLimits",
    "default_parser_child_command",
    "parse_structured_source",
    "parser_child_main",
    "source_file_identity",
    "synthesize_canonical_profile",
]
