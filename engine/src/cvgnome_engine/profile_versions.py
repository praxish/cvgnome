# SPDX-License-Identifier: MPL-2.0
"""Public immutable-profile history and narrow Basic Details mutation contracts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import unicodedata
from urllib.parse import urlsplit
import uuid

from .profile.validation import validate_canonical_profile
from .profile_review import (
    build_profile_review_section_changes,
    build_profile_review_summary,
    profile_source_kind,
)
from .resume import is_baseline_resume_renderable
from .storage import (
    MAX_DRAFT_PROFILE_BYTES,
    MAX_RPC_SAFE_COUNT,
    VaultError,
    _apply_migrations,
    _canonical_uuid,
    _connect,
    _decode_stored_profile_json,
    _json_text,
    _utc_now_ms,
)


PROFILE_VERSION_LIST_LIMIT = 20
PROFILE_VERSION_OFFSET_LIMIT = 10_000

_TEXT_LIMITS: dict[str, tuple[int, int]] = {
    "name": (160, 640),
    "headline": (200, 800),
    "summary": (4_000, 16_000),
    "email": (320, 1_280),
    "phone": (80, 320),
    "url": (2_048, 8_192),
    "location.city": (160, 640),
    "location.region": (160, 640),
    "location.country_code": (2, 2),
}
_TOP_LEVEL_PATCH_FIELDS = ("name", "headline", "summary", "email", "phone", "url")
_LOCATION_PATCH_FIELDS = ("city", "region", "country_code")
_CHANGED_FIELD_ORDER = (
    "name",
    "headline",
    "summary",
    "email",
    "phone",
    "url",
    "location.city",
    "location.region",
    "location.country_code",
)
_BASIC_FIELD_LABELS = {
    "name": "Name",
    "headline": "Headline",
    "summary": "Summary",
    "email": "Email",
    "phone": "Phone",
    "url": "Website",
    "location.city": "City",
    "location.region": "Region",
    "location.country_code": "Country",
}
_CANONICAL_BASIC_FIELDS = {
    "name": "name",
    "headline": "label",
    "summary": "summary",
    "email": "email",
    "phone": "phone",
    "url": "url",
}
_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".localhost.localdomain",
    ".local",
    ".internal",
    ".home",
    ".lan",
    ".test",
    ".invalid",
    ".example",
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_PUBLIC_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _ipv4_is_public(address: ipaddress.IPv4Address) -> bool:
    first, second, third, _fourth = address.packed
    return not (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
        or first == 127
        or (first == 169 and second == 254)
        or first == 0
        or (first == 100 and 64 <= second <= 127)
        or (first == 192 and second == 0 and third in {0, 2})
        or (first == 198 and second in {18, 19})
        or (first == 198 and second == 51 and third == 100)
        or (first == 203 and second == 0 and third == 113)
        or first >= 224
    )


def _whatwg_ipv4_number(value: str) -> tuple[bool, int | None]:
    if not value:
        return False, None
    base = 10
    digits = value
    if value.startswith(("0x", "0X")):
        base = 16
        digits = value[2:]
    elif len(value) >= 2 and value.startswith("0"):
        base = 8
        digits = value[1:]
    if not digits:
        return True, 0
    allowed = {
        8: "01234567",
        10: "0123456789",
        16: "0123456789abcdefABCDEF",
    }[base]
    if any(character not in allowed for character in digits):
        return False, None
    number = int(digits, base)
    return True, number if number <= 0xFFFFFFFF else None


def _whatwg_host_ends_in_number(hostname: str) -> bool:
    parts = hostname.rsplit(".")
    last = parts[-1]
    if not last and len(parts) > 1:
        last = parts[-2]
    if last and all(character in "0123456789" for character in last):
        return True
    valid, _number = _whatwg_ipv4_number(last)
    return valid


def _whatwg_ipv4_address(hostname: str) -> ipaddress.IPv4Address | None:
    parts = hostname.split(".")
    if parts and not parts[-1]:
        parts.pop()
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        valid, number = _whatwg_ipv4_number(part)
        if not valid or number is None:
            return None
        numbers.append(number)
    last = numbers.pop()
    if last > 0xFFFFFFFF >> (8 * len(numbers)) or any(
        number > 255 for number in numbers
    ):
        return None
    ipv4 = last
    for index, number in enumerate(numbers):
        ipv4 += number << (8 * (3 - index))
    return ipaddress.IPv4Address(ipv4)


def _ipv6_in_prefix(
    address: ipaddress.IPv6Address,
    network: str,
    prefix_length: int,
) -> bool:
    shift = 128 - prefix_length
    return int(address) >> shift == int(ipaddress.IPv6Address(network)) >> shift


def _ipv6_embedded_ipv4(
    address: ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    packed = address.packed
    if (
        packed[:12] == b"\x00" * 12
        or packed[:12] == b"\x00" * 10 + b"\xff\xff"
    ):
        return ipaddress.IPv4Address(packed[12:])
    return None


def _ipv6_is_public(address: ipaddress.IPv6Address) -> bool:
    embedded = _ipv6_embedded_ipv4(address)
    if embedded is not None:
        return _ipv4_is_public(embedded)
    special_2001_exception = (
        address
        in {
            ipaddress.IPv6Address("2001:1::1"),
            ipaddress.IPv6Address("2001:1::2"),
        }
        or _ipv6_in_prefix(address, "2001:3::", 32)
        or _ipv6_in_prefix(address, "2001:4:112::", 48)
        or _ipv6_in_prefix(address, "2001:20::", 28)
        or _ipv6_in_prefix(address, "2001:30::", 28)
    )
    return not (
        address in {ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")}
        or address.packed[0] == 0xFF
        or _ipv6_in_prefix(address, "64:ff9b:1::", 48)
        or _ipv6_in_prefix(address, "100::", 64)
        or (_ipv6_in_prefix(address, "2001::", 23) and not special_2001_exception)
        or _ipv6_in_prefix(address, "2001:db8::", 32)
        or _ipv6_in_prefix(address, "2002::", 16)
        or _ipv6_in_prefix(address, "3fff::", 20)
        or _ipv6_in_prefix(address, "fc00::", 7)
        or _ipv6_in_prefix(address, "fe80::", 10)
    )


def _ip_address_is_public(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        return _ipv4_is_public(address)
    return _ipv6_is_public(address)


def _stored_version(row: sqlite3.Row) -> dict[str, Any]:
    try:
        profile_version_id = _canonical_uuid(row["id"], label="stored profile version id")
        parent_value = row["parent_version_id"]
        parent_profile_version_id = (
            None
            if parent_value is None
            else _canonical_uuid(parent_value, label="stored parent profile version id")
        )
        version_number = int(row["version_number"])
        created_at_ms = int(row["created_at_ms"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version has invalid identity data.",
        ) from exc
    if (
        not 1 <= version_number <= MAX_RPC_SAFE_COUNT
        or not 1 <= created_at_ms <= MAX_RPC_SAFE_COUNT
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version has invalid identity data.",
        )
    canonical_json = row["canonical_json"]
    if not isinstance(canonical_json, str):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version failed its integrity check.",
        )
    checksum = str(row["checksum_sha256"])
    if (
        re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != checksum
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version failed its integrity check.",
        )
    source = row["source"]
    if not isinstance(source, str) or not source or len(source) > 120:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version has invalid source data.",
        )
    return {
        "id": profile_version_id,
        "parent_version_id": parent_profile_version_id,
        "version_number": version_number,
        "profile": _decode_stored_profile_json(canonical_json),
        "canonical_json": canonical_json,
        "checksum_sha256": checksum,
        "source": source,
        "created_at_ms": created_at_ms,
    }


def _version_row(connection: sqlite3.Connection, profile_version_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, version_number, parent_version_id, canonical_json,
               checksum_sha256, source, created_at_ms
        FROM profile_versions
        WHERE id = ?
        """,
        (profile_version_id,),
    ).fetchone()
    if row is None:
        raise VaultError(
            "profile_version_not_found",
            "That immutable profile version is no longer available.",
        )
    return row


