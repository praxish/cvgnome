# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from .resume import is_baseline_resume_renderable
from .source_review_schema import SOURCE_REVIEW_MIGRATION
from .memory_schema import memory_migration

SCHEMA_VERSION = 19
DATABASE_FILENAME = "cvgnome.sqlite3"
APPLICATION_ID = 0x43564C54  # "CVLT"
MAX_MANAGED_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_PROFILE_SOURCE_FILES = 64
MAX_PROFILE_SOURCE_FILE_BYTES = 10 * 1024 * 1024
MAX_PROFILE_SOURCE_TOTAL_BYTES = 40 * 1024 * 1024
MAX_EXTRACTED_SOURCE_CHARS = 250_000
MAX_EXTRACTED_SOURCE_TOTAL_CHARS = 2_000_000
MAX_DRAFT_PROFILE_BYTES = 2 * 1024 * 1024
MAX_CANONICAL_PROFILE_FILE_BYTES = 4 * 1024 * 1024
MAX_SCAN_REPORT_BYTES = 128 * 1024
MAX_REVIEW_CANDIDATES = 240
MAX_REVIEW_CANDIDATE_BYTES = 2 * 1024 * 1024
MAX_RPC_SAFE_COUNT = 9_007_199_254_740_991
MAX_SCAN_LIFETIME_MS = 24 * 60 * 60 * 1000
INTERRUPTED_SCAN_GRACE_MS = 5 * 60 * 1000
ORPHAN_SOURCE_GRACE_MS = 24 * 60 * 60 * 1000
SOURCE_FORMATS = {"pdf", "docx", "txt", "md", "json", "csv"}
SOURCE_KINDS = {"resume", "evidence", "preferences", "unclassified"}
EXTRACTION_STATUSES = {"parsed", "failed", "duplicate"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class VaultError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VaultStatus:
    schema_version: int
    database_path: str
    profile_versions: int
    opportunities: int
    artifacts: int
    pending_jobs: int
    source_snapshots: int
    source_imports: int
    source_previews: int
    review_inbox_items: int
    review_deferred_items: int
    review_history_items: int
    source_review_inbox_items: int
    source_review_deferred_items: int
    source_review_history_items: int
    memory_count: int
    source_retention_generation: int
    retained_source_files: int
    retained_source_bytes: int
    retained_source_imports: int
    historical_source_files: int
    historical_source_imports: int
    last_source_import_at_ms: int | None
    source_retention_reset_at_ms: int | None
    latest_profile_name: str | None
    latest_profile_renderable: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileSaveResult:
    id: str
    version_number: int
    checksum_sha256: str
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactSaveResult:
    id: str
    kind: str
    profile_version_id: str
    relative_path: str
    checksum_sha256: str
    byte_size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetainedSourceStatus:
    source_retention_generation: int
    retained_source_files: int
    retained_source_bytes: int
    retained_source_imports: int
    historical_source_files: int
    historical_source_imports: int
    last_source_import_at_ms: int | None
    source_retention_reset_at_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            name TEXT NOT NULL UNIQUE,
            checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
            applied_at_ms INTEGER NOT NULL,
            app_version TEXT NOT NULL
        ) STRICT;

        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL CHECK (json_valid(value_json)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            updated_at_ms INTEGER NOT NULL
        ) STRICT;

        CREATE TABLE profile_versions (
            id TEXT PRIMARY KEY,
            version_number INTEGER NOT NULL UNIQUE CHECK (version_number > 0),
            parent_version_id TEXT REFERENCES profile_versions(id) ON DELETE RESTRICT,
            canonical_json TEXT NOT NULL
                CHECK (json_valid(canonical_json) AND json_type(canonical_json) = 'object'),
            checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
            source TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        ) STRICT;

        CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            media_type TEXT,
            checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
            relative_path TEXT,
            extracted_text TEXT,
            imported_at_ms INTEGER NOT NULL,
            UNIQUE (checksum_sha256, display_name)
        ) STRICT;

        CREATE TABLE opportunities (
            id TEXT PRIMARY KEY,
            company TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            source_url TEXT,
            status TEXT NOT NULL DEFAULT 'saved'
                CHECK (status IN ('saved', 'analyzing', 'ready', 'applying', 'applied', 'archived')),
            job_description TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        ) STRICT;

        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            profile_version_id TEXT REFERENCES profile_versions(id) ON DELETE SET NULL,
            opportunity_id TEXT REFERENCES opportunities(id) ON DELETE SET NULL,
            payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
            relative_path TEXT,
            content_fingerprint TEXT,
            created_at_ms INTEGER NOT NULL
        ) STRICT;

        CREATE TABLE workflow_jobs (
            id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
            result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
            error_code TEXT,
            error_message TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            created_at_ms INTEGER NOT NULL,
            started_at_ms INTEGER,
            finished_at_ms INTEGER
        ) STRICT;

        CREATE TABLE model_usage_events (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            operation TEXT NOT NULL,
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            estimated_cost_microusd INTEGER
                CHECK (estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0),
            is_local INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1)),
            created_at_ms INTEGER NOT NULL
        ) STRICT;

        CREATE INDEX opportunities_status_updated_idx
            ON opportunities(status, updated_at_ms DESC);
        CREATE INDEX artifacts_profile_version_idx
            ON artifacts(profile_version_id, created_at_ms DESC);
        CREATE INDEX artifacts_opportunity_idx
            ON artifacts(opportunity_id, created_at_ms DESC);
        CREATE INDEX workflow_jobs_status_created_idx
            ON workflow_jobs(status, created_at_ms);
        CREATE INDEX model_usage_created_idx
            ON model_usage_events(created_at_ms DESC);
    """,
    2: """
        ALTER TABLE artifacts ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'
            CHECK (status IN ('rendering', 'ready', 'failed'));
        ALTER TABLE artifacts ADD COLUMN format TEXT
            CHECK (format IS NULL OR format IN ('docx', 'pdf'));
        ALTER TABLE artifacts ADD COLUMN checksum_sha256 TEXT
            CHECK (checksum_sha256 IS NULL OR length(checksum_sha256) = 64);
        ALTER TABLE artifacts ADD COLUMN byte_size INTEGER
            CHECK (byte_size IS NULL OR byte_size > 0);
        ALTER TABLE artifacts ADD COLUMN render_fingerprint TEXT
            CHECK (render_fingerprint IS NULL OR length(render_fingerprint) = 64);
        ALTER TABLE artifacts ADD COLUMN template_id TEXT;
        ALTER TABLE artifacts ADD COLUMN suggested_filename TEXT;

        CREATE UNIQUE INDEX artifacts_ready_render_fingerprint_idx
            ON artifacts(render_fingerprint) WHERE status = 'ready';

        CREATE TRIGGER profile_versions_no_update
        BEFORE UPDATE ON profile_versions
        BEGIN
            SELECT RAISE(ABORT, 'profile versions are immutable');
        END;

        CREATE TRIGGER profile_versions_no_delete
        BEFORE DELETE ON profile_versions
        BEGIN
            SELECT RAISE(ABORT, 'profile versions are immutable');
        END;
    """,
    3: """
        ALTER TABLE sources ADD COLUMN byte_size INTEGER
            CHECK (byte_size IS NULL OR byte_size > 0);
        ALTER TABLE sources ADD COLUMN source_format TEXT
            CHECK (source_format IS NULL OR source_format IN ('pdf', 'docx', 'txt', 'md', 'json', 'csv'));
        ALTER TABLE sources ADD COLUMN content_addressed INTEGER NOT NULL DEFAULT 0
            CHECK (content_addressed IN (0, 1));

        CREATE UNIQUE INDEX sources_content_checksum_idx
            ON sources(checksum_sha256) WHERE content_addressed = 1;

        CREATE TABLE source_extractions (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
            parser_contract TEXT NOT NULL,
            extracted_text TEXT NOT NULL,
            extracted_text_sha256 TEXT NOT NULL CHECK (length(extracted_text_sha256) = 64),
            candidate_profile_json TEXT
                CHECK (candidate_profile_json IS NULL OR (
                    json_valid(candidate_profile_json)
                    AND json_type(candidate_profile_json) = 'object'
                )),
            warnings_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(warnings_json) AND json_type(warnings_json) = 'array'),
            created_at_ms INTEGER NOT NULL,
            UNIQUE (source_id, parser_contract)
        ) STRICT;

        CREATE TABLE profile_source_scans (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'preview'
                CHECK (status IN ('preview', 'committing')),
            base_profile_version_id TEXT REFERENCES profile_versions(id) ON DELETE RESTRICT,
            base_profile_checksum_sha256 TEXT
                CHECK (base_profile_checksum_sha256 IS NULL OR length(base_profile_checksum_sha256) = 64),
            draft_profile_json TEXT NOT NULL
                CHECK (json_valid(draft_profile_json) AND json_type(draft_profile_json) = 'object'),
            draft_profile_checksum_sha256 TEXT NOT NULL
                CHECK (length(draft_profile_checksum_sha256) = 64),
            report_json TEXT NOT NULL
                CHECK (json_valid(report_json) AND json_type(report_json) = 'object'),
            expires_at_ms INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            CHECK ((base_profile_version_id IS NULL) = (base_profile_checksum_sha256 IS NULL))
        ) STRICT;

        CREATE TABLE profile_source_scan_items (
            scan_id TEXT NOT NULL REFERENCES profile_source_scans(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            staged_relative_path TEXT NOT NULL,
            display_name TEXT NOT NULL,
            source_format TEXT NOT NULL
                CHECK (source_format IN ('pdf', 'docx', 'txt', 'md', 'json', 'csv')),
            media_type TEXT,
            source_kind TEXT NOT NULL
                CHECK (source_kind IN ('resume', 'evidence', 'preferences', 'unclassified')),
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
            parser_contract TEXT NOT NULL,
            extraction_status TEXT NOT NULL
                CHECK (extraction_status IN ('parsed', 'failed', 'duplicate')),
            issue_code TEXT,
            extracted_text TEXT NOT NULL DEFAULT '',
            extracted_text_sha256 TEXT
                CHECK (extracted_text_sha256 IS NULL OR length(extracted_text_sha256) = 64),
            candidate_profile_json TEXT
                CHECK (candidate_profile_json IS NULL OR (
                    json_valid(candidate_profile_json)
                    AND json_type(candidate_profile_json) = 'object'
                )),
            warnings_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(warnings_json) AND json_type(warnings_json) = 'array'),
            PRIMARY KEY (scan_id, ordinal),
            UNIQUE (scan_id, staged_relative_path)
        ) STRICT;

        CREATE TABLE profile_source_imports (
            id TEXT PRIMARY KEY,
            base_profile_version_id TEXT REFERENCES profile_versions(id) ON DELETE RESTRICT,
            base_profile_checksum_sha256 TEXT
                CHECK (base_profile_checksum_sha256 IS NULL OR length(base_profile_checksum_sha256) = 64),
            output_profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            source_set_checksum_sha256 TEXT NOT NULL CHECK (length(source_set_checksum_sha256) = 64),
            synthesis_contract TEXT NOT NULL,
            report_json TEXT NOT NULL
                CHECK (json_valid(report_json) AND json_type(report_json) = 'object'),
            result_json TEXT NOT NULL
                CHECK (json_valid(result_json) AND json_type(result_json) = 'object'),
            committed_at_ms INTEGER NOT NULL,
            CHECK ((base_profile_version_id IS NULL) = (base_profile_checksum_sha256 IS NULL))
        ) STRICT;

        CREATE TABLE profile_version_sources (
            import_id TEXT NOT NULL REFERENCES profile_source_imports(id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
            extraction_id TEXT REFERENCES source_extractions(id) ON DELETE RESTRICT,
            source_format TEXT NOT NULL
                CHECK (source_format IN ('pdf', 'docx', 'txt', 'md', 'json', 'csv')),
            source_kind TEXT NOT NULL
                CHECK (source_kind IN ('resume', 'evidence', 'preferences', 'unclassified')),
            extraction_status TEXT NOT NULL
                CHECK (extraction_status IN ('parsed', 'failed', 'duplicate')),
            issue_code TEXT,
            PRIMARY KEY (import_id, ordinal)
        ) STRICT;

        CREATE INDEX source_extractions_source_idx
            ON source_extractions(source_id, created_at_ms DESC);
        CREATE INDEX profile_source_scans_expiry_idx
            ON profile_source_scans(status, expires_at_ms);
        CREATE INDEX profile_source_imports_profile_idx
            ON profile_source_imports(output_profile_version_id, committed_at_ms DESC);
        CREATE INDEX profile_version_sources_profile_idx
            ON profile_version_sources(profile_version_id, import_id);

        CREATE TRIGGER content_sources_no_update
        BEFORE UPDATE ON sources
        WHEN OLD.content_addressed = 1
        BEGIN
            SELECT RAISE(ABORT, 'content-addressed sources are immutable');
        END;

        CREATE TRIGGER content_sources_no_delete
        BEFORE DELETE ON sources
        WHEN OLD.content_addressed = 1
        BEGIN
            SELECT RAISE(ABORT, 'content-addressed sources are immutable');
        END;

        CREATE TRIGGER source_extractions_no_update
        BEFORE UPDATE ON source_extractions
        BEGIN
            SELECT RAISE(ABORT, 'source extractions are immutable');
        END;

        CREATE TRIGGER source_extractions_no_delete
        BEFORE DELETE ON source_extractions
        BEGIN
            SELECT RAISE(ABORT, 'source extractions are immutable');
        END;

        CREATE TRIGGER profile_source_imports_no_update
        BEFORE UPDATE ON profile_source_imports
        BEGIN
            SELECT RAISE(ABORT, 'profile source imports are immutable');
        END;

        CREATE TRIGGER profile_source_imports_no_delete
        BEFORE DELETE ON profile_source_imports
        BEGIN
            SELECT RAISE(ABORT, 'profile source imports are immutable');
        END;

        CREATE TRIGGER profile_version_sources_no_update
        BEFORE UPDATE ON profile_version_sources
        BEGIN
            SELECT RAISE(ABORT, 'profile source links are immutable');
        END;

        CREATE TRIGGER profile_version_sources_no_delete
        BEFORE DELETE ON profile_version_sources
        BEGIN
            SELECT RAISE(ABORT, 'profile source links are immutable');
        END;
    """,
    4: """
        ALTER TABLE opportunities ADD COLUMN location TEXT NOT NULL DEFAULT '';
        ALTER TABLE opportunities ADD COLUMN apply_url TEXT;
        ALTER TABLE opportunities ADD COLUMN identity_sha256 TEXT
            CHECK (identity_sha256 IS NULL OR length(identity_sha256) = 64);

        CREATE UNIQUE INDEX opportunities_identity_idx
            ON opportunities(identity_sha256) WHERE identity_sha256 IS NOT NULL;

        CREATE TABLE opportunity_snapshots (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE RESTRICT,
            snapshot_number INTEGER NOT NULL CHECK (snapshot_number > 0),
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 240),
            company TEXT NOT NULL DEFAULT '' CHECK (length(company) <= 160),
            location TEXT NOT NULL DEFAULT '' CHECK (length(location) <= 160),
            source_url TEXT CHECK (source_url IS NULL OR length(source_url) <= 2048),
            apply_url TEXT CHECK (apply_url IS NULL OR length(apply_url) <= 2048),
            description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 32000),
            search_text TEXT NOT NULL CHECK (length(search_text) BETWEEN 1 AND 100000),
            checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
            captured_at_ms INTEGER NOT NULL,
            UNIQUE (opportunity_id, snapshot_number)
        ) STRICT;

        CREATE TABLE opportunity_matches (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE RESTRICT,
            match_number INTEGER NOT NULL CHECK (match_number > 0),
            snapshot_id TEXT NOT NULL REFERENCES opportunity_snapshots(id) ON DELETE RESTRICT,
            snapshot_checksum_sha256 TEXT NOT NULL CHECK (length(snapshot_checksum_sha256) = 64),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            profile_checksum_sha256 TEXT NOT NULL CHECK (length(profile_checksum_sha256) = 64),
            contract TEXT NOT NULL CHECK (length(contract) BETWEEN 1 AND 80),
            input_fingerprint TEXT NOT NULL UNIQUE CHECK (length(input_fingerprint) = 64),
            score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
            signal TEXT NOT NULL CHECK (signal IN ('strong', 'mixed', 'limited')),
            result_json TEXT NOT NULL CHECK (
                json_valid(result_json)
                AND json_type(result_json) = 'object'
                AND length(result_json) <= 24576
            ),
            created_at_ms INTEGER NOT NULL,
            UNIQUE (opportunity_id, match_number),
            UNIQUE (snapshot_id, profile_version_id, contract)
        ) STRICT;

        CREATE INDEX opportunity_snapshots_opportunity_idx
            ON opportunity_snapshots(opportunity_id, snapshot_number DESC);
        CREATE INDEX opportunity_matches_opportunity_idx
            ON opportunity_matches(opportunity_id, match_number DESC);
        CREATE INDEX opportunity_matches_snapshot_idx
            ON opportunity_matches(snapshot_id, created_at_ms DESC);
        CREATE INDEX opportunity_matches_profile_idx
            ON opportunity_matches(profile_version_id, created_at_ms DESC);

        CREATE TRIGGER opportunity_snapshots_no_update
        BEFORE UPDATE ON opportunity_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'opportunity snapshots are immutable');
        END;

        CREATE TRIGGER opportunity_snapshots_no_delete
        BEFORE DELETE ON opportunity_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'opportunity snapshots are immutable');
        END;

        CREATE TRIGGER opportunity_matches_no_update
        BEFORE UPDATE ON opportunity_matches
        BEGIN
            SELECT RAISE(ABORT, 'opportunity matches are immutable');
        END;

        CREATE TRIGGER opportunity_matches_validate_insert
        BEFORE INSERT ON opportunity_matches
        WHEN NOT EXISTS (
            SELECT 1 FROM opportunity_snapshots AS snapshot
            WHERE snapshot.id = NEW.snapshot_id
              AND snapshot.opportunity_id = NEW.opportunity_id
              AND snapshot.checksum_sha256 = NEW.snapshot_checksum_sha256
        ) OR NOT EXISTS (
            SELECT 1 FROM profile_versions AS profile
            WHERE profile.id = NEW.profile_version_id
              AND profile.checksum_sha256 = NEW.profile_checksum_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'opportunity match inputs do not match their immutable pins');
        END;

        CREATE TRIGGER opportunity_matches_no_delete
        BEFORE DELETE ON opportunity_matches
        BEGIN
            SELECT RAISE(ABORT, 'opportunity matches are immutable');
        END;
    """,
    5: """
        DROP TRIGGER profile_version_sources_no_update;
        DROP TRIGGER profile_version_sources_no_delete;
        DROP INDEX profile_version_sources_profile_idx;

        CREATE TABLE profile_version_sources_v5 (
            import_id TEXT NOT NULL REFERENCES profile_source_imports(id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
            display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 240),
            extraction_id TEXT REFERENCES source_extractions(id) ON DELETE RESTRICT,
            source_format TEXT NOT NULL
                CHECK (source_format IN ('pdf', 'docx', 'txt', 'md', 'json', 'csv')),
            source_kind TEXT NOT NULL
                CHECK (source_kind IN ('resume', 'evidence', 'preferences', 'unclassified')),
            extraction_status TEXT NOT NULL
                CHECK (extraction_status IN ('parsed', 'failed', 'duplicate')),
            issue_code TEXT,
            PRIMARY KEY (import_id, ordinal)
        ) STRICT;

        INSERT INTO profile_version_sources_v5(
            import_id, ordinal, profile_version_id, source_id, display_name,
            extraction_id, source_format, source_kind, extraction_status, issue_code
        )
        SELECT
            legacy.import_id,
            legacy.ordinal,
            legacy.profile_version_id,
            legacy.source_id,
            CASE
                WHEN length(source.display_name) BETWEEN 1 AND 240
                    THEN source.display_name
                WHEN length(source.display_name) > 240
                    THEN substr(source.display_name, 1, 240)
                ELSE 'Source'
            END,
            legacy.extraction_id,
            legacy.source_format,
            legacy.source_kind,
            legacy.extraction_status,
            legacy.issue_code
        FROM profile_version_sources AS legacy
        JOIN sources AS source ON source.id = legacy.source_id;

        DROP TABLE profile_version_sources;
        ALTER TABLE profile_version_sources_v5 RENAME TO profile_version_sources;

        CREATE INDEX profile_version_sources_profile_idx
            ON profile_version_sources(profile_version_id, import_id);

        CREATE TRIGGER profile_version_sources_no_update
        BEFORE UPDATE ON profile_version_sources
        BEGIN
            SELECT RAISE(ABORT, 'profile source links are immutable');
        END;

        CREATE TRIGGER profile_version_sources_no_delete
        BEFORE DELETE ON profile_version_sources
        BEGIN
            SELECT RAISE(ABORT, 'profile source links are immutable');
        END;

        ALTER TABLE profile_source_imports
            ADD COLUMN retention_generation INTEGER NOT NULL DEFAULT 1
            CHECK (retention_generation > 0);

        CREATE INDEX profile_source_imports_retention_generation_idx
            ON profile_source_imports(retention_generation, committed_at_ms DESC);

        CREATE TABLE profile_source_retention_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            current_generation INTEGER NOT NULL CHECK (current_generation > 0),
            last_reset_request_id TEXT CHECK (
                last_reset_request_id IS NULL OR (
                    length(last_reset_request_id) = 36
                    AND substr(last_reset_request_id, 9, 1) = '-'
                    AND substr(last_reset_request_id, 14, 1) = '-'
                    AND substr(last_reset_request_id, 19, 1) = '-'
                    AND substr(last_reset_request_id, 24, 1) = '-'
                    AND last_reset_request_id = lower(last_reset_request_id)
                    AND last_reset_request_id NOT GLOB '*[^0-9a-f-]*'
                    AND length(replace(last_reset_request_id, '-', '')) = 32
                )
            ),
            reset_at_ms INTEGER CHECK (reset_at_ms IS NULL OR reset_at_ms > 0),
            CHECK ((last_reset_request_id IS NULL) = (reset_at_ms IS NULL))
        ) STRICT;

        INSERT INTO profile_source_retention_state(
            singleton_id, current_generation, last_reset_request_id, reset_at_ms
        ) VALUES (1, 1, NULL, NULL);

        CREATE TRIGGER profile_source_retention_state_no_delete
        BEFORE DELETE ON profile_source_retention_state
        BEGIN
            SELECT RAISE(ABORT, 'profile source retention state cannot be deleted');
        END;

        CREATE TRIGGER profile_source_retention_state_no_insert
        BEFORE INSERT ON profile_source_retention_state
        WHEN EXISTS (SELECT 1 FROM profile_source_retention_state)
        BEGIN
            SELECT RAISE(ABORT, 'profile source retention state is a singleton');
        END;
    """,
    6: """
        ALTER TABLE opportunities ADD COLUMN tracker_stage TEXT NOT NULL DEFAULT 'tracked'
            CHECK (tracker_stage IN (
                'tracked', 'new', 'matched', 'to_apply', 'applied',
                'interviewing', 'offer', 'rejected', 'closed', 'ignored'
            ));
        ALTER TABLE opportunities ADD COLUMN tracker_updated_at_ms INTEGER NOT NULL DEFAULT 0
            CHECK (tracker_updated_at_ms >= 0);

        UPDATE opportunities
        SET tracker_updated_at_ms = updated_at_ms;

        CREATE INDEX opportunities_tracker_stage_action_idx
            ON opportunities(tracker_stage, tracker_updated_at_ms DESC);
        CREATE INDEX opportunities_tracker_action_idx
            ON opportunities(tracker_updated_at_ms DESC);
    """,
    7: """
        CREATE TABLE tailoring_attempts (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES opportunity_snapshots(id) ON DELETE RESTRICT,
            snapshot_checksum_sha256 TEXT NOT NULL CHECK (length(snapshot_checksum_sha256) = 64),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            profile_checksum_sha256 TEXT NOT NULL CHECK (length(profile_checksum_sha256) = 64),
            status TEXT NOT NULL CHECK (status IN ('prepared', 'armed', 'succeeded', 'failed')),
            input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
            baseline_resume_json TEXT NOT NULL CHECK (
                json_valid(baseline_resume_json)
                AND json_type(baseline_resume_json) = 'object'
            ),
            evidence_chunks_json TEXT NOT NULL CHECK (
                json_valid(evidence_chunks_json)
                AND json_type(evidence_chunks_json) = 'array'
                AND json_array_length(evidence_chunks_json) BETWEEN 1 AND 32
            ),
            selected_evidence_json TEXT CHECK (
                selected_evidence_json IS NULL OR (
                    json_valid(selected_evidence_json)
                    AND json_type(selected_evidence_json) = 'array'
                    AND json_array_length(selected_evidence_json) BETWEEN 1 AND 8
                )
            ),
            provider_kind TEXT NOT NULL CHECK (length(provider_kind) BETWEEN 1 AND 64),
            chat_model_id TEXT NOT NULL CHECK (length(chat_model_id) BETWEEN 1 AND 240),
            embedding_model_id TEXT NOT NULL CHECK (length(embedding_model_id) BETWEEN 1 AND 240),
            embedding_count INTEGER NOT NULL CHECK (embedding_count BETWEEN 2 AND 33),
            embedding_dimension INTEGER CHECK (
                embedding_dimension IS NULL OR embedding_dimension BETWEEN 1 AND 8192
            ),
            error_code TEXT CHECK (
                error_code IS NULL OR error_code IN (
                    'cancelled', 'provider_unavailable', 'provider_auth_failed',
                    'provider_timeout', 'chat_model_unavailable',
                    'embedding_model_unavailable', 'embedding_request_failed',
                    'chat_request_failed', 'provider_response_invalid'
                )
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
            armed_at_ms INTEGER CHECK (armed_at_ms IS NULL OR armed_at_ms >= created_at_ms),
            finished_at_ms INTEGER CHECK (finished_at_ms IS NULL OR finished_at_ms >= created_at_ms),
            CHECK (
                (status = 'prepared'
                    AND selected_evidence_json IS NULL
                    AND embedding_dimension IS NULL
                    AND error_code IS NULL
                    AND armed_at_ms IS NULL
                    AND finished_at_ms IS NULL)
                OR
                (status = 'armed'
                    AND selected_evidence_json IS NOT NULL
                    AND embedding_dimension IS NOT NULL
                    AND error_code IS NULL
                    AND armed_at_ms IS NOT NULL
                    AND finished_at_ms IS NULL)
                OR
                (status = 'succeeded'
                    AND selected_evidence_json IS NOT NULL
                    AND embedding_dimension IS NOT NULL
                    AND error_code IS NULL
                    AND armed_at_ms IS NOT NULL
                    AND finished_at_ms IS NOT NULL)
                OR
                (status = 'failed'
                    AND error_code IS NOT NULL
                    AND finished_at_ms IS NOT NULL)
            )
        ) STRICT;

        CREATE TABLE tailored_resume_drafts (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE REFERENCES tailoring_attempts(id) ON DELETE RESTRICT,
            opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES opportunity_snapshots(id) ON DELETE RESTRICT,
            snapshot_checksum_sha256 TEXT NOT NULL CHECK (length(snapshot_checksum_sha256) = 64),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            profile_checksum_sha256 TEXT NOT NULL CHECK (length(profile_checksum_sha256) = 64),
            provider_kind TEXT NOT NULL CHECK (length(provider_kind) BETWEEN 1 AND 64),
            chat_model_id TEXT NOT NULL CHECK (length(chat_model_id) BETWEEN 1 AND 240),
            embedding_model_id TEXT NOT NULL CHECK (length(embedding_model_id) BETWEEN 1 AND 240),
            input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
            baseline_resume_json TEXT NOT NULL CHECK (
                json_valid(baseline_resume_json)
                AND json_type(baseline_resume_json) = 'object'
            ),
            result_resume_json TEXT NOT NULL CHECK (
                json_valid(result_resume_json)
                AND json_type(result_resume_json) = 'object'
            ),
            selected_evidence_json TEXT NOT NULL CHECK (
                json_valid(selected_evidence_json)
                AND json_type(selected_evidence_json) = 'array'
                AND json_array_length(selected_evidence_json) BETWEEN 1 AND 8
            ),
            quality_issue_codes_json TEXT NOT NULL CHECK (
                json_valid(quality_issue_codes_json)
                AND json_type(quality_issue_codes_json) = 'array'
                AND json_array_length(quality_issue_codes_json) <= 16
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0)
        ) STRICT;

        CREATE INDEX tailoring_attempts_opportunity_created_idx
            ON tailoring_attempts(opportunity_id, created_at_ms DESC, id DESC);
        CREATE INDEX tailoring_attempts_status_created_idx
            ON tailoring_attempts(status, created_at_ms DESC);
        CREATE INDEX tailoring_attempts_input_fingerprint_idx
            ON tailoring_attempts(input_fingerprint, created_at_ms DESC);
        CREATE INDEX tailored_resume_drafts_opportunity_created_idx
            ON tailored_resume_drafts(opportunity_id, created_at_ms DESC, id DESC);

        CREATE TRIGGER tailoring_attempts_validate_insert
        BEFORE INSERT ON tailoring_attempts
        WHEN NEW.status != 'prepared' OR NOT EXISTS (
            SELECT 1 FROM opportunity_snapshots AS snapshot
            WHERE snapshot.id = NEW.snapshot_id
              AND snapshot.opportunity_id = NEW.opportunity_id
              AND snapshot.checksum_sha256 = NEW.snapshot_checksum_sha256
        ) OR NOT EXISTS (
            SELECT 1 FROM profile_versions AS profile
            WHERE profile.id = NEW.profile_version_id
              AND profile.checksum_sha256 = NEW.profile_checksum_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'tailoring attempt inputs do not match their immutable pins');
        END;

        CREATE TRIGGER tailoring_attempts_validate_update
        BEFORE UPDATE ON tailoring_attempts
        WHEN NEW.id IS NOT OLD.id
          OR NEW.opportunity_id IS NOT OLD.opportunity_id
          OR NEW.snapshot_id IS NOT OLD.snapshot_id
          OR NEW.snapshot_checksum_sha256 IS NOT OLD.snapshot_checksum_sha256
          OR NEW.profile_version_id IS NOT OLD.profile_version_id
          OR NEW.profile_checksum_sha256 IS NOT OLD.profile_checksum_sha256
          OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
          OR NEW.baseline_resume_json IS NOT OLD.baseline_resume_json
          OR NEW.evidence_chunks_json IS NOT OLD.evidence_chunks_json
          OR NEW.provider_kind IS NOT OLD.provider_kind
          OR NEW.chat_model_id IS NOT OLD.chat_model_id
          OR NEW.embedding_model_id IS NOT OLD.embedding_model_id
          OR NEW.embedding_count IS NOT OLD.embedding_count
          OR NEW.created_at_ms IS NOT OLD.created_at_ms
          OR NOT (
              (OLD.status = 'prepared' AND NEW.status IN ('armed', 'failed'))
              OR (OLD.status = 'armed' AND NEW.status IN ('succeeded', 'failed'))
          )
        BEGIN
            SELECT RAISE(ABORT, 'tailoring attempt transition is invalid');
        END;

        CREATE TRIGGER tailoring_attempts_validate_success
        BEFORE UPDATE OF status ON tailoring_attempts
        WHEN NEW.status = 'succeeded' AND NOT EXISTS (
            SELECT 1 FROM tailored_resume_drafts AS draft
            WHERE draft.attempt_id = NEW.id
              AND draft.opportunity_id = NEW.opportunity_id
              AND draft.snapshot_id = NEW.snapshot_id
              AND draft.snapshot_checksum_sha256 = NEW.snapshot_checksum_sha256
              AND draft.profile_version_id = NEW.profile_version_id
              AND draft.profile_checksum_sha256 = NEW.profile_checksum_sha256
              AND draft.input_fingerprint = NEW.input_fingerprint
        )
        BEGIN
            SELECT RAISE(ABORT, 'succeeded tailoring attempt requires an immutable draft');
        END;

        CREATE TRIGGER tailoring_attempts_no_delete
        BEFORE DELETE ON tailoring_attempts
        BEGIN
            SELECT RAISE(ABORT, 'tailoring attempts cannot be deleted');
        END;

        CREATE TRIGGER tailored_resume_drafts_validate_insert
        BEFORE INSERT ON tailored_resume_drafts
        WHEN NOT EXISTS (
            SELECT 1 FROM tailoring_attempts AS attempt
            WHERE attempt.id = NEW.attempt_id
              AND attempt.status = 'armed'
              AND attempt.opportunity_id = NEW.opportunity_id
              AND attempt.snapshot_id = NEW.snapshot_id
              AND attempt.snapshot_checksum_sha256 = NEW.snapshot_checksum_sha256
              AND attempt.profile_version_id = NEW.profile_version_id
              AND attempt.profile_checksum_sha256 = NEW.profile_checksum_sha256
              AND attempt.provider_kind = NEW.provider_kind
              AND attempt.chat_model_id = NEW.chat_model_id
              AND attempt.embedding_model_id = NEW.embedding_model_id
              AND attempt.input_fingerprint = NEW.input_fingerprint
              AND attempt.baseline_resume_json = NEW.baseline_resume_json
              AND attempt.selected_evidence_json = NEW.selected_evidence_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'tailored resume draft does not match its armed attempt');
        END;

        CREATE TRIGGER tailored_resume_drafts_no_update
        BEFORE UPDATE ON tailored_resume_drafts
        BEGIN
            SELECT RAISE(ABORT, 'tailored resume drafts are immutable');
        END;

        CREATE TRIGGER tailored_resume_drafts_no_delete
        BEFORE DELETE ON tailored_resume_drafts
        BEGIN
            SELECT RAISE(ABORT, 'tailored resume drafts are immutable');
        END;
    """,
    8: """
        ALTER TABLE artifacts ADD COLUMN tailored_resume_draft_id TEXT
            REFERENCES tailored_resume_drafts(id) ON DELETE RESTRICT;
        ALTER TABLE artifacts ADD COLUMN source_resume_checksum_sha256 TEXT
            CHECK (
                source_resume_checksum_sha256 IS NULL
                OR length(source_resume_checksum_sha256) = 64
            );

        CREATE INDEX artifacts_tailored_resume_draft_idx
            ON artifacts(tailored_resume_draft_id, created_at_ms DESC)
            WHERE tailored_resume_draft_id IS NOT NULL;

        CREATE TRIGGER artifacts_validate_tailored_insert
        BEFORE INSERT ON artifacts
        WHEN (
            NEW.kind IN ('tailored_resume_docx', 'tailored_resume_pdf')
            AND (
                NEW.tailored_resume_draft_id IS NULL
                OR NEW.source_resume_checksum_sha256 IS NULL
                OR NEW.opportunity_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM tailored_resume_drafts AS draft
                    WHERE draft.id = NEW.tailored_resume_draft_id
                      AND draft.profile_version_id = NEW.profile_version_id
                      AND draft.opportunity_id = NEW.opportunity_id
                )
            )
        ) OR (
            NEW.kind NOT IN ('tailored_resume_docx', 'tailored_resume_pdf')
            AND (
                NEW.tailored_resume_draft_id IS NOT NULL
                OR NEW.source_resume_checksum_sha256 IS NOT NULL
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'artifact tailored resume lineage is invalid');
        END;

        CREATE TRIGGER artifacts_tailored_identity_no_update
        BEFORE UPDATE ON artifacts
        WHEN NEW.id IS NOT OLD.id
          OR NEW.kind IS NOT OLD.kind
          OR NEW.profile_version_id IS NOT OLD.profile_version_id
          OR NEW.opportunity_id IS NOT OLD.opportunity_id
          OR NEW.payload_json IS NOT OLD.payload_json
          OR NEW.content_fingerprint IS NOT OLD.content_fingerprint
          OR NEW.created_at_ms IS NOT OLD.created_at_ms
          OR NEW.tailored_resume_draft_id IS NOT OLD.tailored_resume_draft_id
          OR NEW.source_resume_checksum_sha256 IS NOT OLD.source_resume_checksum_sha256
          OR NEW.render_fingerprint IS NOT OLD.render_fingerprint
          OR NEW.format IS NOT OLD.format
          OR NEW.checksum_sha256 IS NOT OLD.checksum_sha256
          OR NEW.byte_size IS NOT OLD.byte_size
          OR NEW.template_id IS NOT OLD.template_id
          OR NEW.suggested_filename IS NOT OLD.suggested_filename
          OR NEW.relative_path IS NOT OLD.relative_path
        BEGIN
            SELECT RAISE(ABORT, 'artifact identity and lineage are immutable');
        END;
    """,
    9: """
        CREATE TABLE tailored_resume_revisions (
            id TEXT PRIMARY KEY
                CHECK(
                    length(id) = 36
                    AND substr(id, 9, 1) = '-'
                    AND substr(id, 14, 1) = '-'
                    AND substr(id, 19, 1) = '-'
                    AND substr(id, 24, 1) = '-'
                    AND id = lower(id)
                    AND length(replace(id, '-', '')) = 32
                    AND replace(id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_id TEXT NOT NULL UNIQUE
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            draft_id TEXT NOT NULL
                REFERENCES tailored_resume_drafts(id) ON DELETE RESTRICT,
            parent_revision_id TEXT
                REFERENCES tailored_resume_revisions(id) ON DELETE RESTRICT,
            revision_number INTEGER NOT NULL
                CHECK(revision_number BETWEEN 1 AND 9007199254740991),
            parent_resume_checksum_sha256 TEXT NOT NULL
                CHECK(
                    length(parent_resume_checksum_sha256) = 64
                    AND parent_resume_checksum_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
            result_resume_json TEXT NOT NULL
                CHECK(json_valid(result_resume_json) AND json_type(result_resume_json) = 'object'),
            result_resume_checksum_sha256 TEXT NOT NULL
                CHECK(
                    length(result_resume_checksum_sha256) = 64
                    AND result_resume_checksum_sha256 NOT GLOB '*[^0-9a-f]*'
                    AND result_resume_checksum_sha256 != parent_resume_checksum_sha256
                ),
            changed_fields_json TEXT NOT NULL
                CHECK(
                    json_valid(changed_fields_json)
                    AND json_type(changed_fields_json) = 'array'
                    AND json_array_length(changed_fields_json) BETWEEN 1 AND 66
                ),
            author_kind TEXT NOT NULL
                CHECK(author_kind = 'user'),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991),
            UNIQUE(draft_id, revision_number),
            UNIQUE(draft_id, parent_revision_id)
        ) STRICT;

        CREATE INDEX tailored_resume_revisions_draft_idx
            ON tailored_resume_revisions(draft_id, revision_number DESC);
        CREATE INDEX tailored_resume_revisions_parent_idx
            ON tailored_resume_revisions(parent_revision_id)
            WHERE parent_revision_id IS NOT NULL;

        CREATE TRIGGER tailored_resume_revisions_validate_insert
        BEFORE INSERT ON tailored_resume_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM tailored_resume_drafts AS draft
            WHERE draft.id = NEW.draft_id
              AND draft.created_at_ms <= NEW.created_at_ms
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) AS field
            WHERE field.type != 'text'
               OR field.value = ''
               OR length(CAST(field.value AS BLOB)) > 512
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_fields_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_fields_json)
        ) OR (
            NEW.parent_revision_id IS NULL
            AND (
                NEW.revision_number != 1
                OR EXISTS (
                    SELECT 1 FROM tailored_resume_revisions AS existing
                    WHERE existing.draft_id = NEW.draft_id
                )
            )
        ) OR (
            NEW.parent_revision_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM tailored_resume_revisions AS parent
                WHERE parent.id = NEW.parent_revision_id
                  AND parent.draft_id = NEW.draft_id
                  AND parent.revision_number + 1 = NEW.revision_number
                  AND parent.result_resume_checksum_sha256 =
                      NEW.parent_resume_checksum_sha256
                  AND parent.created_at_ms <= NEW.created_at_ms
                  AND NOT EXISTS (
                      SELECT 1 FROM tailored_resume_revisions AS child
                      WHERE child.parent_revision_id = parent.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM tailored_resume_revisions AS newer
                      WHERE newer.draft_id = NEW.draft_id
                        AND newer.revision_number > parent.revision_number
                  )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'tailored resume revision lineage is invalid');
        END;

        CREATE TRIGGER tailored_resume_revisions_no_update
        BEFORE UPDATE ON tailored_resume_revisions
        BEGIN
            SELECT RAISE(ABORT, 'tailored resume revisions are immutable');
        END;

        CREATE TRIGGER tailored_resume_revisions_no_delete
        BEFORE DELETE ON tailored_resume_revisions
        BEGIN
            SELECT RAISE(ABORT, 'tailored resume revisions are immutable');
        END;

        ALTER TABLE artifacts ADD COLUMN tailored_resume_revision_id TEXT
            REFERENCES tailored_resume_revisions(id) ON DELETE RESTRICT;

        CREATE INDEX artifacts_tailored_resume_revision_idx
            ON artifacts(tailored_resume_revision_id, created_at_ms DESC)
            WHERE tailored_resume_revision_id IS NOT NULL;

        DROP TRIGGER artifacts_validate_tailored_insert;
        DROP TRIGGER artifacts_tailored_identity_no_update;

        CREATE TRIGGER artifacts_validate_tailored_insert
        BEFORE INSERT ON artifacts
        WHEN (
            NEW.kind IN ('tailored_resume_docx', 'tailored_resume_pdf')
            AND (
                NEW.tailored_resume_draft_id IS NULL
                OR NEW.source_resume_checksum_sha256 IS NULL
                OR NEW.opportunity_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM tailored_resume_drafts AS draft
                    WHERE draft.id = NEW.tailored_resume_draft_id
                      AND draft.profile_version_id = NEW.profile_version_id
                      AND draft.opportunity_id = NEW.opportunity_id
                )
                OR (
                    NEW.tailored_resume_revision_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM tailored_resume_revisions AS revision
                        WHERE revision.id = NEW.tailored_resume_revision_id
                          AND revision.draft_id = NEW.tailored_resume_draft_id
                          AND revision.result_resume_checksum_sha256 =
                              NEW.source_resume_checksum_sha256
                    )
                )
            )
        ) OR (
            NEW.kind NOT IN ('tailored_resume_docx', 'tailored_resume_pdf')
            AND (
                NEW.tailored_resume_draft_id IS NOT NULL
                OR NEW.tailored_resume_revision_id IS NOT NULL
                OR NEW.source_resume_checksum_sha256 IS NOT NULL
            )
        ) OR (
            NEW.tailored_resume_revision_id IS NOT NULL
            AND NEW.tailored_resume_draft_id IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'artifact tailored resume lineage is invalid');
        END;

        CREATE TRIGGER artifacts_tailored_identity_no_update
        BEFORE UPDATE ON artifacts
        WHEN NEW.id IS NOT OLD.id
          OR NEW.kind IS NOT OLD.kind
          OR NEW.profile_version_id IS NOT OLD.profile_version_id
          OR NEW.opportunity_id IS NOT OLD.opportunity_id
          OR NEW.payload_json IS NOT OLD.payload_json
          OR NEW.content_fingerprint IS NOT OLD.content_fingerprint
          OR NEW.created_at_ms IS NOT OLD.created_at_ms
          OR NEW.tailored_resume_draft_id IS NOT OLD.tailored_resume_draft_id
          OR NEW.tailored_resume_revision_id IS NOT OLD.tailored_resume_revision_id
          OR NEW.source_resume_checksum_sha256 IS NOT OLD.source_resume_checksum_sha256
          OR NEW.render_fingerprint IS NOT OLD.render_fingerprint
          OR NEW.format IS NOT OLD.format
          OR NEW.checksum_sha256 IS NOT OLD.checksum_sha256
          OR NEW.byte_size IS NOT OLD.byte_size
          OR NEW.template_id IS NOT OLD.template_id
          OR NEW.suggested_filename IS NOT OLD.suggested_filename
          OR NEW.relative_path IS NOT OLD.relative_path
        BEGIN
            SELECT RAISE(ABORT, 'artifact identity and lineage are immutable');
        END;
    """,
    10: """
        CREATE TABLE profile_basic_update_receipts (
            request_id TEXT PRIMARY KEY
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            parent_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            changed_fields_json TEXT NOT NULL
                CHECK(
                    json_valid(changed_fields_json)
                    AND json_type(changed_fields_json) = 'array'
                    AND json_array_length(changed_fields_json) BETWEEN 1 AND 9
                ),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE INDEX profile_basic_update_receipts_parent_idx
            ON profile_basic_update_receipts(parent_profile_version_id, created_at_ms DESC);

        CREATE TRIGGER profile_basic_update_receipts_validate_insert
        BEFORE INSERT ON profile_basic_update_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM profile_versions AS output
            JOIN profile_versions AS parent
              ON parent.id = NEW.parent_profile_version_id
            WHERE output.id = NEW.output_profile_version_id
              AND output.parent_version_id = NEW.parent_profile_version_id
              AND output.version_number = parent.version_number + 1
              AND output.source = 'local_edit'
              AND output.created_at_ms = NEW.created_at_ms
              AND output.created_at_ms >= parent.created_at_ms
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS newer
                  WHERE newer.version_number > output.version_number
              )
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) AS field
            WHERE field.type != 'text'
               OR field.value NOT IN (
                   'name', 'headline', 'summary', 'email', 'phone', 'url',
                   'location.city', 'location.region', 'location.country_code'
               )
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_fields_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_fields_json)
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile basic update receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_basic_update_receipts_no_update
        BEFORE UPDATE ON profile_basic_update_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile basic update receipts are immutable');
        END;

        CREATE TRIGGER profile_basic_update_receipts_no_delete
        BEFORE DELETE ON profile_basic_update_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile basic update receipts are immutable');
        END;
    """,
    11: """
        CREATE TABLE profile_work_update_receipts (
            request_id TEXT PRIMARY KEY
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            parent_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            operation TEXT NOT NULL
                CHECK(operation IN ('add', 'update', 'remove')),
            entry_index INTEGER NOT NULL
                CHECK(entry_index BETWEEN 0 AND 4999),
            changed_fields_json TEXT NOT NULL
                CHECK(
                    json_valid(changed_fields_json)
                    AND json_type(changed_fields_json) = 'array'
                    AND json_array_length(changed_fields_json) BETWEEN 1 AND 8
                ),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE INDEX profile_work_update_receipts_parent_idx
            ON profile_work_update_receipts(parent_profile_version_id, created_at_ms DESC);

        CREATE TRIGGER profile_work_update_receipts_validate_insert
        BEFORE INSERT ON profile_work_update_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM profile_versions AS output
            JOIN profile_versions AS parent
              ON parent.id = NEW.parent_profile_version_id
            WHERE output.id = NEW.output_profile_version_id
              AND output.parent_version_id = NEW.parent_profile_version_id
              AND output.version_number = parent.version_number + 1
              AND output.source = 'local_edit'
              AND output.created_at_ms = NEW.created_at_ms
              AND output.created_at_ms >= parent.created_at_ms
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS newer
                  WHERE newer.version_number > output.version_number
              )
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) AS field
            WHERE field.type != 'text'
               OR field.value NOT IN (
                   'name', 'position', 'url', 'start_date', 'end_date',
                   'summary', 'highlights', 'location'
               )
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_fields_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_fields_json)
        ) OR (
            NEW.operation = 'add'
            AND (
                NOT EXISTS (
                    SELECT 1 FROM json_each(NEW.changed_fields_json)
                    WHERE value = 'name'
                )
                OR NOT EXISTS (
                    SELECT 1 FROM json_each(NEW.changed_fields_json)
                    WHERE value = 'position'
                )
            )
        ) OR EXISTS (
            SELECT 1
            FROM json_each(NEW.changed_fields_json) AS earlier
            JOIN json_each(NEW.changed_fields_json) AS later
              ON CAST(earlier.key AS INTEGER) < CAST(later.key AS INTEGER)
            WHERE CASE earlier.value
                    WHEN 'name' THEN 1
                    WHEN 'position' THEN 2
                    WHEN 'url' THEN 3
                    WHEN 'start_date' THEN 4
                    WHEN 'end_date' THEN 5
                    WHEN 'summary' THEN 6
                    WHEN 'highlights' THEN 7
                    WHEN 'location' THEN 8
                    ELSE 99
                  END >=
                  CASE later.value
                    WHEN 'name' THEN 1
                    WHEN 'position' THEN 2
                    WHEN 'url' THEN 3
                    WHEN 'start_date' THEN 4
                    WHEN 'end_date' THEN 5
                    WHEN 'summary' THEN 6
                    WHEN 'highlights' THEN 7
                    WHEN 'location' THEN 8
                    ELSE 99
                  END
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile work update receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_work_update_receipts_no_update
        BEFORE UPDATE ON profile_work_update_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile work update receipts are immutable');
        END;

        CREATE TRIGGER profile_work_update_receipts_no_delete
        BEFORE DELETE ON profile_work_update_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile work update receipts are immutable');
        END;
    """,
    12: """
        CREATE TABLE profile_restore_receipts (
            request_id TEXT PRIMARY KEY
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            parent_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            source_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE INDEX profile_restore_receipts_parent_idx
            ON profile_restore_receipts(parent_profile_version_id, created_at_ms DESC);
        CREATE INDEX profile_restore_receipts_source_idx
            ON profile_restore_receipts(source_profile_version_id, created_at_ms DESC);

        CREATE TRIGGER profile_restore_receipts_validate_insert
        BEFORE INSERT ON profile_restore_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM profile_versions AS output
            JOIN profile_versions AS parent
              ON parent.id = NEW.parent_profile_version_id
            JOIN profile_versions AS restored
              ON restored.id = NEW.source_profile_version_id
            WHERE output.id = NEW.output_profile_version_id
              AND output.parent_version_id = NEW.parent_profile_version_id
              AND output.version_number = parent.version_number + 1
              AND restored.version_number < parent.version_number
              AND restored.id != parent.id
              AND output.source = 'local_restore'
              AND output.created_at_ms = NEW.created_at_ms
              AND output.created_at_ms >= parent.created_at_ms
              AND output.canonical_json = restored.canonical_json
              AND output.checksum_sha256 = restored.checksum_sha256
              AND restored.checksum_sha256 != parent.checksum_sha256
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS newer
                  WHERE newer.version_number > output.version_number
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile restore receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_restore_receipts_no_update
        BEFORE UPDATE ON profile_restore_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile restore receipts are immutable');
        END;

        CREATE TRIGGER profile_restore_receipts_no_delete
        BEFORE DELETE ON profile_restore_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile restore receipts are immutable');
        END;
    """,
    13: """
        CREATE TABLE profile_section_update_receipts (
            request_id TEXT PRIMARY KEY
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            section TEXT NOT NULL
                CHECK(section IN ('projects', 'education', 'skills')),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            parent_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            operation TEXT NOT NULL
                CHECK(operation IN ('add', 'update', 'remove')),
            entry_index INTEGER NOT NULL
                CHECK(entry_index BETWEEN 0 AND 4999),
            changed_fields_json TEXT NOT NULL
                CHECK(
                    json_valid(changed_fields_json)
                    AND json_type(changed_fields_json) = 'array'
                    AND json_array_length(changed_fields_json) BETWEEN 1 AND 8
                ),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE INDEX profile_section_update_receipts_parent_idx
            ON profile_section_update_receipts(parent_profile_version_id, created_at_ms DESC);
        CREATE INDEX profile_section_update_receipts_section_idx
            ON profile_section_update_receipts(section, created_at_ms DESC);

        CREATE TRIGGER profile_section_update_receipts_validate_insert
        BEFORE INSERT ON profile_section_update_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM profile_versions AS output
            JOIN profile_versions AS parent
              ON parent.id = NEW.parent_profile_version_id
            WHERE output.id = NEW.output_profile_version_id
              AND output.parent_version_id = NEW.parent_profile_version_id
              AND output.version_number = parent.version_number + 1
              AND output.source = 'local_edit'
              AND output.created_at_ms = NEW.created_at_ms
              AND output.created_at_ms >= parent.created_at_ms
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS newer
                  WHERE newer.version_number > output.version_number
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_basic_update_receipts AS basic_receipt
                  WHERE basic_receipt.output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_work_update_receipts AS work_receipt
                  WHERE work_receipt.output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_restore_receipts AS restore_receipt
                  WHERE restore_receipt.output_profile_version_id = output.id
              )
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) AS field
            WHERE field.type != 'text'
               OR (
                    NEW.section = 'projects'
                    AND field.value NOT IN (
                        'name', 'description', 'url', 'start_date', 'end_date',
                        'highlights', 'keywords'
                    )
               )
               OR (
                    NEW.section = 'education'
                    AND field.value NOT IN (
                        'institution', 'study_type', 'area', 'url', 'start_date',
                        'end_date', 'score', 'courses'
                    )
               )
               OR (
                    NEW.section = 'skills'
                    AND field.value NOT IN ('name', 'level', 'keywords')
               )
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_fields_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_fields_json)
        ) OR (
            NEW.operation = 'add'
            AND (
                (
                    NEW.section = 'projects'
                    AND NOT EXISTS (
                        SELECT 1 FROM json_each(NEW.changed_fields_json)
                        WHERE value = 'name'
                    )
                )
                OR (
                    NEW.section = 'education'
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM json_each(NEW.changed_fields_json)
                            WHERE value = 'institution'
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM json_each(NEW.changed_fields_json)
                            WHERE value IN ('study_type', 'area')
                        )
                    )
                )
                OR (
                    NEW.section = 'skills'
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM json_each(NEW.changed_fields_json)
                            WHERE value = 'name'
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM json_each(NEW.changed_fields_json)
                            WHERE value = 'keywords'
                        )
                    )
                )
            )
        ) OR EXISTS (
            SELECT 1
            FROM json_each(NEW.changed_fields_json) AS earlier
            JOIN json_each(NEW.changed_fields_json) AS later
              ON CAST(earlier.key AS INTEGER) < CAST(later.key AS INTEGER)
            WHERE CASE NEW.section
                    WHEN 'projects' THEN CASE earlier.value
                        WHEN 'name' THEN 1 WHEN 'description' THEN 2
                        WHEN 'url' THEN 3 WHEN 'start_date' THEN 4
                        WHEN 'end_date' THEN 5 WHEN 'highlights' THEN 6
                        WHEN 'keywords' THEN 7 ELSE 99 END
                    WHEN 'education' THEN CASE earlier.value
                        WHEN 'institution' THEN 1 WHEN 'study_type' THEN 2
                        WHEN 'area' THEN 3 WHEN 'url' THEN 4
                        WHEN 'start_date' THEN 5 WHEN 'end_date' THEN 6
                        WHEN 'score' THEN 7 WHEN 'courses' THEN 8 ELSE 99 END
                    WHEN 'skills' THEN CASE earlier.value
                        WHEN 'name' THEN 1 WHEN 'level' THEN 2
                        WHEN 'keywords' THEN 3 ELSE 99 END
                    ELSE 99
                  END >=
                  CASE NEW.section
                    WHEN 'projects' THEN CASE later.value
                        WHEN 'name' THEN 1 WHEN 'description' THEN 2
                        WHEN 'url' THEN 3 WHEN 'start_date' THEN 4
                        WHEN 'end_date' THEN 5 WHEN 'highlights' THEN 6
                        WHEN 'keywords' THEN 7 ELSE 99 END
                    WHEN 'education' THEN CASE later.value
                        WHEN 'institution' THEN 1 WHEN 'study_type' THEN 2
                        WHEN 'area' THEN 3 WHEN 'url' THEN 4
                        WHEN 'start_date' THEN 5 WHEN 'end_date' THEN 6
                        WHEN 'score' THEN 7 WHEN 'courses' THEN 8 ELSE 99 END
                    WHEN 'skills' THEN CASE later.value
                        WHEN 'name' THEN 1 WHEN 'level' THEN 2
                        WHEN 'keywords' THEN 3 ELSE 99 END
                    ELSE 99
                  END
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile section update receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_basic_update_receipts_reject_section_output
        BEFORE INSERT ON profile_basic_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_section_update_receipts AS section_receipt
            WHERE section_receipt.output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a section update receipt');
        END;

        CREATE TRIGGER profile_work_update_receipts_reject_section_output
        BEFORE INSERT ON profile_work_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_section_update_receipts AS section_receipt
            WHERE section_receipt.output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a section update receipt');
        END;

        CREATE TRIGGER profile_restore_receipts_reject_section_output
        BEFORE INSERT ON profile_restore_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_section_update_receipts AS section_receipt
            WHERE section_receipt.output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a section update receipt');
        END;

        CREATE TRIGGER profile_section_update_receipts_no_update
        BEFORE UPDATE ON profile_section_update_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile section update receipts are immutable');
        END;

        CREATE TRIGGER profile_section_update_receipts_no_delete
        BEFORE DELETE ON profile_section_update_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile section update receipts are immutable');
        END;
    """,
    14: """
        CREATE TABLE profile_change_receipts (
            request_id TEXT PRIMARY KEY
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            algorithm_version INTEGER NOT NULL CHECK(algorithm_version = 1),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            parent_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            operations_json TEXT NOT NULL
                CHECK(
                    json_valid(operations_json)
                    AND json_type(operations_json) = 'array'
                    AND json_array_length(operations_json) BETWEEN 1 AND 64
                ),
            changed_sections_json TEXT NOT NULL
                CHECK(
                    json_valid(changed_sections_json)
                    AND json_type(changed_sections_json) = 'array'
                    AND json_array_length(changed_sections_json) BETWEEN 1 AND 4
                ),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE INDEX profile_change_receipts_parent_idx
            ON profile_change_receipts(parent_profile_version_id, created_at_ms DESC);

        CREATE TRIGGER profile_change_receipts_validate_insert
        BEFORE INSERT ON profile_change_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM profile_versions AS output
            JOIN profile_versions AS parent
              ON parent.id = NEW.parent_profile_version_id
            WHERE output.id = NEW.output_profile_version_id
              AND output.parent_version_id = NEW.parent_profile_version_id
              AND output.version_number = parent.version_number + 1
              AND output.source = 'local_edit'
              AND output.created_at_ms = NEW.created_at_ms
              AND output.created_at_ms >= parent.created_at_ms
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS newer
                  WHERE newer.version_number > output.version_number
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_basic_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_work_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_section_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_restore_receipts
                  WHERE output_profile_version_id = output.id
              )
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_sections_json) AS section
            WHERE section.type != 'text'
               OR section.value NOT IN ('work', 'projects', 'education', 'skills')
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_sections_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_sections_json)
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile changes receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_basic_update_receipts_reject_changes_output
        BEFORE INSERT ON profile_basic_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_change_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a changes receipt');
        END;

        CREATE TRIGGER profile_work_update_receipts_reject_changes_output
        BEFORE INSERT ON profile_work_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_change_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a changes receipt');
        END;

        CREATE TRIGGER profile_section_update_receipts_reject_changes_output
        BEFORE INSERT ON profile_section_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_change_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a changes receipt');
        END;

        CREATE TRIGGER profile_restore_receipts_reject_changes_output
        BEFORE INSERT ON profile_restore_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_change_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a changes receipt');
        END;

        CREATE TRIGGER profile_change_receipts_no_update
        BEFORE UPDATE ON profile_change_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile changes receipts are immutable');
        END;

        CREATE TRIGGER profile_change_receipts_no_delete
        BEFORE DELETE ON profile_change_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile changes receipts are immutable');
        END;
    """,
    15: """
        CREATE TABLE profile_manual_start_receipts (
            request_id TEXT PRIMARY KEY
                CHECK(
                    length(request_id) = 36
                    AND substr(request_id, 9, 1) = '-'
                    AND substr(request_id, 14, 1) = '-'
                    AND substr(request_id, 19, 1) = '-'
                    AND substr(request_id, 24, 1) = '-'
                    AND request_id = lower(request_id)
                    AND length(replace(request_id, '-', '')) = 32
                    AND replace(request_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                ),
            request_fingerprint TEXT NOT NULL
                CHECK(
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            patch_json TEXT NOT NULL
                CHECK(
                    json_valid(patch_json)
                    AND json_type(patch_json) = 'object'
                    AND length(CAST(patch_json AS BLOB)) <= 65536
                ),
            changed_fields_json TEXT NOT NULL
                CHECK(
                    json_valid(changed_fields_json)
                    AND json_type(changed_fields_json) = 'array'
                    AND json_array_length(changed_fields_json) BETWEEN 1 AND 9
                ),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE TRIGGER profile_manual_start_receipts_validate_insert
        BEFORE INSERT ON profile_manual_start_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM profile_versions AS output
            WHERE output.id = NEW.output_profile_version_id
              AND output.parent_version_id IS NULL
              AND output.version_number = 1
              AND output.source = 'local_start'
              AND output.created_at_ms = NEW.created_at_ms
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS other
                  WHERE other.id != output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_source_imports
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_version_sources
                  WHERE profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_basic_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_work_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_section_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_change_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_restore_receipts
                  WHERE output_profile_version_id = output.id
              )
        ) OR COALESCE(json_type(NEW.patch_json, '$.name'), '') != 'text'
          OR EXISTS (
            SELECT 1 FROM json_each(NEW.patch_json) AS field
            WHERE field.key NOT IN (
                'name', 'headline', 'summary', 'email', 'phone', 'url', 'location'
            ) OR (
                field.key = 'location' AND field.type != 'object'
            )
        ) OR EXISTS (
            SELECT 1 FROM json_each(json_extract(NEW.patch_json, '$.location')) AS field
            WHERE field.key NOT IN ('city', 'region', 'country_code')
               OR field.type NOT IN ('text', 'null')
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) AS field
            WHERE field.type != 'text'
               OR field.value NOT IN (
                   'name', 'headline', 'summary', 'email', 'phone', 'url',
                   'location.city', 'location.region', 'location.country_code'
               )
        ) OR NOT EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json)
            WHERE value = 'name'
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_fields_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_fields_json)
        ) OR EXISTS (
            SELECT 1
            FROM json_each(NEW.changed_fields_json) AS earlier
            JOIN json_each(NEW.changed_fields_json) AS later
              ON CAST(earlier.key AS INTEGER) < CAST(later.key AS INTEGER)
            WHERE CASE earlier.value
                    WHEN 'name' THEN 1 WHEN 'headline' THEN 2
                    WHEN 'summary' THEN 3 WHEN 'email' THEN 4
                    WHEN 'phone' THEN 5 WHEN 'url' THEN 6
                    WHEN 'location.city' THEN 7 WHEN 'location.region' THEN 8
                    WHEN 'location.country_code' THEN 9 ELSE 99 END >=
                  CASE later.value
                    WHEN 'name' THEN 1 WHEN 'headline' THEN 2
                    WHEN 'summary' THEN 3 WHEN 'email' THEN 4
                    WHEN 'phone' THEN 5 WHEN 'url' THEN 6
                    WHEN 'location.city' THEN 7 WHEN 'location.region' THEN 8
                    WHEN 'location.country_code' THEN 9 ELSE 99 END
        )
        BEGIN
            SELECT RAISE(ABORT, 'manual profile receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_source_imports_reject_manual_start_output
        BEFORE INSERT ON profile_source_imports
        WHEN EXISTS (
            SELECT 1 FROM profile_manual_start_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a manual start receipt');
        END;

        CREATE TRIGGER profile_version_sources_reject_manual_start_output
        BEFORE INSERT ON profile_version_sources
        WHEN EXISTS (
            SELECT 1 FROM profile_manual_start_receipts
            WHERE output_profile_version_id = NEW.profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a manual start receipt');
        END;

        CREATE TRIGGER profile_manual_start_receipts_no_update
        BEFORE UPDATE ON profile_manual_start_receipts
        BEGIN
            SELECT RAISE(ABORT, 'manual profile receipts are immutable');
        END;

        CREATE TRIGGER profile_manual_start_receipts_no_delete
        BEFORE DELETE ON profile_manual_start_receipts
        BEGIN
            SELECT RAISE(ABORT, 'manual profile receipts are immutable');
        END;
    """,
    16: """
        ALTER TABLE profile_source_scans
            ADD COLUMN review_candidates_json TEXT NOT NULL DEFAULT '[]'
            CHECK (
                json_valid(review_candidates_json)
                AND json_type(review_candidates_json) = 'array'
                AND json_array_length(review_candidates_json) <= 240
                AND length(review_candidates_json) <= 2097152
            );

        CREATE TABLE profile_review_items (
            id TEXT PRIMARY KEY CHECK (
                length(id) = 36
                AND substr(id, 9, 1) = '-'
                AND substr(id, 14, 1) = '-'
                AND substr(id, 19, 1) = '-'
                AND substr(id, 24, 1) = '-'
                AND id = lower(id)
                AND id NOT GLOB '*[^0-9a-f-]*'
                AND length(replace(id, '-', '')) = 32
            ),
            import_id TEXT NOT NULL REFERENCES profile_source_imports(id) ON DELETE RESTRICT,
            candidate_ordinal INTEGER NOT NULL CHECK (candidate_ordinal BETWEEN 0 AND 239),
            primary_source_ordinal INTEGER NOT NULL CHECK (primary_source_ordinal BETWEEN 0 AND 63),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            retention_generation INTEGER NOT NULL CHECK (retention_generation > 0),
            candidate_kind TEXT NOT NULL
                CHECK (candidate_kind IN ('education', 'projects', 'skills')),
            operation_kind TEXT NOT NULL CHECK (operation_kind IN ('add', 'update')),
            proposed_json TEXT NOT NULL CHECK (
                json_valid(proposed_json)
                AND json_type(proposed_json) = 'object'
                AND length(proposed_json) <= 32768
            ),
            previous_json TEXT CHECK (
                previous_json IS NULL OR (
                    json_valid(previous_json)
                    AND json_type(previous_json) = 'object'
                    AND length(previous_json) <= 32768
                )
            ),
            candidate_checksum_sha256 TEXT NOT NULL
                CHECK (length(candidate_checksum_sha256) = 64),
            previous_checksum_sha256 TEXT
                CHECK (previous_checksum_sha256 IS NULL OR length(previous_checksum_sha256) = 64),
            evidence_json TEXT NOT NULL CHECK (
                json_valid(evidence_json)
                AND json_type(evidence_json) = 'array'
                AND json_array_length(evidence_json) BETWEEN 1 AND 3
                AND length(evidence_json) <= 8192
            ),
            generator_contract TEXT NOT NULL CHECK (length(generator_contract) BETWEEN 1 AND 80),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
            CHECK (
                (operation_kind = 'add' AND previous_json IS NULL AND previous_checksum_sha256 IS NULL)
                OR
                (operation_kind = 'update' AND previous_json IS NOT NULL AND previous_checksum_sha256 IS NOT NULL)
            ),
            UNIQUE (import_id, candidate_ordinal),
            FOREIGN KEY (import_id, primary_source_ordinal)
                REFERENCES profile_version_sources(import_id, ordinal) ON DELETE RESTRICT
        ) STRICT;

        CREATE TABLE profile_review_item_events (
            request_id TEXT PRIMARY KEY CHECK (
                length(request_id) = 36
                AND substr(request_id, 9, 1) = '-'
                AND substr(request_id, 14, 1) = '-'
                AND substr(request_id, 19, 1) = '-'
                AND substr(request_id, 24, 1) = '-'
                AND request_id = lower(request_id)
                AND request_id NOT GLOB '*[^0-9a-f-]*'
                AND length(replace(request_id, '-', '')) = 32
            ),
            request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
            review_item_id TEXT NOT NULL REFERENCES profile_review_items(id) ON DELETE RESTRICT,
            action TEXT NOT NULL CHECK (action IN ('defer', 'reject', 'reopen')),
            expected_state TEXT NOT NULL CHECK (expected_state IN ('inbox', 'deferred', 'rejected')),
            expected_state_revision INTEGER NOT NULL CHECK (expected_state_revision >= 0),
            result_state TEXT NOT NULL CHECK (result_state IN ('inbox', 'deferred', 'rejected')),
            result_state_revision INTEGER NOT NULL CHECK (
                result_state_revision = expected_state_revision + 1
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
            CHECK (
                (action = 'defer' AND expected_state = 'inbox' AND result_state = 'deferred')
                OR
                (action = 'reject' AND expected_state IN ('inbox', 'deferred') AND result_state = 'rejected')
                OR
                (action = 'reopen' AND expected_state IN ('deferred', 'rejected') AND result_state = 'inbox')
            ),
            UNIQUE (review_item_id, result_state_revision)
        ) STRICT;

        CREATE INDEX profile_review_items_generation_created_idx
            ON profile_review_items(retention_generation, created_at_ms DESC, id DESC);
        CREATE INDEX profile_review_item_events_item_revision_idx
            ON profile_review_item_events(review_item_id, result_state_revision DESC);

        CREATE TRIGGER profile_review_items_validate_insert
        BEFORE INSERT ON profile_review_items
        WHEN NOT EXISTS (
            SELECT 1
            FROM profile_source_imports AS source_import
            JOIN profile_version_sources AS source_link
              ON source_link.import_id = source_import.id
             AND source_link.ordinal = NEW.primary_source_ordinal
            WHERE source_import.id = NEW.import_id
              AND source_import.output_profile_version_id = NEW.profile_version_id
              AND source_import.retention_generation = NEW.retention_generation
              AND source_link.profile_version_id = NEW.profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile review item lineage is invalid');
        END;

        CREATE TRIGGER profile_review_item_events_validate_insert
        BEFORE INSERT ON profile_review_item_events
        WHEN
            (
                NOT EXISTS (
                    SELECT 1 FROM profile_review_item_events
                    WHERE review_item_id = NEW.review_item_id
                )
                AND (NEW.expected_state != 'inbox' OR NEW.expected_state_revision != 0)
            )
            OR
            (
                EXISTS (
                    SELECT 1 FROM profile_review_item_events
                    WHERE review_item_id = NEW.review_item_id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM profile_review_item_events AS prior
                    WHERE prior.review_item_id = NEW.review_item_id
                      AND prior.result_state_revision = NEW.expected_state_revision
                      AND prior.result_state = NEW.expected_state
                      AND prior.result_state_revision = (
                          SELECT MAX(latest.result_state_revision)
                          FROM profile_review_item_events AS latest
                          WHERE latest.review_item_id = NEW.review_item_id
                      )
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'profile review item state pin is invalid');
        END;

        CREATE TRIGGER profile_review_items_no_update
        BEFORE UPDATE ON profile_review_items
        BEGIN
            SELECT RAISE(ABORT, 'profile review items are immutable');
        END;

        CREATE TRIGGER profile_review_items_no_delete
        BEFORE DELETE ON profile_review_items
        BEGIN
            SELECT RAISE(ABORT, 'profile review items are immutable');
        END;

        CREATE TRIGGER profile_review_item_events_no_update
        BEFORE UPDATE ON profile_review_item_events
        BEGIN
            SELECT RAISE(ABORT, 'profile review item events are immutable');
        END;

        CREATE TRIGGER profile_review_item_events_no_delete
        BEFORE DELETE ON profile_review_item_events
        BEGIN
            SELECT RAISE(ABORT, 'profile review item events are immutable');
        END;
    """,
    17: """
        CREATE TABLE profile_review_apply_receipts (
            request_id TEXT PRIMARY KEY CHECK (
                length(request_id) = 36
                AND substr(request_id, 9, 1) = '-'
                AND substr(request_id, 14, 1) = '-'
                AND substr(request_id, 19, 1) = '-'
                AND substr(request_id, 24, 1) = '-'
                AND request_id = lower(request_id)
                AND request_id NOT GLOB '*[^0-9a-f-]*'
                AND length(replace(request_id, '-', '')) = 32
            ),
            request_fingerprint TEXT NOT NULL CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            review_item_id TEXT NOT NULL UNIQUE
                REFERENCES profile_review_items(id) ON DELETE RESTRICT,
            expected_state TEXT NOT NULL CHECK (expected_state = 'inbox'),
            expected_state_revision INTEGER NOT NULL
                CHECK(expected_state_revision >= 0),
            result_state_revision INTEGER NOT NULL CHECK (
                result_state_revision = expected_state_revision + 1
            ),
            retention_generation INTEGER NOT NULL CHECK(retention_generation > 0),
            candidate_kind TEXT NOT NULL
                CHECK(candidate_kind IN ('education', 'projects', 'skills')),
            operation_kind TEXT NOT NULL CHECK(operation_kind IN ('add', 'update')),
            candidate_json TEXT NOT NULL CHECK (
                json_valid(candidate_json)
                AND json_type(candidate_json) = 'object'
                AND length(CAST(candidate_json AS BLOB)) <= 32768
            ),
            output_profile_version_id TEXT NOT NULL UNIQUE
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            parent_profile_version_id TEXT NOT NULL
                REFERENCES profile_versions(id) ON DELETE RESTRICT,
            entry_index INTEGER NOT NULL CHECK(entry_index BETWEEN 0 AND 4999),
            changed_fields_json TEXT NOT NULL CHECK (
                json_valid(changed_fields_json)
                AND json_type(changed_fields_json) = 'array'
                AND json_array_length(changed_fields_json) BETWEEN 1 AND 8
            ),
            created_at_ms INTEGER NOT NULL
                CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;

        CREATE INDEX profile_review_apply_receipts_parent_idx
            ON profile_review_apply_receipts(parent_profile_version_id, created_at_ms DESC);

        CREATE TRIGGER profile_review_apply_receipts_validate_insert
        BEFORE INSERT ON profile_review_apply_receipts
        WHEN NOT EXISTS (
            SELECT 1
            FROM profile_review_items AS item
            JOIN profile_source_retention_state AS retention
              ON retention.singleton_id = 1
            JOIN profile_versions AS output
              ON output.id = NEW.output_profile_version_id
            JOIN profile_versions AS parent
              ON parent.id = NEW.parent_profile_version_id
            WHERE item.id = NEW.review_item_id
              AND item.retention_generation = NEW.retention_generation
              AND item.retention_generation = retention.current_generation
              AND item.candidate_kind = NEW.candidate_kind
              AND item.operation_kind = NEW.operation_kind
              AND output.parent_version_id = parent.id
              AND output.version_number = parent.version_number + 1
              AND output.source = 'local_edit'
              AND output.created_at_ms = NEW.created_at_ms
              AND output.created_at_ms >= parent.created_at_ms
              AND NOT EXISTS (
                  SELECT 1 FROM profile_versions AS newer
                  WHERE newer.version_number > output.version_number
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_basic_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_work_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_section_update_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_change_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_restore_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_manual_start_receipts
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_source_imports
                  WHERE output_profile_version_id = output.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM profile_version_sources
                  WHERE profile_version_id = output.id
              )
              AND (
                  (
                      NEW.expected_state_revision = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM profile_review_item_events
                          WHERE review_item_id = item.id
                      )
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM profile_review_item_events AS prior
                      WHERE prior.review_item_id = item.id
                        AND prior.result_state = NEW.expected_state
                        AND prior.result_state_revision = NEW.expected_state_revision
                        AND prior.result_state_revision = (
                            SELECT MAX(latest.result_state_revision)
                            FROM profile_review_item_events AS latest
                            WHERE latest.review_item_id = item.id
                        )
                  )
              )
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.candidate_json) AS field
            WHERE (
                NEW.candidate_kind = 'projects'
                AND field.key NOT IN (
                    'name', 'description', 'url', 'start_date', 'end_date',
                    'highlights', 'keywords'
                )
            ) OR (
                NEW.candidate_kind = 'education'
                AND field.key NOT IN (
                    'institution', 'study_type', 'area', 'url', 'start_date',
                    'end_date', 'score', 'courses'
                )
            ) OR (
                NEW.candidate_kind = 'skills'
                AND field.key NOT IN ('name', 'level', 'keywords')
            )
        ) OR (
            NEW.candidate_kind = 'projects'
            AND (SELECT count(*) FROM json_each(NEW.candidate_json)) != 7
        ) OR (
            NEW.candidate_kind = 'education'
            AND (SELECT count(*) FROM json_each(NEW.candidate_json)) != 8
        ) OR (
            NEW.candidate_kind = 'skills'
            AND (SELECT count(*) FROM json_each(NEW.candidate_json)) != 3
        ) OR EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) AS field
            WHERE field.type != 'text'
               OR (
                    NEW.candidate_kind = 'projects'
                    AND field.value NOT IN (
                        'name', 'description', 'url', 'start_date', 'end_date',
                        'highlights', 'keywords'
                    )
               )
               OR (
                    NEW.candidate_kind = 'education'
                    AND field.value NOT IN (
                        'institution', 'study_type', 'area', 'url', 'start_date',
                        'end_date', 'score', 'courses'
                    )
               )
               OR (
                    NEW.candidate_kind = 'skills'
                    AND field.value NOT IN ('name', 'level', 'keywords')
               )
        ) OR (
            SELECT count(*) FROM json_each(NEW.changed_fields_json)
        ) != (
            SELECT count(DISTINCT value) FROM json_each(NEW.changed_fields_json)
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile review apply receipt lineage is invalid');
        END;

        CREATE TRIGGER profile_review_apply_receipts_no_update
        BEFORE UPDATE ON profile_review_apply_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile review apply receipts are immutable');
        END;

        CREATE TRIGGER profile_review_apply_receipts_no_delete
        BEFORE DELETE ON profile_review_apply_receipts
        BEGIN
            SELECT RAISE(ABORT, 'profile review apply receipts are immutable');
        END;

        CREATE TRIGGER profile_review_item_events_reject_applied_item
        BEFORE INSERT ON profile_review_item_events
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE review_item_id = NEW.review_item_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'applied profile review items are terminal');
        END;

        CREATE TRIGGER profile_basic_update_receipts_reject_review_apply_output
        BEFORE INSERT ON profile_basic_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_work_update_receipts_reject_review_apply_output
        BEFORE INSERT ON profile_work_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_section_update_receipts_reject_review_apply_output
        BEFORE INSERT ON profile_section_update_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_change_receipts_reject_review_apply_output
        BEFORE INSERT ON profile_change_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_restore_receipts_reject_review_apply_output
        BEFORE INSERT ON profile_restore_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_manual_start_receipts_reject_review_apply_output
        BEFORE INSERT ON profile_manual_start_receipts
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_source_imports_reject_review_apply_output
        BEFORE INSERT ON profile_source_imports
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.output_profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;

        CREATE TRIGGER profile_version_sources_reject_review_apply_output
        BEFORE INSERT ON profile_version_sources
        WHEN EXISTS (
            SELECT 1 FROM profile_review_apply_receipts
            WHERE output_profile_version_id = NEW.profile_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'profile output already has a review apply receipt');
        END;
    """,
}

MIGRATIONS[18] = SOURCE_REVIEW_MIGRATION
MIGRATIONS[19] = memory_migration(MIGRATIONS[16])

MIGRATION_NAMES = {
    1: "initial_local_vault",
    2: "immutable_profiles_and_durable_artifacts",
    3: "profile_source_preview_and_lineage",
    4: "immutable_opportunity_snapshots_and_matches",
    5: "retained_profile_source_generations",
    6: "local_opportunity_tracker",
    7: "immutable_local_tailoring_drafts",
    8: "tailored_resume_artifact_lineage",
    9: "immutable_user_tailored_resume_revisions",
    10: "immutable_profile_basic_update_receipts",
    11: "immutable_profile_work_update_receipts",
    12: "immutable_profile_restore_receipts",
    13: "immutable_profile_section_update_receipts",
    14: "immutable_profile_change_receipts",
    15: "immutable_manual_profile_start_receipts",
    16: "immutable_profile_review_inbox",
    17: "immutable_profile_review_apply_receipts",
    18: "immutable_source_review_decisions",
    19: "immutable_memories_and_review_origins",
}

MIGRATION_APP_VERSIONS = {
    1: "0.2.0",
    2: "0.2.0",
    3: "0.2.0",
    4: "0.2.2",
    5: "0.2.3",
    6: "0.2.4",
    7: "0.3.0",
    8: "0.3.1",
    9: "0.4.3",
    10: "0.4.5",
    11: "0.4.6",
    12: "0.4.7",
    13: "0.4.8",
    14: "0.4.10",
    15: "0.4.11",
    16: "0.4.12",
    17: "0.4.13",
    18: "0.4.15",
    19: "0.4.16",
}


def _utc_now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _database_path(data_dir: Path) -> Path:
    return data_dir.expanduser().resolve() / DATABASE_FILENAME


def _harden_local_permissions(data_dir: Path) -> None:
    if os.name == "nt":
        return
    data_dir.chmod(0o700)
    database_path = _database_path(data_dir)
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            pass


def _prepare_managed_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _write_managed_file(data_dir: Path, relative_path: Path, content: bytes) -> Path:
    root = data_dir.expanduser().resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Managed artifact path must stay inside the local vault")
    _prepare_managed_directory(target.parent)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return target
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_uuid(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID string") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{label} must use canonical lowercase UUID form")
    return canonical


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _json_text(
    value: Any,
    *,
    label: str,
    expected_type: type[dict[Any, Any]] | type[list[Any]],
    max_bytes: int,
) -> str:
    if not isinstance(value, expected_type):
        expected = "object" if expected_type is dict else "array"
        raise ValueError(f"{label} must be a JSON {expected}")
    pending: list[Any] = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        identity = id(item)
        if identity in seen:
            raise ValueError(f"{label} must contain finite JSON values")
        seen.add(identity)
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} object keys must be strings")
                pending.append(child)
        else:
            pending.extend(item)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the local size limit")
    return encoded.decode("utf-8")


def _safe_leaf_name(value: Any, *, ordinal: int) -> str:
    candidate = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = "".join(character for character in candidate if character.isprintable())
    return candidate.strip()[:240] or f"Source {ordinal + 1}"


def _safe_optional_text(value: Any, *, label: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    cleaned = "".join(character for character in value if character.isprintable()).strip()
    if len(cleaned) > max_chars:
        raise ValueError(f"{label} is too long")
    return cleaned or None


def _scan_staging_relative_path(scan_id: str, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Staged source path is invalid")
    relative = Path(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Staged source path is invalid")
    if len(relative.parts) < 4 or relative.parts[:3] != (
        "imports",
        "staging",
        scan_id,
    ):
        raise ValueError("Staged source path is outside this source scan")
    if len(value) > 512:
        raise ValueError("Staged source path is too long")
    return relative


def _managed_scan_staging_path(data_dir: Path, scan_id: str, relative_path: str) -> Path:
    relative = _scan_staging_relative_path(scan_id, relative_path)
    root = data_dir.expanduser().resolve()
    unresolved = root / relative
    current = root
    for component in relative.parts:
        current = current / component
        try:
            if current.is_symlink():
                raise ValueError("Staged source paths may not contain symbolic links")
        except OSError as exc:
            raise ValueError("Staged source path could not be inspected") from exc
    resolved = unresolved.resolve()
    expected_root = (root / "imports" / "staging" / scan_id).resolve()
    if not expected_root.is_relative_to(root) or not resolved.is_relative_to(expected_root):
        raise ValueError("Staged source path escaped its source scan")
    return unresolved


def _read_verified_source_file(
    data_dir: Path,
    *,
    scan_id: str,
    relative_path: str,
    expected_size: int,
    expected_checksum: str,
) -> bytes:
    if not 0 < expected_size <= MAX_PROFILE_SOURCE_FILE_BYTES:
        raise ValueError("Staged source has an invalid size")
    path = _managed_scan_staging_path(data_dir, scan_id, relative_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("Staged source could not be opened") from exc
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_size != expected_size:
            raise ValueError("Staged source size changed")
        content = bytearray()
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while chunk := stream.read(64 * 1024):
                if len(content) + len(chunk) > MAX_PROFILE_SOURCE_FILE_BYTES:
                    raise ValueError("Staged source exceeds the local size limit")
                content.extend(chunk)
                digest.update(chunk)
        if len(content) != expected_size or digest.hexdigest() != expected_checksum:
            raise ValueError("Staged source failed its integrity check")
        return bytes(content)
    finally:
        os.close(descriptor)


def _source_blob_relative_path(checksum_sha256: str) -> Path:
    return Path("sources") / "blobs" / checksum_sha256[:2] / checksum_sha256


def _managed_source_blob_path(data_dir: Path, relative_path: str) -> Path | None:
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    if len(relative.parts) != 4 or relative.parts[:2] != ("sources", "blobs"):
        return None
    checksum = relative.parts[3]
    if relative.parts[2] != checksum[:2] or _SHA256_RE.fullmatch(checksum) is None:
        return None
    root = data_dir.expanduser().resolve()
    target = (root / relative).resolve()
    return target if target.is_relative_to(root) else None


def _ensure_source_blob(
    data_dir: Path,
    *,
    checksum_sha256: str,
    content: bytes,
) -> tuple[Path, str, bool]:
    relative = _source_blob_relative_path(checksum_sha256)
    target = _managed_source_blob_path(data_dir, relative.as_posix())
    if target is None:
        raise VaultError("source_path_invalid", "The managed source path is invalid.")
    if target.exists():
        if target.is_symlink() or not _managed_file_matches(
            target,
            len(content),
            checksum_sha256,
        ):
            raise VaultError(
                "source_integrity_error",
                "A managed source snapshot failed its integrity check.",
            )
        return target, relative.as_posix(), False

    _prepare_managed_directory(target.parent)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            created = False
        if not _managed_file_matches(target, len(content), checksum_sha256):
            if created:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            raise VaultError(
                "source_integrity_error",
                "A managed source snapshot failed its integrity check.",
            )
        if os.name != "nt":
            target.chmod(0o600)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return target, relative.as_posix(), created
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _scan_staging_directory(data_dir: Path, scan_id: str) -> Path:
    canonical_scan_id = _canonical_uuid(scan_id, label="scan_id")
    data_root = data_dir.expanduser().resolve()
    root = data_root / "imports" / "staging"
    target = root / canonical_scan_id
    if target.parent != root:
        raise ValueError("Source scan staging path is invalid")
    current = data_root
    for component in ("imports", "staging"):
        current /= component
        try:
            file_status = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(file_status.st_mode) or not stat.S_ISDIR(file_status.st_mode):
            raise ValueError("Source scan staging contains an unsafe managed ancestor")
        if not current.resolve().is_relative_to(data_root):
            raise ValueError("Source scan staging escaped the local vault")
    return target


def _remove_scan_staging_directory(data_dir: Path, scan_id: str) -> bool:
    target = _scan_staging_directory(data_dir, scan_id)
    try:
        file_status = target.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(file_status.st_mode):
        target.unlink()
    elif stat.S_ISDIR(file_status.st_mode):
        shutil.rmtree(target)
    else:
        target.unlink()
    if os.name != "nt" and target.parent.is_dir():
        try:
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    return True


def _enable_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if mode.lower() != "wal":
                raise VaultError(
                    "vault_storage_error",
                    "The local vault could not enable durable WAL storage.",
                )
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise VaultError(
                    "vault_busy",
                    "The local vault is busy. Close other CVGnome instances and try again.",
                ) from exc
            time.sleep(0.05)


def _connect_without_workspace_reset_recovery(data_dir: Path) -> sqlite3.Connection:
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _harden_local_permissions(data_dir)
    connection = sqlite3.connect(_database_path(data_dir), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    _enable_wal(connection)
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA wal_autocheckpoint = 1000")
    _harden_local_permissions(data_dir)
    current_application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if current_application_id not in (0, APPLICATION_ID):
        connection.close()
        raise VaultError(
            "vault_identity_mismatch",
            "The selected local database is not a CVGnome vault.",
        )
    if current_application_id == 0:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    return connection


def _connect(data_dir: Path) -> sqlite3.Connection:
    # A workspace reset deliberately erases the SQLite receipt tables along with
    # the rest of the vault. Its filesystem journal therefore has to be recovered
    # before *any* ordinary connection can observe or recreate the database.
    from .workspace_reset import recover_workspace_reset

    recover_workspace_reset(data_dir)
    return _connect_without_workspace_reset_recovery(data_dir)


def _execute_migration(connection: sqlite3.Connection, migration: str) -> None:
    statement = ""
    for line in migration.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise VaultError(
            "engine_schema_error",
            "The local engine contains an incomplete database migration.",
        )


def _verify_vault(connection: sqlite3.Connection) -> None:
    applied_migrations = connection.execute(
        """
        SELECT version, name, checksum_sha256
        FROM schema_migrations
        ORDER BY version
        """
    ).fetchall()
    if len(applied_migrations) != SCHEMA_VERSION:
        raise VaultError(
            "vault_migration_history_invalid",
            "The local vault migration history is incomplete.",
        )
    for row in applied_migrations:
        version = int(row["version"])
        migration = MIGRATIONS.get(version)
        expected_name = MIGRATION_NAMES.get(version)
        if migration is None or expected_name is None:
            raise VaultError(
                "vault_migration_history_invalid",
                "The local vault contains a migration this engine does not recognize.",
            )
        expected_checksum = hashlib.sha256(migration.encode("utf-8")).hexdigest()
        if str(row["name"]) != expected_name or str(row["checksum_sha256"]) != expected_checksum:
            raise VaultError(
                "vault_migration_history_invalid",
                "The local vault migration history does not match this engine.",
            )

    foreign_key_failure = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_failure is not None:
        raise VaultError(
            "vault_integrity_error",
            "The local vault contains an invalid relationship.",
        )
    integrity_result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if integrity_result != "ok":
        raise VaultError(
            "vault_integrity_error",
            "The local vault failed its SQLite integrity check.",
        )


def _apply_migrations(connection: sqlite3.Connection) -> int:
    # SQLite's documented table-rebuild procedure requires this outside the
    # transaction. The exclusive writer transaction and pre-commit FK audit
    # preserve atomicity; ordinary application writes always enforce FKs.
    rebuild = int(connection.execute("PRAGMA user_version").fetchone()[0]) < 19 <= SCHEMA_VERSION
    if rebuild:
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise VaultError(
                "vault_schema_newer",
                "The local vault was created by a newer version of CVGnome.",
            )

        for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(target_version)
            if migration is None:
                raise VaultError(
                    "engine_schema_error",
                    "The local engine is missing a required database migration.",
                )
            _execute_migration(connection, migration)
            checksum = hashlib.sha256(migration.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO schema_migrations(
                    version, name, checksum_sha256, applied_at_ms, app_version
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    target_version,
                    MIGRATION_NAMES[target_version],
                    checksum,
                    _utc_now_ms(),
                    MIGRATION_APP_VERSIONS[target_version],
                ),
            )
            connection.execute(f"PRAGMA user_version = {target_version}")

        _verify_vault(connection)
        if rebuild and connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise VaultError("vault_integrity_error", "A local migration failed its evidence-lineage check.")
        connection.commit()
        return SCHEMA_VERSION
    except Exception:
        connection.rollback()
        raise
    finally:
        if rebuild:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")


def _managed_artifact_path(data_dir: Path, relative_path: str) -> Path | None:
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    if relative.parts[:2] != ("artifacts", "resumes"):
        return None
    root = data_dir.expanduser().resolve()
    target = (root / relative).resolve()
    return target if target.is_relative_to(root) else None


def _managed_file_matches(path: Path, expected_size: int, expected_checksum: str) -> bool:
    if not 0 < expected_size <= MAX_MANAGED_ARTIFACT_BYTES:
        return False
    try:
        with path.open("rb") as stream:
            file_status = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_status.st_mode) or file_status.st_size != expected_size:
                return False
            digest = hashlib.sha256()
            total = 0
            while chunk := stream.read(64 * 1024):
                total += len(chunk)
                if total > MAX_MANAGED_ARTIFACT_BYTES:
                    return False
                digest.update(chunk)
    except OSError:
        return False
    return total == expected_size and digest.hexdigest() == expected_checksum


def _current_profile_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, version_number, canonical_json, checksum_sha256
        FROM profile_versions
        ORDER BY version_number DESC
        LIMIT 1
        """
    ).fetchone()


def _profile_summary(profile: dict[str, Any]) -> tuple[str, bool]:
    name = "Unnamed profile"
    basics = profile.get("basics")
    if isinstance(basics, dict):
        candidate = basics.get("name")
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()[:160]
    return name, is_baseline_resume_renderable(profile)


def _decode_stored_profile_json(value: Any) -> dict[str, Any]:
    try:
        profile = json.loads(str(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version failed its integrity check.",
        ) from exc
    if not isinstance(profile, dict):
        raise VaultError(
            "vault_integrity_error",
            "A stored profile version failed its integrity check.",
        )
    return profile


def _save_profile_with_connection(
    connection: sqlite3.Connection,
    *,
    canonical_json: str,
    checksum_sha256: str,
    source: str,
) -> ProfileSaveResult:
    current = _current_profile_row(connection)
    if current is not None and str(current["checksum_sha256"]) == checksum_sha256:
        return ProfileSaveResult(
            id=str(current["id"]),
            version_number=int(current["version_number"]),
            checksum_sha256=checksum_sha256,
            created=False,
        )
    next_version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM profile_versions"
        ).fetchone()[0]
    )
    profile_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO profile_versions(
            id, version_number, parent_version_id, canonical_json,
            checksum_sha256, source, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile_id,
            next_version,
            str(current["id"]) if current is not None else None,
            canonical_json,
            checksum_sha256,
            str(source or "local_edit")[:120],
            _utc_now_ms(),
        ),
    )
    return ProfileSaveResult(
        id=profile_id,
        version_number=next_version,
        checksum_sha256=checksum_sha256,
        created=True,
    )


