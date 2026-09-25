import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.migrations import upgrade
from database.models import Assignment, User
from database.time import aware
from database.v4_models import LmsEvent


async def migration_roundtrip(engine):
    async with engine.begin() as conn:
        # Exact v3 column shape for the tables whose old records are exercised.
        await conn.execute(text("""CREATE TABLE users (
            telegram_id BIGINT PRIMARY KEY, name VARCHAR(255) NOT NULL,
            preferences TEXT, registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"""))
        await conn.execute(text("""CREATE TABLE assignments (
            id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(telegram_id),
            source VARCHAR(32) NOT NULL, external_id VARCHAR(255), course VARCHAR(180),
            title VARCHAR(255) NOT NULL, due_at TIMESTAMP, status VARCHAR(24) NOT NULL,
            url TEXT, notes TEXT, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,
            CONSTRAINT uq_assignment_source_external UNIQUE(user_id, source, external_id))"""))
        await conn.execute(text("INSERT INTO users (telegram_id,name) VALUES (42,'Test')"))
        await conn.execute(text("""INSERT INTO assignments
            (id,user_id,source,external_id,title,due_at,status,created_at,updated_at)
            VALUES (1,42,'lms_ical','one','Legacy done task',:due,'done',:created,:created)"""),
            {"due": datetime(2030, 1, 8, 15), "created": datetime(2029, 1, 1, 10)})
        await conn.run_sync(upgrade)
        await conn.run_sync(upgrade)
        raw = (await conn.execute(text("SELECT due_at, created_at FROM assignments"))).one()
        assert str(raw[0]).startswith("2030-01-08 10:00")  # UTC, shifted exactly once
        assert str(raw[1]).startswith("2029-01-01 10:00")  # server timestamp already UTC
        assert await conn.scalar(text("SELECT count(*) FROM jarvis_schema_version")) == 2
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert "notification_delivery" in names and "lms_events" in names
    async with async_sessionmaker(engine)() as session:
        task = await session.get(Assignment, 1)
        assert task.status == "done" and task.due_at == aware(datetime(2030, 1, 8, 15))
        assert task.estimated_minutes is None and task.progress == 0
        assert (await session.get(User, 42)).name == "Test"
        assert list(await session.scalars(select(LmsEvent))) == []


async def test_existing_sqlite_migrates_non_destructively(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "legacy.db").as_posix())
    try:
        await migration_roundtrip(engine)
    finally:
        await engine.dispose()


async def test_fresh_database_and_future_version_guard(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "fresh.db").as_posix())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(upgrade)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(User(telegram_id=42, name="Test"))
            await session.commit()
            session.add(Assignment(user_id=42, title="Aware", due_at=datetime(2030, 1, 8, 10, tzinfo=timezone.utc)))
            await session.commit()
        async with engine.begin() as conn:
            await conn.run_sync(upgrade)
            assert str(await conn.scalar(text("SELECT due_at FROM assignments"))).startswith("2030-01-08 10:00")
            await conn.execute(text("INSERT INTO jarvis_schema_version VALUES (999)"))
        with pytest.raises(RuntimeError, match="refusing downgrade"):
            async with engine.begin() as conn:
                await conn.run_sync(upgrade)
    finally:
        await engine.dispose()


@pytest.mark.postgres
async def test_postgres_v3_migration():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("CI supplies disposable PostgreSQL 18")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "::1"} and parsed.database == "jarvis_audit_test"
    schema = "migration_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}})
    try:
        await migration_roundtrip(engine)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
