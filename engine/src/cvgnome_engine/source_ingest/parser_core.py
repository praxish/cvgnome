# SPDX-License-Identifier: MPL-2.0
"""Untrusted PDF/DOCX parsing primitives used only by the parser child."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from defusedxml import ElementTree
import hashlib
import io
import os
from pathlib import Path
import re
import stat
from typing import Any, BinaryIO
import unicodedata
import zipfile


SUPPORTED_STRUCTURED_SUFFIXES = frozenset({".docx", ".pdf"})
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_HEADING_STYLE_RE = re.compile(r"^heading\s*([1-6])$", re.IGNORECASE)


class ParserCoreError(RuntimeError):
    """Internal parser failure represented only by a stable error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParserLimits:
    """Hard content limits applied inside the isolated parser."""

    max_input_bytes: int = 25 * 1024 * 1024
    max_output_chars: int = 200_000
    max_pdf_pages: int = 80
    max_docx_entries: int = 256
    max_docx_member_bytes: int = 12 * 1024 * 1024
    max_docx_uncompressed_bytes: int = 50 * 1024 * 1024
    max_docx_compression_ratio: float = 200.0

    def to_payload(self) -> dict[str, int | float]:
        return asdict(self)


def _raise(code: str) -> None:
    raise ParserCoreError(code)


def _required_int(
    value: dict[str, Any], key: str, *, minimum: int, maximum: int
) -> int:
    raw = value.get(key)
    if isinstance(raw, bool):
        _raise("invalid_request")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        _raise("invalid_request")
    if parsed < minimum or parsed > maximum:
        _raise("invalid_request")
    return parsed


def _required_float(
    value: dict[str, Any], key: str, *, minimum: float, maximum: float
) -> float:
    raw = value.get(key)
    if isinstance(raw, bool):
        _raise("invalid_request")
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        _raise("invalid_request")
    if parsed < minimum or parsed > maximum:
        _raise("invalid_request")
    return parsed


def limits_from_payload(value: Any) -> ParserLimits:
    """Validate child-protocol limits instead of trusting the parent payload."""

    if not isinstance(value, dict):
        _raise("invalid_request")
    return ParserLimits(
        max_input_bytes=_required_int(
            value, "max_input_bytes", minimum=1, maximum=100 * 1024 * 1024
        ),
        max_output_chars=_required_int(
            value, "max_output_chars", minimum=1, maximum=2_000_000
        ),
        max_pdf_pages=_required_int(
            value, "max_pdf_pages", minimum=1, maximum=500
        ),
        max_docx_entries=_required_int(
            value, "max_docx_entries", minimum=1, maximum=2_000
        ),
        max_docx_member_bytes=_required_int(
            value,
            "max_docx_member_bytes",
            minimum=1,
            maximum=100 * 1024 * 1024,
        ),
        max_docx_uncompressed_bytes=_required_int(
            value,
            "max_docx_uncompressed_bytes",
            minimum=1,
            maximum=250 * 1024 * 1024,
        ),
        max_docx_compression_ratio=_required_float(
            value,
            "max_docx_compression_ratio",
            minimum=1.0,
            maximum=10_000.0,
        ),
    )


