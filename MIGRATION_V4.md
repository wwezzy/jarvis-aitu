# Jarvis v4 migration and rollback

This guide is for an isolated staging service and a staging database copy. No production migration/deployment is part of PR #9. Never point test commands at a live database.

## Before migration

1. Record the checked-out commit (`git rev-parse HEAD`) and confirm branch `codex/jarvis-ultimate-v4`. Confirm the PR base is current `feat/jarvis-v3`, including its asyncio/greenlet and LMS deadline fixes.
2. Stop only the staging bot process and scheduler. Keep one writer/poller per bot token.
3. Take a provider snapshot or `pg_dump` custom-format backup using secure libpq environment configuration, and restore it into a separate staging rehearsal database. For SQLite, stop staging and copy its database and WAL consistently, or use SQLite's backup API. Keep backups outside this repository.
4. Record row counts and representative IDs/status/timestamps from users, assignments, memory_facts, schedule_entries, reminders, workout_sessions, exercise_sets, gtg_sets, daily_reflections and notification_log. Record a completed LMS task and a user-created reminder. Do not paste personal rows or credentials in the PR.
5. Set staging BOT_TOKEN, ADMIN_ID, DATABASE_URL and TIMEZONE. Confirm these belong to staging without printing their values. Install the branch's pinned-range dependencies from requirements.txt.
6. Establish legacy timestamp provenance. `LEGACY_DATA_TIMEZONE` defaults to TIMEZONE and describes naive event times saved by v3: assignment due_at, reminder remind_at, workout started_at, GTG logged_at and LMS sync timestamps. `LEGACY_SERVER_TIMEZONE` defaults to UTC for other server-generated timestamps. If the old process used another host zone, set it before migration. Historical mixed-zone data cannot be inferred reliably; examine representative records and repair only a reviewed staging copy before acceptance.

## Apply and verify

With environment variables already configured (do not put secrets in shell history):

```sh
python -c "import asyncio; from database.engine import init_db; asyncio.run(init_db())"
```

The same upgrade runs at normal startup. It uses a transaction and a PostgreSQL transaction-scoped advisory lock. No table resets, drops, seed overwrites or destructive automatic downgrades occur.

- Version 1 adds v4 columns to existing assignments/memory/schedule tables; creates normalized LMS events, dated overrides, user settings, durable notification delivery, provider usage, study records and personal calendar events; converts pre-existing legacy naive timestamps to physical UTC once.
- Version 2 adds `provider_usage.usage_source` with safe `unknown` default to distinguish reported/estimated/unknown token counts. Existing version-1 databases never repeat the legacy timezone conversion.
- SQL DateTime columns remain physically UTC without timezone. The UTCDateTime boundary accepts aware inputs or documented local naive inputs and returns aware application-local values. PostgreSQL connections explicitly use UTC.
- New tables and identity/lookup indexes are created through registered SQLAlchemy metadata. Existing IDs, completed statuses, facts, workout records and notification claims remain in place. Versioned changes are forward-only; a newer schema version aborts startup instead of guessing a downgrade.

Run the upgrade command a second time. In the staging database inspect:

```sql
SELECT version FROM jarvis_schema_version ORDER BY version;
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM assignments;
SELECT COUNT(*) FROM memory_facts;
SELECT COUNT(*) FROM schedule_entries;
SELECT COUNT(*) FROM workout_sessions;
SELECT COUNT(*) FROM exercise_sets;
SELECT COUNT(*) FROM gtg_sets;
SELECT COUNT(*) FROM reminders;
SELECT COUNT(*) FROM notification_log;
```

Expect versions 1 and 2. Compare recorded counts and representative values to the backup. A legacy local 15:00 event in UTC+05:00 is physically 10:00 UTC and displays as 15:00; a second run must leave that physical timestamp unchanged. Check completed LMS tasks stay completed and that reminder identities do not change.

Start the staging application, open Telegram `/diag`, then execute the complete checklist in ACCEPTANCE_TESTS_V4.md. Start with all AI providers and optional integrations disabled. Re-enable integrations one at a time. Do not use production Redis command keys, a production PC secret, a production bot token or a production database for staging.

## Automated evidence and limits

Tests recreate representative legacy SQLite and PostgreSQL schemas, migrate twice, preserve done tasks/IDs, verify physical UTC and reject future schema versions. Separate engine instances verify persistence across restart. The PostgreSQL 18 CI job runs the full service suite in isolated random schemas as well as migration tests.

These synthetic rehearsals do not claim that an actual credentialed staging database was migrated. The backup/restore and representative-record checks above are required operator evidence before staging acceptance. No credentials are requested or included in this repository.

## Rollback

1. Stop only the staging v4 process. Retain the migrated database and logs for investigation; do not alter production.
2. Restore the pre-migration backup into a separate database/path. Point the old staging application commit at that restored copy and verify counts/displayed times before starting its scheduler.
3. Restore the prior staging env configuration. Remove/disable optional Calendar write and research flags and the staging Windows scheduled task if they were enabled for testing.
4. Confirm only one bot poller is active. Do not run v3 against a v4-converted database: v3 treats naive physical timestamps as local and would display/schedule them incorrectly.

There is intentionally no destructive in-place downgrade. Retaining both copies makes rollback inspectable and protects newly recorded staging evidence.
