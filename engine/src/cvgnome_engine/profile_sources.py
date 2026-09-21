# SPDX-License-Identifier: MPL-2.0
"""Offline profile construction from natively staged source documents.

This module is the narrow integration layer between the Tauri folder scanner,
the isolated document parser, deterministic synthesis, and immutable vault
persistence.  It never enumerates user-selected folders and never returns
source names, paths, text, or hashes to the renderer.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO

from .profile import canonical_profile_metrics, prepare_canonical_profile
from .review_inbox import partition_review_candidates
from .source_review import build_source_checks
from .resume import is_baseline_resume_renderable
from .source_ingest import (
    ParserLimits,
    SourceParserError,
    SynthesisLimits,
    parse_structured_source,
    synthesize_canonical_profile,
)
from .storage import (
    commit_profile_source_scan,
    create_profile_source_scan,
    discard_all_profile_source_scans,
    discard_profile_source_scan,
    latest_profile,
)

MAX_SOURCE_FILES = 64
MAX_SOURCE_TOTAL_BYTES = 40 * 1024 * 1024
MAX_STRUCTURED_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000
MAX_EXTRACTED_TOTAL_CHARS = 2_000_000
MAX_CANDIDATE_PROFILE_BYTES = 250_000
STRUCTURED_PARSE_TIMEOUT_SECONDS = 15.0
# The native request boundary is 120 seconds. Keep the isolated-parser work
# comfortably inside it so synthesis, persistence, and IPC still have time to
# finish and return an aggregate preview.
SOURCE_PREVIEW_PARSE_BUDGET_SECONDS = 75.0
MIN_STRUCTURED_PARSE_TIMEOUT_SECONDS = 0.05
SCAN_TTL_MS = 23 * 60 * 60 * 1_000
PARSER_CONTRACT = "local-source-parser-v1"
SYNTHESIS_CONTRACT = "deterministic-local-v1"

SUPPORTED_FORMATS = frozenset({"pdf", "docx", "json", "md", "txt", "csv"})
STRUCTURED_FORMATS = frozenset({"pdf", "docx"})
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    "json": "application/json",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
}
WARNING_STAGES = {"scan": 0, "parse": 1, "synthesis": 2}
WARNING_SEVERITIES = {"info": 0, "warning": 1, "blocking": 2}
PROFILE_KEYS = frozenset(
    {
        "basics",
        "work",
        "volunteer",
        "education",
        "awards",
        "certificates",
        "publications",
        "skills",
        "languages",
        "interests",
        "references",
        "projects",
    }
)
PREFERENCE_RE = re.compile(
    r"\b(?:work preferences?|location preferences?|remote preferences?|"
    r"target roles?|employment type|dealbreakers?)\b",
    re.IGNORECASE,
)
RESUME_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:professional |work )?experience\s*$|"
    r"(?:^|\n)\s*(?:#{1,6}\s*)?education\s*$|"
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:technical |core )?skills\s*$|"
    r"(?:^|\n)\s*(?:company|employer|organization|position|job title)\s*[:|]",
    re.IGNORECASE | re.MULTILINE,
)
EVIDENCE_RE = re.compile(
    r"\b(?:achievement|accomplishment|impact|result|increased|reduced|saved|"
    r"grew|launched|shipped|built|delivered)\b.{0,100}"
    r"(?:\d+(?:\.\d+)?\s*(?:%|x|hours?|days?|users?|customers?|k|million)|"
    r"[$£€]\s*\d+)",
    re.IGNORECASE,
)


class ProfileSourceWorkflowError(RuntimeError):
    """A profile-source failure with a stable, renderer-safe error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise ProfileSourceWorkflowError(code, message)


def _canonical_scan_id(value: Any) -> str:
    if not isinstance(value, str):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    normalized = str(parsed)
    if normalized != value:
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    return normalized


def _bounded_count(value: Any, *, maximum: int = 1_000_000) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > maximum
    ):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    return value


def _safe_display_name(value: Any, *, ordinal: int) -> str:
    if not isinstance(value, str):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    cleaned = "".join(character for character in value if character.isprintable()).strip()
    if not cleaned or len(cleaned) > 240:
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    return cleaned


def _sha256(value: Any) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    return value


