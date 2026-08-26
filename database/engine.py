from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from database.models import Base

engine = create_async_engine("sqlite+aiosqlite:///jarvis.db", echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)