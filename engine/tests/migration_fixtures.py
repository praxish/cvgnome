# SPDX-License-Identifier: MPL-2.0
"""Reconstitute the exact historical schema, not a version-number-only downgrade."""

import sqlite3

from cvgnome_engine.storage import MIGRATIONS


def remove_schema_nineteen(connection: sqlite3.Connection) -> None:
    """Fixture-only reverse rebuild; caller owns an isolated test database."""
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA legacy_alter_table = ON")
    connection.execute("DROP TABLE memory_project_receipts")
    connection.execute("ALTER TABLE profile_review_items RENAME TO profile_review_items_schema19")
    schema = MIGRATIONS[16]
    table = schema[schema.index("        CREATE TABLE profile_review_items ("):
                   schema.index("        CREATE TABLE profile_review_item_events (")]
    connection.execute(table)
    fields = [row[1] for row in connection.execute("PRAGMA table_info(profile_review_items)")]
    columns = ", ".join(fields)
    connection.execute(f"INSERT INTO profile_review_items ({columns}) SELECT {columns} FROM profile_review_items_schema19")
    connection.execute("DROP TABLE profile_review_items_schema19")
    connection.execute("DROP TABLE memories")
    connection.execute("PRAGMA legacy_alter_table = OFF")
    connection.execute("CREATE INDEX profile_review_items_generation_created_idx ON profile_review_items(retention_generation, created_at_ms DESC, id DESC)")
    validation = schema[schema.index("        CREATE TRIGGER profile_review_items_validate_insert"):
                        schema.index("        CREATE TRIGGER profile_review_item_events_validate_insert")]
    connection.execute(validation)
    for name, next_name in (("profile_review_items_no_update", "profile_review_items_no_delete"),
                            ("profile_review_items_no_delete", "profile_review_item_events_no_update")):
        connection.execute(schema[schema.index(f"        CREATE TRIGGER {name}"):
                                  schema.index(f"        CREATE TRIGGER {next_name}")])
    connection.execute("DELETE FROM schema_migrations WHERE version = 19")
    connection.execute("PRAGMA user_version = 18")
