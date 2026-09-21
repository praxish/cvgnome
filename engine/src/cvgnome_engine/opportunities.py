# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .storage import (
    VaultError,
    _apply_migrations,
    _canonical_uuid,
    _connect,
    _current_profile_row,
    _utc_now_ms,
)

MATCH_CONTRACT = "deterministic-local-v1"
MAX_TITLE_CHARS = 240
MAX_DESCRIPTION_CHARS = 32_000
MAX_COMPANY_CHARS = 160
MAX_LOCATION_CHARS = 160
MAX_URL_CHARS = 2_048
MAX_SEARCH_CHARS = 200
MAX_NOTES_CHARS = 4_000
MAX_LIST_LIMIT = 20
MAX_LIST_OFFSET = 10_000
MAX_MATCHED_TERMS = 24
MAX_REVIEW_TERMS = 16
MAX_EVIDENCE_ITEMS = 12
MAX_TERM_CHARS = 64
MAX_PROFILE_PATH_CHARS = 256
MAX_SNIPPET_CHARS = 240
MAX_RPC_SAFE_INTEGER = 9_007_199_254_740_991

TRACKER_STAGES = (
    "tracked",
    "new",
    "matched",
    "to_apply",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "closed",
    "ignored",
)
TRACKER_STAGE_SET = frozenset(TRACKER_STAGES)
TRACKER_SORTS = frozenset({"last_action", "strength", "stage"})
_TRACKER_CONFLICT_MESSAGE = (
    "That opportunity changed in another window. "
    "Review the latest tracker state and try again."
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]{0,63}")
_STOP_WORDS = {
    "ability",
    "about",
    "across",
    "after",
    "all",
    "also",
    "and",
    "any",
    "are",
    "been",
    "being",
    "but",
    "can",
    "company",
    "could",
    "candidate",
    "candidates",
    "each",
    "employment",
    "experience",
    "for",
    "from",
    "have",
    "including",
    "into",
    "its",
    "job",
    "join",
    "looking",
    "minimum",
    "more",
    "must",
    "need",
    "needs",
    "not",
    "our",
    "out",
    "over",
    "preferred",
    "qualifications",
    "required",
    "requirements",
    "responsibilities",
    "role",
    "should",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "using",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "will",
    "with",
    "within",
    "work",
    "working",
    "would",
    "year",
    "years",
    "you",
    "your",
}
_PUBLIC_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "basics": ("label", "summary"),
    "work": (
        "name",
        "organization",
        "company",
        "employer",
        "position",
        "title",
        "role",
        "summary",
        "description",
        "highlights",
    ),
    "volunteer": ("organization", "position", "summary", "highlights"),
    "education": ("institution", "studyType", "area", "summary", "courses"),
    "awards": ("title", "awarder", "summary"),
    "certificates": ("name", "issuer"),
    "certifications": ("name", "issuer"),
    "publications": ("name", "publisher", "summary"),
    "skills": ("name", "level", "keywords"),
    "languages": ("language", "fluency"),
    "interests": ("name", "keywords"),
    "projects": ("name", "description", "highlights", "keywords", "roles"),
}
_CONTEXT_EVIDENCE_FIELDS = ("career_narrative", "impact_evidence", "domain_context")
_SAVE_PARAM_KEYS = {"title", "description", "company", "location", "source_url", "apply_url"}
_LIST_PARAM_KEYS = {"query", "offset", "stage", "sort"}
_UPDATE_PARAM_KEYS = {
    "opportunity_id",
    "expected_tracker_updated_at_ms",
    "tracker_stage",
    "notes",
}


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _reject_unknown_params(params: dict[str, Any], allowed: set[str]) -> None:
    if any(key not in allowed for key in params):
        raise ValueError("params contains unsupported fields")


def _truncate_utf8(value: str, max_chars: int) -> str:
    candidate = value[:max_chars]
    while _utf8_len(candidate) > max_chars:
        candidate = candidate[:-1]
    return candidate


def _clean_line(value: Any, *, label: str, maximum: int, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        qualifier = "non-empty text" if required else "text"
        raise ValueError(f"{label} must be {qualifier}")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} contains unsupported control characters")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if required and not cleaned:
        raise ValueError(f"{label} must be non-empty text")
    if len(cleaned) > maximum or _utf8_len(cleaned) > maximum:
        raise ValueError(f"{label} may not exceed {maximum:,} characters")
    return cleaned