def _normalized_scan_items(
    data_dir: Path,
    *,
    scan_id: str,
    items: Any,
) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ValueError("Source scan items must be a non-empty array")
    if len(items) > MAX_PROFILE_SOURCE_FILES:
        raise ValueError(f"Source scans may contain at most {MAX_PROFILE_SOURCE_FILES} files")

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    extracted_total = 0
    seen_paths: set[str] = set()
    for expected_ordinal, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("Each source scan item must be an object")
        ordinal = item.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal != expected_ordinal:
            raise ValueError("Source scan ordinals must be contiguous and start at zero")
        relative = _scan_staging_relative_path(scan_id, item.get("managed_relative_path"))
        relative_path = relative.as_posix()
        if relative_path in seen_paths:
            raise ValueError("Source scan paths must be unique")
        seen_paths.add(relative_path)

        raw_format = str(item.get("format") or "").strip().lower().lstrip(".")
        source_format = "md" if raw_format == "markdown" else raw_format
        if source_format not in SOURCE_FORMATS:
            raise ValueError("Source format is not allowed")
        byte_size = item.get("byte_size")
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
            raise ValueError("Source byte_size must be a positive integer")
        per_file_limit = (
            MAX_PROFILE_SOURCE_FILE_BYTES
            if source_format in {"pdf", "docx"}
            else 2 * 1024 * 1024
        )
        if byte_size > per_file_limit:
            raise ValueError("A staged source exceeds its format size limit")
        total_bytes += byte_size
        if total_bytes > MAX_PROFILE_SOURCE_TOTAL_BYTES:
            raise ValueError("Source scan exceeds the aggregate byte limit")
        checksum = _sha256(item.get("checksum_sha256"), label="checksum_sha256")
        _read_verified_source_file(
            data_dir,
            scan_id=scan_id,
            relative_path=relative_path,
            expected_size=byte_size,
            expected_checksum=checksum,
        )

        source_kind = str(item.get("source_kind") or "unclassified").strip().lower()
        if source_kind not in SOURCE_KINDS:
            raise ValueError("source_kind is not supported")
        extraction_status = str(item.get("extraction_status") or "parsed").strip().lower()
        if extraction_status not in EXTRACTION_STATUSES:
            raise ValueError("extraction_status is not supported")
        parser_contract = str(item.get("parser_contract") or "").strip()
        if _CONTRACT_RE.fullmatch(parser_contract) is None:
            raise ValueError("parser_contract is invalid")
        issue_code = _safe_optional_text(item.get("issue_code"), label="issue_code", max_chars=80)
        if issue_code is not None and _CONTRACT_RE.fullmatch(issue_code) is None:
            raise ValueError("issue_code is invalid")

        extracted_text = item.get("extracted_text", "")
        if not isinstance(extracted_text, str):
            raise ValueError("extracted_text must be text")
        if len(extracted_text) > MAX_EXTRACTED_SOURCE_CHARS:
            raise ValueError("A source extraction exceeds the local character limit")
        extracted_total += len(extracted_text)
        if extracted_total > MAX_EXTRACTED_SOURCE_TOTAL_CHARS:
            raise ValueError("Source extractions exceed the aggregate character limit")
        if extraction_status == "failed" and extracted_text:
            raise ValueError("Failed source extractions may not contain extracted text")
        computed_extracted_checksum = hashlib.sha256(extracted_text.encode("utf-8")).hexdigest()
        supplied_extracted_checksum = item.get("extracted_text_sha256")
        if supplied_extracted_checksum is not None:
            supplied_extracted_checksum = _sha256(
                supplied_extracted_checksum,
                label="extracted_text_sha256",
            )
            if supplied_extracted_checksum != computed_extracted_checksum:
                raise ValueError("Extracted text checksum does not match its content")
        extracted_checksum = (
            None
            if extraction_status == "failed" and not extracted_text
            else computed_extracted_checksum
        )

        candidate_profile = item.get("candidate_profile")
        candidate_json = None
        if candidate_profile is not None:
            candidate_json = _json_text(
                candidate_profile,
                label="candidate_profile",
                expected_type=dict,
                max_bytes=MAX_DRAFT_PROFILE_BYTES,
            )
        warnings_json = _json_text(
            item.get("warnings", []),
            label="warnings",
            expected_type=list,
            max_bytes=MAX_SCAN_REPORT_BYTES,
        )
        media_type = _safe_optional_text(
            item.get("media_type"),
            label="media_type",
            max_chars=120,
        )
        normalized.append(
            {
                "ordinal": ordinal,
                "staged_relative_path": relative_path,
                "display_name": _safe_leaf_name(item.get("display_name"), ordinal=ordinal),
                "source_format": source_format,
                "media_type": media_type,
                "source_kind": source_kind,
                "byte_size": byte_size,
                "checksum_sha256": checksum,
                "parser_contract": parser_contract,
                "extraction_status": extraction_status,
                "issue_code": issue_code,
                "extracted_text": extracted_text,
                "extracted_text_sha256": extracted_checksum,
                "candidate_profile_json": candidate_json,
                "warnings_json": warnings_json,
            }
        )
    return normalized


