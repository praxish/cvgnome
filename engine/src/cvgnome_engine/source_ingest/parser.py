# SPDX-License-Identifier: MPL-2.0
"""Parent-side boundary for isolated PDF and DOCX extraction."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Sequence

from .parser_core import ParserLimits, SUPPORTED_STRUCTURED_SUFFIXES
from .parser_memory import MemoryMonitorUnavailable, memory_sampler


_PROTOCOL_VERSION = 1
_RESOURCE_POLL_SECONDS = 0.025
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_MESSAGES = {
    "input_changed": "The staged source no longer matches its recorded hash and size.",
    "input_too_large": "The source exceeds the local parser input limit.",
    "input_unavailable": "The staged source is unavailable.",
    "invalid_request": "The local source parser rejected its request.",
    "invalid_response": "The local source parser returned an invalid response.",
    "output_limit": "The local source parser exceeded its output limit.",
    "parser_failed": "The source could not be parsed safely.",
    "parser_timeout": "Source parsing exceeded its time limit.",
    "parser_unavailable": "The local source parser is unavailable.",
    "resource_limit": "Source parsing exceeded a local resource limit.",
    "unsafe_document": "The document package failed local safety checks.",
    "unsupported_type": "Only PDF and DOCX sources are supported by this parser.",
}


class SourceParserError(RuntimeError):
    """A structured parser failure whose message is safe for display."""

    def __init__(self, code: str) -> None:
        normalized = code if code in _SAFE_ERROR_MESSAGES else "parser_failed"
        self.code = normalized
        self.message = _SAFE_ERROR_MESSAGES[normalized]
        super().__init__(self.message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ParserResourceLimits:
    """Linux address-space cap / macOS sampled group-RSS threshold, plus rlimits.

    The macOS threshold is not a hard allocation cap: the parent terminates the
    process group when a sample exceeds it. Unsupported monitoring fails closed.
    """

    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 10
    file_descriptors: int = 64

    def to_payload(self, *, output_bytes: int) -> dict[str, int]:
        return {**asdict(self), "output_bytes": output_bytes}


def _open_regular_file(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceParserError("input_unavailable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SourceParserError("input_unavailable")
        return os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def source_file_identity(
    path: str | os.PathLike[str], *, max_input_bytes: int = 25 * 1024 * 1024
) -> tuple[int, str]:
    """Return ``(size_bytes, sha256)`` for a bounded regular staged file."""

    if (
        isinstance(max_input_bytes, bool)
        or not isinstance(max_input_bytes, int)
        or not 1 <= max_input_bytes <= 100 * 1024 * 1024
    ):
        raise ValueError("max_input_bytes is outside the supported range")
    source_path = Path(path)
    total = 0
    digest = hashlib.sha256()
    try:
        with _open_regular_file(source_path) as handle:
            while True:
                chunk = handle.read(min(1024 * 1024, max_input_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_input_bytes:
                    raise SourceParserError("input_too_large")
                digest.update(chunk)
    except SourceParserError:
        raise
    except OSError as exc:
        raise SourceParserError("input_unavailable") from exc
    return total, digest.hexdigest()


def default_parser_child_command() -> tuple[str, ...]:
    """Return the child command for source and frozen engine runtimes.

    The packaged sidecar entry point must route ``source-parser-child`` to
    :func:`cvgnome_engine.source_ingest.parser_child_main`. Keeping the
    child main callable avoids recursively launching the desktop UI.
    """

    if getattr(sys, "frozen", False):
        return (sys.executable, "source-parser-child")
    return (
        sys.executable,
        "-I",
        "-m",
        "cvgnome_engine.source_ingest.parser_child",
    )


def _child_environment() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass


def _communicate_supervised(
    process: subprocess.Popen[bytes],
    request: bytes,
    response_file: BinaryIO,
    *,
    timeout_seconds: float,
    memory_bytes: int,
    output_bytes: int,
    sample_memory: Callable[[int], int] | None,
) -> None:
    """Wait with a wall deadline and sampled resource checks; kill on failure.

    Sampling cannot prevent a short-lived memory spike between observations.
    Linux additionally has a verified address-space limit inside the child.
    """

    try:
        deadline = time.monotonic() + timeout_seconds
        request_input: bytes | None = request
        while True:
            if sample_memory is not None:
                try:
                    resident_bytes = sample_memory(process.pid)
                except MemoryMonitorUnavailable as exc:
                    if process.poll() is None:
                        raise SourceParserError("parser_unavailable") from exc
                else:
                    if resident_bytes > memory_bytes:
                        raise SourceParserError("resource_limit")
            if os.fstat(response_file.fileno()).st_size > output_bytes:
                raise SourceParserError("output_limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceParserError("parser_timeout")
            try:
                process.communicate(
                    input=request_input,
                    timeout=min(_RESOURCE_POLL_SECONDS, remaining),
                )
            except subprocess.TimeoutExpired:
                # communicate preserves its unsent input across retries.
                request_input = None
                continue
            break
    except SourceParserError:
        _terminate_process_tree(process)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        _terminate_process_tree(process)
        raise SourceParserError("parser_unavailable") from exc


def _validate_limits(limits: ParserLimits, resources: ParserResourceLimits) -> None:
    if not (1 <= limits.max_input_bytes <= 100 * 1024 * 1024):
        raise ValueError("max_input_bytes is outside the supported range")
    if not (1 <= limits.max_output_chars <= 2_000_000):
        raise ValueError("max_output_chars is outside the supported range")
    if not (1 <= limits.max_pdf_pages <= 500):
        raise ValueError("max_pdf_pages is outside the supported range")
    if not (1 <= limits.max_docx_entries <= 2_000):
        raise ValueError("max_docx_entries is outside the supported range")
    if not (1 <= limits.max_docx_member_bytes <= 100 * 1024 * 1024):
        raise ValueError("max_docx_member_bytes is outside the supported range")
    if not (1 <= limits.max_docx_uncompressed_bytes <= 250 * 1024 * 1024):
        raise ValueError("max_docx_uncompressed_bytes is outside the supported range")
    if not (1.0 <= limits.max_docx_compression_ratio <= 10_000.0):
        raise ValueError("max_docx_compression_ratio is outside the supported range")
    if not (64 * 1024 * 1024 <= resources.memory_bytes <= 8 * 1024 * 1024 * 1024):
        raise ValueError("memory_bytes is outside the supported range")
    if not (1 <= resources.cpu_seconds <= 60):
        raise ValueError("cpu_seconds is outside the supported range")
    if not (16 <= resources.file_descriptors <= 256):
        raise ValueError("file_descriptors is outside the supported range")


def _validated_expected_identity(
    *,
    actual_size: int,
    actual_sha256: str,
    expected_size: int | None,
    expected_sha256: str | None,
) -> tuple[int, str]:
    if expected_size is not None and (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size != actual_size
    ):
        raise SourceParserError("input_changed")
    normalized_expected_hash = (
        expected_sha256.strip().casefold()
        if isinstance(expected_sha256, str)
        else actual_sha256
    )
    if not _SHA256_RE.fullmatch(normalized_expected_hash):
        raise SourceParserError("input_changed")
    if normalized_expected_hash != actual_sha256:
        raise SourceParserError("input_changed")
    return actual_size, actual_sha256


def _response_budget(max_output_chars: int) -> int:
    # Every extracted character can expand to a six-byte JSON escape.
    return max(64 * 1024, max_output_chars * 6 + 32 * 1024)


def _validate_source(
    raw: Any,
    *,
    expected_size: int,
    expected_sha256: str,
    suffix: str,
    max_output_chars: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SourceParserError("invalid_response")
    source_id = raw.get("source_id")
    display_name = raw.get("display_name")
    media_type = raw.get("media_type")
    kind = raw.get("kind")
    sha256 = raw.get("sha256")
    size_bytes = raw.get("size_bytes")
    text = raw.get("text")
    text_sha256 = raw.get("text_sha256")
    extraction_status = raw.get("extraction_status")
    warnings = raw.get("warnings")
    expected_kind = suffix[1:]
    expected_media_type = (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
        if suffix == ".docx"
        else "application/pdf"
    )
    if (
        source_id != f"src_{expected_sha256[:24]}"
        or not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name) > 255
        or any(not character.isprintable() for character in display_name)
        or media_type != expected_media_type
        or kind != expected_kind
        or sha256 != expected_sha256
        or isinstance(size_bytes, bool)
        or size_bytes != expected_size
        or not isinstance(text, str)
        or len(text) > max_output_chars
        or not isinstance(text_sha256, str)
        or text_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest()
        or extraction_status not in {"extracted", "needs_review"}
        or not isinstance(warnings, list)
        or any(
            not isinstance(item, str)
            or item not in ("no_text_extracted", "partial_parse")
            for item in warnings
        )
        or len(set(warnings)) != len(warnings)
        or ("no_text_extracted" in warnings) != (not text)
        or ("partial_parse" in warnings and not text)
    ):
        raise SourceParserError("invalid_response")
    if bool(text) != (extraction_status == "extracted"):
        raise SourceParserError("invalid_response")
    # Reconstruct the result so unexpected child keys can never cross the boundary.
    return {
        "source_id": source_id,
        "display_name": display_name,
        "media_type": media_type,
        "kind": kind,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "text": text,
        "text_sha256": text_sha256,
        "extraction_status": extraction_status,
        "warnings": list(warnings),
    }


def parse_structured_source(
    path: str | os.PathLike[str],
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    limits: ParserLimits | None = None,
    resource_limits: ParserResourceLimits | None = None,
    timeout_seconds: float = 15.0,
    child_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Verify and extract one staged PDF or DOCX in an isolated process.

    Supplying ``expected_size`` and ``expected_sha256`` verifies a native
    staging manifest. When omitted, the parent records the identity immediately
    before launch and the child verifies that the exact same bytes are read.
    ``child_command`` is primarily for a packaged sidecar or boundary tests.
    """

    source_path = Path(path)
    suffix = source_path.suffix.casefold()
    if suffix not in SUPPORTED_STRUCTURED_SUFFIXES:
        raise SourceParserError("unsupported_type")
    active_limits = limits or ParserLimits()
    active_resources = resource_limits or ParserResourceLimits()
    _validate_limits(active_limits, active_resources)
    if not isinstance(timeout_seconds, (int, float)) or isinstance(
        timeout_seconds, bool
    ):
        raise ValueError("timeout_seconds must be a number")
    bounded_timeout = max(0.05, min(float(timeout_seconds), 60.0))

    actual_size, actual_sha256 = source_file_identity(
        source_path, max_input_bytes=active_limits.max_input_bytes
    )
    verified_size, verified_sha256 = _validated_expected_identity(
        actual_size=actual_size,
        actual_sha256=actual_sha256,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    max_response_bytes = _response_budget(active_limits.max_output_chars)
    payload = {
        "version": _PROTOCOL_VERSION,
        "path": str(source_path.resolve()),
        "suffix": suffix,
        "expected_size": verified_size,
        "expected_sha256": verified_sha256,
        "parser_limits": active_limits.to_payload(),
        "resource_limits": active_resources.to_payload(
            output_bytes=max_response_bytes
        ),
    }
    encoded_request = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded_request) > 64 * 1024:
        raise SourceParserError("invalid_request")
    command = tuple(child_command or default_parser_child_command())
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("child_command must contain non-empty strings")
    try:
        sample_memory = memory_sampler()
    except MemoryMonitorUnavailable as exc:
        raise SourceParserError("parser_unavailable") from exc

    try:
        with tempfile.TemporaryFile() as response_file:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=response_file,
                    stderr=subprocess.DEVNULL,
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=_child_environment(),
                    close_fds=True,
                    start_new_session=(os.name == "posix"),
                )
            except OSError as exc:
                raise SourceParserError("parser_unavailable") from exc
            _communicate_supervised(
                process,
                encoded_request,
                response_file,
                timeout_seconds=bounded_timeout,
                memory_bytes=active_resources.memory_bytes,
                output_bytes=max_response_bytes,
                sample_memory=sample_memory,
            )
            response_file.seek(0)
            stdout = response_file.read(max_response_bytes + 1)
    except SourceParserError:
        raise
    except OSError as exc:
        raise SourceParserError("parser_unavailable") from exc

    if len(stdout) > max_response_bytes:
        raise SourceParserError("output_limit")
    if not stdout and process.returncode is not None and process.returncode < 0:
        output_signal = getattr(signal, "SIGXFSZ", None)
        if output_signal is not None and -process.returncode == output_signal:
            raise SourceParserError("output_limit")
        resource_signals = {
            candidate
            for name in ("SIGABRT", "SIGKILL", "SIGSEGV", "SIGXCPU")
            if (candidate := getattr(signal, name, None)) is not None
        }
        if -process.returncode in resource_signals:
            raise SourceParserError("resource_limit")
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceParserError("invalid_response") from exc
    if not isinstance(response, dict) or response.get("version") != _PROTOCOL_VERSION:
        raise SourceParserError("invalid_response")
    if response.get("ok") is True:
        if process.returncode != 0:
            raise SourceParserError("parser_failed")
        return _validate_source(
            response.get("source"),
            expected_size=verified_size,
            expected_sha256=verified_sha256,
            suffix=suffix,
            max_output_chars=active_limits.max_output_chars,
        )

    raw_error = response.get("error")
    code = raw_error.get("code") if isinstance(raw_error, dict) else None
    if not isinstance(code, str):
        raise SourceParserError("parser_failed")
    raise SourceParserError(code)


__all__ = [
    "ParserLimits",
    "ParserResourceLimits",
    "SourceParserError",
    "default_parser_child_command",
    "parse_structured_source",
    "source_file_identity",
]
