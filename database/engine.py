from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import get_settings
import database.notification_models  # noqa: F401  # register v3 notification tables
import database.v4_models  # noqa: F401

settings = get_settings()
def normalize_database_url(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


DATABASE_URL = normalize_database_url(settings.database_url)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True,
    connect_args={"server_settings": {"timezone": "UTC"}} if DATABASE_URL.startswith("postgresql") else {})
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from database.migrations import upgrade
    async with engine.begin() as conn:
        await conn.run_sync(upgrade)
