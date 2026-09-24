import os
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from database.engine import normalize_database_url
from database.models import Assignment, Base, MemoryFact, User
from database.notification_models import NotificationLog


@pytest.mark.parametrize("prefix", ["postgres://", "postgresql://", "postgresql+asyncpg://"])
def test_postgres_urls_use_async_driver(prefix):
    assert normalize_database_url(prefix + "localhost/test") == "postgresql+asyncpg://localhost/test"


def test_all_models_and_indexes_compile_for_postgres():
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))
    assert "BIGINT" in str(CreateTable(User.__table__).compile(dialect=dialect))
    assert "uq_notification_user_event" in str(CreateTable(NotificationLog.__table__).compile(dialect=dialect))


async def check_persistence(url):
    """Independent engines model application restart, without dropping tables."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            # Model an existing schema before the audit's additive table.
            await connection.run_sync(lambda conn: Base.metadata.create_all(
                conn, tables=[t for t in Base.metadata.sorted_tables if t.name != "notification_log"],
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(User(telegram_id=9_000_000_042, name="Synthetic test"))
            await session.commit()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all([
                MemoryFact(user_id=9_000_000_042, key="test", value="remembered"),
                Assignment(user_id=9_000_000_042, title="Persisted", due_at=datetime(2030, 1, 8)),
                NotificationLog(user_id=9_000_000_042, event_key="persisted", kind="audit"),
            ])
            await session.commit()
    finally:
        await engine.dispose()
    restarted = create_async_engine(url)
    try:
        # Startup is idempotent and does not delete state.
        async with restarted.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(restarted)() as session:
            assert (await session.get(User, 9_000_000_042)).name == "Synthetic test"
            assert (await session.scalar(select(MemoryFact))).value == "remembered"
            assert (await session.scalar(select(Assignment))).title == "Persisted"
            assert (await session.scalar(select(NotificationLog))).kind == "audit"
    finally:
        await restarted.dispose()


async def test_file_database_survives_engine_restart(tmp_path):
    await check_persistence("sqlite+aiosqlite:///" + (tmp_path / "persistence.db").as_posix())


@pytest.mark.postgres
async def test_postgres_survives_engine_restart():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires disposable loopback PostgreSQL; CI supplies it")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "::1"} and parsed.database == "jarvis_audit_test"
    await check_persistence(url)
