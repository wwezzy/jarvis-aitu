from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import get_settings
from database.engine import DATABASE_URL
from services.assignments import assignment_counts, format_deadlines, list_upcoming_assignments, lms_status, mark_assignment_done, sync_lms_ical
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
    items = await list_upcoming_assignments(message.from_user.id, days=30, include_unknown=True, limit=30)
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
        await message.answer("LMS ещё не подключён. Добавь персональную Moodle/iCal ссылку в Render Environment как LMS_ICAL_URL. Саму ссылку в чат или GitHub не отправляй.")
        return
    status = await message.answer("Синхронизирую LMS…")
    try:
        result = await sync_lms_ical(message.from_user.id)
        jobs = await sync_assignment_jobs(scheduler, bot, message.from_user.id)
        await status.edit_text("✅ LMS sync завершён.\n" f"Новых: {result.get('created', 0)}\n" f"Обновлено: {result.get('updated', 0)}\n" f"Событий в feed: {result.get('events_seen', 0)}\n" f"Активных deadline-уведомлений: {jobs}")
    except Exception as exc:
        await status.edit_text(f"❌ LMS sync: {type(exc).__name__}. Подробность сохранена в /diag и server logs.")


@router.message(Command("diag"))
async def diag_command(message: Message, scheduler: AsyncIOScheduler) -> None:
    if not _authorized(message):
        return
    llm = get_llm_diagnostics()
    lms = await lms_status(message.from_user.id)
    counts = await assignment_counts(message.from_user.id)
    db_kind = "PostgreSQL" if DATABASE_URL.startswith("postgresql") else "SQLite"
    db_warning = "" if db_kind == "PostgreSQL" else " ⚠️ Render redeploy can erase local SQLite"
    last_llm = llm.get("last_success_at") or "ещё нет"
    llm_error = llm.get("last_error")
    if llm_error:
        llm_error = str(llm_error).replace("\n", " ")[:350]
    lines = [
        "🧪 JARVIS DIAGNOSTICS",
        f"Telegram/Scheduler: ✅ · jobs={len(scheduler.get_jobs())}",
        f"Gemini projects: {llm['configured_keys']} · model={llm['primary_model']}",
        f"Gemini last success: {last_llm}",
        f"Gemini mode: {llm.get('last_mode') or '-'} · key slot={llm.get('last_key_slot') or '-'}",
        f"Gemini cooling slots: {llm.get('cooling_key_slots') or 'none'}",
        f"Redis: {'✅' if settings.redis_url and settings.redis_token else '⚠️ not configured'}",
        f"Database: {db_kind}{db_warning}",
        f"LMS iCal: {'✅ configured' if lms['configured'] else '⚪ not configured'}",
        f"LMS last success: {lms.get('last_success_at') or '-'}",
        f"Assignments: {counts['pending']} pending · {counts['unknown_deadline']} without exact date",
    ]
    if lms.get("last_error"):
        lines.append(f"LMS last error: {str(lms['last_error']).replace(chr(10), ' ')[:350]}")
    if llm_error:
        lines.append(f"LLM last error: {llm_error}")
    if llm.get("last_debug_id"):
        lines.append(f"LLM debug id: {llm['last_debug_id']}")
    await message.answer("\n".join(lines)[:4096])