def _open_regular_file(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _raise("input_unavailable")
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            _raise("input_unavailable")
        return os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def read_verified_bytes(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_input_bytes: int,
) -> bytes:
    """Read a regular, non-symlink file and verify the staging manifest."""

    if expected_size < 0 or expected_size > max_input_bytes:
        _raise("input_too_large" if expected_size > max_input_bytes else "invalid_request")
    chunks: list[bytes] = []
    total = 0
    digest = hashlib.sha256()
    with _open_regular_file(path) as handle:
        while True:
            chunk = handle.read(min(1024 * 1024, max_input_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_input_bytes:
                _raise("input_too_large")
            digest.update(chunk)
            chunks.append(chunk)
    if total != expected_size or digest.hexdigest() != expected_sha256:
        _raise("input_changed")
    return b"".join(chunks)


def _safe_display_name(path: Path, suffix: str) -> str:
    cleaned = "".join(
        character
        for character in path.name
        if character.isprintable()
        and not unicodedata.category(character).startswith("C")
    ).strip()
    return cleaned[:255] or f"source{suffix}"


def _validated_docx_members(
    archive: zipfile.ZipFile, *, limits: ParserLimits
) -> dict[str, zipfile.ZipInfo]:
    members = [member for member in archive.infolist() if not member.is_dir()]
    if len(members) > limits.max_docx_entries:
        _raise("unsafe_document")

    total_uncompressed = 0
    by_name: dict[str, zipfile.ZipInfo] = {}
    for member in members:
        normalized = member.filename.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        if normalized.startswith("/") or ".." in parts or normalized in by_name:
            _raise("unsafe_document")
        if member.flag_bits & 0x1 or member.file_size < 0 or member.compress_size < 0:
            _raise("unsafe_document")
        if member.file_size > limits.max_docx_member_bytes:
            _raise("unsafe_document")
        total_uncompressed += int(member.file_size)
        if total_uncompressed > limits.max_docx_uncompressed_bytes:
            _raise("unsafe_document")
        if member.file_size > 0:
            ratio = float(member.file_size) / float(max(1, member.compress_size))
            if ratio > limits.max_docx_compression_ratio:
                _raise("unsafe_document")
        by_name[normalized] = member
    return by_name


def _read_docx_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    limits: ParserLimits,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    with archive.open(member, "r") as handle:
        while True:
            chunk = handle.read(
                min(1024 * 1024, limits.max_docx_member_bytes - total + 1)
            )
            if not chunk:
                break
            total += len(chunk)
            if total > limits.max_docx_member_bytes:
                _raise("unsafe_document")
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_docx_text(raw: bytes, *, limits: ParserLimits) -> tuple[str, bool]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = _validated_docx_members(archive, limits=limits)
            document = members.get("word/document.xml")
            if document is None:
                return "", False
            xml_bytes = _read_docx_member(archive, document, limits=limits)
    except ParserCoreError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return "", False

    try:
        root = ElementTree.fromstring(xml_bytes)
    except Exception:  # noqa: BLE001 - malformed or entity-bearing XML is untrusted.
        return "", False

    namespace = {"w": _WORD_NAMESPACE}
    paragraphs: list[str] = []
    used = 0
    truncated = False
    paragraph_nodes = root.findall(".//w:p", namespace)
    for paragraph_index, paragraph in enumerate(paragraph_nodes):
        fragments = [
            node.text
            for node in paragraph.findall(".//w:t", namespace)
            if node.text
        ]
        joined = "".join(fragments).strip()
        if not joined:
            continue
        paragraph_style = paragraph.find("./w:pPr/w:pStyle", namespace)
        style_id = (
            paragraph_style.get(f"{{{_WORD_NAMESPACE}}}val", "").strip()
            if paragraph_style is not None
            else ""
        )
        heading_match = _DOCX_HEADING_STYLE_RE.fullmatch(style_id.replace("_", " "))
        if style_id.casefold() == "title":
            joined = f"# {joined}"
        elif heading_match:
            # A Word Heading 1 is a document section; lower heading levels are
            # useful as nested resume-entry headings to the synthesis parser.
            heading_level = min(6, int(heading_match.group(1)) + 1)
            joined = f"{'#' * heading_level} {joined}"
        else:
            numbered = paragraph.find("./w:pPr/w:numPr", namespace) is not None
            list_style = any(
                marker in style_id.casefold() for marker in ("list", "bullet", "number")
            )
            if (numbered or list_style) and not joined.startswith(("- ", "* ", "• ")):
                joined = f"- {joined}"
        separator = 1 if paragraphs else 0
        remaining = limits.max_output_chars - used - separator
        if remaining <= 0:
            truncated = True
            break
        if len(joined) > remaining:
            truncated = True
        clipped = joined[:remaining].strip()
        if clipped:
            paragraphs.append(clipped)
            used += separator + len(clipped)
        if used >= limits.max_output_chars and paragraph_index + 1 < len(paragraph_nodes):
            truncated = True
            break
    return "\n".join(paragraphs), truncated


def _extract_pdf_text(raw: bytes, *, limits: ParserLimits) -> tuple[str, bool]:
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - dependency/runtime failure is protocol-safe.
        _raise("parser_unavailable")

    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
    except Exception:  # noqa: BLE001 - malformed PDF.
        return "", False
    if getattr(reader, "is_encrypted", False):
        try:
            if reader.decrypt("") == 0:
                return "", False
        except Exception:  # noqa: BLE001 - password-protected or malformed PDF.
            return "", False

    pages: list[str] = []
    used = 0
    try:
        total_pages = len(reader.pages)
    except Exception:  # noqa: BLE001 - broken page tree.
        return "", False
    page_count = min(total_pages, limits.max_pdf_pages)
    truncated = total_pages > limits.max_pdf_pages
    for page_index in range(page_count):
        if used >= limits.max_output_chars:
            truncated = True
            break
        try:
            text = reader.pages[page_index].extract_text() or ""
        except Exception:  # noqa: BLE001 - one broken page need not discard the rest.
            truncated = True
            continue
        cleaned = text.strip()
        if not cleaned:
            continue
        separator = 1 if pages else 0
        remaining = limits.max_output_chars - used - separator
        if remaining <= 0:
            truncated = True
            break
        if len(cleaned) > remaining:
            truncated = True
        clipped = cleaned[:remaining].strip()
        if clipped:
            pages.append(clipped)
            used += separator + len(clipped)
    return "\n".join(pages), truncated


def parse_verified_path(
    path: Path,
    *,
    suffix: str,
    expected_size: int,
    expected_sha256: str,
    limits: ParserLimits,
) -> dict[str, Any]:
    """Parse one staged structured file after verifying its exact bytes."""

    normalized_suffix = suffix.casefold()
    if normalized_suffix not in SUPPORTED_STRUCTURED_SUFFIXES:
        _raise("unsupported_type")
    raw = read_verified_bytes(
        path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        max_input_bytes=limits.max_input_bytes,
    )
    text, truncated = (
        _extract_docx_text(raw, limits=limits)
        if normalized_suffix == ".docx"
        else _extract_pdf_text(raw, limits=limits)
    )
    text = text[: limits.max_output_chars]
    warnings = [] if text else ["no_text_extracted"]
    if text and truncated:
        warnings.append("partial_parse")
    source = {
        "source_id": f"src_{expected_sha256[:24]}",
        "display_name": _safe_display_name(path, normalized_suffix),
        "media_type": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
            if normalized_suffix == ".docx"
            else "application/pdf"
        ),
        "kind": normalized_suffix[1:],
        "sha256": expected_sha256,
        "size_bytes": expected_size,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "extraction_status": "extracted" if text else "needs_review",
        "warnings": warnings,
    }
    return source


__all__ = [
    "ParserCoreError",
    "ParserLimits",
    "SUPPORTED_STRUCTURED_SUFFIXES",
    "limits_from_payload",
    "parse_verified_path",
    "read_verified_bytes",
]