def _managed_staged_path(
    data_dir: Path,
    *,
    scan_id: str,
    ordinal: int,
    source_format: str,
    value: Any,
) -> Path:
    if not isinstance(value, str) or "\\" in value:
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    relative = Path(value)
    expected_name = f"{ordinal:04d}.{source_format}"
    if (
        relative.is_absolute()
        or relative.parts != ("imports", "staging", scan_id, expected_name)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    root = data_dir.expanduser().resolve()
    target = root / relative
    expected_parent = root / "imports" / "staging" / scan_id
    try:
        if target.parent.resolve(strict=True) != expected_parent.resolve(strict=True):
            _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
        metadata = target.lstat()
    except OSError:
        _fail(
            "profile_source_input_changed",
            "A staged source changed or became unavailable. Scan the folder again.",
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail(
            "profile_source_input_changed",
            "A staged source changed or became unavailable. Scan the folder again.",
        )
    return target


def _open_regular(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(
            "profile_source_input_changed",
            "A staged source changed or became unavailable. Scan the folder again.",
        )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(
                "profile_source_input_changed",
                "A staged source changed or became unavailable. Scan the folder again.",
            )
        return os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def _read_verified_bytes(item: dict[str, Any]) -> bytes:
    maximum = (
        MAX_STRUCTURED_BYTES
        if item["format"] in STRUCTURED_FORMATS
        else MAX_TEXT_BYTES
    )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with _open_regular(item["path"]) as stream:
            while True:
                chunk = stream.read(min(64 * 1024, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum or total > item["byte_size"]:
                    _fail(
                        "profile_source_input_changed",
                        "A staged source changed or became unavailable. Scan the folder again.",
                    )
                digest.update(chunk)
                chunks.append(chunk)
    except ProfileSourceWorkflowError:
        raise
    except OSError:
        _fail(
            "profile_source_input_changed",
            "A staged source changed or became unavailable. Scan the folder again.",
        )
    if total != item["byte_size"] or digest.hexdigest() != item["checksum_sha256"]:
        _fail(
            "profile_source_input_changed",
            "A staged source changed or became unavailable. Scan the folder again.",
        )
    return b"".join(chunks)


def _normalized_manifest(data_dir: Path, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    scan_id = _canonical_scan_id(params.get("scan_id"))
    raw_sources = params.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) > MAX_SOURCE_FILES:
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    items: list[dict[str, Any]] = []
    total_bytes = 0
    for ordinal, raw in enumerate(raw_sources):
        raw_ordinal = raw.get("ordinal") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or not isinstance(raw_ordinal, int)
            or isinstance(raw_ordinal, bool)
            or raw_ordinal != ordinal
        ):
            _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
        source_format = str(raw.get("format") or "").strip().casefold().lstrip(".")
        if source_format == "markdown":
            source_format = "md"
        if source_format not in SUPPORTED_FORMATS:
            _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
        byte_size = _bounded_count(raw.get("byte_size"), maximum=MAX_STRUCTURED_BYTES)
        maximum = MAX_STRUCTURED_BYTES if source_format in STRUCTURED_FORMATS else MAX_TEXT_BYTES
        if byte_size <= 0 or byte_size > maximum:
            _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
        total_bytes += byte_size
        if total_bytes > MAX_SOURCE_TOTAL_BYTES:
            _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
        items.append(
            {
                "ordinal": ordinal,
                "managed_relative_path": raw.get("managed_relative_path"),
                "display_name": _safe_display_name(raw.get("display_name"), ordinal=ordinal),
                "format": source_format,
                "media_type": MEDIA_TYPES[source_format],
                "byte_size": byte_size,
                "checksum_sha256": _sha256(raw.get("checksum_sha256")),
                "path": _managed_staged_path(
                    data_dir,
                    scan_id=scan_id,
                    ordinal=ordinal,
                    source_format=source_format,
                    value=raw.get("managed_relative_path"),
                ),
            }
        )
    return scan_id, items


def _decode_text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_utf8") from exc
    if "\x00" in text:
        raise ValueError("nul_character")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _profile_candidate(value: Any, *, depth: int = 4) -> dict[str, Any] | None:
    if depth <= 0 or not isinstance(value, dict):
        return None
    if isinstance(value.get("basics"), dict) or any(
        isinstance(value.get(key), list) for key in PROFILE_KEYS - {"basics"}
    ):
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        return value if len(encoded) <= MAX_CANDIDATE_PROFILE_BYTES else None
    for key in ("canonical_profile", "profile", "resume_json", "baseline_resume_json", "payload"):
        found = _profile_candidate(value.get(key), depth=depth - 1)
        if found is not None:
            return found
    return None


def _csv_as_text(text: str) -> tuple[str, bool]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[list[str]] = []
    clipped = False
    for index, row in enumerate(reader):
        if index >= 2_000:
            clipped = True
            break
        if len(row) > 100 or any(len(cell) > 4_000 or "\x00" in cell for cell in row):
            raise ValueError("unsafe_csv")
        rows.append(row)
    if not rows:
        return "", clipped
    headers = [re.sub(r"\s+", " ", cell).strip()[:120] for cell in rows[0]]
    useful_headers = bool(headers) and all(headers) and len(set(headers)) == len(headers)
    output: list[str] = []
    if useful_headers and len(rows) > 1:
        for row in rows[1:]:
            block = [
                f"{header}: {re.sub(r'\s+', ' ', value).strip()}"
                for header, value in zip(headers, row, strict=False)
                if value.strip()
            ]
            if block:
                output.extend(block)
                output.append("")
    else:
        output = [" | ".join(cell.strip() for cell in row) for row in rows]
    rendered = "\n".join(output).strip()
    if len(rendered) > MAX_EXTRACTED_CHARS:
        rendered = rendered[:MAX_EXTRACTED_CHARS]
        clipped = True
    return rendered, clipped


def _parse_text_item(item: dict[str, Any]) -> tuple[str, dict[str, Any] | None, list[str]]:
    raw = _read_verified_bytes(item)
    text = _decode_text(raw)
    warnings: list[str] = []
    candidate: dict[str, Any] | None = None
    if item["format"] == "json":
        parsed = json.loads(text)
        candidate = _profile_candidate(parsed)
        text = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    elif item["format"] == "csv":
        text, clipped = _csv_as_text(text)
        if clipped:
            warnings.append("partial_parse")
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
        warnings.append("partial_parse")
    if not text.strip():
        raise ValueError("no_text")
    return text, candidate, sorted(set(warnings))


def _parse_item(
    item: dict[str, Any],
    *,
    structured_timeout_seconds: float = STRUCTURED_PARSE_TIMEOUT_SECONDS,
) -> tuple[str, dict[str, Any] | None, list[str]]:
    if item["format"] not in STRUCTURED_FORMATS:
        return _parse_text_item(item)
    parsed = parse_structured_source(
        item["path"],
        expected_size=item["byte_size"],
        expected_sha256=item["checksum_sha256"],
        limits=ParserLimits(
            max_input_bytes=MAX_STRUCTURED_BYTES,
            max_output_chars=MAX_EXTRACTED_CHARS,
        ),
        timeout_seconds=structured_timeout_seconds,
    )
    text = str(parsed.get("text") or "")
    if not text.strip():
        raise ValueError("no_text")
    warnings = ["partial_parse" for value in parsed.get("warnings", []) if value]
    return text, None, sorted(set(warnings))


def _parser_contract(source_format: str) -> str:
    return f"{PARSER_CONTRACT}-{source_format}"


def _classify_source(text: str, candidate: dict[str, Any] | None) -> str:
    if candidate is not None:
        return "resume"
    preference = bool(PREFERENCE_RE.search(text))
    resume = bool(RESUME_RE.search(text)) or bool(
        re.search(r"(?:^|\n)\s*(?:name|full name|email|phone)\s*[:|]", text, re.I)
    )
    evidence = bool(EVIDENCE_RE.search(text))
    if resume:
        return "resume"
    if preference:
        return "preferences"
    if evidence:
        return "evidence"
    return "unclassified"


def _warning(
    code: str,
    *,
    stage: str,
    severity: str,
    count: int = 1,
) -> dict[str, Any]:
    return {"code": code, "stage": stage, "severity": severity, "count": count}


def _aggregate_warnings(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for value in values:
        count = value.get("count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            counts[(str(value["code"]), str(value["stage"]), str(value["severity"]))] += count
    return [
        _warning(code, stage=stage, severity=severity, count=count)
        for (code, stage, severity), count in sorted(
            counts.items(),
            key=lambda item: (
                WARNING_STAGES.get(item[0][1], 99),
                WARNING_SEVERITIES.get(item[0][2], 99),
                item[0][0],
            ),
        )
    ]


def _scan_warnings(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    warnings: list[dict[str, Any]] = []
    for raw_code, raw_count in raw.items():
        if not isinstance(raw_code, str):
            _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
        count = _bounded_count(raw_count)
        if not count:
            continue
        if raw_code == "unsupported_file_type":
            warnings.append(_warning(raw_code, stage="scan", severity="info", count=count))
        elif raw_code == "file_too_large":
            warnings.append(_warning(raw_code, stage="scan", severity="warning", count=count))
        elif raw_code == "duplicate_content":
            # Duplicate files are counted after the staged manifest is verified.
            continue
        elif raw_code == "depth_limit_reached":
            warnings.append(
                _warning("scan_limit_reached", stage="scan", severity="warning", count=count)
            )
    return warnings


def _source_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {kind: 0 for kind in ("resume", "evidence", "preferences", "unclassified")}
    for item in items:
        if item["extraction_status"] == "parsed":
            counts[item["source_kind"]] += 1
    return counts


def _profile_counts(profile: dict[str, Any]) -> dict[str, int]:
    metrics = canonical_profile_metrics(profile)
    context = (
        profile.get("meta", {}).get("canonical_context", {})
        if isinstance(profile.get("meta"), dict)
        else {}
    )
    if not isinstance(context, dict):
        context = {}
    evidence = context.get("impact_evidence")
    preferences = context.get("work_preferences")
    return {
        "work_entries": int(metrics["work_entries"]),
        "education_entries": len(profile.get("education", []))
        if isinstance(profile.get("education"), list)
        else 0,
        "skill_groups": int(metrics["skill_groups"]),
        "evidence_claims": len(evidence) if isinstance(evidence, list) else 0,
        "preference_items": len(preferences) if isinstance(preferences, list) else 0,
    }


def _profile_name(profile: dict[str, Any]) -> str:
    basics = profile.get("basics")
    value = basics.get("name") if isinstance(basics, dict) else None
    return value.strip()[:160] if isinstance(value, str) and value.strip() else "Unnamed profile"


def _monotonic() -> float:
    """Return monotonic time through a narrow, testable deadline boundary."""

    return time.monotonic()


def _failed_parse_item(
    safe_item: dict[str, Any],
    *,
    parser_contract: str,
    issue_code: str,
) -> dict[str, Any]:
    return {
        **safe_item,
        "source_kind": "unclassified",
        "parser_contract": parser_contract,
        "extraction_status": "failed",
        "issue_code": issue_code,
        "extracted_text": "",
        "candidate_profile": None,
        "warnings": [issue_code],
    }


def preview_profile_sources(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Parse a native staging manifest and persist an aggregate-only preview."""

    scan_id, manifest = _normalized_manifest(data_dir, params)
    raw_counts = params.get("file_counts")
    if not isinstance(raw_counts, dict):
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")
    discovered = _bounded_count(raw_counts.get("discovered"))
    staged = _bounded_count(raw_counts.get("staged"), maximum=MAX_SOURCE_FILES)
    skipped = _bounded_count(raw_counts.get("skipped"))
    if staged != len(manifest) or discovered < staged:
        _fail("profile_source_manifest_invalid", "The source scan manifest is invalid.")

    warnings = _scan_warnings(params.get("scan_issues", {}))
    format_counts = dict(sorted(Counter(item["format"] for item in manifest).items()))
    current = latest_profile(data_dir)
    existing_profile = current["profile"] if current is not None else None
    base_reference = (
        {
            "profile_version_id": current["id"],
            "version_number": current["version_number"],
            "profile_name": _profile_name(current["profile"]),
        }
        if current is not None
        else None
    )
    expires_at_ms = time.time_ns() // 1_000_000 + SCAN_TTL_MS

    if not manifest:
        # There is no database preview to reconcile later, so remove the empty
        # UUID-scoped native staging directory before returning its aggregates.
        discard_profile_source_scan(data_dir, scan_id)
        warnings.extend(
            [
                _warning("missing_identity", stage="synthesis", severity="blocking"),
                _warning(
                    "insufficient_resume_content",
                    stage="synthesis",
                    severity="blocking",
                ),
            ]
        )
        return {
            "scan_id": scan_id,
            "expires_at_ms": expires_at_ms,
            "base_profile": base_reference,
            "file_counts": {
                "discovered": discovered,
                "staged": staged,
                "parsed": 0,
                "duplicates": 0,
                "skipped": skipped,
                "failed": 0,
            },
            "source_counts": _source_counts([]),
            "format_counts": format_counts,
            "warnings": _aggregate_warnings(warnings),
            "can_build": False,
            "review_candidate_count": 0,
            "source_review_count": 0,
        }

    # Parse each unique byte snapshot/parser-contract pair once. Allocation is
    # a second pass so a noisy text file cannot consume the aggregate budget
    # before a structured profile source discovered later in the folder.
    parse_deadline = _monotonic() + SOURCE_PREVIEW_PARSE_BUDGET_SECONDS
    unique_items: dict[tuple[str, str], dict[str, Any]] = {}
    for item in manifest:
        checksum = item["checksum_sha256"]
        parser_contract = _parser_contract(item["format"])
        extraction_key = (checksum, parser_contract)
        if extraction_key in unique_items:
            continue
        safe_item = {key: value for key, value in item.items() if key != "path"}
        structured_timeout_seconds = STRUCTURED_PARSE_TIMEOUT_SECONDS
        budget_limited_timeout = False
        if item["format"] in STRUCTURED_FORMATS:
            remaining_seconds = parse_deadline - _monotonic()
            if remaining_seconds < MIN_STRUCTURED_PARSE_TIMEOUT_SECONDS:
                unique_items[extraction_key] = _failed_parse_item(
                    safe_item,
                    parser_contract=parser_contract,
                    issue_code="partial_parse",
                )
                warnings.append(_warning("partial_parse", stage="parse", severity="warning"))
                continue
            structured_timeout_seconds = min(
                STRUCTURED_PARSE_TIMEOUT_SECONDS,
                remaining_seconds,
            )
            budget_limited_timeout = (
                structured_timeout_seconds < STRUCTURED_PARSE_TIMEOUT_SECONDS
            )
        try:
            text, candidate, item_warnings = _parse_item(
                item,
                structured_timeout_seconds=structured_timeout_seconds,
            )
        except SourceParserError as exc:
            if exc.code in {"input_changed", "input_too_large", "input_unavailable"}:
                _fail(
                    "profile_source_input_changed",
                    "A staged source changed or became unavailable. Scan the folder again.",
                )
            issue_code = (
                "partial_parse"
                if exc.code == "parser_timeout" and budget_limited_timeout
                else "unreadable_document"
            )
            unique_items[extraction_key] = _failed_parse_item(
                safe_item,
                parser_contract=parser_contract,
                issue_code=issue_code,
            )
            warnings.append(_warning(issue_code, stage="parse", severity="warning"))
            continue
        except (ValueError, csv.Error, json.JSONDecodeError):
            unique_items[extraction_key] = _failed_parse_item(
                safe_item,
                parser_contract=parser_contract,
                issue_code="unreadable_document",
            )
            warnings.append(_warning("unreadable_document", stage="parse", severity="warning"))
            continue

        source_kind = _classify_source(text, candidate)
        unique_items[extraction_key] = {
            **safe_item,
            "source_kind": source_kind,
            "parser_contract": parser_contract,
            "extraction_status": "parsed",
            "issue_code": None,
            "extracted_text": text,
            "extracted_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "candidate_profile": candidate,
            "warnings": item_warnings,
        }

    source_priority = {"resume": 1, "evidence": 2, "preferences": 3, "unclassified": 4}
    allocatable = sorted(
        (
            item
            for item in unique_items.values()
            if item["extraction_status"] == "parsed"
        ),
        key=lambda item: (
            0
            if item.get("candidate_profile") is not None
            else source_priority[item["source_kind"]],
            str(item["display_name"]).casefold(),
            str(item["display_name"]),
            str(item["checksum_sha256"]),
            str(item["parser_contract"]),
        ),
    )
    remaining_extracted_chars = MAX_EXTRACTED_TOTAL_CHARS
    for item in allocatable:
        text = str(item["extracted_text"])
        if remaining_extracted_chars <= 0:
            item.update(
                {
                    "source_kind": "unclassified",
                    "extraction_status": "failed",
                    "issue_code": "partial_parse",
                    "extracted_text": "",
                    "candidate_profile": None,
                    "warnings": ["partial_parse"],
                }
            )
            item.pop("extracted_text_sha256", None)
            warnings.append(_warning("partial_parse", stage="parse", severity="warning"))
            continue
        if len(text) > remaining_extracted_chars:
            text = text[:remaining_extracted_chars]
            item["extracted_text"] = text
            item["extracted_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            item["candidate_profile"] = None
            item["warnings"] = sorted({*item["warnings"], "partial_parse"})
        remaining_extracted_chars -= len(text)

    persistence_items: list[dict[str, Any]] = []
    synthesis_sources: list[dict[str, Any]] = []
    emitted_extractions: set[tuple[str, str]] = set()
    for item in manifest:
        checksum = item["checksum_sha256"]
        extraction_key = (checksum, _parser_contract(item["format"]))
        primary = unique_items[extraction_key]
        if extraction_key in emitted_extractions:
            persistence_items.append(
                {
                    **{key: value for key, value in item.items() if key != "path"},
                    "source_kind": primary["source_kind"],
                    "parser_contract": primary["parser_contract"],
                    "extraction_status": "duplicate",
                    "issue_code": "duplicate_content",
                    "extracted_text": "",
                    "extracted_text_sha256": hashlib.sha256(b"").hexdigest(),
                    "candidate_profile": None,
                    "warnings": ["duplicate_content"],
                }
            )
            warnings.append(_warning("duplicate_content", stage="parse", severity="info"))
            continue
        emitted_extractions.add(extraction_key)
        persistence_items.append(primary)
        if primary["extraction_status"] != "parsed":
            continue
        synthesis_sources.append(
            {
                "display_name": primary["display_name"],
                "media_type": primary["media_type"],
                "sha256": checksum,
                "text": primary["extracted_text"],
                "origin_ordinal": int(primary["ordinal"]),
            }
        )
        if primary["source_kind"] == "unclassified":
            warnings.append(_warning("unclassified_source", stage="parse", severity="warning"))
        if primary["warnings"]:
            warnings.append(_warning("partial_parse", stage="parse", severity="warning"))

    draft_profile, synthesis_report = synthesize_canonical_profile(
        existing_profile=existing_profile,
        sources=synthesis_sources,
        limits=SynthesisLimits(
            max_sources=MAX_SOURCE_FILES,
            max_chars_per_source=MAX_EXTRACTED_CHARS,
            max_total_chars=MAX_EXTRACTED_TOTAL_CHARS,
        ),
    )
    review_candidates: list[dict[str, Any]] = []
    if existing_profile is not None and is_baseline_resume_renderable(existing_profile):
        draft_profile, review_candidates = partition_review_candidates(
            existing_profile=existing_profile,
            draft_profile=draft_profile,
            synthesis_sources=synthesis_sources,
        )
    prepared_profile, compaction_report = prepare_canonical_profile(draft_profile)
    source_checks = build_source_checks(
        profile=prepared_profile,
        basic_candidates=synthesis_report.pop("basic_candidates", []),
        sources=synthesis_sources,
        items=persistence_items,
    )
    conflicts = int(synthesis_report.get("fact_actions", {}).get("conflict_preserved", 0))
    if conflicts:
        warnings.append(
            _warning("conflicting_facts", stage="synthesis", severity="warning", count=conflicts)
        )
    if synthesis_report.get("warnings"):
        warnings.append(_warning("partial_parse", stage="synthesis", severity="warning"))
    basics = prepared_profile.get("basics")
    has_name = bool(
        isinstance(basics, dict)
        and isinstance(basics.get("name"), str)
        and basics["name"].strip()
    )
    renderable = is_baseline_resume_renderable(prepared_profile)
    if not has_name:
        warnings.append(_warning("missing_identity", stage="synthesis", severity="blocking"))
    if not renderable:
        warnings.append(
            _warning(
                "insufficient_resume_content",
                stage="synthesis",
                severity="blocking",
            )
        )
    aggregate_warnings = _aggregate_warnings(warnings)
    parsed_count = sum(
        item["extraction_status"] == "parsed" for item in persistence_items
    )
    duplicate_count = sum(
        item["extraction_status"] == "duplicate" for item in persistence_items
    )
    failed_count = sum(
        item["extraction_status"] == "failed" for item in persistence_items
    )
    can_build = bool(parsed_count and renderable and not any(
        warning["severity"] == "blocking" for warning in aggregate_warnings
    ))
    file_counts = {
        "discovered": discovered,
        "staged": staged,
        "parsed": parsed_count,
        "duplicates": duplicate_count,
        "skipped": skipped,
        "failed": failed_count,
    }
    source_counts = _source_counts(persistence_items)
    profile_counts = _profile_counts(prepared_profile)
    used_sources = int(synthesis_report.get("source_count_used", 0))
    report = {
        "synthesis_contract": SYNTHESIS_CONTRACT,
        "warning_count": sum(warning["count"] for warning in aggregate_warnings),
        "conflict_count": conflicts,
        "synthesis": synthesis_report,
        "compaction": compaction_report,
        "ui": {
            "file_counts": file_counts,
            "source_counts": source_counts,
            "format_counts": format_counts,
            "warnings": aggregate_warnings,
            "can_build": can_build,
            "profile_counts": profile_counts,
            "used_sources": used_sources,
            "review_candidate_count": len(review_candidates),
            "source_review_count": len(source_checks),
        },
        "provider_calls": 0,
    }
    create_profile_source_scan(
        data_dir,
        scan_id=scan_id,
        base_profile_version_id=current["id"] if current is not None else None,
        base_profile_checksum_sha256=(
            current["checksum_sha256"] if current is not None else None
        ),
        items=persistence_items,
        draft_profile=prepared_profile,
        report=report,
        expires_at_ms=expires_at_ms,
        review_candidates=review_candidates,
        source_checks=source_checks,
    )
    return {
        "scan_id": scan_id,
        "expires_at_ms": expires_at_ms,
        "base_profile": base_reference,
        "file_counts": file_counts,
        "source_counts": source_counts,
        "format_counts": format_counts,
        "warnings": aggregate_warnings,
        "can_build": can_build,
        "review_candidate_count": len(review_candidates),
        "source_review_count": len(source_checks),
    }


def commit_profile_sources(data_dir: Path, scan_id: Any) -> dict[str, Any]:
    """Commit one reviewed scan and return a renderer-safe aggregate receipt."""

    normalized = _canonical_scan_id(scan_id)
    result = commit_profile_source_scan(data_dir, normalized)
    ui = result.get("ui") if isinstance(result.get("ui"), dict) else {}
    profile_name = result.get("profile_name")
    renderable = result.get("renderable")
    profile_counts = ui.get("profile_counts")
    expected_profile_counts = {
        "work_entries",
        "education_entries",
        "skill_groups",
        "evidence_claims",
        "preference_items",
    }
    if (
        not isinstance(profile_name, str)
        or not profile_name.strip()
        or not isinstance(renderable, bool)
        or not isinstance(profile_counts, dict)
        or set(profile_counts) != expected_profile_counts
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in profile_counts.values()
        )
    ):
        _fail(
            "profile_source_commit_invalid",
            "The created profile version could not be verified.",
        )
    parsed = int(result.get("parsed_sources", 0))
    used = int(ui.get("used_sources", parsed))
    used = max(0, min(used, parsed))
    warnings = ui.get("warnings") if isinstance(ui.get("warnings"), list) else []
    review_item_count = result.get("review_item_count")
    if (
        not isinstance(review_item_count, int)
        or isinstance(review_item_count, bool)
        or review_item_count < 0
    ):
        _fail(
            "profile_source_commit_invalid",
            "The created review suggestions could not be verified.",
        )
    return {
        "scan_id": normalized,
        "profile_version_id": result["profile_version_id"],
        "version_number": result["version_number"],
        "created": bool(result["created"]),
        "checksum_sha256": result["checksum_sha256"],
        "profile_name": profile_name.strip()[:160],
        "renderable": renderable,
        "source_counts": {
            "parsed": parsed,
            "used": used,
            "unused": max(0, parsed - used),
        },
        "profile_counts": profile_counts,
        "warnings": warnings,
        "review_item_count": review_item_count,
        "source_review_count": int(result.get("source_review_count", 0)),
    }


def discard_profile_sources(data_dir: Path, scan_id: Any) -> dict[str, bool]:
    """Discard one preview and its staging directory without exposing details."""

    result = discard_profile_source_scan(data_dir, _canonical_scan_id(scan_id))
    return {"discarded": bool(result.get("discarded") or result.get("staging_removed"))}


def discard_all_profile_sources(data_dir: Path) -> dict[str, int]:
    """Discard all unfinished previews and return only an aggregate count."""

    return discard_all_profile_source_scans(data_dir)


__all__ = [
    "ProfileSourceWorkflowError",
    "commit_profile_sources",
    "discard_all_profile_sources",
    "discard_profile_sources",
    "preview_profile_sources",
]
