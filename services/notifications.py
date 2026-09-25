from __future__ import annotations

import logging
from datetime import datetime, timedelta

from config import get_settings
from database.models import Assignment

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from database.engine import async_session_factory
from database.notification_models import NotificationLog
from database.time import utcnow, aware
from database.v4_models import NotificationDelivery


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
                sent_at=utcnow(),
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
    from services.preferences import get_preferences
    from services.schedule import resolve_day
    local = aware(now)
    prefs = await get_preferences(user_id)
    clock = local.strftime("%H:%M")
    sleeping = (clock >= prefs.dnd_start or clock < prefs.dnd_end) if prefs.dnd_start > prefs.dnd_end else prefs.dnd_start <= clock < prefs.dnd_end
    if sleeping:
        return True
    blocks = await resolve_day(user_id, local.date())
    return any(b['start'] <= clock < b['end'] and (
        b['category'] in {'training', 'sleep', 'deep_work'}
        or (b['category'] == 'study' and b['block_type'] == 'fixed')) for b in blocks)


def notification_buttons(row):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = []
    if row.task_id:
        buttons.append(InlineKeyboardButton(text="Done", callback_data=f"notice:done:{row.id}"))
    buttons.extend([
        InlineKeyboardButton(text="Snooze 1h", callback_data=f"notice:snooze:{row.id}"),
        InlineKeyboardButton(text="Reschedule", callback_data=f"notice:reschedule:{row.id}"),
        InlineKeyboardButton(text="Mute", callback_data=f"notice:mute:{row.id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


async def _still_relevant(row, now):
    if row.expires_at and aware(row.expires_at) <= aware(now):
        return False
    if row.task_id:
        async with async_session_factory() as db:
            task = await db.get(Assignment, row.task_id)
            if not task or task.user_id != row.user_id or task.status != "pending":
                return False
            if row.event_key.startswith("deadline:") and (task.due_at is None or not row.event_key.startswith(
                    f"deadline:{task.id}:{task.due_at.isoformat()}:")):
                return False
    if row.event_key.startswith("schedule:"):
        from datetime import date
        from services.schedule import resolve_day
        parts = row.event_key.split(":")
        if len(parts) == 6:
            blocks = await resolve_day(row.user_id, date.fromisoformat(parts[2]))
            if not any(str(b["id"]).replace(":", "@") == parts[1] and b["start"] == f"{parts[3]}:{parts[4]}" for b in blocks):
                return False
    return True


async def deliver(bot, delivery_id, *, now=None, **kwargs):
    from services.preferences import get_preferences
    now = aware(now or datetime.now(settings.timezone))
    async with async_session_factory() as db:
        row = await db.get(NotificationDelivery, delivery_id)
        if not row or row.status != "queued" or aware(row.scheduled_at) > now:
            return False
        prefs = await get_preferences(row.user_id)
        if row.kind in prefs.muted_kinds or (row.kind == 'quizopen' and not prefs.quiz_open_notices) or not await _still_relevant(row, now):
            row.status = "cancelled"
            await db.commit()
            return False
        if not row.critical and await is_dnd(row.user_id, now):
            return False
        claim = await db.execute(update(NotificationDelivery).where(
            NotificationDelivery.id == delivery_id, NotificationDelivery.status == "queued"
        ).values(status="sending", attempts=NotificationDelivery.attempts + 1))
        await db.commit()
        if claim.rowcount != 1:
            return False
        user_id, key, kind, body = row.user_id, row.event_key, row.kind, row.text
        markup = notification_buttons(row)
    # Existing v3 claims prevent duplicate sends after migration/restart.
    if not await claim_notification(user_id, event_key=key, kind=kind, message=body):
        status, error = "uncertain", "previous_claim"
    else:
        try:
            await bot.send_message(user_id, body, **({"reply_markup": markup} | kwargs))
            status, error = "sent", None
        except Exception as exc:
            # Telegram has no idempotency token: a timeout might mean delivered.
            # Keep it visible as uncertain; automatic retry could duplicate it.
            status, error = "uncertain", type(exc).__name__
            logger.warning("Notification delivery uncertain error=%s", error)
    async with async_session_factory() as db:
        await db.execute(update(NotificationDelivery).where(NotificationDelivery.id == delivery_id).values(
            status=status, last_error=error))
        if status == 'sent' and key.startswith('reminder:'):
            from database.models import Reminder
            await db.execute(update(Reminder).where(Reminder.id == int(key.split(':')[1]),
                Reminder.user_id == user_id).values(is_sent=True))
        await db.commit()
    return status == "sent"


async def notify_once(bot, user_id: int, event_key: str, text: str, *,
                      now: datetime | None = None, critical: bool = False,
                      expires_at=None, **kwargs) -> bool:
    now = aware(now or datetime.now(settings.timezone))
    kind = event_key.split(":", 1)[0]
    if not event_key or len(event_key) > 255 or len(kind) > 40:
        raise ValueError("Invalid notification identity")
    task_id = int(event_key.split(":")[1]) if kind == "deadline" else None
    async with async_session_factory() as db:
        row = await db.scalar(select(NotificationDelivery).where(
            NotificationDelivery.user_id == user_id, NotificationDelivery.event_key == event_key))
        if row is None:
            row = NotificationDelivery(user_id=user_id, event_key=event_key, kind=kind,
                text=text[:4000], task_id=task_id, scheduled_at=now,
                expires_at=expires_at or now + timedelta(days=1), critical=critical)
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                row = await db.scalar(select(NotificationDelivery).where(
                    NotificationDelivery.user_id == user_id, NotificationDelivery.event_key == event_key))
                if row is None:
                    raise
        if critical and row.status == "queued" and not row.critical:
            row.critical = True
            await db.commit()
        delivery_id = row.id
    return await deliver(bot, delivery_id, now=now, **kwargs)


async def flush_queued(bot, user_id, now=None):
    now = aware(now or datetime.now(settings.timezone))
    async with async_session_factory() as db:
        rows = list(await db.scalars(select(NotificationDelivery.id).where(
            NotificationDelivery.user_id == user_id, NotificationDelivery.status == "queued",
            NotificationDelivery.scheduled_at <= now).order_by(NotificationDelivery.critical.desc(), NotificationDelivery.scheduled_at).limit(3)))
    return sum([await deliver(bot, row_id, now=now) for row_id in rows])


async def notification_feedback(user_id, notice_id, action, now=None):
    from services.preferences import get_preferences, save_preferences
    from services.assignments import mark_assignment_done
    now = aware(now or datetime.now(settings.timezone))
    async with async_session_factory() as db:
        row = await db.get(NotificationDelivery, notice_id)
        if not row or row.user_id != user_id:
            raise ValueError("Notification not found")
        task_id, kind = row.task_id, row.kind
        if action == "snooze":
            # User explicitly authorizes a new delivery; original history stays.
            key = f"snooze:{notice_id}"
            previous = await db.scalar(select(NotificationDelivery.id).where(
                NotificationDelivery.user_id == user_id, NotificationDelivery.event_key == key))
            if previous is None:
                db.add(NotificationDelivery(user_id=user_id, event_key=key, kind=kind,
                    text=row.text, task_id=task_id, scheduled_at=now + timedelta(hours=1),
                    expires_at=now + timedelta(days=1)))
            await db.commit()
            return "Отложено на час."
        if action == "reschedule":
            return f"Открой задачу #{task_id} в Mini App и измени срок." if task_id else "Используй /remind ДАТА ВРЕМЯ | текст или Week для события."
    if action == "done" and task_id:
        await mark_assignment_done(user_id, task_id)
        return "Задача завершена, будущие уведомления отменены."
    if action == "mute":
        prefs = await get_preferences(user_id)
        await save_preferences(user_id, {"muted_kinds": sorted(set(prefs.muted_kinds + [kind]))})
        return f"Уведомления {kind} отключены. Вернуть можно в настройках."
    raise ValueError("Unsupported notification action")
