from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import get_settings
from database.engine import async_session_factory
from database.models import Assignment, Reminder, ScheduleEntry
from database.time import aware
from services.assignments import list_upcoming_assignments, sync_lms_ical
from services.notifications import claim_notification, is_dnd, notify_once
from services.schedule import list_schedule_entries, today_schedule, week_schedule

logger = logging.getLogger(__name__)
settings = get_settings()

_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _notification_trigger(weekday: int, start: str, minutes_before: int) -> tuple[str, int, int]:
    base = datetime(2026, 1, 5) + timedelta(days=weekday)
    hour, minute = [int(part) for part in start.split(":", 1)]
    run_at = base.replace(hour=hour, minute=minute) - timedelta(minutes=minutes_before)
    return _DOW[run_at.weekday()], run_at.hour, run_at.minute


async def _in_dnd(user_id: int, now: datetime | None = None) -> bool:
    return await is_dnd(user_id, now or datetime.now(settings.timezone))


async def send_schedule_notice(
    bot: Bot,
    user_id: int,
    entry_id: int,
    start: str,
    title: str,
    category: str,
    minutes_before: int,
) -> None:
    local = datetime.now(settings.timezone)
    occurrence = local + timedelta(minutes=minutes_before)
    async with async_session_factory() as db:
        row = await db.get(ScheduleEntry, entry_id)
        if (row is None or row.user_id != user_id or not row.enabled or row.start != start
                or row.title != title or row.category != category
                or row.weekday != occurrence.weekday() or _smart_schedule_lead(row) != minutes_before):
            return
    event_key = f"schedule:{entry_id}:{occurrence.date()}:{start}:{minutes_before}"
    message = f"⏱ Через {minutes_before} мин\n{start} — {title}"
    if category == "training":
        message += "\nПодготовь форму, воду и оставь время на разминку."
    await notify_once(bot, user_id, event_key, message, now=local)


async def send_saved_reminder(bot: Bot, user_id: int, text: str, reminder_id: int) -> None:
    async with async_session_factory() as db:
        row = await db.get(Reminder, reminder_id)
        if row is None or row.user_id != user_id or row.is_sent:
            return
        if await notify_once(bot, user_id, f"reminder:{reminder_id}", f"🔔 Напоминание\n{row.text}"):
            row.is_sent = True
            await db.commit()


