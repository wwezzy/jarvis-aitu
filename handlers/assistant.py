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
from services.attachments import AttachmentError, ensure_size, transcribe_telegram_voice
from services.direct_intents import try_direct_answer
from services.llm import extract_actions, generate_reply
from services.memory import build_memory_context, upsert_memory_updates
from services.pc_agent import build_signed_command, explicit_pc_command
from services.reflections import upsert_reflection
from services.scheduler import send_saved_reminder, sync_assignment_jobs
from services.users import ensure_user
from services.workouts import log_workout

logger = logging.getLogger(__name__)
router = Router(name="assistant")
settings = get_settings()


def _local_now() -> datetime:
    return datetime.now(settings.timezone)


async def _download_attachment(
    message: Message,
    bot: Bot,
) -> tuple[str, bytes | None, str | None, str | None]:
    text = message.text or message.caption or ""
    file_bytes: bytes | None = None
    mime_type: str | None = None
    filename: str | None = None

    if message.photo:
        photo = message.photo[-1]
        ensure_size(photo.file_size)
        file_info = await bot.get_file(photo.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        file_bytes = downloaded.read()
        mime_type = "image/jpeg"
        filename = "photo.jpg"
        if not text:
            text = "Проанализируй изображение и выполни запрос пользователя."

    elif message.document:
        ensure_size(message.document.file_size)
        file_info = await bot.get_file(message.document.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        file_bytes = downloaded.read()
        mime_type = message.document.mime_type or "application/octet-stream"
        filename = message.document.file_name or "document"
        if not text:
            text = f"Проанализируй файл {filename} и выдели главное, важные требования и действия."

    elif message.voice:
        ensure_size(message.voice.file_size, audio=True)
        file_info = await bot.get_file(message.voice.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        transcript = await transcribe_telegram_voice(downloaded.read())
        text = (
            f"{text}\n\n[Расшифровка голосового]\n{transcript}".strip()
            if text
            else transcript
        )
        # Voice is normalized to text before routing. This allows deterministic
        # schedule/deadline intents and text-only fallback providers to work too.
        return text, None, None, None

    return text, file_bytes, mime_type, filename


async def _apply_actions(
    *,
    message: Message,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    pc_redis: AsyncRedis | None,
    actions: dict,
    now: datetime,
) -> list[str]:
    confirmations: list[str] = []

    workout_log = actions.get("workout_log")
    if workout_log and workout_log.get("sets"):
        await log_workout(
            message.from_user.id,
            workout_log.get("title") or "Workout",
            workout_log.get("sets") or [],
            notes=workout_log.get("notes"),
            source="telegram_ai",
            started_at=now.replace(tzinfo=None),
        )
        confirmations.append("Workout сохранён")

    habit_logs = actions.get("habit_logs") or []
    if habit_logs:
        async with async_session_factory() as db:
            for item in habit_logs[:20]:
                name = str(item.get("name") or "").strip()
                if name:
                    db.add(Habit(user_id=message.from_user.id, name=name[:255]))
            await db.commit()
        confirmations.append("habit log сохранён")

    reflection = actions.get("reflection")
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
        confirmations.append("рефлексия сохранена")

    memory_updates = actions.get("memory_updates") or []
    if memory_updates:
        await upsert_memory_updates(message.from_user.id, memory_updates)
        confirmations.append("память обновлена")

    assignments = actions.get("assignments") or []
    if assignments:
        saved = await upsert_assignment_payloads(
            message.from_user.id,
            assignments,
            source="telegram_ai",
        )
        if saved:
            await sync_assignment_jobs(scheduler, bot, message.from_user.id)
            confirmations.append(f"заданий обновлено: {len(saved)}")

    reminders = actions.get("reminders") or []
    created_reminders = 0
    for payload in reminders[:20]:
        try:
            remind_at = datetime.strptime(payload["remind_at"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping invalid reminder payload")
            continue

        text_value = str(payload.get("text") or "Напоминание")[:255]
        async with async_session_factory() as db:
            reminder = Reminder(
                user_id=message.from_user.id,
                text=text_value,
                remind_at=remind_at,
            )
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
        created_reminders += 1

    if created_reminders:
        confirmations.append(f"напоминаний создано: {created_reminders}")

    # Model output is not authorization to operate the PC.

    return confirmations


@router.message(F.text | F.photo | F.document | F.voice)
async def assistant_message(
    message: Message,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    pc_redis: AsyncRedis | None = None,
) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return

    status = await message.answer("Jarvis thinking…")

    try:
        user = await ensure_user(message.from_user.id, message.from_user.full_name)
        try:
            text, file_bytes, mime_type, filename = await _download_attachment(message, bot)
        except AttachmentError as exc:
            await status.edit_text(str(exc))
            return
        now = _local_now()

        if file_bytes is None:
            command = explicit_pc_command(text)
            if command:
                if pc_redis is None or not settings.pc_agent_secret:
                    await status.edit_text("PC-agent unavailable: Redis/PC_AGENT_SECRET is not configured.")
                else:
                    await pc_redis.set(
                        f"jarvis:pc_command:{settings.admin_id}",
                        build_signed_command(command, settings.admin_id, settings.pc_agent_secret),
                        ex=90,
                    )
                    await status.edit_text(f"Signed PC command queued: {command}.")
                return
            direct_reply = await try_direct_answer(message.from_user.id, text, now)
            if direct_reply:
                await status.edit_text(direct_reply[:4096])
                return

        memory_context = await build_memory_context(message.from_user.id, text, now)

        reply = await generate_reply(
            text=text,
            user_id=message.from_user.id,
            user_name=message.from_user.full_name,
            preferences=user.preferences,
            memory_context=memory_context,
            file_bytes=file_bytes,
            mime_type=mime_type,
            filename=filename,
        )

        # Critical v3 rule: the user sees the reply before any structured extraction.
        await status.edit_text(reply[:4096])

        try:
            actions = await extract_actions(
                text=text,
                reply=reply,
                user_name=message.from_user.full_name,
                memory_context=memory_context,
            )
            confirmations = await _apply_actions(
                message=message,
                bot=bot,
                scheduler=scheduler,
                pc_redis=pc_redis,
                actions=actions,
                now=now,
            )
            if confirmations:
                await message.answer("✅ " + " · ".join(confirmations)[:3900])
        except Exception as exc:
            logger.warning("Post-reply action pipeline failed error=%s", type(exc).__name__)
            await message.answer("Не удалось сохранить все действия. Проверь журнал перед повтором.")

    except Exception as exc:
        logger.error("Message reply pipeline failed error=%s", type(exc).__name__)
        await status.edit_text(
            f"Jarvis не смог обработать запрос: {type(exc).__name__}. "
            "Расписание и дедлайны доступны через /today и /deadlines."
        )
