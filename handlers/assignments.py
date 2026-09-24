from __future__ import annotations

import asyncio
from sqlalchemy import text

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import get_settings
from database.engine import DATABASE_URL, async_session_factory
from services.assignments import (
    assignment_counts,
    format_deadlines,
    list_upcoming_assignments,
    lms_status,
    mark_assignment_done,
    sync_lms_ical,
)
from services.llm import get_llm_diagnostics
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
        await message.answer("Использование: /done 12")
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
    llm = get_llm_diagnostics()
    db_kind = "PostgreSQL" if DATABASE_URL.startswith("postgresql") else "SQLite"
    try:
        async with asyncio.timeout(5):
            async with async_session_factory() as db:
                await db.execute(text("SELECT 1"))
            lms = await lms_status(message.from_user.id)
            counts = await assignment_counts(message.from_user.id)
        database_status = "reachable"
    except Exception as exc:
        database_status = f"unavailable ({type(exc).__name__})"
        lms, counts = None, None
    lines = [
        "🧪 JARVIS DIAGNOSTICS",
        f"Scheduler: jobs={len(scheduler.get_jobs())}",
        f"Database: {db_kind} · {database_status}",
        f"Redis: {'configured (not probed)' if settings.redis_url and settings.redis_token else 'not configured'}",
        "PC agent: heartbeat unavailable; connectivity cannot be verified",
    ]
    for name, configured in llm["providers"].items():
        cooldown = llm["cooldowns_seconds"].get(name, 0)
        lines.append(f"Provider {name}: {'configured' if configured else 'not configured'} · cooldown={cooldown}s")
    lines.append(f"Last AI: {llm.get('last_provider') or '-'} / {llm.get('last_mode') or '-'}")
    if db_kind == "SQLite":
        lines.append("SQLite: local storage requires a persistent volume across redeploys")
    if lms is None:
        lines.append("LMS: status unavailable (database check failed)")
    else:
        lines.append(f"LMS: {'configured' if lms['configured'] else 'not configured'} · last success={lms.get('last_success_at') or '-'}")
        if lms.get("last_error"):
            # Older database rows may contain raw exception messages with credentials.
            lines.append("LMS: last sync failed; check configuration and retry")
    if counts:
        lines.append(f"Assignments: {counts['pending']} pending")
    await message.answer("\n".join(lines)[:4096])
