from __future__ import annotations


from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import get_settings
from database.engine import async_session_factory
from services.assignments import (
    assignment_counts,
    format_deadlines,
    list_upcoming_assignments,
    lms_status,
    mark_assignment_done,
    sync_lms_ical,
)
from services.scheduler import sync_assignment_jobs

router = Router(name="assignments")
settings = get_settings()


def _authorized(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == settings.admin_id)


@router.message(Command("deadlines"))
async def deadlines_command(message: Message) -> None:
    if not _authorized(message):
        return
    items = await list_upcoming_assignments(
        message.from_user.id, days=30, include_unknown=True, limit=30
    )
    await message.answer(format_deadlines(items)[:4096])


@router.message(Command("done"))
async def done_command(message: Message, bot: Bot, scheduler: AsyncIOScheduler) -> None:
    if not _authorized(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        from handlers.assistant import assistant_message
        from services.llm import redis
        await assistant_message(message, bot, scheduler, pc_redis=redis)
        return
    try:
        assignment_id = int(parts[1].strip())
        row = await mark_assignment_done(message.from_user.id, assignment_id)
    except (TypeError, ValueError):
        await message.answer("Не нашёл такое задание. Пример: /done 12")
        return
    await sync_assignment_jobs(scheduler, bot, message.from_user.id)
    await message.answer(f"✅ #{row['id']} закрыто: {row['title']}")


@router.message(Command("lms_sync"))
async def lms_sync_command(message: Message, bot: Bot, scheduler: AsyncIOScheduler) -> None:
    if not _authorized(message):
        return
    if not settings.lms_ical_url:
        await message.answer(
            "LMS ещё не подключён. Нужна персональная Moodle/iCal ссылка в LMS_ICAL_URL."
        )
        return
    status = await message.answer("Синхронизирую LMS…")
    try:
        result = await sync_lms_ical(message.from_user.id)
        jobs = await sync_assignment_jobs(scheduler, bot, message.from_user.id)
        await status.edit_text(
            "✅ LMS sync завершён.\n"
            f"Новых: {result.get('created', 0)}\n"
            f"Обновлено: {result.get('updated', 0)}\n"
            f"Событий в feed: {result.get('events_seen', 0)}\n"
            f"Deadline-уведомлений: {jobs}"
        )
    except Exception as exc:
        await status.edit_text(
            f"❌ LMS sync: {type(exc).__name__}. Подробность сохранена в /diag."
        )


@router.message(Command("diag"))
async def diag_command(message: Message, scheduler: AsyncIOScheduler) -> None:
    if not _authorized(message):
        return
    import json
    from services.diagnostics import diagnostics
    from services.telegram_text import chunks
    data = await diagnostics(message.from_user.id, scheduler, factory=async_session_factory,
                             lms_reader=lms_status, counts_reader=assignment_counts)
    pc = data.get("pc", {})
    heading = f"Database: {data['database']['type']} · {data['database']['status']}\n"
    heading += "PC agent: heartbeat unavailable\n" if not pc.get("online") else "PC agent: online\n"
    if data.get("lms", {}).get("last_sync_failed"):
        heading += "LMS: last sync failed\n"
    for part in chunks(heading + json.dumps(data, ensure_ascii=False, indent=2)):
        await message.answer(part)
