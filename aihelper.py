from upstash_redis.asyncio import Redis as AsyncRedis
from aiohttp import web
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import select
from dotenv import load_dotenv
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database.engine import init_db, async_session_factory
from database.models import User, Workout, Habit, Reminder
from services.llm import parse_user_message, client
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
redis_client = AsyncRedis(url=os.getenv("UPSTASH_REDIS_REST_URL"), token=os.getenv("UPSTASH_REDIS_REST_TOKEN"))

# ВСТАВЬ СЮДА СВОЙ TELEGRAM ID
ADMIN_ID = 2016952162

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()


async def send_reminder(user_id: int, text: str, reminder_id: int):
    try:
        await bot.send_message(user_id, f"🔔 **Напоминание:**\n{text}", parse_mode="Markdown")
        async with async_session_factory() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            reminder = result.scalar_one_or_none()
            if reminder:
                reminder.is_sent = True
                await session.commit()
    except Exception as e:
        print(f"Ошибка отправки напоминания: {e}")


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer("🔒 Отказано в доступе. Я служу только своему создателю.")
        return

    telegram_id: int = message.from_user.id
    user_name: str = message.from_user.full_name

    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user: User | None = result.scalar_one_or_none()
        if user is None:
            session.add(User(telegram_id=telegram_id, name=user_name))
            await session.commit()

    await message.answer(f"Привет, {user_name}! Я твой личный Джарвис. 🚀\nСистема безопасности активирована.")


