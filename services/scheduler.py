from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import get_settings
from database.engine import async_session_factory
from database.models import Reminder
from services.schedule import DAY_A, DAY_B

settings = get_settings()


def _minus_30(time_str: str) -> tuple[int, int]:
    value = datetime.strptime(time_str, "%H:%M") - timedelta(minutes=30)
    return value.hour, value.minute


async def send_schedule_notice(bot: Bot, user_id: int, start: str, title: str) -> None:
    await bot.send_message(user_id, f"⏱ Через 30 минут\n{start} — {title}")


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


def _register_block_notices(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    user_id: int,
    *,
    weekdays: str,
    label: str,
    blocks,
) -> None:
    for index, block in enumerate(blocks):
        if block.category == "sleep":
            continue
        hour, minute = _minus_30(block.start)
        scheduler.add_job(
            send_schedule_notice,
            "cron",
            day_of_week=weekdays,
            hour=hour,
            minute=minute,
            args=[bot, user_id, block.start, block.title],
            id=f"schedule:{label}:{index}",
            replace_existing=True,
            misfire_grace_time=600,
        )


def register_master_schedule(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> None:
    if not settings.enable_master_schedule:
        return

    _register_block_notices(scheduler, bot, user_id, weekdays="tue,thu", label="A", blocks=DAY_A)
    _register_block_notices(scheduler, bot, user_id, weekdays="mon,wed", label="B", blocks=DAY_B)

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
