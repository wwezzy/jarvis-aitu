"""Export a restored, local-only PostgreSQL backup to a verified SQLite copy.

Never accepts a cloud source, overwrites a destination, prints row data or
connects to Telegram. Restore the custom-format backup into jarvis_recovery
on loopback first. RECOVERY_DATABASE_URL stays in the operator environment.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


def canonical(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def digest(rows):
    payload = sorted(json.dumps(dict(row), sort_keys=True, ensure_ascii=False, default=canonical) for row in rows)
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


async def export_copy(source_url: str, destination: Path):
    parsed = make_url(source_url)
    if (parsed.drivername != "postgresql+asyncpg" or parsed.host not in {"127.0.0.1", "::1"}
            or parsed.database != "jarvis_recovery"):
        raise ValueError("Only the isolated loopback jarvis_recovery database is accepted")
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Recovery never overwrites an existing file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + "." + uuid.uuid4().hex[:10] + ".partial")

    # Import metadata only. No service, bot, scheduler, provider or user profile.
    from database.models import Base
    import database.notification_models  # noqa: F401
    import database.v4_models  # noqa: F401
    import database.assistant_models  # noqa: F401
    from database.migrations import upgrade

    source = create_async_engine(source_url, connect_args={"server_settings": {"timezone": "UTC"}})
    target = create_async_engine("sqlite+aiosqlite:///" + partial.as_posix())

    @event.listens_for(target.sync_engine, "connect")
    def configure(db, _):
        db.execute("PRAGMA foreign_keys=ON")

    counts = {}
    try:
        # The source is a disposable restored copy, never the live service.
        async with source.begin() as connection:
            await connection.run_sync(upgrade)
        async with source.connect() as connection:
            versions = list((await connection.execute(text("SELECT version FROM jarvis_schema_version ORDER BY version"))).scalars())
            names = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
        if versions != [1, 2, 3] or names - set(Base.metadata.tables) - {"jarvis_schema_version"}:
            raise ValueError("Unexpected source schema; refusing a partial recovery")
        async with target.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("CREATE TABLE jarvis_schema_version (version INTEGER PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO jarvis_schema_version VALUES (:version)"), [{"version": v} for v in versions])
            async with source.connect() as original:
                for table in Base.metadata.sorted_tables:
                    rows = (await original.execute(select(table))).mappings().all()
                    if rows:
                        await connection.execute(table.insert(), [dict(row) for row in rows])
                    restored = (await connection.execute(select(table))).mappings().all()
                    if digest(rows) != digest(restored):
                        raise ValueError("Recovery verification failed")
                    counts[table.name] = len(rows)
            if (await connection.execute(text("PRAGMA integrity_check"))).scalar() != "ok":
                raise ValueError("SQLite integrity check failed")
            if (await connection.execute(text("PRAGMA foreign_key_check"))).first():
                raise ValueError("SQLite foreign key check failed")
        # Prove that rerunning migration is idempotent on the restored PG copy.
        async with source.begin() as connection:
            before = {table.name: digest((await connection.execute(select(table))).mappings()) for table in Base.metadata.sorted_tables}
            await connection.run_sync(upgrade)
            after = {table.name: digest((await connection.execute(select(table))).mappings()) for table in Base.metadata.sorted_tables}
            if before != after:
                raise ValueError("Migration rerun changed existing records")
    finally:
        await source.dispose()
        await target.dispose()
    # A hard link publishes atomically and fails if the destination appeared.
    os.link(partial, destination)
    partial.unlink()
    return {"recovered": True, "schema_versions": versions, "tables": len(counts),
            "records": sum(counts.values()), "row_hashes_match": True, "migration_idempotent": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    # Metadata/migrations need a settings shape, never an actual Telegram token.
    os.environ.setdefault("BOT_TOKEN", "42:recovery-unused")
    os.environ.setdefault("ADMIN_ID", "42")
    source = os.environ.get("RECOVERY_DATABASE_URL", "")
    try:
        print(json.dumps(asyncio.run(export_copy(source, args.output))))
    except Exception as error:
        print(json.dumps({"recovered": False, "error_class": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
