from __future__ import annotations

import logging
import asyncio
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from upstash_redis.asyncio import Redis as AsyncRedis

from config import get_settings
from database.engine import async_session_factory
from database.models import Habit, Reminder
from services.assignments import upsert_assignment_payloads
from services.attachments import AttachmentError, BoundedDownload, ensure_size, transcribe_telegram_voice
from services.direct_intents import try_direct_answer
from services.llm import extract_actions, generate_reply
from services.memory import build_memory_context, upsert_memory_updates
from services.pc_agent import handle_pc_request
from services.reflections import upsert_reflection
from services.scheduler import send_saved_reminder, sync_assignment_jobs, sync_schedule_jobs, restore_pending_reminders
from services.users import ensure_user
from services.inbox import CollectionStore, normalize, starts_collection, finishes_collection
from services.telegram_text import deliver
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
        downloaded = await bot.download_file(file_info.file_path, destination=BoundedDownload())
        file_bytes = downloaded.read()
        ensure_size(len(file_bytes))
        mime_type = "image/jpeg"
        filename = "photo.jpg"
        if not text:
            text = "Проанализируй изображение и выполни запрос пользователя."

    elif message.document:
        ensure_size(message.document.file_size)
        file_info = await bot.get_file(message.document.file_id)
        downloaded = await bot.download_file(file_info.file_path, destination=BoundedDownload())
        file_bytes = downloaded.read()
        ensure_size(len(file_bytes))
        mime_type = message.document.mime_type or "application/octet-stream"
        filename = message.document.file_name or "document"
        if not text:
            text = f"Проанализируй файл {filename} и выдели главное, важные требования и действия."

    elif message.voice:
        from services.rate_limit import allow
        if not allow(message.from_user.id, "transcription", limit=6):
            raise AttachmentError("Слишком много голосовых. Повтори через минуту; текстовые команды доступны.")
        ensure_size(message.voice.file_size, audio=True)
        file_info = await bot.get_file(message.voice.file_id)
        downloaded = await bot.download_file(file_info.file_path, destination=BoundedDownload(audio=True))
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
        saved = await upsert_memory_updates(message.from_user.id, memory_updates)
        if saved:
            confirmations.append(f"память обновлена: {saved}")

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

    from services.observability import new_correlation, correlation_id
    from services.telemetry import request_user
    correlation_token = new_correlation()
    user_token = request_user.set(message.from_user.id)
    status = await message.answer("Jarvis thinking…")
    delivered = False

    try:
        user = await ensure_user(message.from_user.id, message.from_user.full_name)
        collection = CollectionStore(pc_redis)
        chat_id = getattr(getattr(message, "chat", None), "id", message.from_user.id)
        raw_text = message.text or message.caption or ""
        batch = None
        if starts_collection(raw_text):
            await collection.start(user.telegram_id if hasattr(user, "telegram_id") else message.from_user.id, chat_id)
            await status.edit_text("Сбор открыт на 15 минут. До 12 материалов / 12 MB. /done — анализ, /cancel_collect — удалить.")
            return
        if raw_text.strip() == "/cancel_collect":
            await collection.cancel(message.from_user.id, chat_id)
            await status.edit_text("Сбор отменён; временные материалы удалены.")
            return
        if finishes_collection(raw_text):
            batch = await collection.finish(message.from_user.id, chat_id)
            if not batch or not batch[0]:
                await status.edit_text("Нет собранных материалов. /collect — начать; /done 12 — закрыть задание.")
                return
        try:
            text, file_bytes, mime_type, filename = await _download_attachment(message, bot)
        except AttachmentError as exc:
            await status.edit_text(str(exc))
            return
        now = _local_now()
        files = []
        if file_bytes:
            extracted, native = await normalize(file_bytes, filename, mime_type or "application/octet-stream")
            text = f"{text}\n\n[{filename}]\n{extracted}" if extracted else text
            if native:
                files.append(native)
            file_bytes = None
        try:
            collecting = await collection.active(message.from_user.id, chat_id) if not batch else False
        except Exception:
            # Redis is optional for the deterministic database-backed core.
            direct_reply = await try_direct_answer(message.from_user.id, text, now) if not files else None
            if direct_reply:
                await deliver(message, status, direct_reply)
                return
            await status.edit_text("Redis временно недоступен. Сбор нельзя проверить; материалы не обработаны. Повтори позже.")
            return
        if batch:
            text, files = batch
        elif collecting:
            count = await collection.append(message.from_user.id, chat_id, text, files)
            await status.edit_text(f"Сохранён материал {count}. /done — анализировать вместе.")
            return
        elif getattr(message, "media_group_id", None) and pc_redis is not None:
            # Only Telegram-declared albums aggregate automatically. Unrelated
            # quick messages remain independent; ambiguous text uses /collect.
            chat_id = f"{chat_id}:album:{message.media_group_id}"
            await collection.start(message.from_user.id, chat_id)
            await collection.append(message.from_user.id, chat_id, text, files)
            await asyncio.sleep(1.5)
            if not await pc_redis.set(collection.key(message.from_user.id, chat_id) + ":processing", "1", ex=180, nx=True):
                await status.edit_text("Материал добавлен в анализ альбома.")
                return
            batch = await collection.finish(message.from_user.id, chat_id)
            text, files = batch

        if not files and not batch:
            pc_reply = await handle_pc_request(message.text or "", message.from_user.id, chat_id, pc_redis, settings.pc_agent_secret)
            if pc_reply:
                await status.edit_text(pc_reply)
                return
            direct_reply = await try_direct_answer(message.from_user.id, text, now)
            if direct_reply:
                await deliver(message, status, direct_reply)
                delivered = True
                if text.startswith(("/task ", "/remind ", "/schedule ", "/override ", "/study timer")) or direct_reply.startswith("Сохранено только"):
                    await sync_assignment_jobs(scheduler, bot, message.from_user.id)
                    await sync_schedule_jobs(scheduler, bot, message.from_user.id)
                    await restore_pending_reminders(scheduler, bot)
                return

        from services.rate_limit import allow
        if not allow(message.from_user.id, "ai", limit=10):
            await status.edit_text("Слишком много AI-запросов. Попробуй через минуту; /today и /tasks доступны.")
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
            attachments=files,
        )

        # Critical v3 rule: the user sees the reply before any structured extraction.
        await deliver(message, status, reply)
        delivered = True
        if batch:
            if reply.startswith("Сейчас AI-каналы не ответили"):
                await message.answer("Материалы остаются в сборе до истечения TTL. Повтори /done или удали /cancel_collect.")
            elif not await collection.acknowledge(message.from_user.id, chat_id):
                await message.answer("Во время анализа добавлены материалы; сбор сохранён. /done — повторить с ними.")

        try:
            actions = await extract_actions(
                text=text,
                reply=reply,
                user_name=message.from_user.full_name,
                memory_context=memory_context,
            )
            from services.action_evidence import supported_actions
            actions = supported_actions(actions, text)
            confirmations = await _apply_actions(
                message=message,
                bot=bot,
                scheduler=scheduler,
                pc_redis=pc_redis,
                actions=actions,
                now=now,
            )
            from services.commands import apply_schedule_text
            schedule_confirmation = await apply_schedule_text(message.from_user.id, text, now)
            if schedule_confirmation:
                confirmations.append(schedule_confirmation)
                await sync_schedule_jobs(scheduler, bot, message.from_user.id)
            if confirmations:
                await message.answer("✅ " + " · ".join(confirmations)[:3900])
            try:
                from services.telemetry import daily_usage
                usage = await daily_usage(message.from_user.id)
                if usage['budget_warning']:
                    from services.notifications import notify_once
                    await notify_once(bot, message.from_user.id, f"budget:{usage['date']}",
                        "⚠ Оценка AI-расходов достигла 80% дневного бюджета. /cost — детали. Неизвестные цены не включены.")
            except Exception as exc:
                logger.warning("Budget warning unavailable error=%s", type(exc).__name__)

        except Exception as exc:
            logger.warning("Post-reply action pipeline failed error=%s", type(exc).__name__)
            await message.answer("Не удалось сохранить все действия. Проверь журнал перед повтором.")

    except Exception as exc:
        logger.error("Message reply pipeline failed error=%s", type(exc).__name__)
        send = message.answer if delivered else status.edit_text
        await send(
            f"Jarvis не смог обработать запрос: {type(exc).__name__} · {correlation_id.get()}. "
            "Расписание и дедлайны доступны через /today и /deadlines."
        )
    finally:
        correlation_id.reset(correlation_token)
        request_user.reset(user_token)
