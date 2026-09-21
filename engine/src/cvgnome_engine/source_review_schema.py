# SPDX-License-Identifier: MPL-2.0
"""Append-only storage for deterministic source checks (schema 18)."""

SOURCE_REVIEW_MIGRATION = """
    ALTER TABLE profile_source_scans ADD COLUMN source_review_json TEXT NOT NULL
      DEFAULT '[]' CHECK (json_valid(source_review_json)
        AND json_type(source_review_json) = 'array'
        AND json_array_length(source_review_json) <= 240
        AND length(source_review_json) <= 2097152);

    CREATE TABLE source_review_items (
      id TEXT PRIMARY KEY CHECK (length(id) = 36),
      import_id TEXT NOT NULL REFERENCES profile_source_imports(id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 239),
      profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
      retention_generation INTEGER NOT NULL CHECK (retention_generation > 0),
      kind TEXT NOT NULL CHECK (kind IN ('basic_conflict', 'duplicate_document')),
      material_json TEXT NOT NULL CHECK (json_valid(material_json)
        AND json_type(material_json) = 'object' AND length(material_json) <= 32768),
      checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
      created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
      UNIQUE(import_id, ordinal)
    ) STRICT;
    CREATE TABLE source_review_receipts (
      request_id TEXT PRIMARY KEY CHECK (length(request_id) = 36),
      request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
      request_json TEXT NOT NULL CHECK (json_valid(request_json)
        AND json_type(request_json) = 'object' AND length(request_json) <= 32768),
      parent_profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
      output_profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
      profile_changed INTEGER NOT NULL CHECK (profile_changed IN (0,1)),
      result_json TEXT NOT NULL CHECK (json_valid(result_json)
        AND json_type(result_json) = 'object' AND length(result_json) <= 65536),
      created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
      CHECK ((profile_changed = 0 AND parent_profile_version_id = output_profile_version_id)
        OR (profile_changed = 1 AND parent_profile_version_id != output_profile_version_id))
    ) STRICT;
    CREATE UNIQUE INDEX source_review_receipts_output_idx
      ON source_review_receipts(output_profile_version_id) WHERE profile_changed = 1;
    CREATE TABLE source_review_events (
      request_id TEXT NOT NULL REFERENCES source_review_receipts(request_id) ON DELETE RESTRICT,
      item_id TEXT NOT NULL REFERENCES source_review_items(id) ON DELETE RESTRICT,
      action TEXT NOT NULL CHECK (action IN
        ('keep_profile','use_source','acknowledge_duplicate','defer','reopen')),
      expected_state TEXT NOT NULL CHECK (expected_state IN ('inbox','deferred')),
      expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
      state TEXT NOT NULL CHECK (state IN ('inbox','deferred','resolved')),
      revision INTEGER NOT NULL CHECK (revision = expected_revision + 1),
      PRIMARY KEY(request_id, item_id),
      UNIQUE(item_id, revision),
      CHECK ((action = 'defer' AND expected_state = 'inbox' AND state = 'deferred')
        OR (action = 'reopen' AND expected_state = 'deferred' AND state = 'inbox')
        OR (action IN ('keep_profile','use_source','acknowledge_duplicate')
          AND expected_state = 'inbox' AND state = 'resolved'))
    ) STRICT;
    CREATE INDEX source_review_items_generation_idx
      ON source_review_items(retention_generation, created_at_ms DESC, id);
    CREATE TRIGGER source_review_items_lineage BEFORE INSERT ON source_review_items
    WHEN NOT EXISTS (SELECT 1 FROM profile_source_imports i
      WHERE i.id = NEW.import_id AND i.output_profile_version_id = NEW.profile_version_id
        AND i.retention_generation = NEW.retention_generation)
    BEGIN SELECT RAISE(ABORT, 'source review lineage invalid'); END;
    CREATE TRIGGER source_review_events_lineage BEFORE INSERT ON source_review_events
    WHEN NOT EXISTS (SELECT 1 FROM source_review_items i
      JOIN profile_source_retention_state r ON r.singleton_id = 1
      WHERE i.id = NEW.item_id AND i.retention_generation = r.current_generation
        AND ((i.kind = 'basic_conflict' AND NEW.action != 'acknowledge_duplicate')
          OR (i.kind = 'duplicate_document' AND NEW.action NOT IN ('keep_profile','use_source'))))
      OR NEW.expected_state != COALESCE((SELECT state FROM source_review_events
        WHERE item_id = NEW.item_id ORDER BY revision DESC LIMIT 1), 'inbox')
      OR NEW.expected_revision != COALESCE((SELECT revision FROM source_review_events
        WHERE item_id = NEW.item_id ORDER BY revision DESC LIMIT 1), 0)
    BEGIN SELECT RAISE(ABORT, 'source review decision lineage invalid'); END;
    CREATE TRIGGER source_review_receipts_lineage BEFORE INSERT ON source_review_receipts
    WHEN NEW.profile_changed = 1 AND NOT EXISTS (SELECT 1 FROM profile_versions v
      JOIN profile_versions p ON p.id = NEW.parent_profile_version_id
      WHERE v.id = NEW.output_profile_version_id AND v.parent_version_id = p.id
        AND v.version_number = p.version_number + 1 AND v.source = 'local_edit')
    BEGIN SELECT RAISE(ABORT, 'source review profile lineage invalid'); END;
    CREATE TRIGGER source_review_items_immutable_update BEFORE UPDATE ON source_review_items
    BEGIN SELECT RAISE(ABORT, 'source review items are immutable'); END;
    CREATE TRIGGER source_review_items_immutable_delete BEFORE DELETE ON source_review_items
    BEGIN SELECT RAISE(ABORT, 'source review items are immutable'); END;
    CREATE TRIGGER source_review_receipts_immutable_update BEFORE UPDATE ON source_review_receipts
    BEGIN SELECT RAISE(ABORT, 'source review receipts are immutable'); END;
    CREATE TRIGGER source_review_receipts_immutable_delete BEFORE DELETE ON source_review_receipts
    BEGIN SELECT RAISE(ABORT, 'source review receipts are immutable'); END;
    CREATE TRIGGER source_review_events_immutable_update BEFORE UPDATE ON source_review_events
    BEGIN SELECT RAISE(ABORT, 'source review events are immutable'); END;
    CREATE TRIGGER source_review_events_immutable_delete BEFORE DELETE ON source_review_events
    BEGIN SELECT RAISE(ABORT, 'source review events are immutable'); END;
"""
