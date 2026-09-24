from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import get_settings
from database.engine import DATABASE_URL
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
    lms = await lms_status(message.from_user.id)
    counts = await assignment_counts(message.from_user.id)
    db_kind = "PostgreSQL" if DATABASE_URL.startswith("postgresql") else "SQLite"

    providers = llm.get("providers") or {}
    models = llm.get("models") or {}
    lines = [
        "🧪 JARVIS V3 DIAGNOSTICS",
        f"Telegram/Scheduler: ✅ · jobs={len(scheduler.get_jobs())}",
        (
            "AI providers: "
            f"OpenAI={'✅' if providers.get('openai') else '—'} · "
            f"NVIDIA={'✅' if providers.get('nvidia') else '—'} · "
            f"Gemini={'✅' if providers.get('gemini') else '—'}"
        ),
        f"Last AI: {llm.get('last_provider') or '-'} / {llm.get('last_model') or '-'} / {llm.get('last_mode') or '-'}",
        f"OpenAI default/planner: {models.get('openai_default') or '-'} / {models.get('openai_planner') or '-'}",
        f"Provider cooldowns: {llm.get('cooldowns_seconds') or 'none'}",
        f"Redis: {'✅' if settings.redis_url and settings.redis_token else '⚠️ not configured'}",
        f"Database: {db_kind}{'' if db_kind == 'PostgreSQL' else ' ⚠️ ephemeral on Render'}",
        f"LMS iCal: {'✅ configured' if lms['configured'] else '⚪ not configured'}",
        f"LMS last success: {lms.get('last_success_at') or '-'}",
        f"Assignments: {counts['pending']} pending · {counts['unknown_deadline']} without exact date",
    ]

    if lms.get("last_error"):
        lines.append(
            f"LMS last error: {str(lms['last_error']).replace(chr(10), ' ')[:300]}"
        )
    if llm.get("last_error"):
        lines.append(
            f"AI last error: {str(llm['last_error']).replace(chr(10), ' ')[:300]}"
        )
    if llm.get("last_debug_id"):
        lines.append(f"AI debug id: {llm['last_debug_id']}")

    await message.answer("\n".join(lines)[:4096])
