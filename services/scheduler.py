from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import get_settings
from database.engine import async_session_factory
from database.models import Reminder
from services.assignments import format_deadlines, list_upcoming_assignments, sync_lms_ical
from services.schedule import list_schedule_entries

logger = logging.getLogger(__name__)
settings = get_settings()

_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _notification_trigger(weekday: int, start: str, minutes_before: int) -> tuple[str, int, int]:
    base = datetime(2026, 1, 5) + timedelta(days=weekday)
    hour, minute = [int(part) for part in start.split(":", 1)]
    run_at = base.replace(hour=hour, minute=minute) - timedelta(minutes=minutes_before)
    return _DOW[run_at.weekday()], run_at.hour, run_at.minute


async def send_schedule_notice(bot: Bot, user_id: int, start: str, title: str, minutes_before: int) -> None:
    await bot.send_message(user_id, f"⏱ Через {minutes_before} мин\n{start} — {title}")


async def send_gtg_prompt(bot: Bot, user_id: int) -> None:
    from handlers.gtg import gtg_keyboard
    await bot.send_message(user_id, "GTG checkpoint. Один чистый субмаксимальный подход, без отказа.", reply_markup=gtg_keyboard())


async def send_detox_signal(bot: Bot, user_id: int) -> None:
    await bot.send_message(user_id, "23:00 — Screen Detox. Закрыть лишние экраны и завершить активные задачи.")


async def send_reflection_prompt(bot: Bot, user_id: int) -> None:
    markup = None
    if settings.webapp_url:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть вечернюю рефлексию", web_app=WebAppInfo(url=f"{settings.webapp_url}/app#reflection"))]])
    await bot.send_message(user_id, "22:30 — рефлексия: Deep Work, питание/белок, RIR, настроение и энергия.", reply_markup=markup)


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
        result = await db.execute(select(Reminder).where(Reminder.is_sent.is_(False), Reminder.remind_at > now))
        rows = result.scalars().all()
    for row in rows:
        scheduler.add_job(send_saved_reminder, "date", run_date=row.remind_at, args=[bot, row.user_id, row.text, row.id], id=f"reminder:{row.id}", replace_existing=True, misfire_grace_time=300)
    return len(rows)


def _clear_dynamic_schedule_jobs(scheduler: AsyncIOScheduler) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("schedule:block:"):
            scheduler.remove_job(job.id)


async def sync_schedule_jobs(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> int:
    _clear_dynamic_schedule_jobs(scheduler)
    rows = await list_schedule_entries(user_id)
    count = 0
    for row in rows:
        if row.notify_before_min is None or row.category in {"reflection", "sleep"}:
            continue
        dow, hour, minute = _notification_trigger(row.weekday, row.start, row.notify_before_min)
        scheduler.add_job(send_schedule_notice, "cron", day_of_week=dow, hour=hour, minute=minute, args=[bot, user_id, row.start, row.title, row.notify_before_min], id=f"schedule:block:{row.id}", replace_existing=True, misfire_grace_time=600)
        count += 1
    return count


def _clear_assignment_jobs(scheduler: AsyncIOScheduler) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("deadline:item:"):
            scheduler.remove_job(job.id)


async def send_assignment_notice(bot: Bot, user_id: int, assignment_id: int, title: str, course: str | None, due_at: str, minutes_before: int) -> None:
    course_text = f"[{course}] " if course else ""
    lead = "через сутки" if minutes_before >= 1440 else (f"через {minutes_before // 60} ч" if minutes_before >= 60 else f"через {minutes_before} мин")
    await bot.send_message(user_id, f"📚 Дедлайн {lead}\n#{assignment_id} · {course_text}{title}\nСдать до {due_at}")


async def sync_assignment_jobs(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> int:
    _clear_assignment_jobs(scheduler)
    now = datetime.now(settings.timezone).replace(tzinfo=None)
    items = await list_upcoming_assignments(user_id, now=now.replace(tzinfo=settings.timezone), days=45, include_unknown=False, limit=200)
    count = 0
    for item in items:
        due_text = item.get("due_at")
        if not due_text:
            continue
        try:
            due_at = datetime.fromisoformat(due_text)
        except ValueError:
            continue
        for minutes_before in (1440, 180, 30):
            run_at = due_at - timedelta(minutes=minutes_before)
            if run_at <= now:
                continue
            scheduler.add_job(send_assignment_notice, "date", run_date=run_at, args=[bot, user_id, item["id"], item["title"], item.get("course"), due_at.strftime("%d.%m %H:%M"), minutes_before], id=f"deadline:item:{item['id']}:{minutes_before}", replace_existing=True, misfire_grace_time=900)
            count += 1
    return count


async def send_deadline_digest(bot: Bot, user_id: int) -> None:
    now = datetime.now(settings.timezone)
    items = await list_upcoming_assignments(user_id, now=now, days=7, include_unknown=True, limit=20)
    if items:
        await bot.send_message(user_id, format_deadlines(items, title="☀️ Дедлайны на ближайшие 7 дней"))


async def sync_lms_and_notify(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> None:
    if not settings.lms_ical_url:
        return
    try:
        result = await sync_lms_ical(user_id)
        assignment_jobs = await sync_assignment_jobs(scheduler, bot, user_id)
        logger.info("LMS sync complete: created=%s updated=%s events=%s deadline_jobs=%s", result.get("created"), result.get("updated"), result.get("events_seen"), assignment_jobs)
        changed = result.get("changed") or []
        if changed:
            lines = ["📚 LMS обновил дедлайны:"]
            for item in changed[:8]:
                due = item.get("due_at")
                if due:
                    try:
                        due_text = datetime.fromisoformat(due).strftime("%d.%m %H:%M")
                    except ValueError:
                        due_text = due
                else:
                    due_text = "дата не указана"
                course = f"[{item['course']}] " if item.get("course") else ""
                lines.append(f"• {due_text} · {course}{item['title']}")
            await bot.send_message(user_id, "\n".join(lines)[:4096])
    except Exception:
        logger.exception("Scheduled LMS sync failed")


async def register_master_schedule(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> int:
    block_jobs = 0
    if settings.enable_master_schedule:
        block_jobs = await sync_schedule_jobs(scheduler, bot, user_id)
        scheduler.add_job(send_gtg_prompt, "cron", day_of_week="tue,thu", hour="9,12,18,21", minute=0, args=[bot, user_id], id="schedule:gtg", replace_existing=True, misfire_grace_time=600)
        scheduler.add_job(send_reflection_prompt, "cron", hour=22, minute=30, args=[bot, user_id], id="schedule:reflection", replace_existing=True, misfire_grace_time=600)
        scheduler.add_job(send_detox_signal, "cron", hour=23, minute=0, args=[bot, user_id], id="schedule:detox", replace_existing=True, misfire_grace_time=600)
    deadline_jobs = await sync_assignment_jobs(scheduler, bot, user_id)
    logger.info("Registered %s deadline reminder jobs", deadline_jobs)
    scheduler.add_job(send_deadline_digest, "cron", hour=8, minute=10, args=[bot, user_id], id="deadline:digest", replace_existing=True, misfire_grace_time=1800)
    if settings.lms_ical_url:
        scheduler.add_job(sync_lms_and_notify, "interval", minutes=settings.lms_sync_minutes, args=[scheduler, bot, user_id], id="lms:sync", replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=600)
    return block_jobs
