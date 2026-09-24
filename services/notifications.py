from __future__ import annotations

import logging
from datetime import datetime

from config import get_settings
from database.models import ScheduleEntry

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.engine import async_session_factory
from database.notification_models import NotificationLog


settings = get_settings()
logger = logging.getLogger(__name__)


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
    if not event_key or len(event_key) > 255 or not kind or len(kind) > 40:
        raise ValueError("Invalid notification identity")
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
            existing = await db.scalar(select(NotificationLog.id).where(
                NotificationLog.user_id == user_id, NotificationLog.event_key == event_key,
            ))
            if existing is None:
                raise
            return False
        return True


async def is_dnd(user_id: int, now: datetime) -> bool:
    local = now.astimezone(settings.timezone) if now.tzinfo else now
    clock = local.strftime("%H:%M")
    # Baseline sleep window covers midnight; timetable sleep rows stop at 23:59.
    if clock >= "23:30" or clock < "07:00":
        return True
    async with async_session_factory() as db:
        rows = (await db.scalars(select(ScheduleEntry).where(
            ScheduleEntry.user_id == user_id, ScheduleEntry.enabled.is_(True),
            ScheduleEntry.weekday == local.weekday(),
            ScheduleEntry.start <= clock, ScheduleEntry.end > clock,
        ))).all()
    return any(
        r.category in {"training", "sleep", "deep_work"}
        or (r.category == "study" and r.block_type == "fixed")
        for r in rows
    )


async def notify_once(bot, user_id: int, event_key: str, text: str, *,
                      now: datetime | None = None, critical: bool = False, **kwargs) -> bool:
    now = now or datetime.now(settings.timezone)
    if not critical and await is_dnd(user_id, now):
        return False
    if not await claim_notification(user_id, event_key=event_key,
                                    kind=event_key.split(":", 1)[0], message=text):
        return False
    try:
        await bot.send_message(user_id, text, **kwargs)
    except Exception as exc:
        # Claim intentionally remains: Telegram may have accepted before a timeout.
        logger.warning("Notification delivery uncertain error=%s", type(exc).__name__)
        return False
    return True