def _clean_description(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("description must be non-empty text")
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    if any(
        unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
        for character in normalized
    ):
        raise ValueError("description contains unsupported control characters")
    cleaned_lines = [" ".join(line.split()) for line in normalized.split("\n")]
    cleaned = "\n".join(cleaned_lines).strip()
    if not cleaned:
        raise ValueError("description must be non-empty text")
    if len(cleaned) > MAX_DESCRIPTION_CHARS or _utf8_len(cleaned) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"description may not exceed {MAX_DESCRIPTION_CHARS:,} UTF-8 bytes"
        )
    encoded_json_bytes = _utf8_len(json.dumps(cleaned, ensure_ascii=False)) - 2
    if encoded_json_bytes > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"description may not exceed {MAX_DESCRIPTION_CHARS:,} JSON-encoded bytes"
        )
    return cleaned


def _clean_notes(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("notes must be text")
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    if any(
        unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
        for character in normalized
    ):
        raise ValueError("notes contains unsupported control characters")
    cleaned = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if len(cleaned) > MAX_NOTES_CHARS or _utf8_len(cleaned) > MAX_NOTES_CHARS:
        raise ValueError(f"notes may not exceed {MAX_NOTES_CHARS:,} characters")
    return cleaned


def _tracker_stage(value: Any) -> str:
    if not isinstance(value, str) or value not in TRACKER_STAGE_SET:
        raise ValueError("tracker_stage is not a supported stage")
    return value


def _https_url(value: Any, *, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an HTTPS URL or null")
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > MAX_URL_CHARS
        or _utf8_len(candidate) > MAX_URL_CHARS
        or "\\" in candidate
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in candidate
        )
    ):
        raise ValueError(f"{label} must be an HTTPS URL of at most {MAX_URL_CHARS:,} characters")
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid HTTPS URL") from exc
    if parsed.scheme.casefold() != "https" or not hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an absolute HTTPS URL without credentials")
    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"{label} contains an invalid hostname") from exc
    if ":" in ascii_host:
        ascii_host = f"[{ascii_host}]"
    netloc = ascii_host
    if port is not None and port != 443:
        netloc += f":{port}"
    canonical = urlunsplit(
        SplitResult("https", netloc, parsed.path, parsed.query, parsed.fragment)
    )
    if len(canonical) > MAX_URL_CHARS or _utf8_len(canonical) > MAX_URL_CHARS:
        raise ValueError(f"{label} is too long after hostname normalization")
    return canonical


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    result: list[str] = []
    for match in _TOKEN_RE.finditer(normalized):
        term = match.group(0).strip(".-")
        if not term or term in _STOP_WORDS:
            continue
        if len(term) < 3 and term not in {"ai", "bi", "c#", "c++", "go", "r"}:
            continue
        result.append(term[:MAX_TERM_CHARS])
    return result


def _pointer_component(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _selected_strings(
    value: Any,
    *,
    path: tuple[str, ...],
) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        without_controls = "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in value
        )
        cleaned = " ".join(without_controls.split())
        if cleaned:
            yield "/" + "/".join(path), cleaned
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _selected_strings(child, path=(*path, str(index)))


