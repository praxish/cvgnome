# SPDX-License-Identifier: MPL-2.0
"""Private child-process protocol for untrusted structured documents."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, BinaryIO

from .parser_core import (
    ParserCoreError,
    limits_from_payload,
    parse_verified_path,
)


_PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str) -> dict[str, Any]:
    return {
        "version": _PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": code[:64]},
    }


def _emit(payload: dict[str, Any], stream: BinaryIO) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    stream.write(encoded)
    stream.flush()


def _lower_resource_limit(resource_module: Any, name: str, desired: int) -> None:
    resource_id = getattr(resource_module, name, None)
    if resource_id is None:
        raise ParserCoreError("parser_unavailable")
    try:
        current_soft, current_hard = resource_module.getrlimit(resource_id)
        infinity = resource_module.RLIM_INFINITY
        hard = desired if current_hard == infinity else min(int(current_hard), desired)
        soft = min(desired, hard)
        if current_soft != infinity:
            soft = min(int(current_soft), soft)
        resource_module.setrlimit(resource_id, (soft, hard))
        actual_soft, actual_hard = resource_module.getrlimit(resource_id)
        if any(value == infinity or value > desired for value in (actual_soft, actual_hard)):
            raise ParserCoreError("parser_unavailable")
    except (OSError, ValueError) as exc:
        raise ParserCoreError("parser_unavailable") from exc


def _bounded_resource_int(
    payload: dict[str, Any], key: str, *, minimum: int, maximum: int
) -> int:
    raw = payload.get(key)
    if isinstance(raw, bool):
        raise ParserCoreError("invalid_request")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        raise ParserCoreError("invalid_request") from None
    if parsed < minimum or parsed > maximum:
        raise ParserCoreError("invalid_request")
    return parsed


def _apply_resource_limits(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ParserCoreError("invalid_request")
    memory_bytes = _bounded_resource_int(
        payload,
        "memory_bytes",
        minimum=64 * 1024 * 1024,
        maximum=8 * 1024 * 1024 * 1024,
    )
    cpu_seconds = _bounded_resource_int(
        payload, "cpu_seconds", minimum=1, maximum=60
    )
    file_descriptors = _bounded_resource_int(
        payload, "file_descriptors", minimum=16, maximum=256
    )
    output_bytes = _bounded_resource_int(
        payload, "output_bytes", minimum=64 * 1024, maximum=16 * 1024 * 1024
    )
    if os.name != "posix" or not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        raise ParserCoreError("parser_unavailable")
    try:
        import resource
    except ImportError as exc:
        raise ParserCoreError("parser_unavailable") from exc
    if sys.platform.startswith("linux"):
        _lower_resource_limit(resource, "RLIMIT_AS", memory_bytes)
    # macOS RLIMIT_AS is ineffective. The parent requires an available native
    # process-group RSS watchdog before supplying this child's document request.
    _lower_resource_limit(resource, "RLIMIT_CPU", max(1, math.ceil(cpu_seconds)))
    _lower_resource_limit(resource, "RLIMIT_NOFILE", file_descriptors)
    _lower_resource_limit(resource, "RLIMIT_FSIZE", output_bytes)
    _lower_resource_limit(resource, "RLIMIT_CORE", 0)


def _read_request(stream: BinaryIO) -> dict[str, Any]:
    raw = stream.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise ParserCoreError("invalid_request")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ParserCoreError("invalid_request") from None
    if not isinstance(payload, dict) or payload.get("version") != _PROTOCOL_VERSION:
        raise ParserCoreError("invalid_request")
    return payload


def child_main(
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
) -> int:
    """Serve exactly one bounded parser request and emit exactly one response."""

    reader = input_stream or sys.stdin.buffer
    writer = output_stream or sys.stdout.buffer
    try:
        payload = _read_request(reader)
        _apply_resource_limits(payload.get("resource_limits"))
        raw_path = payload.get("path")
        suffix = payload.get("suffix")
        expected_size = payload.get("expected_size")
        expected_sha256 = payload.get("expected_sha256")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or len(raw_path) > 4096
            or not isinstance(suffix, str)
            or len(suffix) > 16
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_sha256, str)
            or not _SHA256_RE.fullmatch(expected_sha256)
        ):
            raise ParserCoreError("invalid_request")
        limits = limits_from_payload(payload.get("parser_limits"))
        source = parse_verified_path(
            Path(raw_path),
            suffix=suffix,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            limits=limits,
        )
        _emit({"version": _PROTOCOL_VERSION, "ok": True, "source": source}, writer)
        return 0
    except ParserCoreError as exc:
        _emit(_error(exc.code), writer)
        return 2
    except MemoryError:
        _emit(_error("resource_limit"), writer)
        return 3
    except Exception:  # noqa: BLE001 - raw parser details never cross this boundary.
        _emit(_error("parser_failed"), writer)
        return 3


def main() -> int:
    return child_main()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["child_main", "main"]
