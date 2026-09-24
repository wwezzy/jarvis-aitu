from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.engine import async_session_factory
from database.notification_models import NotificationLog


async def claim_notification(
    user_id: int,
    *,
    event_key: str,
    kind: str,
    message: str | None = None,
) -> bool:
    """Return True exactly once for a user/event key.

    This makes scheduler restarts and overlapping jobs idempotent.
    """
    async with async_session_factory() as db:
        result = await db.execute(
            select(NotificationLog.id).where(
                NotificationLog.user_id == user_id,
                NotificationLog.event_key == event_key,
            )
        )
        if result.scalar_one_or_none() is not None:
            return False

        db.add(
            NotificationLog(
                user_id=user_id,
                event_key=event_key[:255],
                kind=kind[:40],
                message=(message or "")[:4000] or None,
                sent_at=datetime.now(),
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return False
        return True