def _profile_evidence_strings(profile: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for section, allowed_fields in _PUBLIC_EVIDENCE_FIELDS.items():
        raw_section = profile.get(section)
        entries = [raw_section] if isinstance(raw_section, dict) else raw_section
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            base = (section,) if isinstance(raw_section, dict) else (section, str(index))
            for field in allowed_fields:
                if field in entry:
                    yield from _selected_strings(
                        entry[field],
                        path=(*base, _pointer_component(field)),
                    )

    meta = profile.get("meta")
    if not isinstance(meta, dict):
        return
    context = meta.get("canonical_context")
    if isinstance(context, dict):
        for field in _CONTEXT_EVIDENCE_FIELDS:
            if field in context:
                yield from _selected_strings(
                    context[field],
                    path=("meta", "canonical_context", field),
                )
    dossier = meta.get("dossier")
    if not isinstance(dossier, dict):
        return
    claims = dossier.get("claims")
    if isinstance(claims, list):
        for index, claim in enumerate(claims):
            if isinstance(claim, dict) and "text" in claim:
                yield from _selected_strings(
                    claim["text"],
                    path=("meta", "dossier", "claims", str(index), "text"),
                )
    if "skill_signals" in dossier:
        yield from _selected_strings(
            dossier["skill_signals"],
            path=("meta", "dossier", "skill_signals"),
        )


def _evidence_alignment(
    *,
    title: str,
    description: str,
    profile: dict[str, Any],
) -> tuple[int, str, dict[str, Any]]:
    description_terms = _tokens(description)
    title_terms = _tokens(title)
    counts = Counter(description_terms)
    first_position: dict[str, int] = {}
    for index, term in enumerate((*title_terms, *description_terms)):
        first_position.setdefault(term, index)
    for term in title_terms:
        counts[term] += 3

    ranked_job_terms = sorted(
        counts,
        key=lambda term: (-min(counts[term], 4), first_position[term], term),
    )[:40]

    profile_evidence: dict[str, tuple[str, str]] = {}
    for path, snippet in _profile_evidence_strings(profile):
        safe_path = _truncate_utf8(path, MAX_PROFILE_PATH_CHARS)
        safe_snippet = _truncate_utf8(snippet, MAX_SNIPPET_CHARS)
        for term in _tokens(snippet):
            profile_evidence.setdefault(term, (safe_path, safe_snippet))

    matched = [term for term in ranked_job_terms if term in profile_evidence]
    unmatched = [term for term in ranked_job_terms if term not in profile_evidence]
    total_weight = sum(min(counts[term], 4) for term in ranked_job_terms)
    matched_weight = sum(min(counts[term], 4) for term in matched)
    score = round((matched_weight * 100) / total_weight) if total_weight else 0
    if len(matched) < 2:
        score = min(score, 29)
    elif len(matched) < 3:
        score = min(score, 59)
    signal = "strong" if score >= 60 else "mixed" if score >= 30 else "limited"

    matched_terms = matched[:MAX_MATCHED_TERMS]
    evidence = [
        {
            "term": term,
            "profile_path": profile_evidence[term][0],
            "snippet": profile_evidence[term][1],
        }
        for term in matched[:MAX_EVIDENCE_ITEMS]
    ]
    return score, signal, {
        "matched_terms": matched_terms,
        "terms_to_review": unmatched[:MAX_REVIEW_TERMS],
        "evidence": evidence,
    }


def _normalized_posting(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _clean_line(
            params.get("title"),
            label="title",
            maximum=MAX_TITLE_CHARS,
            required=True,
        ),
        "company": _clean_line(
            params.get("company", ""),
            label="company",
            maximum=MAX_COMPANY_CHARS,
            required=False,
        ),
        "location": _clean_line(
            params.get("location", ""),
            label="location",
            maximum=MAX_LOCATION_CHARS,
            required=False,
        ),
        "source_url": _https_url(params.get("source_url"), label="source_url"),
        "apply_url": _https_url(params.get("apply_url"), label="apply_url"),
        "description": _clean_description(params.get("description")),
    }


def _posting_checksum(posting: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json(posting))


def _posting_search_text(posting: dict[str, Any]) -> str:
    return "\n".join(
        (
            posting["title"],
            posting["company"],
            posting["location"],
            posting["description"],
        )
    ).casefold()


def _opportunity_identity(posting: dict[str, Any], snapshot_checksum: str) -> str:
    stable_url = posting["source_url"] or posting["apply_url"]
    if stable_url:
        return _sha256_text(f"url\0{stable_url}")
    return _sha256_text(f"content\0{snapshot_checksum}")


def _match_with_connection(
    connection: sqlite3.Connection,
    *,
    opportunity_id: str,
    snapshot: sqlite3.Row,
    profile: sqlite3.Row,
) -> sqlite3.Row:
    input_fingerprint = _sha256_text(
        "\0".join(
            (
                MATCH_CONTRACT,
                opportunity_id,
                str(snapshot["id"]),
                str(snapshot["checksum_sha256"]),
                str(profile["checksum_sha256"]),
            )
        )
    )
    existing = connection.execute(
        """
        SELECT * FROM opportunity_matches
        WHERE input_fingerprint = ?
        """,
        (input_fingerprint,),
    ).fetchone()
    if existing is not None:
        return existing

    profile_json = json.loads(str(profile["canonical_json"]))
    score, signal, result = _evidence_alignment(
        title=str(snapshot["title"]),
        description=str(snapshot["description"]),
        profile=profile_json,
    )
    result_json = _canonical_json(result)
    if _utf8_len(result_json) > 24_576:
        raise VaultError(
            "opportunity_match_too_large",
            "The local evidence alignment result exceeded its safe response limit.",
        )
    match_id = str(uuid.uuid4())
    match_number = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(match_number), 0) + 1
            FROM opportunity_matches WHERE opportunity_id = ?
            """,
            (opportunity_id,),
        ).fetchone()[0]
    )
    created_at_ms = _utc_now_ms()
    connection.execute(
        """
        INSERT INTO opportunity_matches(
            id, opportunity_id, match_number, snapshot_id, snapshot_checksum_sha256,
            profile_version_id, profile_checksum_sha256, contract,
            input_fingerprint, score, signal, result_json, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            opportunity_id,
            match_number,
            str(snapshot["id"]),
            str(snapshot["checksum_sha256"]),
            str(profile["id"]),
            str(profile["checksum_sha256"]),
            MATCH_CONTRACT,
            input_fingerprint,
            score,
            signal,
            result_json,
            created_at_ms,
        ),
    )
    return connection.execute(
        "SELECT * FROM opportunity_matches WHERE id = ?",
        (match_id,),
    ).fetchone()


