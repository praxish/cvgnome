# SPDX-License-Identifier: MPL-2.0
"""Deterministic baseline-resume derivation and local document rendering.

The public functions in this package deliberately accept and return plain Python
objects or bytes.  They do not know about SQLite, RPC, the desktop shell, or any
network service, which keeps the resume kernel easy to test and reuse.
"""

from .derive import derive_baseline_resume, is_baseline_resume_renderable
from .render import render_docx_bytes, render_pdf_bytes

__all__ = [
    "derive_baseline_resume",
    "is_baseline_resume_renderable",
    "render_docx_bytes",
    "render_pdf_bytes",
]