def _read_text(value: Any, *, field: str) -> str | None:
    if not isinstance(value, str):
        return None
    max_chars, max_bytes = _TEXT_LIMITS[field]
    normalized = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    if field == "summary":
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        cleaned = "".join(
            character
            for character in normalized
            if character == "\n" or not unicodedata.category(character).startswith("C")
        )
        cleaned = cleaned.strip()
    else:
        cleaned = "".join(
            " " if unicodedata.category(character) == "Cc" else character
            for character in normalized
            if unicodedata.category(character) == "Cc"
            or not unicodedata.category(character).startswith("C")
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    cleaned = cleaned[:max_chars]
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_bytes:
        cleaned = encoded[:max_bytes].decode("utf-8", "ignore")
    return cleaned.rstrip() or None


def _public_url(value: Any) -> str | None:
    cleaned = _read_text(value, field="url")
    if (
        cleaned is None
        or any(character.isspace() for character in cleaned)
        or "\\" in cleaned
    ):
        return None
    try:
        parsed = urlsplit(cleaned)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = parsed.hostname.rstrip(".")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    if _whatwg_host_ends_in_number(ascii_hostname):
        address = _whatwg_ipv4_address(ascii_hostname)
        if address is None:
            return None
    else:
        try:
            address = ipaddress.ip_address(ascii_hostname)
        except ValueError:
            address = None
    if address is not None:
        if not _ip_address_is_public(address):
            return None
    else:
        labels = ascii_hostname.split(".")
        if (
            not ascii_hostname
            or ascii_hostname == "localhost"
            or ascii_hostname == "localhost.localdomain"
            or "." not in ascii_hostname
            or ascii_hostname.endswith(_LOCAL_HOST_SUFFIXES)
            or len(ascii_hostname) > 253
            or all(character in "0123456789." for character in ascii_hostname)
            or any(_PUBLIC_HOST_LABEL_RE.fullmatch(label) is None for label in labels)
        ):
            return None
    return cleaned


def _public_basics(version: dict[str, Any]) -> dict[str, Any]:
    profile = version["profile"]
    basics = profile.get("basics")
    if not isinstance(basics, dict):
        basics = {}
    location = basics.get("location")
    if not isinstance(location, dict):
        location = {}
    raw_country_code = location.get("countryCode")
    country_code = None
    if isinstance(raw_country_code, str):
        candidate_country_code = unicodedata.normalize("NFC", raw_country_code).strip().upper()
        if _COUNTRY_CODE_RE.fullmatch(candidate_country_code):
            country_code = candidate_country_code
    return {
        "profile_version_id": version["id"],
        "version_number": version["version_number"],
        "created_at_ms": version["created_at_ms"],
        "renderable": is_baseline_resume_renderable(profile),
        "name": _read_text(basics.get("name"), field="name") or "",
        "headline": _read_text(basics.get("label"), field="headline"),
        "summary": _read_text(basics.get("summary"), field="summary"),
        "email": _read_text(basics.get("email"), field="email"),
        "phone": _read_text(basics.get("phone"), field="phone"),
        "url": _public_url(basics.get("url")),
        "location": {
            "city": _read_text(location.get("city"), field="location.city"),
            "region": _read_text(location.get("region"), field="location.region"),
            "country_code": country_code,
        },
    }


def _version_summary(version: dict[str, Any]) -> dict[str, Any]:
    basics = _public_basics(version)
    return {
        "profile_version_id": version["id"],
        "parent_profile_version_id": version["parent_version_id"],
        "version_number": version["version_number"],
        "created_at_ms": version["created_at_ms"],
        "source_kind": profile_source_kind(version["source"]),
        "name": basics["name"] or "Unnamed profile",
        "headline": basics["headline"],
        "renderable": basics["renderable"],
    }


def list_profile_versions(data_dir: Path, offset: Any) -> dict[str, Any]:
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > PROFILE_VERSION_OFFSET_LIMIT
    ):
        raise ValueError("offset must be an integer between 0 and 10000")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN")
        total_items = int(
            connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT id, version_number, parent_version_id, canonical_json,
                   checksum_sha256, source, created_at_ms
            FROM profile_versions
            ORDER BY version_number DESC
            LIMIT ? OFFSET ?
            """,
            (PROFILE_VERSION_LIST_LIMIT, offset),
        ).fetchall()
        current = connection.execute(
            "SELECT id FROM profile_versions ORDER BY version_number DESC LIMIT 1"
        ).fetchone()
        versions = [_stored_version(row) for row in rows]
        connection.commit()
    finally:
        connection.close()
    if not 0 <= total_items <= MAX_RPC_SAFE_COUNT:
        raise VaultError(
            "vault_integrity_error",
            "The local profile history exceeds the supported size.",
        )
    current_id = None
    if current is not None:
        try:
            current_id = _canonical_uuid(current["id"], label="current profile version id")
        except ValueError as exc:
            raise VaultError(
                "vault_integrity_error",
                "The current profile version has invalid identity data.",
            ) from exc
    page_end = offset + len(versions)
    return {
        "current_profile_version_id": current_id,
        "total_items": total_items,
        "offset": offset,
        "limit": PROFILE_VERSION_LIST_LIMIT,
        "next_offset": (
            page_end
            if page_end < total_items and page_end <= PROFILE_VERSION_OFFSET_LIMIT
            else None
        ),
        "items": [_version_summary(version) for version in versions],
    }


def get_profile_basics(data_dir: Path, profile_version_id: Any) -> dict[str, Any]:
    normalized_id = _canonical_uuid(profile_version_id, label="profile_version_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        version = _stored_version(_version_row(connection, normalized_id))
    finally:
        connection.close()
    return _public_basics(version)


def get_profile_review_version(data_dir: Path, profile_version_id: Any) -> dict[str, Any]:
    normalized_id = _canonical_uuid(profile_version_id, label="profile_version_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        version = _stored_version(_version_row(connection, normalized_id))
    finally:
        connection.close()
    return build_profile_review_summary(version)


def diff_profile_versions(
    data_dir: Path,
    from_profile_version_id: Any,
    to_profile_version_id: Any,
) -> dict[str, Any]:
    from_id = _canonical_uuid(
        from_profile_version_id,
        label="from_profile_version_id",
    )
    to_id = _canonical_uuid(to_profile_version_id, label="to_profile_version_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        from_version = _stored_version(_version_row(connection, from_id))
        to_version = _stored_version(_version_row(connection, to_id))
    finally:
        connection.close()
    before = _public_basics(from_version)
    after = _public_basics(to_version)
    before_values = {
        field: (
            before["location"][field.removeprefix("location.")]
            if field.startswith("location.")
            else before[field]
        )
        for field in _CHANGED_FIELD_ORDER
    }
    after_values = {
        field: (
            after["location"][field.removeprefix("location.")]
            if field.startswith("location.")
            else after[field]
        )
        for field in _CHANGED_FIELD_ORDER
    }
    basic_changes = [
        {
            "field": field,
            "label": _BASIC_FIELD_LABELS[field],
            "before": before_values[field],
            "after": after_values[field],
        }
        for field in _CHANGED_FIELD_ORDER
        if before_values[field] != after_values[field]
    ]
    section_changes = [
        change
        for change in build_profile_review_section_changes(
            from_version["profile"],
            to_version["profile"],
        )
        if change["content_changed"]
    ]
    total_changes = len(basic_changes) + len(section_changes)
    return {
        "from_version": _version_summary(from_version),
        "to_version": _version_summary(to_version),
        "basic_changes": basic_changes,
        "section_changes": section_changes,
        "total_changes": total_changes,
    }


def _normalize_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"patch.{field} must be text or null")
    normalized = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    if field == "summary":
        normalized = (
            normalized.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\t", " ")
        )
    if any(
        unicodedata.category(character).startswith("C")
        and not (field == "summary" and character == "\n")
        for character in normalized
    ):
        raise ValueError(f"patch.{field} contains unsupported control characters")
    normalized = normalized.strip()
    max_chars, max_bytes = _TEXT_LIMITS[field]
    if not normalized:
        raise ValueError(f"patch.{field} must not be blank; use null to clear it")
    if len(normalized) > max_chars or len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"patch.{field} exceeds its local size limit")
    return normalized


def _validate_email(value: str) -> None:
    if _EMAIL_RE.fullmatch(value) is None:
        raise ValueError("patch.email must be a valid email address")
    local, domain = value.rsplit("@", 1)
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("patch.email must be a valid email address")
    if any(delimiter in domain for delimiter in ("/", "\\", ":", "?", "#", "[", "]")):
        raise ValueError("patch.email must be a valid email address")
    normalized_domain = domain[:-1] if domain.endswith(".") else domain
    try:
        ascii_domain = normalized_domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("patch.email must be a valid email address") from exc
    labels = ascii_domain.split(".")
    if (
        not ascii_domain
        or len(ascii_domain) > 253
        or len(labels) < 2
        or any(_PUBLIC_HOST_LABEL_RE.fullmatch(label) is None for label in labels)
    ):
        raise ValueError("patch.email must be a valid email address")


def _validate_phone(value: str) -> None:
    if sum(character.isdecimal() for character in value) < 3:
        raise ValueError("patch.phone must contain a usable phone number")


def _validate_url(value: str) -> None:
    if _public_url(value) != value:
        raise ValueError(
            "patch.url must be a public credential-free HTTP or HTTPS URL"
        )


def _normalize_patch(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch must be a non-empty object")
    allowed = set(_TOP_LEVEL_PATCH_FIELDS) | {"location"}
    if any(not isinstance(key, str) or key not in allowed for key in patch):
        raise ValueError("patch contains unsupported fields")
    normalized: dict[str, Any] = {}
    for field in _TOP_LEVEL_PATCH_FIELDS:
        if field not in patch:
            continue
        raw_value = patch[field]
        if raw_value is None:
            if field == "name":
                raise ValueError("patch.name must be non-empty text")
            normalized[field] = None
            continue
        value = _normalize_text(raw_value, field=field)
        if field == "email":
            _validate_email(value)
        elif field == "phone":
            _validate_phone(value)
        elif field == "url":
            _validate_url(value)
        normalized[field] = value
    if "location" in patch:
        raw_location = patch["location"]
        if not isinstance(raw_location, dict) or not raw_location:
            raise ValueError("patch.location must be a non-empty object")
        if any(
            not isinstance(key, str) or key not in _LOCATION_PATCH_FIELDS
            for key in raw_location
        ):
            raise ValueError("patch.location contains unsupported fields")
        location: dict[str, str | None] = {}
        for field in _LOCATION_PATCH_FIELDS:
            if field not in raw_location:
                continue
            raw_value = raw_location[field]
            api_field = f"location.{field}"
            if raw_value is None:
                location[field] = None
                continue
            value = _normalize_text(raw_value, field=api_field)
            if field == "country_code":
                value = value.upper()
                if _COUNTRY_CODE_RE.fullmatch(value) is None:
                    raise ValueError("patch.location.country_code must be two ASCII letters")
            location[field] = value
        normalized["location"] = location
    return normalized


def _apply_basic_patch(
    profile: dict[str, Any],
    patch: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    result = deepcopy(profile)
    current_basics = result.get("basics")
    basics = deepcopy(current_basics) if isinstance(current_basics, dict) else {}
    changed: set[str] = set()
    for field in _TOP_LEVEL_PATCH_FIELDS:
        if field not in patch:
            continue
        canonical_field = _CANONICAL_BASIC_FIELDS[field]
        before_present = canonical_field in basics
        before_value = basics.get(canonical_field)
        value = patch[field]
        if value is None:
            basics.pop(canonical_field, None)
            if before_present:
                changed.add(field)
        else:
            basics[canonical_field] = value
            if not before_present or before_value != value:
                changed.add(field)
    if "location" in patch:
        current_location = basics.get("location")
        location = deepcopy(current_location) if isinstance(current_location, dict) else {}
        for field, value in patch["location"].items():
            canonical_field = "countryCode" if field == "country_code" else field
            api_field = f"location.{field}"
            before_present = canonical_field in location
            before_value = location.get(canonical_field)
            if value is None:
                location.pop(canonical_field, None)
                if before_present:
                    changed.add(api_field)
            else:
                location[canonical_field] = value
                if not before_present or before_value != value:
                    changed.add(api_field)
        if location:
            basics["location"] = location
        elif isinstance(current_location, dict):
            basics.pop("location", None)
    final_name = basics.get("name")
    if not isinstance(final_name, str):
        raise ValueError("The edited profile must retain a non-empty name")
    _ = _normalize_text(final_name, field="name")
    result["basics"] = basics
    changed_fields = [field for field in _CHANGED_FIELD_ORDER if field in changed]
    if not changed_fields:
        raise ValueError("patch does not make a semantic change to the current profile")
    validate_canonical_profile(result)
    return result, changed_fields


def _update_receipt(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    created: bool,
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(receipt["request_id"], label="stored request id")
        parent_id = _canonical_uuid(
            receipt["parent_profile_version_id"],
            label="stored parent profile version id",
        )
        output_id = _canonical_uuid(
            receipt["output_profile_version_id"],
            label="stored output profile version id",
        )
        changed_fields = json.loads(str(receipt["changed_fields_json"]))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile update receipt failed its integrity check.",
        ) from exc
    if (
        not isinstance(changed_fields, list)
        or not 1 <= len(changed_fields) <= len(_CHANGED_FIELD_ORDER)
        or any(field not in _CHANGED_FIELD_ORDER for field in changed_fields)
        or changed_fields != [field for field in _CHANGED_FIELD_ORDER if field in changed_fields]
        or len(set(changed_fields)) != len(changed_fields)
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile update receipt failed its integrity check.",
        )
    version = _stored_version(_version_row(connection, output_id))
    if (
        version["parent_version_id"] != parent_id
        or version["source"] != "local_edit"
        or version["created_at_ms"] != int(receipt["created_at_ms"])
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile update receipt has invalid lineage.",
        )
    basics = _public_basics(version)
    if not basics["name"]:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile update receipt points to a nameless profile.",
        )
    return {
        "request_id": request_id,
        "profile_version_id": output_id,
        "parent_profile_version_id": parent_id,
        "version_number": version["version_number"],
        "created_at_ms": version["created_at_ms"],
        "created": created,
        "changed_fields": changed_fields,
        "profile_name": basics["name"],
        "headline": basics["headline"],
        "renderable": basics["renderable"],
    }


def update_profile_basics(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "expected_parent_profile_version_id",
        "request_id",
        "patch",
    }
    if set(params) != expected_keys:
        raise ValueError(
            "params must contain exactly expected_parent_profile_version_id, "
            "request_id, and patch"
        )
    parent_id = _canonical_uuid(
        params["expected_parent_profile_version_id"],
        label="expected_parent_profile_version_id",
    )
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    patch = _normalize_patch(params["patch"])
    fingerprint_input = {
        "expected_parent_profile_version_id": parent_id,
        "request_id": request_id,
        "patch": patch,
    }
    fingerprint_json = json.dumps(
        fingerprint_input,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_fingerprint = hashlib.sha256(fingerprint_json.encode("utf-8")).hexdigest()

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM profile_basic_update_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise VaultError(
                    "profile_update_request_conflict",
                    "That profile update request was already used with different inputs.",
                )
            result = _update_receipt(connection, existing, created=False)
            connection.commit()
            return result

        current_row = connection.execute(
            """
            SELECT id, version_number, parent_version_id, canonical_json,
                   checksum_sha256, source, created_at_ms
            FROM profile_versions
            ORDER BY version_number DESC
            LIMIT 1
            """
        ).fetchone()
        if current_row is None:
            raise VaultError(
                "profile_missing",
                "Build or import a career profile before editing Basic Details.",
            )
        current = _stored_version(current_row)
        if current["id"] != parent_id:
            raise VaultError(
                "profile_update_conflict",
                "The career profile changed before this edit could be saved.",
            )
        if current["version_number"] >= MAX_RPC_SAFE_COUNT:
            raise VaultError(
                "profile_version_limit",
                "The local profile history cannot be advanced.",
            )
        edited_profile, changed_fields = _apply_basic_patch(current["profile"], patch)
        canonical_json = _json_text(
            edited_profile,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
        checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if checksum == current["checksum_sha256"]:
            raise ValueError("patch does not make a semantic change to the current profile")
        output_id = str(uuid.uuid4())
        version_number = current["version_number"] + 1
        created_at_ms = max(_utc_now_ms(), current["created_at_ms"])
        connection.execute(
            """
            INSERT INTO profile_versions(
                id, version_number, parent_version_id, canonical_json,
                checksum_sha256, source, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, 'local_edit', ?)
            """,
            (
                output_id,
                version_number,
                parent_id,
                canonical_json,
                checksum,
                created_at_ms,
            ),
        )
        changed_fields_json = json.dumps(changed_fields, separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO profile_basic_update_receipts(
                request_id, request_fingerprint, output_profile_version_id,
                parent_profile_version_id, changed_fields_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_fingerprint,
                output_id,
                parent_id,
                changed_fields_json,
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_basic_update_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        result = _update_receipt(connection, receipt, created=True)
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            "The local vault rejected invalid profile update lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _restore_receipt(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    created: bool,
) -> dict[str, Any]:
    try:
        request_id = _canonical_uuid(receipt["request_id"], label="stored request id")
        parent_id = _canonical_uuid(
            receipt["parent_profile_version_id"],
            label="stored parent profile version id",
        )
        output_id = _canonical_uuid(
            receipt["output_profile_version_id"],
            label="stored output profile version id",
        )
        source_id = _canonical_uuid(
            receipt["source_profile_version_id"],
            label="stored restore source profile version id",
        )
        request_fingerprint = str(receipt["request_fingerprint"])
        created_at_ms = int(receipt["created_at_ms"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile restore receipt failed its integrity check.",
        ) from exc
    if (
        re.fullmatch(r"[0-9a-f]{64}", request_fingerprint) is None
        or not 1 <= created_at_ms <= MAX_RPC_SAFE_COUNT
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile restore receipt failed its integrity check.",
        )

    parent = _stored_version(_version_row(connection, parent_id))
    source = _stored_version(_version_row(connection, source_id))
    output = _stored_version(_version_row(connection, output_id))
    if (
        source["version_number"] >= parent["version_number"]
        or source_id == parent_id
        or source["checksum_sha256"] == parent["checksum_sha256"]
        or output["parent_version_id"] != parent_id
        or output["version_number"] != parent["version_number"] + 1
        or output["source"] != "local_restore"
        or output["created_at_ms"] != created_at_ms
        or output["created_at_ms"] < parent["created_at_ms"]
        or output["canonical_json"] != source["canonical_json"]
        or output["checksum_sha256"] != source["checksum_sha256"]
    ):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile restore receipt has invalid lineage.",
        )

    basics = _public_basics(output)
    return {
        "request_id": request_id,
        "profile_version_id": output_id,
        "parent_profile_version_id": parent_id,
        "restored_from_profile_version_id": source_id,
        "restored_from_version_number": source["version_number"],
        "version_number": output["version_number"],
        "created_at_ms": output["created_at_ms"],
        "created": created,
        "profile_name": basics["name"] or "Unnamed profile",
        "renderable": basics["renderable"],
    }


def restore_profile_version(data_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Append an exact historical profile snapshot as the new current version."""

    expected_keys = {
        "source_profile_version_id",
        "expected_parent_profile_version_id",
        "request_id",
    }
    if set(params) != expected_keys:
        raise ValueError(
            "params must contain exactly source_profile_version_id, "
            "expected_parent_profile_version_id, and request_id"
        )
    source_id = _canonical_uuid(
        params["source_profile_version_id"],
        label="source_profile_version_id",
    )
    parent_id = _canonical_uuid(
        params["expected_parent_profile_version_id"],
        label="expected_parent_profile_version_id",
    )
    request_id = _canonical_uuid(params["request_id"], label="request_id")
    fingerprint_input = {
        "expected_parent_profile_version_id": parent_id,
        "request_id": request_id,
        "source_profile_version_id": source_id,
    }
    fingerprint_json = json.dumps(
        fingerprint_input,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_fingerprint = hashlib.sha256(
        fingerprint_json.encode("utf-8")
    ).hexdigest()

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM profile_restore_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise VaultError(
                    "profile_update_request_conflict",
                    "That profile restore request was already used with different inputs.",
                )
            result = _restore_receipt(connection, existing, created=False)
            connection.commit()
            return result

        current_row = connection.execute(
            """
            SELECT id, version_number, parent_version_id, canonical_json,
                   checksum_sha256, source, created_at_ms
            FROM profile_versions
            ORDER BY version_number DESC
            LIMIT 1
            """
        ).fetchone()
        if current_row is None:
            raise VaultError(
                "profile_missing",
                "Build or import a career profile before restoring profile history.",
            )
        current = _stored_version(current_row)
        if current["id"] != parent_id:
            raise VaultError(
                "profile_update_conflict",
                "The career profile changed before this historical version could be restored.",
            )
        if source_id == parent_id:
            raise ValueError("source_profile_version_id must identify a historical version")
        source = _stored_version(_version_row(connection, source_id))
        if source["version_number"] >= current["version_number"]:
            raise ValueError("source_profile_version_id must identify a historical version")
        if source["checksum_sha256"] == current["checksum_sha256"]:
            raise ValueError("The selected historical version already matches the current profile")
        if current["version_number"] >= MAX_RPC_SAFE_COUNT:
            raise VaultError(
                "profile_version_limit",
                "The local profile history cannot be advanced.",
            )

        output_id = str(uuid.uuid4())
        version_number = current["version_number"] + 1
        created_at_ms = max(_utc_now_ms(), current["created_at_ms"])
        connection.execute(
            """
            INSERT INTO profile_versions(
                id, version_number, parent_version_id, canonical_json,
                checksum_sha256, source, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, 'local_restore', ?)
            """,
            (
                output_id,
                version_number,
                parent_id,
                source["canonical_json"],
                source["checksum_sha256"],
                created_at_ms,
            ),
        )
        connection.execute(
            """
            INSERT INTO profile_restore_receipts(
                request_id, request_fingerprint, output_profile_version_id,
                parent_profile_version_id, source_profile_version_id, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_fingerprint,
                output_id,
                parent_id,
                source_id,
                created_at_ms,
            ),
        )
        receipt = connection.execute(
            "SELECT * FROM profile_restore_receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        result = _restore_receipt(connection, receipt, created=True)
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise VaultError(
            "vault_integrity_error",
            "The local vault rejected invalid profile restore lineage.",
        ) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "PROFILE_VERSION_LIST_LIMIT",
    "PROFILE_VERSION_OFFSET_LIMIT",
    "diff_profile_versions",
    "get_profile_basics",
    "get_profile_review_version",
    "list_profile_versions",
    "restore_profile_version",
    "update_profile_basics",
]