def _detail_from_rows(
    *,
    opportunity: sqlite3.Row,
    snapshot: sqlite3.Row,
    match: sqlite3.Row,
    profile: sqlite3.Row,
) -> dict[str, Any]:
    result = json.loads(str(match["result_json"]))
    return {
        "opportunity": {
            "id": str(opportunity["id"]),
            "title": str(opportunity["title"]),
            "company": str(opportunity["company"]),
            "location": str(opportunity["location"]),
            "source_url": (
                str(opportunity["source_url"])
                if opportunity["source_url"] is not None
                else None
            ),
            "apply_url": (
                str(opportunity["apply_url"])
                if opportunity["apply_url"] is not None
                else None
            ),
            "status": str(opportunity["status"]),
            "notes": str(opportunity["notes"]),
            "tracker_stage": str(opportunity["tracker_stage"]),
            "tracker_updated_at_ms": int(opportunity["tracker_updated_at_ms"]),
            "created_at_ms": int(opportunity["created_at_ms"]),
            "updated_at_ms": int(opportunity["updated_at_ms"]),
        },
        "snapshot": {
            "id": str(snapshot["id"]),
            "checksum_sha256": str(snapshot["checksum_sha256"]),
            "captured_at_ms": int(snapshot["captured_at_ms"]),
            "description": str(snapshot["description"]),
        },
        "match": {
            "id": str(match["id"]),
            "contract": str(match["contract"]),
            "score": int(match["score"]),
            "signal": str(match["signal"]),
            "matched_terms": list(result.get("matched_terms", [])),
            "terms_to_review": list(result.get("terms_to_review", [])),
            "evidence": list(result.get("evidence", [])),
            "created_at_ms": int(match["created_at_ms"]),
        },
        "profile_version": {
            "id": str(profile["id"]),
            "version_number": int(profile["version_number"]),
            "checksum_sha256": str(profile["checksum_sha256"]),
        },
    }


def _detail_with_connection(
    connection: sqlite3.Connection,
    opportunity_id: str,
) -> dict[str, Any]:
    opportunity = connection.execute(
        "SELECT * FROM opportunities WHERE id = ?",
        (opportunity_id,),
    ).fetchone()
    if opportunity is None:
        raise VaultError(
            "opportunity_not_found",
            "That local opportunity no longer exists.",
        )
    snapshot = connection.execute(
        """
        SELECT * FROM opportunity_snapshots
        WHERE opportunity_id = ?
        ORDER BY snapshot_number DESC
        LIMIT 1
        """,
        (opportunity_id,),
    ).fetchone()
    if snapshot is None:
        raise VaultError(
            "opportunity_not_ready",
            "That local opportunity does not have a saved posting snapshot.",
        )
    match = connection.execute(
        """
        SELECT * FROM opportunity_matches
        WHERE snapshot_id = ?
        ORDER BY match_number DESC
        LIMIT 1
        """,
        (str(snapshot["id"]),),
    ).fetchone()
    if match is None:
        raise VaultError(
            "opportunity_not_ready",
            "That local opportunity does not have an evidence alignment yet.",
        )
    profile = connection.execute(
        """
        SELECT id, version_number, checksum_sha256
        FROM profile_versions WHERE id = ?
        """,
        (str(match["profile_version_id"]),),
    ).fetchone()
    if profile is None:
        raise VaultError(
            "vault_integrity_error",
            "The local opportunity refers to a missing profile version.",
        )
    detail = _detail_from_rows(
        opportunity=opportunity,
        snapshot=snapshot,
        match=match,
        profile=profile,
    )
    if _utf8_len(json.dumps(detail, ensure_ascii=False, separators=(",", ":"))) > 60 * 1024:
        raise VaultError(
            "opportunity_detail_too_large",
            "The local opportunity detail exceeded its safe response limit.",
        )
    return detail


