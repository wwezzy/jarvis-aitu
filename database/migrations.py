"""Versioned, forward-only v3 -> v4 migration; runs in one startup transaction.

No table drops, automatic downgrades, or guessed server timezone conversions.
Legacy event times used TIMEZONE; legacy host-generated times used the server
timezone (UTC by default). Set LEGACY_SERVER_TIMEZONE before the first upgrade
if the old process ran in a different zone. Back up before upgrading staging.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateColumn

from config import get_settings
from database.models import Base

VERSION = 1
ADDED = {
    "memory_facts": ("confidence", "provenance", "expires_at", "last_used_at"),
    "schedule_entries": ("location", "commute_minutes", "preparation_minutes", "importance", "provenance"),
    "assignments": ("estimated_minutes", "progress", "importance", "risk", "consequence", "preparation_minutes",
                    "testing_buffer_minutes", "provenance", "confidence", "deadline_overridden"),
}
# These v3 values were explicitly saved in the user's local time. Remaining
# legacy timestamp fields used datetime.now() / database CURRENT_TIMESTAMP.
LOCAL_COLUMNS = {
    ("assignments", "due_at"), ("reminders", "remind_at"),
    ("workout_sessions", "started_at"), ("gtg_sets", "logged_at"),
    ("lms_sync_state", "last_attempt_at"), ("lms_sync_state", "last_success_at"),
}


def upgrade(connection):
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(741104001)"))
        connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    connection.execute(text("CREATE TABLE IF NOT EXISTS jarvis_schema_version (version INTEGER PRIMARY KEY)"))
    current = connection.execute(text("SELECT max(version) FROM jarvis_schema_version")).scalar() or 0
    if current > VERSION:
        raise RuntimeError("Database schema is newer than this application; refusing downgrade")
    if current == VERSION:
        return
    old_tables = set(inspect(connection).get_table_names())
    old_columns = {name: inspect(connection).get_columns(name) for name in old_tables}
    for table, additions in ADDED.items():
        if table not in old_tables:
            continue
        present = {col["name"] for col in old_columns[table]}
        for name in additions:
            if name not in present:
                column = Base.metadata.tables[table].c[name]
                ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
                connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {ddl}'))
    # Convert only pre-existing timestamps, before the UTC type is used to read.
    server_zone = ZoneInfo(os.getenv("LEGACY_SERVER_TIMEZONE", "UTC"))
    local_zone = ZoneInfo(os.getenv("LEGACY_DATA_TIMEZONE", str(get_settings().timezone)))
    for name, table in Base.metadata.tables.items():
        if name not in old_columns:
            continue
        pk = next(iter(table.primary_key.columns)).name
        present = {col["name"] for col in old_columns[name]}
        for col in table.c:
            if col.name not in present or col.type.__class__.__name__ != "UTCDateTime":
                continue
            zone = local_zone if (name, col.name) in LOCAL_COLUMNS else server_zone
            rows = connection.execute(text(f'SELECT "{pk}", "{col.name}" FROM "{name}" WHERE "{col.name}" IS NOT NULL')).all()
            for key, value in rows:
                stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=zone)
                stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
                connection.execute(text(f'UPDATE "{name}" SET "{col.name}"=:stamp WHERE "{pk}"=:key'), {"stamp": stamp, "key": key})
    Base.metadata.create_all(connection)
    connection.execute(text("INSERT INTO jarvis_schema_version (version) VALUES (:version)"), {"version": VERSION})
