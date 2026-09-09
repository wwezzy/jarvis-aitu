from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from upstash_redis.asyncio import Redis as AsyncRedis

from config import get_settings
from database.engine import async_session_factory
from database.models import Habit, Reminder
from services.assignments import upsert_assignment_payloads
from services.direct_intents import try_direct_answer
from services.llm import parse_user_message
from services.memory import build_memory_context, upsert_memory_updates
from services.pc_agent import build_signed_command
from services.reflections import upsert_reflection
from services.scheduler import send_saved_reminder, sync_assignment_jobs
from services.users import ensure_user
from services.workouts import log_workout

logger = logging.getLogger(__name__)
router = Router(name="assistant")
settings = get_settings()


def _local_now() -> datetime:
    return datetime.now(settings.timezone)


async def _download_attachment(message: Message, bot: Bot) -> tuple[str, bytes | None, str | None]:
    text = message.text or message.caption or ""
    file_bytes: bytes | None = None
    mime_type: str | None = None

    if message.photo:
        file_info = await bot.get_file(message.photo[-1].file_id)
        downloaded = await bot.download_file(file_info.file_path)
        file_bytes = downloaded.read()
        mime_type = "image/jpeg"
    elif message.document:
        file_info = await bot.get_file(message.document.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        file_bytes = downloaded.read()
        mime_type = message.document.mime_type or "application/octet-stream"
    elif message.voice:
        file_info = await bot.get_file(message.voice.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        file_bytes = downloaded.read()
        mime_type = "audio/ogg"
        if not text:
            text = "Прослушай голосовое сообщение, извлеки факты и выполни просьбу."

    return text, file_bytes, mime_type


@router.message(F.text | F.photo | F.document | F.voice)
async def assistant_message(
    message: Message,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    pc_redis: AsyncRedis | None = None,
) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return

    status = await message.answer("Jarvis processing…")

    try:
        user = await ensure_user(message.from_user.id, message.from_user.full_name)
        text, file_bytes, mime_type = await _download_attachment(message, bot)
        now = _local_now()

        if file_bytes is None:
            direct_reply = await try_direct_answer(message.from_user.id, text, now)
            if direct_reply:
                await status.edit_text(direct_reply[:4096])
                return

        memory_context = await build_memory_context(message.from_user.id, text, now)

        data = await parse_user_message(
            text=text,
            user_id=message.from_user.id,
            user_name=message.from_user.full_name,
            preferences=user.preferences,
            memory_context=memory_context,
            file_bytes=file_bytes,
            mime_type=mime_type,
        )

        reply = data.get("reply") or "Принято."

        workout_log = data.get("workout_log")
        if workout_log and workout_log.get("sets"):
            await log_workout(
                message.from_user.id,
                workout_log.get("title") or "Workout",
                workout_log.get("sets") or [],
                notes=workout_log.get("notes"),
                source="telegram_llm",
                started_at=now.replace(tzinfo=None),
            )
            reply += "\n\n✅ Workout записан в structured journal."

        habit_logs = data.get("habit_logs") or []
        if habit_logs:
            async with async_session_factory() as db:
                for item in habit_logs[:20]:
                    name = str(item.get("name") or "").strip()
                    if name:
                        db.add(Habit(user_id=message.from_user.id, name=name[:255]))
                await db.commit()
            reply += "\n✅ Habit log сохранён."

        reflection = data.get("reflection")
        if reflection:
            await upsert_reflection(
                message.from_user.id,
                now.date(),
                deep_work_hours=reflection.get("deep_work_hours"),
                calories_hit=reflection.get("calories_hit"),
                protein_hit=reflection.get("protein_hit"),
                rir_respected=reflection.get("rir_respected"),
                mood=reflection.get("mood"),
                energy=reflection.get("energy"),
                notes=reflection.get("notes"),
            )
            reply += "\n✅ Вечерняя рефлексия сохранена."

        memory_updates = data.get("memory_updates") or []
        if memory_updates:
            await upsert_memory_updates(message.from_user.id, memory_updates)
            reply += "\n🧠 Long-term memory updated."

        assignments = data.get("assignments") or []
        if assignments:
            saved = await upsert_assignment_payloads(
                message.from_user.id,
                assignments,
                source="telegram_llm",
            )
            if saved:
                await sync_assignment_jobs(scheduler, bot, message.from_user.id)
                reply += f"\n📚 Assignments updated: {len(saved)}."

        reminders = data.get("reminders") or []
        for payload in reminders[:20]:
            try:
                remind_at = datetime.strptime(payload["remind_at"], "%Y-%m-%d %H:%M:%S")
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid reminder payload: %r", payload)
                continue

            text_value = str(payload.get("text") or "Напоминание")[:255]
            async with async_session_factory() as db:
                reminder = Reminder(user_id=message.from_user.id, text=text_value, remind_at=remind_at)
                db.add(reminder)
                await db.commit()
                await db.refresh(reminder)

            if remind_at > now.replace(tzinfo=None):
                scheduler.add_job(
                    send_saved_reminder,
                    "date",
                    run_date=remind_at,
                    args=[bot, message.from_user.id, text_value, reminder.id],
                    id=f"reminder:{reminder.id}",
                    replace_existing=True,
                    misfire_grace_time=300,
                )
        if reminders:
            reply += "\n⏰ Reminder pipeline updated."

        system_command = data.get("system_command")
        if system_command in {"lock", "sleep", "shutdown", "restart"}:
            if pc_redis is None or not settings.pc_agent_secret:
                reply += "\nPC-agent unavailable: Redis/PC_AGENT_SECRET is not configured."
            else:
                signed_payload = build_signed_command(
                    system_command, settings.admin_id, settings.pc_agent_secret
                )
                await pc_redis.set(
                    f"jarvis:pc_command:{settings.admin_id}",
                    signed_payload,
                    ex=90,
                )
                reply += f"\n📡 Signed PC command queued: {system_command}."

        await status.edit_text(reply[:4096])

    except Exception as exc:
        logger.exception("Message pipeline failed")
        await status.edit_text(
            f"Pipeline error: {type(exc).__name__}. Детали сохранены в server logs."
        )