def _source_set_checksum(items: list[sqlite3.Row] | list[dict[str, Any]]) -> str:
    checksums = sorted({str(item["checksum_sha256"]) for item in items})
    payload = "profile-source-set-v1\0" + "\0".join(checksums)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _normalized_review_candidates(
    candidates: Any,
    *,
    source_count: int,
) -> list[dict[str, Any]]:
    if not isinstance(candidates, list) or len(candidates) > MAX_REVIEW_CANDIDATES:
        raise ValueError("review_candidates must be a bounded JSON array")
    normalized: list[dict[str, Any]] = []
    allowed_keys = {
        "candidate_kind",
        "operation_kind",
        "proposed",
        "previous",
        "evidence",
        "candidate_checksum_sha256",
        "previous_checksum_sha256",
    }
    for candidate_index, raw in enumerate(candidates):
        if not isinstance(raw, dict) or any(key not in allowed_keys for key in raw):
            raise ValueError(f"review_candidates[{candidate_index}] is invalid")
        candidate_kind = raw.get("candidate_kind")
        operation_kind = raw.get("operation_kind")
        if candidate_kind not in {"education", "projects", "skills"}:
            raise ValueError(
                f"review_candidates[{candidate_index}].candidate_kind is invalid"
            )
        if operation_kind not in {"add", "update"}:
            raise ValueError(
                f"review_candidates[{candidate_index}].operation_kind is invalid"
            )
        proposed = raw.get("proposed")
        previous = raw.get("previous")
        proposed_json = _json_text(
            proposed,
            label=f"review_candidates[{candidate_index}].proposed",
            expected_type=dict,
            max_bytes=32 * 1024,
        )
        if operation_kind == "add":
            if previous is not None:
                raise ValueError(
                    f"review_candidates[{candidate_index}].previous must be null for an add"
                )
            previous_json = None
        else:
            previous_json = _json_text(
                previous,
                label=f"review_candidates[{candidate_index}].previous",
                expected_type=dict,
                max_bytes=32 * 1024,
            )
            if previous_json == proposed_json:
                raise ValueError(
                    f"review_candidates[{candidate_index}] must contain a semantic change"
                )
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_evidence, list) or not 1 <= len(raw_evidence) <= 3:
            raise ValueError(
                f"review_candidates[{candidate_index}].evidence must contain one to three items"
            )
        evidence: list[dict[str, Any]] = []
        seen_evidence: set[tuple[int, str]] = set()
        for evidence_index, raw_item in enumerate(raw_evidence):
            if not isinstance(raw_item, dict) or set(raw_item) != {
                "source_ordinal",
                "locator",
                "excerpt",
            }:
                raise ValueError(
                    f"review_candidates[{candidate_index}].evidence[{evidence_index}] is invalid"
                )
            source_ordinal = raw_item.get("source_ordinal")
            if (
                not isinstance(source_ordinal, int)
                or isinstance(source_ordinal, bool)
                or not 0 <= source_ordinal < source_count
            ):
                raise ValueError(
                    f"review_candidates[{candidate_index}].evidence[{evidence_index}].source_ordinal is invalid"
                )
            locator = raw_item.get("locator")
            if not isinstance(locator, dict):
                raise ValueError(
                    f"review_candidates[{candidate_index}].evidence[{evidence_index}].locator is invalid"
                )
            if locator.get("kind") == "line" and set(locator) == {
                "kind",
                "start",
                "end",
            }:
                start = locator.get("start")
                end = locator.get("end")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not 1 <= start <= end <= 2_000_000
                ):
                    raise ValueError(
                        f"review_candidates[{candidate_index}].evidence[{evidence_index}].locator is invalid"
                    )
                normalized_locator: dict[str, Any] = {
                    "kind": "line",
                    "start": start,
                    "end": end,
                }
            elif locator.get("kind") == "json_pointer" and set(locator) == {
                "kind",
                "value",
            }:
                pointer = locator.get("value")
                if (
                    not isinstance(pointer, str)
                    or not pointer.startswith("/")
                    or len(pointer) > 512
                    or any(not character.isprintable() for character in pointer)
                ):
                    raise ValueError(
                        f"review_candidates[{candidate_index}].evidence[{evidence_index}].locator is invalid"
                    )
                normalized_locator = {"kind": "json_pointer", "value": pointer}
            else:
                raise ValueError(
                    f"review_candidates[{candidate_index}].evidence[{evidence_index}].locator is invalid"
                )
            excerpt = _safe_optional_text(
                raw_item.get("excerpt"),
                label=(
                    f"review_candidates[{candidate_index}].evidence[{evidence_index}].excerpt"
                ),
                max_chars=360,
            )
            if excerpt is None:
                raise ValueError(
                    f"review_candidates[{candidate_index}].evidence[{evidence_index}].excerpt must not be blank"
                )
            evidence_marker = (
                source_ordinal,
                _json_text(
                    normalized_locator,
                    label="review evidence locator",
                    expected_type=dict,
                    max_bytes=1024,
                ),
            )
            if evidence_marker in seen_evidence:
                continue
            seen_evidence.add(evidence_marker)
            evidence.append(
                {
                    "source_ordinal": source_ordinal,
                    "locator": normalized_locator,
                    "excerpt": excerpt,
                }
            )
        if not evidence:
            raise ValueError(f"review_candidates[{candidate_index}].evidence is empty")
        material = {
            "candidate_kind": candidate_kind,
            "operation_kind": operation_kind,
            "proposed": json.loads(proposed_json),
            "previous": json.loads(previous_json) if previous_json is not None else None,
            "evidence": evidence,
        }
        checksum = hashlib.sha256(
            _json_text(
                material,
                label="review candidate material",
                expected_type=dict,
                max_bytes=64 * 1024,
            ).encode("utf-8")
        ).hexdigest()
        supplied_checksum = raw.get("candidate_checksum_sha256")
        if supplied_checksum is not None and supplied_checksum != checksum:
            raise ValueError(
                f"review_candidates[{candidate_index}].candidate_checksum_sha256 changed"
            )
        previous_checksum = (
            hashlib.sha256(previous_json.encode("utf-8")).hexdigest()
            if previous_json is not None
            else None
        )
        supplied_previous_checksum = raw.get("previous_checksum_sha256")
        if (
            supplied_previous_checksum is not None
            and supplied_previous_checksum != previous_checksum
        ):
            raise ValueError(
                f"review_candidates[{candidate_index}].previous_checksum_sha256 changed"
            )
        normalized.append(
            {
                **material,
                "candidate_checksum_sha256": checksum,
                "previous_checksum_sha256": previous_checksum,
            }
        )
    _json_text(
        normalized,
        label="review_candidates",
        expected_type=list,
        max_bytes=MAX_REVIEW_CANDIDATE_BYTES,
    )
    return normalized


