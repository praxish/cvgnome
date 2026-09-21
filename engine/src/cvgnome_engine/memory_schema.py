# SPDX-License-Identifier: MPL-2.0
"""Schema 19: immutable local memories and a second review-evidence origin.

The existing item table must be rebuilt because its source-import foreign key
was NOT NULL. The migration runner temporarily disables foreign-key enforcement
outside its transaction, then verifies every foreign key before committing.
Historical SQL and all existing rows, event pins, and apply receipts survive.
"""


def memory_migration(review_schema: str) -> str:
    start = review_schema.index("        CREATE TABLE profile_review_items (")
    end = review_schema.index("        CREATE TABLE profile_review_item_events (")
    table = review_schema[start:end].replace(
        "import_id TEXT NOT NULL REFERENCES profile_source_imports(id) ON DELETE RESTRICT,",
        "import_id TEXT REFERENCES profile_source_imports(id) ON DELETE RESTRICT,\n"
        "            memory_id TEXT REFERENCES memories(id) ON DELETE RESTRICT,",
    ).replace(
        "primary_source_ordinal INTEGER NOT NULL CHECK", "primary_source_ordinal INTEGER CHECK"
    ).replace(
        "            UNIQUE (import_id, candidate_ordinal),",
        """            CHECK (
                (memory_id IS NULL AND import_id IS NOT NULL AND primary_source_ordinal IS NOT NULL
                 AND generator_contract = 'deterministic-review-candidate-v1')
                OR
                (memory_id IS NOT NULL AND import_id IS NULL AND primary_source_ordinal IS NULL
                 AND candidate_kind = 'projects' AND operation_kind = 'add'
                 AND candidate_ordinal = 0 AND generator_contract = 'user-memory-project-v1')
            ),
            UNIQUE (memory_id),
            UNIQUE (import_id, candidate_ordinal),""",
    )
    validation = review_schema[
        review_schema.index("        CREATE TRIGGER profile_review_items_validate_insert"):
        review_schema.index("        CREATE TRIGGER profile_review_item_events_validate_insert")
    ].replace("WHEN NOT EXISTS (", "WHEN NEW.memory_id IS NULL AND NOT EXISTS (", 1)
    immutable = review_schema[
        review_schema.index("        CREATE TRIGGER profile_review_items_no_update"):
        review_schema.index("        CREATE TRIGGER profile_review_item_events_no_update")
    ]
    return """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY CHECK(length(id) = 36),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            profile_version_id TEXT NOT NULL REFERENCES profile_versions(id) ON DELETE RESTRICT,
            retention_generation INTEGER NOT NULL CHECK(retention_generation > 0),
            title TEXT CHECK(title IS NULL OR (length(title) BETWEEN 1 AND 160)),
            narrative TEXT NOT NULL CHECK(length(narrative) BETWEEN 1 AND 12000
                AND length(CAST(narrative AS BLOB)) <= 48000),
            content_checksum_sha256 TEXT NOT NULL CHECK(length(content_checksum_sha256) = 64),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;
        CREATE INDEX memories_generation_created_idx
            ON memories(retention_generation, created_at_ms DESC, id DESC);
        CREATE TRIGGER memories_no_update BEFORE UPDATE ON memories
        BEGIN SELECT RAISE(ABORT, 'memories are immutable'); END;
        CREATE TRIGGER memories_no_delete BEFORE DELETE ON memories
        BEGIN SELECT RAISE(ABORT, 'memories are immutable'); END;

        PRAGMA legacy_alter_table = ON;
        ALTER TABLE profile_review_items RENAME TO profile_review_items_schema18;
    """ + table + """
        INSERT INTO profile_review_items (
            id, import_id, candidate_ordinal, primary_source_ordinal,
            profile_version_id, retention_generation, candidate_kind,
            operation_kind, proposed_json, previous_json, candidate_checksum_sha256,
            previous_checksum_sha256, evidence_json, generator_contract, created_at_ms
        ) SELECT id, import_id, candidate_ordinal, primary_source_ordinal,
            profile_version_id, retention_generation, candidate_kind,
            operation_kind, proposed_json, previous_json, candidate_checksum_sha256,
            previous_checksum_sha256, evidence_json, generator_contract, created_at_ms
          FROM profile_review_items_schema18;
        DROP TABLE profile_review_items_schema18;
        PRAGMA legacy_alter_table = OFF;
        CREATE INDEX profile_review_items_generation_created_idx
            ON profile_review_items(retention_generation, created_at_ms DESC, id DESC);
    """ + validation + immutable + """
        CREATE TRIGGER profile_review_items_validate_memory_insert
        BEFORE INSERT ON profile_review_items
        WHEN NEW.memory_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM memories AS memory
            JOIN profile_source_retention_state AS retention ON retention.singleton_id = 1
            JOIN profile_versions AS profile ON profile.id = NEW.profile_version_id
            WHERE memory.id = NEW.memory_id
              AND memory.retention_generation = NEW.retention_generation
              AND memory.retention_generation = retention.current_generation
              AND NOT EXISTS (SELECT 1 FROM profile_versions newer
                              WHERE newer.version_number > profile.version_number)
        )
        BEGIN SELECT RAISE(ABORT, 'memory review item lineage is invalid'); END;

        CREATE TABLE memory_project_receipts (
            request_id TEXT PRIMARY KEY CHECK(length(request_id) = 36),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
            review_item_id TEXT NOT NULL UNIQUE REFERENCES profile_review_items(id) ON DELETE RESTRICT,
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms BETWEEN 1 AND 9007199254740991)
        ) STRICT;
        CREATE TRIGGER memory_project_receipts_validate_insert
        BEFORE INSERT ON memory_project_receipts WHEN NOT EXISTS (
            SELECT 1 FROM profile_review_items item
            WHERE item.id = NEW.review_item_id AND item.memory_id = NEW.memory_id
              AND item.created_at_ms = NEW.created_at_ms
        )
        BEGIN SELECT RAISE(ABORT, 'memory proposal receipt lineage is invalid'); END;
        CREATE TRIGGER memory_project_receipts_no_update BEFORE UPDATE ON memory_project_receipts
        BEGIN SELECT RAISE(ABORT, 'memory proposal receipts are immutable'); END;
        CREATE TRIGGER memory_project_receipts_no_delete BEFORE DELETE ON memory_project_receipts
        BEGIN SELECT RAISE(ABORT, 'memory proposal receipts are immutable'); END;
    """
