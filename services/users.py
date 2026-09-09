from __future__ import annotations

from sqlalchemy import select

from database.engine import async_session_factory
from database.models import User


async def ensure_user(user_id: int, name: str) -> User:
    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=user_id, name=name)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        elif name and user.name != name:
            user.name = name
            await session.commit()
        return user