def save_and_match_opportunity(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_params(params, _SAVE_PARAM_KEYS)
    posting = _normalized_posting(params)
    snapshot_checksum = _posting_checksum(posting)
    identity = _opportunity_identity(posting, snapshot_checksum)
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        profile = _current_profile_row(connection)
        if profile is None:
            raise VaultError(
                "profile_missing",
                "Build a canonical profile before matching an opportunity.",
            )
        now_ms = _utc_now_ms()
        opportunity = connection.execute(
            "SELECT * FROM opportunities WHERE identity_sha256 = ?",
            (identity,),
        ).fetchone()
        if opportunity is None:
            opportunity_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO opportunities(
                    id, company, title, source_url, status, job_description,
                    notes, created_at_ms, updated_at_ms, location, apply_url,
                    identity_sha256, tracker_stage, tracker_updated_at_ms
                ) VALUES (?, ?, ?, ?, 'saved', ?, '', ?, ?, ?, ?, ?, 'tracked', ?)
                """,
                (
                    opportunity_id,
                    posting["company"],
                    posting["title"],
                    posting["source_url"],
                    posting["description"],
                    now_ms,
                    now_ms,
                    posting["location"],
                    posting["apply_url"],
                    identity,
                    now_ms,
                ),
            )
        else:
            opportunity_id = str(opportunity["id"])
            connection.execute(
                """
                UPDATE opportunities
                SET company = ?, title = ?, source_url = ?, job_description = ?,
                    location = ?, apply_url = ?, updated_at_ms = MAX(updated_at_ms, ?)
                WHERE id = ?
                """,
                (
                    posting["company"],
                    posting["title"],
                    posting["source_url"],
                    posting["description"],
                    posting["location"],
                    posting["apply_url"],
                    now_ms,
                    opportunity_id,
                ),
            )

        snapshot = connection.execute(
            """
            SELECT * FROM opportunity_snapshots
            WHERE opportunity_id = ?
            ORDER BY snapshot_number DESC
            LIMIT 1
            """,
            (opportunity_id,),
        ).fetchone()
        if snapshot is None or str(snapshot["checksum_sha256"]) != snapshot_checksum:
            snapshot_id = str(uuid.uuid4())
            snapshot_number = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(snapshot_number), 0) + 1
                    FROM opportunity_snapshots WHERE opportunity_id = ?
                    """,
                    (opportunity_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO opportunity_snapshots(
                    id, opportunity_id, snapshot_number, title, company,
                    location, source_url, apply_url, description,
                    search_text, checksum_sha256, captured_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    opportunity_id,
                    snapshot_number,
                    posting["title"],
                    posting["company"],
                    posting["location"],
                    posting["source_url"],
                    posting["apply_url"],
                    posting["description"],
                    _posting_search_text(posting),
                    snapshot_checksum,
                    now_ms,
                ),
            )
            snapshot = connection.execute(
                "SELECT * FROM opportunity_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()

        _match_with_connection(
            connection,
            opportunity_id=opportunity_id,
            snapshot=snapshot,
            profile=profile,
        )
        connection.execute(
            """
            UPDATE opportunities
            SET status = 'ready', updated_at_ms = MAX(updated_at_ms, ?)
            WHERE id = ?
            """,
            (now_ms, opportunity_id),
        )
        detail = _detail_with_connection(connection, opportunity_id)
        connection.commit()
        return detail
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_opportunity(data_dir: Path, opportunity_id: Any) -> dict[str, Any]:
    canonical_id = _canonical_uuid(opportunity_id, label="opportunity_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        return _detail_with_connection(connection, canonical_id)
    finally:
        connection.close()


def rematch_opportunity(data_dir: Path, opportunity_id: Any) -> dict[str, Any]:
    canonical_id = _canonical_uuid(opportunity_id, label="opportunity_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        opportunity = connection.execute(
            "SELECT id FROM opportunities WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        if opportunity is None:
            raise VaultError(
                "opportunity_not_found",
                "That local opportunity no longer exists.",
            )
        snapshot = connection.execute(
            """
            SELECT * FROM opportunity_snapshots
            WHERE opportunity_id = ?
            ORDER BY snapshot_number DESC
            LIMIT 1
            """,
            (canonical_id,),
        ).fetchone()
        if snapshot is None:
            raise VaultError(
                "opportunity_not_ready",
                "That local opportunity does not have a saved posting snapshot.",
            )
        profile = _current_profile_row(connection)
        if profile is None:
            raise VaultError(
                "profile_missing",
                "Build a canonical profile before matching an opportunity.",
            )
        _match_with_connection(
            connection,
            opportunity_id=canonical_id,
            snapshot=snapshot,
            profile=profile,
        )
        connection.execute(
            """
            UPDATE opportunities
            SET status = 'ready', updated_at_ms = MAX(updated_at_ms, ?)
            WHERE id = ?
            """,
            (_utc_now_ms(), canonical_id),
        )
        detail = _detail_with_connection(connection, canonical_id)
        connection.commit()
        return detail
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_opportunity(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Optimistically update mutable tracker state without altering pinned evidence."""

    _reject_unknown_params(params, _UPDATE_PARAM_KEYS)
    required = {"opportunity_id", "expected_tracker_updated_at_ms"}
    if not required.issubset(params):
        raise ValueError(
            "opportunity_id and expected_tracker_updated_at_ms are required"
        )
    if "tracker_stage" not in params and "notes" not in params:
        raise ValueError("at least one of tracker_stage or notes is required")

    opportunity_id = _canonical_uuid(params["opportunity_id"], label="opportunity_id")
    expected_updated_at_ms = _bounded_integer(
        params["expected_tracker_updated_at_ms"],
        label="expected_tracker_updated_at_ms",
        minimum=0,
        maximum=MAX_RPC_SAFE_INTEGER,
    )
    tracker_stage = (
        _tracker_stage(params["tracker_stage"])
        if "tracker_stage" in params
        else None
    )
    notes = _clean_notes(params["notes"]) if "notes" in params else None

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        opportunity = connection.execute(
            "SELECT * FROM opportunities WHERE id = ?",
            (opportunity_id,),
        ).fetchone()
        if opportunity is None:
            raise VaultError(
                "opportunity_not_found",
                "That local opportunity no longer exists.",
            )
        current_tracker_updated_at_ms = int(opportunity["tracker_updated_at_ms"])
        if current_tracker_updated_at_ms != expected_updated_at_ms:
            raise VaultError(
                "opportunity_tracker_conflict",
                _TRACKER_CONFLICT_MESSAGE,
            )

        next_stage = (
            tracker_stage
            if tracker_stage is not None
            else str(opportunity["tracker_stage"])
        )
        next_notes = notes if notes is not None else str(opportunity["notes"])
        changed = (
            next_stage != str(opportunity["tracker_stage"])
            or next_notes != str(opportunity["notes"])
        )
        if changed:
            next_updated_at_ms = max(_utc_now_ms(), current_tracker_updated_at_ms + 1)
            updated = connection.execute(
                """
                UPDATE opportunities
                SET tracker_stage = ?, notes = ?, tracker_updated_at_ms = ?
                WHERE id = ? AND tracker_updated_at_ms = ?
                """,
                (
                    next_stage,
                    next_notes,
                    next_updated_at_ms,
                    opportunity_id,
                    current_tracker_updated_at_ms,
                ),
            )
            if updated.rowcount != 1:
                raise VaultError(
                    "opportunity_tracker_conflict",
                    _TRACKER_CONFLICT_MESSAGE,
                )

        detail = _detail_with_connection(connection, opportunity_id)
        connection.commit()
        return detail
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _bounded_integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _search_query(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("query must be text")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("query contains unsupported control characters")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if len(cleaned) > MAX_SEARCH_CHARS or _utf8_len(cleaned) > MAX_SEARCH_CHARS:
        raise ValueError(f"query may not exceed {MAX_SEARCH_CHARS} characters")
    return cleaned.casefold()


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _sqlite_note_casefold(value: Any) -> str:
    """Bound legacy notes before applying Python's full Unicode casefold."""

    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value[:MAX_NOTES_CHARS]).casefold()


def list_opportunities(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_params(params, _LIST_PARAM_KEYS)
    if set(params) != _LIST_PARAM_KEYS:
        raise ValueError("params must contain exactly query, offset, stage, and sort")
    query = _search_query(params["query"])
    offset = _bounded_integer(
        params["offset"],
        label="offset",
        minimum=0,
        maximum=MAX_LIST_OFFSET,
    )
    stage = params["stage"]
    if not isinstance(stage, str) or (stage != "all" and stage not in TRACKER_STAGE_SET):
        raise ValueError("stage must be all or a supported tracker stage")
    sort = params["sort"]
    if not isinstance(sort, str) or sort not in TRACKER_SORTS:
        raise ValueError("sort must be last_action, strength, or stage")

    conditions: list[str] = []
    bindings: list[Any] = []
    if query:
        pattern = _like_pattern(query)
        conditions.append(
            "(s.search_text LIKE ? ESCAPE '\\' "
            "OR cvgnome_note_casefold(o.notes) LIKE ? ESCAPE '\\')"
        )
        bindings.extend((pattern, pattern))
    if stage != "all":
        conditions.append("o.tracker_stage = ?")
        bindings.append(stage)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    if sort == "strength":
        order_by = "m.score DESC, o.tracker_updated_at_ms DESC, o.id ASC"
    elif sort == "stage":
        stage_order = " ".join(
            f"WHEN '{stage_name}' THEN {ordinal}"
            for ordinal, stage_name in enumerate(TRACKER_STAGES)
        )
        order_by = (
            f"CASE o.tracker_stage {stage_order} ELSE {len(TRACKER_STAGES)} END ASC, "
            "o.tracker_updated_at_ms DESC, o.id ASC"
        )
    else:
        order_by = "o.tracker_updated_at_ms DESC, o.id ASC"

    joins = """
        JOIN opportunity_snapshots AS s ON s.id = (
            SELECT latest_snapshot.id
            FROM opportunity_snapshots AS latest_snapshot
            WHERE latest_snapshot.opportunity_id = o.id
            ORDER BY latest_snapshot.snapshot_number DESC
            LIMIT 1
        )
        JOIN opportunity_matches AS m ON m.id = (
            SELECT latest_match.id
            FROM opportunity_matches AS latest_match
            WHERE latest_match.snapshot_id = s.id
            ORDER BY latest_match.match_number DESC
            LIMIT 1
        )
        JOIN profile_versions AS p ON p.id = m.profile_version_id
    """
    connection = _connect(data_dir)
    try:
        connection.create_function(
            "cvgnome_note_casefold",
            1,
            _sqlite_note_casefold,
            deterministic=True,
        )
        _apply_migrations(connection)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM opportunities AS o {joins} {where}",
                bindings,
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT
                o.id, o.title, o.company, o.location, o.status,
                o.tracker_stage, o.tracker_updated_at_ms,
                o.created_at_ms, o.updated_at_ms,
                s.id AS snapshot_id,
                s.checksum_sha256 AS snapshot_checksum_sha256,
                s.captured_at_ms,
                m.id AS match_id, m.contract, m.score, m.signal,
                m.created_at_ms AS match_created_at_ms,
                p.id AS profile_version_id, p.version_number,
                p.checksum_sha256 AS profile_checksum_sha256
            FROM opportunities AS o
            {joins}
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*bindings, MAX_LIST_LIMIT, offset),
        ).fetchall()
    finally:
        connection.close()

    items = [
        {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "company": str(row["company"]),
            "location": str(row["location"]),
            "status": str(row["status"]),
            "tracker_stage": str(row["tracker_stage"]),
            "tracker_updated_at_ms": int(row["tracker_updated_at_ms"]),
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
            "snapshot": {
                "id": str(row["snapshot_id"]),
                "checksum_sha256": str(row["snapshot_checksum_sha256"]),
                "captured_at_ms": int(row["captured_at_ms"]),
            },
            "match": {
                "id": str(row["match_id"]),
                "contract": str(row["contract"]),
                "score": int(row["score"]),
                "signal": str(row["signal"]),
                "created_at_ms": int(row["match_created_at_ms"]),
            },
            "profile_version": {
                "id": str(row["profile_version_id"]),
                "version_number": int(row["version_number"]),
                "checksum_sha256": str(row["profile_checksum_sha256"]),
            },
        }
        for row in rows
    ]
    return {"items": items, "total": total}