@dp.message(F.text.lower() == "аналитика тренировок")
async def analyze_workouts(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    status = await message.answer("📊 Поднимаю архивы из базы данных...")

    async with async_session_factory() as session:
        result = await session.execute(
            select(Workout).where(Workout.user_id == ADMIN_ID).order_by(Workout.workout_date.desc()).limit(10)
        )
        workouts = result.scalars().all()

    if not workouts:
        await status.edit_text("База пуста. Вы еще не записывали тренировки.")
        return

    workout_history = "\n".join([f"{w.workout_date}: {w.workout_type} - {w.notes}" for w in workouts])

    prompt = f"""Ты спортивный аналитик. Вот последние 10 записей тренировок пользователя:
    {workout_history}
    Сделай краткий анализ прогресса, укажи на сильные стороны и дай 1-2 конкретных совета по восстановлению или корректировке нагрузки. Текст должен быть коротким и мотивирующим."""

    response = await client.aio.models.generate_content(
        model='gemini-3.6-flash',
        contents=[prompt]
    )

    await status.edit_text(f"💪 **Аналитика физической подготовки:**\n\n{response.text}", parse_mode="Markdown")

# УДАЛИЛИ ШАБЛОННЫЕ КОМАНДЫ, ТЕПЕРЬ ВСЁ ИДЕТ ЧЕРЕЗ ИИ
@dp.message(F.text | F.photo | F.document | F.voice)
async def handle_any_message(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    status_msg = await message.answer("👀 Джарвис обрабатывает...")

    try:
        text = message.text or message.caption or ""
        file_bytes = None
        mime_type = None

        if message.photo:
            file_info = await bot.get_file(message.photo[-1].file_id)
            mime_type = "image/jpeg"
            downloaded_file = await bot.download_file(file_info.file_path)
            file_bytes = downloaded_file.read()
        elif message.document:
            file_info = await bot.get_file(message.document.file_id)
            mime_type = message.document.mime_type
            downloaded_file = await bot.download_file(file_info.file_path)
            file_bytes = downloaded_file.read()
        elif message.voice:
            file_info = await bot.get_file(message.voice.file_id)
            mime_type = "audio/ogg"
            downloaded_file = await bot.download_file(file_info.file_path)
            file_bytes = downloaded_file.read()
            text = "Прослушай это голосовое сообщение и выполни поручения."

        async with async_session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
            user = result.scalar_one_or_none()

            if user is None:
                user = User(telegram_id=message.from_user.id, name=message.from_user.full_name)
                session.add(user)
                await session.commit()
                user_prefs = None
            else:
                user_prefs = user.preferences

        data = await parse_user_message(text, message.from_user.full_name, user_prefs, file_bytes, mime_type)

        reply_text = data.get("reply", "Принято.")
        extracted_items = data.get("extracted_data", [])
        new_prefs = data.get("new_preferences")
        reminders = data.get("reminders", [])

        # НОВАЯ ПЕРЕМЕННАЯ: Читаем команду из ответа Gemini
        sys_cmd = data.get("system_command")

        # --- СИСТЕМА УПРАВЛЕНИЯ НОУТБУКОМ (ЧЕРЕЗ REDIS) ---
        if sys_cmd in ["lock", "sleep", "shutdown"]:
            # Отправляем команду в облако, она "сгорит" через 60 секунд, если ноут выключен
            await redis_client.set("pc_command", sys_cmd, ex=60)
            reply_text += f"\n\n*(📡 Команда {sys_cmd} отправлена на G16)*"
        # ---------------------------------------------------

        if extracted_items or new_prefs or reminders:
            async with async_session_factory() as session:
                if new_prefs:
                    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
                    db_user = result.scalar_one()
                    db_user.preferences = new_prefs

                for item in extracted_items:
                    item_type = item.get("type")
                    if item_type == "workout":
                        session.add(Workout(user_id=message.from_user.id, workout_type=item.get("name", "Тренировка"),
                                            notes=item.get("notes")))
                    elif item_type == "habit":
                        session.add(Habit(user_id=message.from_user.id, name=item.get("name", "Привычка")))

                for rem in reminders:
                    rem_time = datetime.strptime(rem["remind_at"], "%Y-%m-%d %H:%M:%S")
                    new_rem = Reminder(user_id=message.from_user.id, text=rem["text"], remind_at=rem_time)
                    session.add(new_rem)
                    await session.flush()

                    scheduler.add_job(
                        send_reminder,
                        'date',
                        run_date=rem_time,
                        args=[message.from_user.id, rem["text"], new_rem.id]
                    )
                await session.commit()

            if extracted_items:
                reply_text += "\n\n*(✅ Данные сохранены)*"
            if new_prefs:
                reply_text += "\n\n*(🧠 Досье обновлено)*"
            if reminders:
                reply_text += "\n\n*(⏰ Таймер заведен)*"

        await status_msg.edit_text(reply_text, parse_mode="Markdown")

    except Exception as e:
        print(f"Ошибка: {e}")
        await status_msg.edit_text("❌ Ошибка при обработке. Убедись, что формат поддерживается.")


async def morning_briefing():
    try:
        # Получаем данные пользователя
        async with async_session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == ADMIN_ID))
            user = result.scalar_one_or_none()
            user_prefs = user.preferences if user else "Досье пусто."

        # Формируем системный промпт для генерации утренней сводки
        prompt = f"""Ты Джарвис. Напиши бодрое, короткое утреннее сообщение для пользователя.
        Контекст пользователя: {user_prefs}.
        Обязательно напомни про режим Lock-in и упомяни, что библиотека AITU открыта до 22:00, так что времени на продуктивную работу полно.
        Без лишней воды, строго по делу и с уважением."""

        response = await client.aio.models.generate_content(
            model='gemini-3.6-flash',
            contents=[prompt]
        )
        await bot.send_message(ADMIN_ID, f"🌅 **Утренний протокол активен:**\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        print(f"Ошибка брифинга: {e}")

async def water_reminder():
    try:
        await bot.send_message(
            ADMIN_ID,
            "💧 **Сэр, режим Lock-in требует ресурса.**\nПожалуйста, выпейте стакан воды.",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка напоминания о воде: {e}")

async def health_check(request):
    return web.Response(text="Джарвис в сети и готов к работе!")


async def main():
    print("Инициализация базы данных...")
    await init_db()
    print("Запуск планировщика задач...")

    scheduler.add_job(morning_briefing, trigger='cron', hour=7, minute=0)
    scheduler.add_job(water_reminder, trigger='cron', hour='8-22/2')
    scheduler.start()

    # --- DUMMY-СЕРВЕР ДЛЯ ОБХОДА БЛОКИРОВОК ОБЛАКА ---
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web-сервер запущен на порту {port}")
    # ------------------------------------------------

# ... (тут заканчивается функция main) ...
    print("Система Джарвис запускается...")
    await dp.start_polling(bot)

# ПРОВЕРЬ: Здесь не должно быть никаких пробелов в начале строк!
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Джарвис отключен.")


        # Принудительный рестарт облака