def _scan_item_counts(items: list[sqlite3.Row] | list[dict[str, Any]]) -> dict[str, int]:
    statuses = {status: 0 for status in EXTRACTION_STATUSES}
    for item in items:
        statuses[str(item["extraction_status"])] += 1
    return {
        "source_count": len(items),
        "parsed_sources": statuses["parsed"],
        "failed_sources": statuses["failed"],
        "duplicate_sources": statuses["duplicate"],
    }


def _profile_content_counts(profile: dict[str, Any]) -> dict[str, int]:
    def list_count(key: str) -> int:
        value = profile.get(key)
        return len(value) if isinstance(value, list) else 0

    return {
        "work_entries": list_count("work"),
        "education_entries": list_count("education"),
        "skill_groups": list_count("skills"),
    }


def _safe_report_counts(
    report: dict[str, Any],
    items: list[sqlite3.Row] | list[dict[str, Any]],
) -> dict[str, int]:
    warning_value = report.get("warning_count")
    if isinstance(warning_value, int) and not isinstance(warning_value, bool) and warning_value >= 0:
        warning_count = min(warning_value, 1_000_000)
    else:
        report_warnings = report.get("warnings")
        warning_count = len(report_warnings) if isinstance(report_warnings, list) else 0
        for item in items:
            try:
                warnings = json.loads(str(item["warnings_json"]))
            except (json.JSONDecodeError, TypeError):
                warnings = []
            if isinstance(warnings, list):
                warning_count += len(warnings)
            if str(item["extraction_status"]) == "failed":
                warning_count += 1

    conflict_value = report.get("conflict_count")
    if isinstance(conflict_value, int) and not isinstance(conflict_value, bool) and conflict_value >= 0:
        conflict_count = min(conflict_value, 1_000_000)
    else:
        conflicts = report.get("conflicts")
        if isinstance(conflicts, list):
            conflict_count = len(conflicts)
        else:
            fact_actions = report.get("fact_actions")
            preserved = (
                fact_actions.get("conflict_preserved")
                if isinstance(fact_actions, dict)
                else None
            )
            conflict_count = (
                min(preserved, 1_000_000)
                if isinstance(preserved, int)
                and not isinstance(preserved, bool)
                and preserved >= 0
                else 0
            )
    return {
        "warning_count": warning_count,
        "conflict_count": conflict_count,
    }


