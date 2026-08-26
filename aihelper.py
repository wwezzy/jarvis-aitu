import asyncio
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import select
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database.engine import init_db, async_session_factory
from database.models import User, Workout, Habit, Reminder
from services.llm import parse_user_message

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

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


@dp.message(F.text | F.photo | F.document | F.voice)
async def handle_any_message(message: Message, bot: Bot):
    # --- СИСТЕМА ДИАГНОСТИКИ ---
    print(f"📥 Пришло сообщение от ID: {message.from_user.id}")

    if message.from_user.id != ADMIN_ID:
        print(f"❌ ДОСТУП ЗАКРЫТ: В коде стоит ADMIN_ID = {ADMIN_ID}, а пишет {message.from_user.id}")
        return
        # ---------------------------

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

            # АВТОРЕГИСТРАЦИЯ (на случай если ты не нажал /start)
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

async def main():
    print("Инициализация базы данных...")
    await init_db()
    print("Запуск планировщика задач...")
    scheduler.start()
    print("Система Джарвис запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Джарвис отключен.")