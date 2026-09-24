"""Offline tests: never inherit credentials or a developer's database."""
import os
import socket

import pytest
import pytest_asyncio
import aiohttp
import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

for name in list(os.environ):
    if name.startswith(("GEMINI_", "OPENAI_", "NVIDIA_", "UPSTASH_", "LMS_", "PC_AGENT_")):
        os.environ.pop(name)
os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    BOT_TOKEN="42:offline-test-placeholder",
    ADMIN_ID="42",
    GEMINI_API_KEY="primary",
    GEMINI_API_KEY_2="backup",
    DATABASE_URL="sqlite+aiosqlite:///:memory:",
    TIMEZONE="Asia/Almaty",
    MINIAPP_DEV_MODE="false",
    JARVIS_PROFILE_FILE="",
)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded(original):
        def call(sock, address):
            # Windows asyncio constructs its wakeup socketpair through loopback.
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original(sock, address)
            raise AssertionError("External network disabled: mock the transport")
        return call

    monkeypatch.setattr(socket.socket, "connect", guarded(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(connect_ex))

    async def deny_async(*args, **kwargs):
        raise AssertionError("HTTP disabled: mock the transport")

    def deny_sync(*args, **kwargs):
        raise AssertionError("HTTP disabled: mock the transport")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", deny_async)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny_async)
    monkeypatch.setattr(httpx.Client, "send", deny_sync)


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    from database import engine as database_engine
    from database.models import Base, User
    from services import assignments, memory, notifications, schedule, scheduler
    from handlers import assistant

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "test.db").as_posix())

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_engine, "async_session_factory", factory)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all([User(telegram_id=42, name="Test"), User(telegram_id=43, name="Other")])
        await session.commit()
    for module in (assignments, memory, notifications, schedule, scheduler, assistant):
        monkeypatch.setattr(module, "async_session_factory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()
