from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from config import get_settings
from services.schedule import today_schedule
from services.users import ensure_user

router = Router(name="start")
settings = get_settings()


def miniapp_keyboard() -> InlineKeyboardMarkup | None:
    if not settings.webapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ Open Jarvis Console",
                    web_app=WebAppInfo(url=f"{settings.webapp_url}/app"),
                )
            ]
        ]
    )


@router.message(CommandStart())
async def start_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        await message.answer("🔒 Access denied. Jarvis is in private mode.")
        return

    await ensure_user(message.from_user.id, message.from_user.full_name)
    text = (
        "JARVIS ONLINE\n\n"
        "/today — текущий протокол\n"
        "/gtg — быстрые подходы\n"
        "/workouts — последние тренировки\n"
        "/memory — долговременная память\n"
        "/app — Mini App"
    )
    await message.answer(text, reply_markup=miniapp_keyboard())


@router.message(Command("app"))
async def app_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return
    if not settings.webapp_url:
        await message.answer("Mini App код установлен, но WEBAPP_URL ещё не задан в .env.")
        return
    await message.answer("Jarvis Console", reply_markup=miniapp_keyboard())


@router.message(Command("today"))
async def today_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return
    schedule = today_schedule()
    label = {"A": "День A", "B": "День B", "FLEX": "Flexible"}[schedule["kind"]]
    lines = [f"{schedule['date']} · {label}"]
    for block in schedule["blocks"]:
        marker = "▶" if block["status"] == "current" else "·"
        lines.append(f"{marker} {block['start']}–{block['end']}  {block['title']}")
    await message.answer("\n".join(lines))
