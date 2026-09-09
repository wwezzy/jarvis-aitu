from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import get_settings
from database.engine import async_session_factory
from database.models import Reminder
from services.schedule import list_schedule_entries

settings = get_settings()

_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _notification_trigger(weekday: int, start: str, minutes_before: int) -> tuple[str, int, int]:
    # 2026-01-05 is a Monday. Using a reference week makes previous-day rollover safe.
    base = datetime(2026, 1, 5) + timedelta(days=weekday)
    hour, minute = [int(part) for part in start.split(":", 1)]
    run_at = base.replace(hour=hour, minute=minute) - timedelta(minutes=minutes_before)
    return _DOW[run_at.weekday()], run_at.hour, run_at.minute


async def send_schedule_notice(
    bot: Bot,
    user_id: int,
    start: str,
    title: str,
    minutes_before: int,
) -> None:
    await bot.send_message(user_id, f"⏱ Через {minutes_before} мин\n{start} — {title}")


async def send_gtg_prompt(bot: Bot, user_id: int) -> None:
    from handlers.gtg import gtg_keyboard

    await bot.send_message(
        user_id,
        "GTG checkpoint. Один чистый субмаксимальный подход, без отказа.",
        reply_markup=gtg_keyboard(),
    )


async def send_detox_signal(bot: Bot, user_id: int) -> None:
    await bot.send_message(user_id, "23:00 — Screen Detox. Закрыть лишние экраны и завершить активные задачи.")


async def send_reflection_prompt(bot: Bot, user_id: int) -> None:
    markup = None
    if settings.webapp_url:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть вечернюю рефлексию",
                        web_app=WebAppInfo(url=f"{settings.webapp_url}/app#reflection"),
                    )
                ]
            ]
        )
    await bot.send_message(
        user_id,
        "22:30 — рефлексия: Deep Work, питание/белок, RIR, настроение и энергия.",
        reply_markup=markup,
    )


async def send_saved_reminder(bot: Bot, user_id: int, text: str, reminder_id: int) -> None:
    await bot.send_message(user_id, f"🔔 Напоминание\n{text}")
    async with async_session_factory() as db:
        result = await db.execute(select(Reminder).where(Reminder.id == reminder_id))
        row = result.scalar_one_or_none()
        if row is not None:
            row.is_sent = True
            await db.commit()


async def restore_pending_reminders(scheduler: AsyncIOScheduler, bot: Bot) -> int:
    now = datetime.now(settings.timezone).replace(tzinfo=None)
    async with async_session_factory() as db:
        result = await db.execute(
            select(Reminder).where(Reminder.is_sent.is_(False), Reminder.remind_at > now)
        )
        rows = result.scalars().all()

    for row in rows:
        scheduler.add_job(
            send_saved_reminder,
            "date",
            run_date=row.remind_at,
            args=[bot, row.user_id, row.text, row.id],
            id=f"reminder:{row.id}",
            replace_existing=True,
            misfire_grace_time=300,
        )
    return len(rows)


def _clear_dynamic_schedule_jobs(scheduler: AsyncIOScheduler) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("schedule:block:"):
            scheduler.remove_job(job.id)


async def sync_schedule_jobs(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> int:
    """Rebuild block notifications from the SQL schedule after every edit."""
    _clear_dynamic_schedule_jobs(scheduler)
    rows = await list_schedule_entries(user_id)
    count = 0
    for row in rows:
        if row.notify_before_min is None:
            continue
        # Dedicated jobs handle these to avoid duplicate messages.
        if row.category in {"reflection", "sleep"}:
            continue
        dow, hour, minute = _notification_trigger(row.weekday, row.start, row.notify_before_min)
        scheduler.add_job(
            send_schedule_notice,
            "cron",
            day_of_week=dow,
            hour=hour,
            minute=minute,
            args=[bot, user_id, row.start, row.title, row.notify_before_min],
            id=f"schedule:block:{row.id}",
            replace_existing=True,
            misfire_grace_time=600,
        )
        count += 1
    return count


async def register_master_schedule(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> int:
    if not settings.enable_master_schedule:
        return 0

    block_jobs = await sync_schedule_jobs(scheduler, bot, user_id)

    # GtG remains deliberately separate from the daily calendar.
    scheduler.add_job(
        send_gtg_prompt,
        "cron",
        day_of_week="tue,thu",
        hour="9,12,18,21",
        minute=0,
        args=[bot, user_id],
        id="schedule:gtg",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        send_reflection_prompt,
        "cron",
        hour=22,
        minute=30,
        args=[bot, user_id],
        id="schedule:reflection",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        send_detox_signal,
        "cron",
        hour=23,
        minute=0,
        args=[bot, user_id],
        id="schedule:detox",
        replace_existing=True,
        misfire_grace_time=600,
    )
    return block_jobs