async def restore_pending_reminders(scheduler: AsyncIOScheduler, bot: Bot) -> int:
    now = datetime.now(settings.timezone)
    async with async_session_factory() as db:
        result = await db.execute(
            select(Reminder).where(
                Reminder.is_sent.is_(False),
                Reminder.remind_at > now,
            )
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


def _clear_dynamic_schedule_jobs(scheduler: AsyncIOScheduler, user_id: int) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith(f"schedule:block:{user_id}:"):
            scheduler.remove_job(job.id)


def _smart_schedule_lead(row) -> int | None:
    if row.notify_before_min is None:
        return None
    # v3 deliberately does NOT notify for every planned block.
    if row.block_type == "fixed" and row.category == "study":
        return 60 if row.notify_before_min == 30 else row.notify_before_min
    if row.category == "training" and row.block_type != "flex":
        return 60 if row.notify_before_min == 30 else row.notify_before_min

    # Preserve explicit user-created notifications for genuinely important non-routine blocks,
    # but suppress the old default 30-minute spam for ordinary study/flex/routine blocks.
    if row.notify_before_min is not None and row.notify_before_min != 30 and row.category not in {
        "routine",
        "recovery",
        "nutrition",
        "commute",
        "rest",
        "reflection",
        "sleep",
        "flex",
    }:
        return row.notify_before_min
    return None


async def sync_schedule_jobs(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    user_id: int,
) -> int:
    _clear_dynamic_schedule_jobs(scheduler, user_id)
    rows = await list_schedule_entries(user_id)
    count = 0

    for row in rows:
        minutes_before = _smart_schedule_lead(row)
        if minutes_before is None:
            continue

        dow, hour, minute = _notification_trigger(
            row.weekday,
            row.start,
            minutes_before,
        )
        scheduler.add_job(
            send_schedule_notice,
            "cron",
            day_of_week=dow,
            hour=hour,
            minute=minute,
            args=[
                bot,
                user_id,
                row.id,
                row.start,
                row.title,
                row.category,
                minutes_before,
            ],
            id=f"schedule:block:{user_id}:{row.id}",
            replace_existing=True,
            misfire_grace_time=900,
        )
        count += 1
    return count


def _clear_assignment_jobs(scheduler: AsyncIOScheduler, user_id: int) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith(f"deadline:item:{user_id}:"):
            scheduler.remove_job(job.id)


def _assignment_leads(item: dict) -> tuple[int, ...]:
    title = f"{item.get('course') or ''} {item.get('title') or ''}".lower()
    large_markers = (
        "project",
        "проект",
        "assignment",
        "ассайн",
        "report",
        "репорт",
        "lab",
        "лаборатор",
        "sdp",
        "сро",
        "бөж",
    )
    if any(marker in title for marker in large_markers):
        return (4320, 1440, 180)  # 3 days, 24h, 3h
    return (1440, 180)


async def send_assignment_notice(
    bot: Bot,
    user_id: int,
    assignment_id: int,
    title: str,
    course: str | None,
    due_at: str,
    minutes_before: int,
) -> None:
    async with async_session_factory() as db:
        row = await db.get(Assignment, assignment_id)
        if (row is None or row.user_id != user_id or row.status != "pending"
                or row.due_at is None or row.due_at != aware(datetime.fromisoformat(due_at))):
            return
        title, course = row.title, row.course
    event_key = f"deadline:{assignment_id}:{aware(datetime.fromisoformat(due_at)).isoformat()}:{minutes_before}"
    course_text = f"[{course}] " if course else ""
    if minutes_before >= 1440:
        days = minutes_before // 1440
        lead = f"через {days} дн."
    elif minutes_before >= 60:
        lead = f"через {minutes_before // 60} ч"
    else:
        lead = f"через {minutes_before} мин"

    await notify_once(
        bot, user_id, event_key,
        f"📚 Дедлайн {lead}\n"
        f"#{assignment_id} · {course_text}{title}\n"
        f"Сдать до {due_at}",
    )


async def sync_assignment_jobs(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    user_id: int,
) -> int:
    _clear_assignment_jobs(scheduler, user_id)
    now = datetime.now(settings.timezone)
    items = await list_upcoming_assignments(
        user_id,
        now=now.replace(tzinfo=settings.timezone),
        days=45,
        include_unknown=False,
        limit=200,
    )

    count = 0
    for item in items:
        due_text = item.get("due_at")
        if not due_text:
            continue
        try:
            due_at = aware(datetime.fromisoformat(due_text))
        except ValueError:
            continue

        for minutes_before in _assignment_leads(item):
            run_at = due_at - timedelta(minutes=minutes_before)
            if run_at <= now:
                continue
            scheduler.add_job(
                send_assignment_notice,
                "date",
                run_date=run_at,
                args=[
                    bot,
                    user_id,
                    item["id"],
                    item["title"],
                    item.get("course"),
                    due_at.isoformat(),
                    minutes_before,
                ],
                id=f"deadline:item:{user_id}:{item['id']}:{minutes_before}",
                replace_existing=True,
                misfire_grace_time=1800,
            )
            count += 1
    return count


async def send_morning_brief(bot: Bot, user_id: int) -> None:
    if await _in_dnd(user_id):
        return

    local = datetime.now(settings.timezone)
    schedule = await today_schedule(user_id, local)
    deadlines = await list_upcoming_assignments(
        user_id,
        now=local,
        days=3,
        include_unknown=True,
        limit=8,
    )

    important_blocks = [
        block
        for block in schedule["blocks"]
        if block.get("block_type") == "fixed"
        or block.get("category") == "training"
    ]

    lines = [f"☀️ Сегодня · {schedule['weekday_name']}"]
    if important_blocks:
        lines.append("Ключевые события:")
        for block in important_blocks[:6]:
            lines.append(f"• {block['start']}–{block['end']} {block['title']}")
    else:
        lines.append("Жёстких событий сегодня нет.")

    if deadlines:
        lines.append("")
        lines.append("Ближайшие дедлайны:")
        for item in deadlines[:4]:
            due = item.get("due_at") or "дата не уточнена"
            if item.get("due_at"):
                try:
                    due = datetime.fromisoformat(item["due_at"]).strftime("%d.%m %H:%M")
                except ValueError:
                    pass
            course = f"[{item['course']}] " if item.get("course") else ""
            lines.append(f"• {due} · {course}{item['title']}")

    event_key = f"brief:morning:{local.date().isoformat()}"
    message = "\n".join(lines)
    if await claim_notification(
        user_id,
        event_key=event_key,
        kind="morning_brief",
        message=message,
    ):
        await bot.send_message(user_id, message[:4096])


async def send_evening_brief(bot: Bot, user_id: int) -> None:
    if await _in_dnd(user_id):
        return

    local = datetime.now(settings.timezone)
    tomorrow = local.date() + timedelta(days=1)
    week = await week_schedule(user_id)
    day = next(
        (item for item in week["days"] if item["weekday"] == tomorrow.weekday()),
        None,
    )
    blocks = (day or {}).get("blocks") or []
    fixed = [block for block in blocks if block.get("block_type") == "fixed"]
    training = [block for block in blocks if block.get("category") == "training" and block.get("block_type") != "flex"]

    lines = ["🌙 Завтра"]
    if fixed:
        first = min(fixed, key=lambda item: item["start"])
        lines.append(f"Первое обязательное: {first['start']} — {first['title']}")
    else:
        lines.append("Утром обязательных пар нет.")
    if training:
        lines.append(f"Тренировка: {training[0]['start']} — {training[0]['title']}")

    deadlines = await list_upcoming_assignments(
        user_id,
        now=local,
        days=2,
        include_unknown=False,
        limit=6,
    )
    if deadlines:
        lines.append("Проверь ближайшие дедлайны: /deadlines")

    event_key = f"brief:evening:{local.date().isoformat()}"
    message = "\n".join(lines)
    if await claim_notification(
        user_id,
        event_key=event_key,
        kind="evening_brief",
        message=message,
    ):
        await bot.send_message(user_id, message)


async def sync_lms_and_notify(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    user_id: int,
) -> None:
    if not settings.lms_ical_url:
        return
    try:
        result = await sync_lms_ical(user_id)
        assignment_jobs = await sync_assignment_jobs(scheduler, bot, user_id)
        logger.info(
            "LMS sync complete: created=%s updated=%s events=%s deadline_jobs=%s",
            result.get("created"),
            result.get("updated"),
            result.get("events_seen"),
            assignment_jobs,
        )
        # No push on every LMS change: v3 intentionally avoids sync spam.
    except Exception:
        logger.exception("Scheduled LMS sync failed")


async def register_master_schedule(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    user_id: int,
) -> int:
    block_jobs = 0
    if settings.enable_master_schedule:
        block_jobs = await sync_schedule_jobs(scheduler, bot, user_id)

        # Two useful summaries replace the old GTG/reflection/detox spam.
        scheduler.add_job(
            send_morning_brief,
            "cron",
            hour=8,
            minute=15,
            args=[bot, user_id],
            id="brief:morning",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        scheduler.add_job(
            send_evening_brief,
            "cron",
            hour=21,
            minute=45,
            args=[bot, user_id],
            id="brief:evening",
            replace_existing=True,
            misfire_grace_time=1800,
        )

    deadline_jobs = await sync_assignment_jobs(scheduler, bot, user_id)
    logger.info("Registered %s deadline reminder jobs", deadline_jobs)

    if settings.lms_ical_url:
        scheduler.add_job(
            sync_lms_and_notify,
            "interval",
            minutes=settings.lms_sync_minutes,
            args=[scheduler, bot, user_id],
            id="lms:sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )

    return block_jobs