def _safe_commit_ui(
    report: dict[str, Any],
    items: list[sqlite3.Row] | list[dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Copy only fixed aggregate UI fields into the retry-safe receipt."""

    allowed_codes = {
        "unsupported_file_type",
        "file_too_large",
        "encrypted_document",
        "unreadable_document",
        "duplicate_content",
        "unclassified_source",
        "partial_parse",
        "scan_limit_reached",
        "conflicting_facts",
        "missing_identity",
        "insufficient_resume_content",
        "base_profile_changed",
    }
    allowed_stages = {"scan", "parse", "synthesis"}
    allowed_severities = {"info", "warning", "blocking"}
    raw_ui = report.get("ui")
    raw_ui = raw_ui if isinstance(raw_ui, dict) else {}
    safe_warnings: list[dict[str, Any]] = []
    raw_warnings = raw_ui.get("warnings")
    if isinstance(raw_warnings, list):
        for raw in raw_warnings[:64]:
            if not isinstance(raw, dict):
                continue
            code = raw.get("code")
            stage = raw.get("stage")
            severity = raw.get("severity")
            count = raw.get("count")
            if (
                code in allowed_codes
                and stage in allowed_stages
                and severity in allowed_severities
                and isinstance(count, int)
                and not isinstance(count, bool)
                and 0 < count <= 1_000_000
            ):
                safe_warnings.append(
                    {
                        "code": code,
                        "stage": stage,
                        "severity": severity,
                        "count": count,
                    }
                )

    context: dict[str, Any] = {}
    meta = profile.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("canonical_context"), dict):
        context = meta["canonical_context"]
    evidence = context.get("impact_evidence")
    preferences = context.get("work_preferences")
    profile_counts = {
        **_profile_content_counts(profile),
        "evidence_claims": len(evidence) if isinstance(evidence, list) else 0,
        "preference_items": len(preferences) if isinstance(preferences, list) else 0,
    }
    parsed_sources = sum(
        str(item["extraction_status"]) == "parsed" for item in items
    )
    used_value = raw_ui.get("used_sources")
    used_sources = (
        min(used_value, parsed_sources)
        if isinstance(used_value, int)
        and not isinstance(used_value, bool)
        and used_value >= 0
        else parsed_sources
    )
    return {
        "warnings": safe_warnings,
        "profile_counts": profile_counts,
        "used_sources": used_sources,
    }


def _remove_artifact_temporaries(data_dir: Path, relative_path: str) -> None:
    target = _managed_artifact_path(data_dir, relative_path)
    if target is None or not target.parent.is_dir():
        return
    for temporary in target.parent.glob(f".{target.name}.*.tmp"):
        try:
            if temporary.is_file() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            continue


def _reconcile_artifacts(connection: sqlite3.Connection, data_dir: Path) -> None:
    rows = connection.execute(
        """
        SELECT id, relative_path, checksum_sha256, byte_size
        FROM artifacts
        WHERE status = 'rendering'
        """
    ).fetchall()
    if not rows:
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            target = _managed_artifact_path(data_dir, str(row["relative_path"] or ""))
            valid = target is not None and _managed_file_matches(
                target,
                int(row["byte_size"] or -1),
                str(row["checksum_sha256"] or ""),
            )
            if valid:
                connection.execute(
                    "UPDATE artifacts SET status = 'ready' WHERE id = ?",
                    (str(row["id"]),),
                )
            else:
                connection.execute("DELETE FROM artifacts WHERE id = ?", (str(row["id"]),))
                if target is not None:
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        pass
            _remove_artifact_temporaries(data_dir, str(row["relative_path"] or ""))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _reconcile_profile_source_state(
    connection: sqlite3.Connection,
    data_dir: Path,
) -> None:
    now_ms = _utc_now_ms()
    stale_rows = connection.execute(
        """
        SELECT id
        FROM profile_source_scans
        WHERE (status = 'preview' AND expires_at_ms <= ?)
           OR (status = 'committing' AND updated_at_ms <= ?)
        """,
        (now_ms, now_ms - INTERRUPTED_SCAN_GRACE_MS),
    ).fetchall()
    if stale_rows:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "DELETE FROM profile_source_scans WHERE id = ?",
                [(str(row["id"]),) for row in stale_rows],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for row in stale_rows:
            try:
                _remove_scan_staging_directory(data_dir, str(row["id"]))
            except OSError:
                continue

    active_scan_ids = {
        str(row["id"])
        for row in connection.execute("SELECT id FROM profile_source_scans").fetchall()
    }
    staging_root = data_dir.expanduser().resolve() / "imports" / "staging"
    orphan_cutoff_seconds = (now_ms - ORPHAN_SOURCE_GRACE_MS) / 1000
    if staging_root.is_dir() and not staging_root.is_symlink():
        for candidate in staging_root.iterdir():
            if candidate.name in active_scan_ids:
                continue
            try:
                canonical_id = _canonical_uuid(candidate.name, label="scan_id")
                candidate_status = candidate.lstat()
            except (OSError, ValueError):
                continue
            if candidate_status.st_mtime > orphan_cutoff_seconds:
                continue
            try:
                _remove_scan_staging_directory(data_dir, canonical_id)
            except OSError:
                continue

    known_paths = {
        str(row["relative_path"])
        for row in connection.execute(
            """
            SELECT relative_path
            FROM sources
            WHERE content_addressed = 1 AND relative_path IS NOT NULL
            """
        ).fetchall()
    }
    blob_root = data_dir.expanduser().resolve() / "sources" / "blobs"
    if not blob_root.is_dir() or blob_root.is_symlink():
        return
    orphan_cutoff_seconds = (_utc_now_ms() - ORPHAN_SOURCE_GRACE_MS) / 1000
    for candidate in blob_root.glob("*/*"):
        try:
            relative_path = candidate.relative_to(data_dir.expanduser().resolve()).as_posix()
        except ValueError:
            continue
        try:
            candidate_status = candidate.lstat()
            if candidate_status.st_mtime > orphan_cutoff_seconds:
                continue
            if stat.S_ISLNK(candidate_status.st_mode):
                candidate.unlink()
            elif stat.S_ISREG(candidate_status.st_mode) and relative_path not in known_paths:
                candidate.unlink()
        except OSError:
            continue
    for prefix_directory in blob_root.iterdir():
        try:
            if prefix_directory.is_dir() and not prefix_directory.is_symlink():
                prefix_directory.rmdir()
        except OSError:
            continue


def _retained_source_status(connection: sqlite3.Connection) -> RetainedSourceStatus:
    state = connection.execute(
        """
        SELECT current_generation, reset_at_ms
        FROM profile_source_retention_state
        WHERE singleton_id = 1
        """
    ).fetchone()
    if state is None:
        raise VaultError(
            "vault_integrity_error",
            "The local source-retention state is missing.",
        )
    generation = int(state["current_generation"])
    retained_files = connection.execute(
        """
        SELECT COUNT(*) AS file_count, COALESCE(SUM(byte_size), 0) AS byte_count
        FROM (
            SELECT DISTINCT source.id, source.byte_size
            FROM profile_source_imports AS source_import
            JOIN profile_version_sources AS profile_source
              ON profile_source.import_id = source_import.id
            JOIN sources AS source ON source.id = profile_source.source_id
            WHERE source_import.retention_generation = ?
        )
        """,
        (generation,),
    ).fetchone()
    retained_imports = connection.execute(
        """
        SELECT COUNT(*) AS import_count, MAX(committed_at_ms) AS last_import_at_ms
        FROM profile_source_imports
        WHERE retention_generation = ?
        """,
        (generation,),
    ).fetchone()
    historical = connection.execute(
        """
        SELECT
            COUNT(DISTINCT profile_source.source_id) AS file_count,
            COUNT(DISTINCT source_import.id) AS import_count
        FROM profile_source_imports AS source_import
        LEFT JOIN profile_version_sources AS profile_source
          ON profile_source.import_id = source_import.id
        WHERE source_import.retention_generation < ?
        """,
        (generation,),
    ).fetchone()
    return RetainedSourceStatus(
        source_retention_generation=generation,
        retained_source_files=int(retained_files["file_count"]),
        retained_source_bytes=int(retained_files["byte_count"]),
        retained_source_imports=int(retained_imports["import_count"]),
        historical_source_files=int(historical["file_count"]),
        historical_source_imports=int(historical["import_count"]),
        last_source_import_at_ms=(
            int(retained_imports["last_import_at_ms"])
            if retained_imports["last_import_at_ms"] is not None
            else None
        ),
        source_retention_reset_at_ms=(
            int(state["reset_at_ms"]) if state["reset_at_ms"] is not None else None
        ),
    )


def _workspace_reset_snapshot(connection: sqlite3.Connection) -> dict[str, int]:
    """Return the exact destructive-reset guard fields shown by system.status."""

    state = connection.execute(
        """
        SELECT current_generation
        FROM profile_source_retention_state
        WHERE singleton_id = 1
        """
    ).fetchone()
    if state is None:
        raise VaultError(
            "vault_integrity_error",
            "The local source-retention state is missing.",
        )
    snapshot = {
        "profile_versions": int(
            connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0]
        ),
        "opportunities": int(
            connection.execute(
                """
                SELECT COUNT(*) FROM opportunities AS opportunity
                WHERE EXISTS (
                    SELECT 1
                    FROM opportunity_snapshots AS snapshot
                    JOIN opportunity_matches AS match
                      ON match.snapshot_id = snapshot.id
                    WHERE snapshot.opportunity_id = opportunity.id
                      AND snapshot.snapshot_number = (
                          SELECT MAX(latest.snapshot_number)
                          FROM opportunity_snapshots AS latest
                          WHERE latest.opportunity_id = opportunity.id
                      )
                )
                """
            ).fetchone()[0]
        ),
        "artifacts": int(
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE status = 'ready'"
            ).fetchone()[0]
        ),
        "source_imports": int(
            connection.execute("SELECT COUNT(*) FROM profile_source_imports").fetchone()[
                0
            ]
        ),
        "source_previews": int(
            connection.execute("SELECT COUNT(*) FROM profile_source_scans").fetchone()[0]
        ),
        "source_retention_generation": int(state["current_generation"]),
        "memory_count": int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
    }
    snapshot.update(_profile_review_state_counts(connection))
    from .source_review import source_review_counts
    snapshot.update({f"source_review_{key}_items": value
                     for key, value in source_review_counts(connection).items()})
    return snapshot


def _profile_review_state_counts(connection: sqlite3.Connection) -> dict[str, int]:
    row = connection.execute(
        """
        WITH retention AS (
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
        ),
        item_states AS (
            SELECT
                item.retention_generation,
                CASE
                  WHEN apply_receipt.review_item_id IS NOT NULL THEN 'applied'
                  ELSE COALESCE(
                    (
                        SELECT event.result_state
                        FROM profile_review_item_events AS event
                        WHERE event.review_item_id = item.id
                        ORDER BY event.result_state_revision DESC
                        LIMIT 1
                    ),
                    'inbox'
                  )
                END AS item_state
            FROM profile_review_items AS item
            LEFT JOIN profile_review_apply_receipts AS apply_receipt
              ON apply_receipt.review_item_id = item.id
        )
        SELECT
            COALESCE(SUM(
                item.retention_generation = retention.current_generation
                AND item.item_state = 'inbox'
            ), 0) AS inbox,
            COALESCE(SUM(
                item.retention_generation = retention.current_generation
                AND item.item_state = 'deferred'
            ), 0) AS deferred,
            COALESCE(SUM(
                item.retention_generation != retention.current_generation
                OR item.item_state IN ('rejected', 'applied')
            ), 0) AS history,
            COUNT(retention.current_generation) AS retention_rows
        FROM retention
        LEFT JOIN item_states AS item ON 1 = 1
        """
    ).fetchone()
    if row is None or int(row["retention_rows"]) == 0:
        raise VaultError("vault_integrity_error", "The local review inbox is unavailable.")
    return {
        "review_inbox_items": int(row["inbox"]),
        "review_deferred_items": int(row["deferred"]),
        "review_history_items": int(row["history"]),
    }


def initialize_vault(data_dir: Path) -> VaultStatus:
    connection = _connect(data_dir)
    try:
        schema_version = _apply_migrations(connection)
        _reconcile_artifacts(connection, data_dir)
        _reconcile_profile_source_state(connection, data_dir)
        reset_snapshot = _workspace_reset_snapshot(connection)
        counts = {
            "profile_versions": reset_snapshot["profile_versions"],
            "opportunities": reset_snapshot["opportunities"],
            "artifacts": reset_snapshot["artifacts"],
            "pending_jobs": connection.execute(
                "SELECT COUNT(*) FROM workflow_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0],
            "source_snapshots": connection.execute(
                "SELECT COUNT(*) FROM sources WHERE content_addressed = 1"
            ).fetchone()[0],
            "source_imports": reset_snapshot["source_imports"],
            "source_previews": reset_snapshot["source_previews"],
            "memory_count": reset_snapshot["memory_count"],
        }
        latest_row = connection.execute(
            "SELECT canonical_json FROM profile_versions ORDER BY version_number DESC LIMIT 1"
        ).fetchone()
        latest_profile_name = None
        latest_profile_renderable = None
        if latest_row is not None:
            latest_json = _decode_stored_profile_json(latest_row["canonical_json"])
            basics = latest_json.get("basics")
            if isinstance(basics, dict):
                candidate = basics.get("name")
                if isinstance(candidate, str) and candidate.strip():
                    latest_profile_name = candidate.strip()[:160]
            latest_profile_renderable = is_baseline_resume_renderable(latest_json)
        retained_status = _retained_source_status(connection)
        review_counts = {
            field: reset_snapshot[field]
            for field in (
                "review_inbox_items",
                "review_deferred_items",
                "review_history_items",
                "source_review_inbox_items",
                "source_review_deferred_items",
                "source_review_history_items",
            )
        }
    finally:
        connection.close()

    return VaultStatus(
        schema_version=schema_version,
        database_path=str(_database_path(data_dir)),
        latest_profile_name=latest_profile_name,
        latest_profile_renderable=latest_profile_renderable,
        **retained_status.to_dict(),
        **review_counts,
        **counts,
    )


def reset_profile_source_retention(
    data_dir: Path,
    *,
    expected_generation: int,
    request_id: str,
) -> RetainedSourceStatus:
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or expected_generation <= 0
    ):
        raise ValueError("expected_generation must be a positive integer")
    request_id = _canonical_uuid(request_id, label="request_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        state = connection.execute(
            """
            SELECT current_generation, last_reset_request_id
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if state is None:
            raise VaultError(
                "vault_integrity_error",
                "The local source-retention state is missing.",
            )
        current_generation = int(state["current_generation"])
        if state["last_reset_request_id"] == request_id:
            result = _retained_source_status(connection)
            connection.commit()
            return result
        preview = connection.execute(
            "SELECT 1 FROM profile_source_scans LIMIT 1"
        ).fetchone()
        if preview is not None:
            raise VaultError(
                "profile_source_reset_busy",
                "Finish or discard the current source preview before resetting imports.",
            )
        if current_generation != expected_generation:
            raise VaultError(
                "profile_source_reset_conflict",
                "The retained import set changed. Refresh its status and try again.",
            )
        if current_generation >= 9_223_372_036_854_775_806:
            raise VaultError(
                "profile_source_reset_limit",
                "The retained import generation cannot be advanced.",
            )
        reset_at_ms = _utc_now_ms()
        connection.execute(
            """
            UPDATE profile_source_retention_state
            SET current_generation = ?, last_reset_request_id = ?, reset_at_ms = ?
            WHERE singleton_id = 1
            """,
            (current_generation + 1, request_id, reset_at_ms),
        )
        result = _retained_source_status(connection)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_profile(
    data_dir: Path,
    profile: dict[str, Any],
    *,
    source: str = "local_edit",
) -> ProfileSaveResult:
    try:
        canonical_json = _json_text(
            profile,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
    except ValueError as exc:
        if str(exc) == "Canonical profile must be a JSON object":
            raise
        raise ValueError("Canonical profile must contain only valid JSON values") from exc
    checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        result = _save_profile_with_connection(
            connection,
            canonical_json=canonical_json,
            checksum_sha256=checksum,
            source=source,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _canonical_source_manifest(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict) or set(source) != {
        "scan_id",
        "managed_relative_path",
        "display_name",
        "raw_sha256",
        "raw_bytes",
    }:
        raise ValueError("source must be an exact canonical-file manifest")
    scan_id = _canonical_uuid(source.get("scan_id"), label="scan_id")
    expected_path = f"imports/staging/{scan_id}/0000.json"
    relative_path = _scan_staging_relative_path(
        scan_id,
        source.get("managed_relative_path"),
    ).as_posix()
    if relative_path != expected_path:
        raise ValueError("Canonical profile staging path is invalid")
    display_name = source.get("display_name")
    if not isinstance(display_name, str):
        raise ValueError("display_name must be text")
    raw_bytes = source.get("raw_bytes")
    if (
        not isinstance(raw_bytes, int)
        or isinstance(raw_bytes, bool)
        or not 0 < raw_bytes <= MAX_CANONICAL_PROFILE_FILE_BYTES
    ):
        raise ValueError("raw_bytes must be a positive integer within the 4 MiB limit")
    return {
        "scan_id": scan_id,
        "managed_relative_path": relative_path,
        "display_name": _safe_leaf_name(display_name, ordinal=0),
        "raw_sha256": _sha256(source.get("raw_sha256"), label="raw_sha256"),
        "raw_bytes": raw_bytes,
    }


def import_canonical_profile_source(data_dir: Path, source: Any) -> dict[str, Any]:
    """Import a staged canonical profile while retaining its exact original bytes."""

    from .profile import (
        CanonicalProfileValidationError,
        canonical_profile_metrics,
        prepare_canonical_profile,
    )

    manifest = _canonical_source_manifest(source)
    scan_id = str(manifest["scan_id"])
    data_dir = data_dir.expanduser().resolve()
    connection = _connect(data_dir)
    created_blob_path: Path | None = None
    committed_result: dict[str, Any] | None = None
    cleanup_staging = False
    try:
        _apply_migrations(connection)
        committed_result = _committed_scan_result(connection, scan_id)
        if committed_result is not None:
            cleanup_staging = True
            return committed_result
        if connection.execute(
            "SELECT 1 FROM profile_source_scans WHERE id = ?",
            (scan_id,),
        ).fetchone() is not None:
            raise VaultError(
                "profile_source_scan_conflict",
                "A source preview already uses this import identifier.",
            )
        cleanup_staging = True

        content = _read_verified_source_file(
            data_dir,
            scan_id=scan_id,
            relative_path=str(manifest["managed_relative_path"]),
            expected_size=int(manifest["raw_bytes"]),
            expected_checksum=str(manifest["raw_sha256"]),
        )
        try:
            raw_profile = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultError(
                "canonical_profile.invalid_json",
                "The selected canonical profile is not valid UTF-8 JSON.",
            ) from exc
        try:
            prepared, compaction_report = prepare_canonical_profile(raw_profile)
        except CanonicalProfileValidationError as exc:
            raise VaultError(exc.code, str(exc)) from exc
        canonical_json = _json_text(
            prepared,
            label="Canonical profile",
            expected_type=dict,
            max_bytes=MAX_DRAFT_PROFILE_BYTES,
        )
        profile_checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        blob_path, blob_relative_path, blob_created = _ensure_source_blob(
            data_dir,
            checksum_sha256=str(manifest["raw_sha256"]),
            content=content,
        )
        if blob_created:
            created_blob_path = blob_path

        connection.execute("BEGIN IMMEDIATE")
        committed_result = _committed_scan_result(connection, scan_id)
        if committed_result is not None:
            connection.rollback()
            return committed_result
        if connection.execute(
            "SELECT 1 FROM profile_source_scans WHERE id = ?",
            (scan_id,),
        ).fetchone() is not None:
            raise VaultError(
                "profile_source_scan_conflict",
                "A source preview already uses this import identifier.",
            )
        retention = connection.execute(
            """
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if retention is None:
            raise VaultError(
                "vault_integrity_error",
                "The local source-retention state is missing.",
            )
        current = _current_profile_row(connection)
        base_profile_version_id = str(current["id"]) if current is not None else None
        base_profile_checksum = (
            str(current["checksum_sha256"]) if current is not None else None
        )
        now_ms = _utc_now_ms()
        saved = _save_profile_with_connection(
            connection,
            canonical_json=canonical_json,
            checksum_sha256=profile_checksum,
            source=f"file_import:{manifest['display_name']}",
        )
        source_row = connection.execute(
            """
            SELECT id, relative_path, byte_size
            FROM sources
            WHERE content_addressed = 1 AND checksum_sha256 = ?
            """,
            (manifest["raw_sha256"],),
        ).fetchone()
        if source_row is None:
            source_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO sources(
                    id, display_name, media_type, checksum_sha256,
                    relative_path, extracted_text, imported_at_ms,
                    byte_size, source_format, content_addressed
                ) VALUES (?, ?, 'application/json', ?, ?, NULL, ?, ?, 'json', 1)
                """,
                (
                    source_id,
                    manifest["display_name"],
                    manifest["raw_sha256"],
                    blob_relative_path,
                    now_ms,
                    manifest["raw_bytes"],
                ),
            )
        else:
            if (
                str(source_row["relative_path"]) != blob_relative_path
                or int(source_row["byte_size"] or -1) != int(manifest["raw_bytes"])
            ):
                raise VaultError(
                    "source_integrity_error",
                    "A managed source snapshot does not match its database record.",
                )
            source_id = str(source_row["id"])

        metrics = canonical_profile_metrics(prepared)
        result = {
            "profile_version_id": saved.id,
            "version_number": saved.version_number,
            "created": saved.created,
            "checksum_sha256": saved.checksum_sha256,
            "profile_name": _profile_summary(prepared)[0],
            "work_entries": metrics["work_entries"],
            "education_entries": len(prepared.get("education", []))
            if isinstance(prepared.get("education"), list)
            else 0,
            "skill_groups": metrics["skill_groups"],
            "renderable": is_baseline_resume_renderable(prepared),
            "compaction_report": compaction_report,
        }
        report_json = _json_text(
            {
                "synthesis_contract": "canonical-file-v1",
                "source_count": 1,
                "compaction_report": compaction_report,
            },
            label="canonical import report",
            expected_type=dict,
            max_bytes=MAX_SCAN_REPORT_BYTES,
        )
        result_json = _json_text(
            result,
            label="canonical import result",
            expected_type=dict,
            max_bytes=MAX_SCAN_REPORT_BYTES,
        )
        source_set_checksum = hashlib.sha256(
            f"profile-source-set-v1\0{manifest['raw_sha256']}".encode("ascii")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO profile_source_imports(
                id, base_profile_version_id, base_profile_checksum_sha256,
                output_profile_version_id, source_set_checksum_sha256,
                synthesis_contract, report_json, result_json, committed_at_ms,
                retention_generation
            ) VALUES (?, ?, ?, ?, ?, 'canonical-file-v1', ?, ?, ?, ?)
            """,
            (
                scan_id,
                base_profile_version_id,
                base_profile_checksum,
                saved.id,
                source_set_checksum,
                report_json,
                result_json,
                now_ms,
                int(retention["current_generation"]),
            ),
        )
        connection.execute(
            """
            INSERT INTO profile_version_sources(
                import_id, ordinal, profile_version_id, source_id,
                display_name, extraction_id, source_format, source_kind,
                extraction_status, issue_code
            ) VALUES (?, 0, ?, ?, ?, NULL, 'json', 'unclassified', 'parsed', NULL)
            """,
            (scan_id, saved.id, source_id, manifest["display_name"]),
        )
        connection.commit()
        committed_result = result
    except Exception:
        connection.rollback()
        if created_blob_path is not None:
            try:
                relative_path = created_blob_path.relative_to(data_dir).as_posix()
                referenced = connection.execute(
                    "SELECT 1 FROM sources WHERE relative_path = ? LIMIT 1",
                    (relative_path,),
                ).fetchone()
                if (
                    referenced is None
                    and created_blob_path.is_file()
                    and not created_blob_path.is_symlink()
                ):
                    created_blob_path.unlink()
            except (OSError, ValueError, sqlite3.Error):
                pass
        raise
    finally:
        if cleanup_staging:
            try:
                cleanup_staging = connection.execute(
                    "SELECT 1 FROM profile_source_scans WHERE id = ?",
                    (scan_id,),
                ).fetchone() is None
            except sqlite3.Error:
                cleanup_staging = False
        connection.close()
        if cleanup_staging:
            try:
                _remove_scan_staging_directory(data_dir, scan_id)
            except (OSError, ValueError):
                pass
    assert committed_result is not None
    return committed_result


def _validate_profile_base(
    current: sqlite3.Row | None,
    *,
    base_profile_version_id: str | None,
    base_profile_checksum_sha256: str | None,
) -> None:
    if current is None:
        if base_profile_version_id is not None or base_profile_checksum_sha256 is not None:
            raise VaultError(
                "profile_source_base_changed",
                "The current profile changed. Build a new source preview and try again.",
            )
        return
    if (
        str(current["id"]) != base_profile_version_id
        or str(current["checksum_sha256"]) != base_profile_checksum_sha256
    ):
        raise VaultError(
            "profile_source_base_changed",
            "The current profile changed. Build a new source preview and try again.",
        )


def _validate_source_scan_buildable(scan: sqlite3.Row) -> None:
    try:
        draft_profile = json.loads(str(scan["draft_profile_json"]))
        report = json.loads(str(scan["report_json"]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise VaultError(
            "profile_source_report_invalid",
            "The source preview report is invalid.",
        ) from exc
    ui = report.get("ui") if isinstance(report, dict) else None
    if isinstance(draft_profile, dict) and isinstance(report, dict):
        from .source_recovery import validate_recovered_scan
        if validate_recovered_scan(scan, draft_profile, report):
            return
    if (
        not isinstance(draft_profile, dict)
        or not isinstance(ui, dict)
        or ui.get("can_build") is not True
        or not is_baseline_resume_renderable(draft_profile)
    ):
        raise VaultError(
            "profile_source_not_buildable",
            "The source preview needs usable profile content before it can be committed.",
        )


def _safe_scan_result(
    *,
    scan_id: str,
    draft_profile: dict[str, Any],
    items: list[sqlite3.Row] | list[dict[str, Any]],
    report: dict[str, Any],
    review_candidate_count: int,
    expires_at_ms: int,
) -> dict[str, Any]:
    profile_name, renderable = _profile_summary(draft_profile)
    return {
        "scan_id": scan_id,
        "status": "preview",
        **_scan_item_counts(items),
        **_profile_content_counts(draft_profile),
        **_safe_report_counts(report, items),
        "review_candidate_count": review_candidate_count,
        "source_review_count": int(report.get("ui", {}).get("source_review_count", 0)),
        "profile_name": profile_name,
        "renderable": renderable,
        "draft_profile_checksum_sha256": hashlib.sha256(
            _json_text(
                draft_profile,
                label="draft_profile",
                expected_type=dict,
                max_bytes=MAX_DRAFT_PROFILE_BYTES,
            ).encode("utf-8")
        ).hexdigest(),
        "expires_at_ms": expires_at_ms,
    }


def create_profile_source_scan(
    data_dir: Path,
    *,
    scan_id: str,
    base_profile_version_id: str | None,
    base_profile_checksum_sha256: str | None,
    items: list[dict[str, Any]],
    draft_profile: dict[str, Any],
    report: dict[str, Any],
    expires_at_ms: int,
    review_candidates: list[dict[str, Any]] | None = None,
    source_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from .source_review import normalize_source_checks
    scan_id = _canonical_uuid(scan_id, label="scan_id")
    if base_profile_version_id is not None:
        base_profile_version_id = _canonical_uuid(
            base_profile_version_id,
            label="base_profile_version_id",
        )
    if base_profile_checksum_sha256 is not None:
        base_profile_checksum_sha256 = _sha256(
            base_profile_checksum_sha256,
            label="base_profile_checksum_sha256",
        )
    if (base_profile_version_id is None) != (base_profile_checksum_sha256 is None):
        raise ValueError("Profile source base id and checksum must be supplied together")
    now_ms = _utc_now_ms()
    if (
        not isinstance(expires_at_ms, int)
        or isinstance(expires_at_ms, bool)
        or expires_at_ms <= now_ms
        or expires_at_ms > now_ms + MAX_SCAN_LIFETIME_MS
    ):
        raise ValueError("expires_at_ms must be within the next 24 hours")
    draft_profile_json = _json_text(
        draft_profile,
        label="draft_profile",
        expected_type=dict,
        max_bytes=MAX_DRAFT_PROFILE_BYTES,
    )
    draft_checksum = hashlib.sha256(draft_profile_json.encode("utf-8")).hexdigest()
    report_json = _json_text(
        report,
        label="report",
        expected_type=dict,
        max_bytes=MAX_SCAN_REPORT_BYTES,
    )
    normalized_items = _normalized_scan_items(data_dir, scan_id=scan_id, items=items)
    source_review_json = _json_text(
        normalize_source_checks(source_checks or [], source_count=len(normalized_items)),
        label="source checks", expected_type=list, max_bytes=MAX_REVIEW_CANDIDATE_BYTES,
    )
    normalized_review_candidates = _normalized_review_candidates(
        review_candidates or [],
        source_count=len(normalized_items),
    )
    review_candidates_json = _json_text(
        normalized_review_candidates,
        label="review_candidates",
        expected_type=list,
        max_bytes=MAX_REVIEW_CANDIDATE_BYTES,
    )

    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        committed = connection.execute(
            "SELECT 1 FROM profile_source_imports WHERE id = ?",
            (scan_id,),
        ).fetchone()
        if committed is not None:
            raise VaultError(
                "profile_source_scan_committed",
                "This source preview was already committed.",
            )
        current = _current_profile_row(connection)
        _validate_profile_base(
            current,
            base_profile_version_id=base_profile_version_id,
            base_profile_checksum_sha256=base_profile_checksum_sha256,
        )
        existing = connection.execute(
            "SELECT * FROM profile_source_scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
        if existing is not None:
            existing_items = connection.execute(
                """
                SELECT * FROM profile_source_scan_items
                WHERE scan_id = ? ORDER BY ordinal
                """,
                (scan_id,),
            ).fetchall()
            equivalent = (
                str(existing["status"]) == "preview"
                and existing["base_profile_version_id"] == base_profile_version_id
                and existing["base_profile_checksum_sha256"]
                == base_profile_checksum_sha256
                and str(existing["draft_profile_json"]) == draft_profile_json
                and str(existing["report_json"]) == report_json
                and str(existing["review_candidates_json"]) == review_candidates_json
                and str(existing["source_review_json"]) == source_review_json
                and int(existing["expires_at_ms"]) == expires_at_ms
                and len(existing_items) == len(normalized_items)
                and all(
                    all(
                        existing_item[key] == normalized_item[key]
                        for key in normalized_item
                    )
                    for existing_item, normalized_item in zip(
                        existing_items,
                        normalized_items,
                        strict=True,
                    )
                )
            )
            if not equivalent:
                raise VaultError(
                    "profile_source_scan_conflict",
                    "A different source preview already uses this identifier.",
                )
            connection.rollback()
            return _safe_scan_result(
                scan_id=scan_id,
                draft_profile=json.loads(draft_profile_json),
                items=normalized_items,
                report=json.loads(report_json),
                review_candidate_count=len(normalized_review_candidates),
                expires_at_ms=expires_at_ms,
            )

        connection.execute(
            """
            INSERT INTO profile_source_scans(
                id, status, base_profile_version_id,
                base_profile_checksum_sha256, draft_profile_json,
                draft_profile_checksum_sha256, report_json, expires_at_ms,
                created_at_ms, updated_at_ms, review_candidates_json, source_review_json
            ) VALUES (?, 'preview', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                base_profile_version_id,
                base_profile_checksum_sha256,
                draft_profile_json,
                draft_checksum,
                report_json,
                expires_at_ms,
                now_ms,
                now_ms,
                review_candidates_json,
                source_review_json,
            ),
        )
        connection.executemany(
            """
            INSERT INTO profile_source_scan_items(
                scan_id, ordinal, staged_relative_path, display_name,
                source_format, media_type, source_kind, byte_size,
                checksum_sha256, parser_contract, extraction_status,
                issue_code, extracted_text, extracted_text_sha256,
                candidate_profile_json, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scan_id,
                    item["ordinal"],
                    item["staged_relative_path"],
                    item["display_name"],
                    item["source_format"],
                    item["media_type"],
                    item["source_kind"],
                    item["byte_size"],
                    item["checksum_sha256"],
                    item["parser_contract"],
                    item["extraction_status"],
                    item["issue_code"],
                    item["extracted_text"],
                    item["extracted_text_sha256"],
                    item["candidate_profile_json"],
                    item["warnings_json"],
                )
                for item in normalized_items
            ],
        )
        connection.commit()
        return _safe_scan_result(
            scan_id=scan_id,
            draft_profile=json.loads(draft_profile_json),
            items=normalized_items,
            report=json.loads(report_json),
            review_candidate_count=len(normalized_review_candidates),
            expires_at_ms=expires_at_ms,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _committed_scan_result(connection: sqlite3.Connection, scan_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT result_json FROM profile_source_imports WHERE id = ?",
        (scan_id,),
    ).fetchone()
    return json.loads(str(row["result_json"])) if row is not None else None


def _reset_committing_scan(data_dir: Path, scan_id: str) -> None:
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE profile_source_scans
            SET status = 'preview', updated_at_ms = ?
            WHERE id = ? AND status = 'committing'
            """,
            (_utc_now_ms(), scan_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
    finally:
        connection.close()


def commit_profile_source_scan(data_dir: Path, scan_id: str) -> dict[str, Any]:
    scan_id = _canonical_uuid(scan_id, label="scan_id")
    data_dir = data_dir.expanduser().resolve()
    connection = _connect(data_dir)
    created_blob_paths: list[Path] = []
    committed_result: dict[str, Any] | None = None
    try:
        _apply_migrations(connection)
        committed_result = _committed_scan_result(connection, scan_id)
        if committed_result is not None:
            try:
                _remove_scan_staging_directory(data_dir, scan_id)
            except OSError:
                pass
            return committed_result

        connection.execute("BEGIN IMMEDIATE")
        scan = connection.execute(
            "SELECT * FROM profile_source_scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
        if scan is None:
            raise VaultError(
                "profile_source_scan_not_found",
                "The source preview no longer exists. Build a new preview and try again.",
            )
        if int(scan["expires_at_ms"]) <= _utc_now_ms():
            connection.execute("DELETE FROM profile_source_scans WHERE id = ?", (scan_id,))
            connection.commit()
            _remove_scan_staging_directory(data_dir, scan_id)
            raise VaultError(
                "profile_source_scan_expired",
                "The source preview expired. Build a new preview and try again.",
            )
        if str(scan["status"]) != "preview":
            raise VaultError(
                "profile_source_scan_busy",
                "The source preview is already being committed.",
            )
        _validate_profile_base(
            _current_profile_row(connection),
            base_profile_version_id=scan["base_profile_version_id"],
            base_profile_checksum_sha256=scan["base_profile_checksum_sha256"],
        )
        _validate_source_scan_buildable(scan)
        connection.execute(
            "UPDATE profile_source_scans SET status = 'committing', updated_at_ms = ? WHERE id = ?",
            (_utc_now_ms(), scan_id),
        )
        # Keep the write transaction through bounded source verification and
        # publication. A process exit must roll this marker back to preview;
        # persisting it early strands an explicitly confirmed recovery request.

        items = connection.execute(
            """
            SELECT * FROM profile_source_scan_items
            WHERE scan_id = ? ORDER BY ordinal
            """,
            (scan_id,),
        ).fetchall()
        if not items or len(items) > MAX_PROFILE_SOURCE_FILES:
            raise VaultError(
                "profile_source_manifest_invalid",
                "The source preview manifest is invalid.",
            )
        if [int(item["ordinal"]) for item in items] != list(range(len(items))):
            raise VaultError(
                "profile_source_manifest_invalid",
                "The source preview manifest is invalid.",
            )

        blob_metadata: dict[str, tuple[str, int]] = {}
        for item in items:
            checksum = _sha256(item["checksum_sha256"], label="checksum_sha256")
            content = _read_verified_source_file(
                data_dir,
                scan_id=scan_id,
                relative_path=str(item["staged_relative_path"]),
                expected_size=int(item["byte_size"]),
                expected_checksum=checksum,
            )
            target, relative_path, created = _ensure_source_blob(
                data_dir,
                checksum_sha256=checksum,
                content=content,
            )
            if created:
                created_blob_paths.append(target)
            blob_metadata[checksum] = (relative_path, len(content))

        committed_result = _committed_scan_result(connection, scan_id)
        if committed_result is not None:
            connection.rollback()
            try:
                _remove_scan_staging_directory(data_dir, scan_id)
            except OSError:
                pass
            return committed_result
        scan = connection.execute(
            "SELECT * FROM profile_source_scans WHERE id = ? AND status = 'committing'",
            (scan_id,),
        ).fetchone()
        if scan is None:
            raise VaultError(
                "profile_source_scan_interrupted",
                "The source preview was interrupted. Build a new preview and try again.",
            )
        _validate_profile_base(
            _current_profile_row(connection),
            base_profile_version_id=scan["base_profile_version_id"],
            base_profile_checksum_sha256=scan["base_profile_checksum_sha256"],
        )
        _validate_source_scan_buildable(scan)

        source_ids: dict[str, str] = {}
        extraction_ids: dict[int, str | None] = {}
        source_snapshots_created = 0
        source_snapshots_reused = 0
        extractions_created = 0
        extractions_reused = 0
        now_ms = _utc_now_ms()
        retention = connection.execute(
            """
            SELECT current_generation
            FROM profile_source_retention_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if retention is None:
            raise VaultError(
                "vault_integrity_error",
                "The local source-retention state is missing.",
            )
        for item in items:
            checksum = str(item["checksum_sha256"])
            source_row = connection.execute(
                """
                SELECT id, relative_path, byte_size
                FROM sources
                WHERE content_addressed = 1 AND checksum_sha256 = ?
                """,
                (checksum,),
            ).fetchone()
            expected_relative_path, expected_size = blob_metadata[checksum]
            if source_row is None:
                source_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, display_name, media_type, checksum_sha256,
                        relative_path, extracted_text, imported_at_ms,
                        byte_size, source_format, content_addressed
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 1)
                    """,
                    (
                        source_id,
                        str(item["display_name"]),
                        item["media_type"],
                        checksum,
                        expected_relative_path,
                        now_ms,
                        expected_size,
                        str(item["source_format"]),
                    ),
                )
                source_snapshots_created += 1
            else:
                if (
                    str(source_row["relative_path"]) != expected_relative_path
                    or int(source_row["byte_size"] or -1) != expected_size
                ):
                    raise VaultError(
                        "source_integrity_error",
                        "A managed source snapshot does not match its database record.",
                    )
                source_id = str(source_row["id"])
                source_snapshots_reused += 1
            source_ids[checksum] = source_id

            extraction_id: str | None = None
            status = str(item["extraction_status"])
            existing_extraction = connection.execute(
                """
                SELECT id, extracted_text_sha256, candidate_profile_json, warnings_json
                FROM source_extractions
                WHERE source_id = ? AND parser_contract = ?
                """,
                (source_id, str(item["parser_contract"])),
            ).fetchone()
            if status == "duplicate":
                if existing_extraction is not None:
                    extraction_id = str(existing_extraction["id"])
                    extractions_reused += 1
            elif status != "failed" and item["extracted_text_sha256"] is not None:
                if existing_extraction is not None:
                    if (
                        str(existing_extraction["extracted_text_sha256"])
                        != str(item["extracted_text_sha256"])
                        or existing_extraction["candidate_profile_json"]
                        != item["candidate_profile_json"]
                        or str(existing_extraction["warnings_json"])
                        != str(item["warnings_json"])
                    ):
                        raise VaultError(
                            "source_extraction_conflict",
                            "The same parser contract produced different source evidence.",
                        )
                    extraction_id = str(existing_extraction["id"])
                    extractions_reused += 1
                else:
                    extraction_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO source_extractions(
                            id, source_id, parser_contract, extracted_text,
                            extracted_text_sha256, candidate_profile_json,
                            warnings_json, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            extraction_id,
                            source_id,
                            str(item["parser_contract"]),
                            str(item["extracted_text"]),
                            str(item["extracted_text_sha256"]),
                            item["candidate_profile_json"],
                            str(item["warnings_json"]),
                            now_ms,
                        ),
                    )
                    extractions_created += 1
            extraction_ids[int(item["ordinal"])] = extraction_id

        draft_profile_json = str(scan["draft_profile_json"])
        draft_profile = json.loads(draft_profile_json)
        saved = _save_profile_with_connection(
            connection,
            canonical_json=draft_profile_json,
            checksum_sha256=str(scan["draft_profile_checksum_sha256"]),
            source=f"source_scan:{scan_id}",
        )
        profile_name, renderable = _profile_summary(draft_profile)
        report = json.loads(str(scan["report_json"]))
        try:
            review_candidates = _normalized_review_candidates(
                json.loads(str(scan["review_candidates_json"])),
                source_count=len(items),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VaultError(
                "profile_source_review_candidates_invalid",
                "The source preview review candidates are invalid.",
            ) from exc
        if review_candidates and scan["base_profile_version_id"] is None:
            raise VaultError(
                "profile_source_review_candidates_invalid",
                "A first profile import cannot create review inbox items.",
            )
        for candidate in review_candidates:
            if any(
                str(items[int(evidence["source_ordinal"])]["extraction_status"])
                != "parsed"
                for evidence in candidate["evidence"]
            ):
                raise VaultError(
                    "profile_source_review_candidates_invalid",
                    "Review candidate evidence must come from a parsed source.",
                )
        synthesis_contract = str(
            report.get("synthesis_contract") or "deterministic-local-v1"
        ).strip()
        if _CONTRACT_RE.fullmatch(synthesis_contract) is None:
            raise VaultError(
                "profile_source_report_invalid",
                "The source synthesis contract is invalid.",
            )
        counts = _scan_item_counts(items)
        result = {
            "scan_id": scan_id,
            "status": "committed",
            "profile_version_id": saved.id,
            "version_number": saved.version_number,
            "created": saved.created,
            "checksum_sha256": saved.checksum_sha256,
            "profile_name": profile_name,
            "renderable": renderable,
            **counts,
            **_profile_content_counts(draft_profile),
            **_safe_report_counts(report, items),
            "ui": _safe_commit_ui(report, items, draft_profile),
            "source_snapshots_created": source_snapshots_created,
            "source_snapshots_reused": source_snapshots_reused,
            "extractions_created": extractions_created,
            "extractions_reused": extractions_reused,
            "synthesis_contract": synthesis_contract,
            "provider_calls": 0,
            "review_item_count": len(review_candidates),
            "source_review_count": len(json.loads(str(scan["source_review_json"]))),
        }
        result_json = _json_text(
            result,
            label="source import result",
            expected_type=dict,
            max_bytes=MAX_SCAN_REPORT_BYTES,
        )
        connection.execute(
            """
            INSERT INTO profile_source_imports(
                id, base_profile_version_id, base_profile_checksum_sha256,
                output_profile_version_id, source_set_checksum_sha256,
                synthesis_contract, report_json, result_json, committed_at_ms,
                retention_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                scan["base_profile_version_id"],
                scan["base_profile_checksum_sha256"],
                saved.id,
                _source_set_checksum(items),
                synthesis_contract,
                str(scan["report_json"]),
                result_json,
                now_ms,
                int(retention["current_generation"]),
            ),
        )
        connection.executemany(
            """
            INSERT INTO profile_version_sources(
                import_id, ordinal, profile_version_id, source_id,
                display_name, extraction_id, source_format, source_kind,
                extraction_status, issue_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scan_id,
                    int(item["ordinal"]),
                    saved.id,
                    source_ids[str(item["checksum_sha256"])],
                    str(item["display_name"]),
                    extraction_ids[int(item["ordinal"])],
                    str(item["source_format"]),
                    str(item["source_kind"]),
                    str(item["extraction_status"]),
                    item["issue_code"],
                )
                for item in items
            ],
        )
        connection.executemany(
            """
            INSERT INTO profile_review_items(
                id, import_id, candidate_ordinal, primary_source_ordinal,
                profile_version_id, retention_generation, candidate_kind,
                operation_kind, proposed_json, previous_json,
                candidate_checksum_sha256, previous_checksum_sha256,
                evidence_json, generator_contract, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(
                        uuid.uuid5(
                            uuid.UUID(scan_id),
                            (
                                "profile-review-item-v1:"
                                f"{candidate_ordinal}:"
                                f"{candidate['candidate_checksum_sha256']}"
                            ),
                        )
                    ),
                    scan_id,
                    candidate_ordinal,
                    int(candidate["evidence"][0]["source_ordinal"]),
                    saved.id,
                    int(retention["current_generation"]),
                    candidate["candidate_kind"],
                    candidate["operation_kind"],
                    _json_text(
                        candidate["proposed"],
                        label="review proposed entry",
                        expected_type=dict,
                        max_bytes=32 * 1024,
                    ),
                    (
                        _json_text(
                            candidate["previous"],
                            label="review previous entry",
                            expected_type=dict,
                            max_bytes=32 * 1024,
                        )
                        if candidate["previous"] is not None
                        else None
                    ),
                    candidate["candidate_checksum_sha256"],
                    candidate["previous_checksum_sha256"],
                    _json_text(
                        candidate["evidence"],
                        label="review evidence",
                        expected_type=list,
                        max_bytes=8 * 1024,
                    ),
                    "deterministic-review-candidate-v1",
                    now_ms,
                )
                for candidate_ordinal, candidate in enumerate(review_candidates)
            ],
        )
        from .source_review import persist_source_checks
        persist_source_checks(
            connection, scan=scan, profile_version_id=saved.id,
            retention_generation=int(retention["current_generation"]), items=items,
            now_ms=now_ms,
        )
        connection.execute("DELETE FROM profile_source_scans WHERE id = ?", (scan_id,))
        connection.commit()
        committed_result = result
    except Exception:
        connection.rollback()
        for path in created_blob_paths:
            try:
                relative_path = path.relative_to(data_dir).as_posix()
                referenced = connection.execute(
                    "SELECT 1 FROM sources WHERE relative_path = ? LIMIT 1",
                    (relative_path,),
                ).fetchone()
                if referenced is None and path.is_file() and not path.is_symlink():
                    path.unlink()
            except (OSError, ValueError, sqlite3.Error):
                continue
        _reset_committing_scan(data_dir, scan_id)
        raise
    finally:
        connection.close()

    try:
        _remove_scan_staging_directory(data_dir, scan_id)
    except OSError:
        pass
    assert committed_result is not None
    return committed_result


def discard_profile_source_scan(data_dir: Path, scan_id: str) -> dict[str, Any]:
    scan_id = _canonical_uuid(scan_id, label="scan_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        committed = _committed_scan_result(connection, scan_id)
        if committed is not None:
            removed_staging = _remove_scan_staging_directory(data_dir, scan_id)
            return {
                "scan_id": scan_id,
                "status": "committed",
                "discarded": False,
                "staging_removed": removed_staging,
            }
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "DELETE FROM profile_source_scans WHERE id = ?",
            (scan_id,),
        )
        discarded = cursor.rowcount > 0
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    removed_staging = _remove_scan_staging_directory(data_dir, scan_id)
    return {
        "scan_id": scan_id,
        "status": "discarded",
        "discarded": discarded,
        "staging_removed": removed_staging,
    }


def discard_all_profile_source_scans(data_dir: Path) -> dict[str, int]:
    """Discard every uncommitted scan without touching durable source evidence."""

    connection = _connect(data_dir)
    scan_ids: list[str] = []
    committed_ids: set[str] = set()
    try:
        _apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT scan.id, source_import.id AS committed_id
            FROM profile_source_scans AS scan
            LEFT JOIN profile_source_imports AS source_import
              ON source_import.id = scan.id
            ORDER BY scan.id
            """
        ).fetchall()
        for row in rows:
            try:
                scan_ids.append(_canonical_uuid(row["id"], label="scan_id"))
            except ValueError:
                continue
            if row["committed_id"] is not None:
                committed_ids.add(str(row["id"]))
        connection.execute("DELETE FROM profile_source_scans")
        connection.commit()

        # Reacquire the writer lock before touching staging. If another process
        # recreated one of these UUIDs after the deletion committed, its new row
        # makes that directory ineligible for cleanup.
        try:
            connection.execute("BEGIN IMMEDIATE")
            active_ids = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM profile_source_scans"
                ).fetchall()
            }
            for scan_id in scan_ids:
                if scan_id in active_ids or scan_id in committed_ids:
                    continue
                try:
                    _remove_scan_staging_directory(data_dir, scan_id)
                except (OSError, ValueError):
                    continue
            connection.commit()
        except (sqlite3.Error, VaultError):
            connection.rollback()
        return {
            "discarded_previews": min(len(rows), MAX_RPC_SAFE_COUNT),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def latest_profile(data_dir: Path) -> dict[str, Any] | None:
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        row = connection.execute(
            """
            SELECT id, version_number, canonical_json, checksum_sha256, source, created_at_ms
            FROM profile_versions
            ORDER BY version_number DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "version_number": int(row["version_number"]),
        "profile": _decode_stored_profile_json(row["canonical_json"]),
        "checksum_sha256": str(row["checksum_sha256"]),
        "source": str(row["source"]),
        "created_at_ms": int(row["created_at_ms"]),
    }


def get_profile_version(data_dir: Path, profile_version_id: Any) -> dict[str, Any]:
    """Load one immutable profile version by its canonical opaque identifier."""

    normalized_id = _canonical_uuid(profile_version_id, label="profile_version_id")
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        row = connection.execute(
            """
            SELECT id, version_number, canonical_json, checksum_sha256, source, created_at_ms
            FROM profile_versions
            WHERE id = ?
            """,
            (normalized_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise VaultError(
            "profile_version_not_found",
            "That immutable profile version is no longer available.",
        )
    return {
        "id": str(row["id"]),
        "version_number": int(row["version_number"]),
        "profile": _decode_stored_profile_json(row["canonical_json"]),
        "checksum_sha256": str(row["checksum_sha256"]),
        "source": str(row["source"]),
        "created_at_ms": int(row["created_at_ms"]),
    }


def save_artifact_bytes(
    data_dir: Path,
    *,
    profile_version_id: str,
    kind: str,
    extension: str,
    content: bytes,
    metadata: dict[str, Any] | None = None,
    tailored_resume_draft_id: str | None = None,
    tailored_resume_revision_id: str | None = None,
) -> ArtifactSaveResult:
    if not isinstance(content, bytes) or not content:
        raise ValueError("Artifact content must be non-empty bytes")
    if len(content) > MAX_MANAGED_ARTIFACT_BYTES:
        raise ValueError("Artifact exceeds the 20 MiB local limit")
    normalized_kind = str(kind).strip().lower()
    normalized_extension = str(extension).strip().lower().lstrip(".")
    baseline_kinds = {"resume_pdf", "resume_docx"}
    tailored_kinds = {"tailored_resume_pdf", "tailored_resume_docx"}
    if normalized_kind not in baseline_kinds | tailored_kinds:
        raise ValueError("Unsupported artifact kind")
    if normalized_extension not in {"pdf", "docx"}:
        raise ValueError("Unsupported artifact extension")
    is_tailored = normalized_kind in tailored_kinds
    expected_kind = (
        f"tailored_resume_{normalized_extension}"
        if is_tailored
        else f"resume_{normalized_extension}"
    )
    if normalized_kind != expected_kind:
        raise ValueError("Artifact kind and extension do not match")
    if is_tailored:
        tailored_resume_draft_id = _canonical_uuid(
            tailored_resume_draft_id,
            label="tailored_resume_draft_id",
        )
        if tailored_resume_revision_id is not None:
            tailored_resume_revision_id = _canonical_uuid(
                tailored_resume_revision_id,
                label="tailored_resume_revision_id",
            )
    elif tailored_resume_draft_id is not None or tailored_resume_revision_id is not None:
        raise ValueError("Baseline artifacts cannot reference tailored resume lineage")
    if normalized_extension == "pdf":
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-64:]:
            raise ValueError("Rendered PDF payload is invalid")
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content), strict=True)
            if len(reader.pages) < 1:
                raise ValueError("Rendered PDF payload is invalid")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Rendered PDF payload is invalid") from exc
    else:
        try:
            with ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
                if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                    raise ValueError("Rendered DOCX payload is invalid")
                archive.testzip()
        except BadZipFile as exc:
            raise ValueError("Rendered DOCX payload is invalid") from exc

    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Artifact metadata must be a JSON object")
    payload = metadata or {}
    try:
        payload_json = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Artifact metadata must contain only valid JSON values") from exc

    checksum = hashlib.sha256(content).hexdigest()
    data_dir = data_dir.expanduser().resolve()
    artifact_id = str(uuid.uuid4())
    relative_path = Path("artifacts") / "resumes" / f"{artifact_id}.{normalized_extension}"
    target: Path | None = None
    connection = _connect(data_dir)
    try:
        _apply_migrations(connection)
        profile_row = connection.execute(
            "SELECT checksum_sha256 FROM profile_versions WHERE id = ?",
            (profile_version_id,),
        ).fetchone()
        if profile_row is None:
            raise ValueError("Profile version does not exist")

        template_id = str(payload.get("template_id") or "classic")[:80]
        renderer_contract = str(payload.get("renderer_contract_version") or 1)[:24]
        opportunity_id: str | None = None
        source_resume_checksum_sha256: str | None = None
        fingerprint_source: tuple[str, ...]
        if is_tailored:
            draft_row = connection.execute(
                """
                SELECT opportunity_id, profile_version_id, result_resume_json
                FROM tailored_resume_drafts
                WHERE id = ?
                """,
                (tailored_resume_draft_id,),
            ).fetchone()
            if draft_row is None:
                raise ValueError("Tailored resume draft does not exist")
            if str(draft_row["profile_version_id"]) != profile_version_id:
                raise ValueError("Tailored resume draft and profile version do not match")
            opportunity_id = str(draft_row["opportunity_id"])
            if tailored_resume_revision_id is None:
                source_resume_checksum_sha256 = hashlib.sha256(
                    str(draft_row["result_resume_json"]).encode("utf-8")
                ).hexdigest()
                fingerprint_source = (
                    str(tailored_resume_draft_id),
                    source_resume_checksum_sha256,
                    "tailored-resume-v1",
                )
            else:
                revision_row = connection.execute(
                    """
                    SELECT draft_id, result_resume_json,
                           result_resume_checksum_sha256
                    FROM tailored_resume_revisions
                    WHERE id = ?
                    """,
                    (tailored_resume_revision_id,),
                ).fetchone()
                if revision_row is None:
                    raise ValueError("Tailored resume revision does not exist")
                if str(revision_row["draft_id"]) != tailored_resume_draft_id:
                    raise ValueError("Tailored resume revision and draft do not match")
                revision_json = str(revision_row["result_resume_json"])
                source_resume_checksum_sha256 = hashlib.sha256(
                    revision_json.encode("utf-8")
                ).hexdigest()
                if (
                    source_resume_checksum_sha256
                    != str(revision_row["result_resume_checksum_sha256"])
                ):
                    raise ValueError("Tailored resume revision checksum is invalid")
                fingerprint_source = (
                    str(tailored_resume_draft_id),
                    tailored_resume_revision_id,
                    source_resume_checksum_sha256,
                    "tailored-resume-revision-v1",
                )
        else:
            fingerprint_source = (
                str(profile_row["checksum_sha256"]),
                "baseline-v1",
            )
        render_fingerprint = hashlib.sha256(
            "\0".join(
                (*fingerprint_source,
                    f"renderer-v{renderer_contract}",
                    normalized_extension,
                    template_id,
                )
            ).encode("utf-8")
        ).hexdigest()
        existing = connection.execute(
            """
            SELECT id, kind, profile_version_id, opportunity_id,
                   tailored_resume_draft_id, tailored_resume_revision_id,
                   source_resume_checksum_sha256,
                   relative_path, checksum_sha256, byte_size
            FROM artifacts
            WHERE render_fingerprint = ? AND status = 'ready'
            """,
            (render_fingerprint,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["kind"]) != normalized_kind
                or str(existing["profile_version_id"]) != profile_version_id
                or existing["opportunity_id"] != opportunity_id
                or existing["tailored_resume_draft_id"] != tailored_resume_draft_id
                or existing["tailored_resume_revision_id"]
                != tailored_resume_revision_id
                or existing["source_resume_checksum_sha256"]
                != source_resume_checksum_sha256
            ):
                raise ValueError("Existing artifact lineage does not match its fingerprint")
            existing_path = _managed_artifact_path(data_dir, str(existing["relative_path"]))
            if (
                existing_path is not None
                and str(existing["checksum_sha256"]) == checksum
                and _managed_file_matches(
                    existing_path,
                    int(existing["byte_size"]),
                    str(existing["checksum_sha256"]),
                )
            ):
                return ArtifactSaveResult(
                    id=str(existing["id"]),
                    kind=str(existing["kind"]),
                    profile_version_id=str(existing["profile_version_id"]),
                    relative_path=str(existing["relative_path"]),
                    checksum_sha256=str(existing["checksum_sha256"]),
                    byte_size=int(existing["byte_size"]),
                )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE artifacts SET status = 'failed' WHERE id = ?",
                (str(existing["id"]),),
            )
            connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO artifacts(
                id, kind, profile_version_id, opportunity_id, payload_json,
                relative_path, content_fingerprint, created_at_ms, status,
                format, checksum_sha256, byte_size, render_fingerprint,
                template_id, suggested_filename, tailored_resume_draft_id,
                tailored_resume_revision_id, source_resume_checksum_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'rendering', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                normalized_kind,
                profile_version_id,
                opportunity_id,
                payload_json,
                relative_path.as_posix(),
                checksum,
                _utc_now_ms(),
                normalized_extension,
                checksum,
                len(content),
                render_fingerprint,
                template_id,
                str(payload.get("suggested_filename") or "")[:240] or None,
                tailored_resume_draft_id,
                tailored_resume_revision_id,
                source_resume_checksum_sha256,
            ),
        )
        connection.commit()
        target = _managed_artifact_path(data_dir, relative_path.as_posix())
        if target is None:
            raise ValueError("Managed artifact path must stay inside the local vault")
        _write_managed_file(data_dir, relative_path, content)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE artifacts SET status = 'ready' WHERE id = ?",
            (artifact_id,),
        )
        connection.commit()
        return ArtifactSaveResult(
            id=artifact_id,
            kind=normalized_kind,
            profile_version_id=profile_version_id,
            relative_path=relative_path.as_posix(),
            checksum_sha256=checksum,
            byte_size=len(content),
        )
    except Exception:
        connection.rollback()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            connection.commit()
        except Exception:
            connection.rollback()
        if target is not None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        connection.close()
