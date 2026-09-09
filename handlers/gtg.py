from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import get_settings
from services.gtg import add_gtg_set, gtg_stats

router = Router(name="gtg")
settings = get_settings()


def gtg_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+5", callback_data="gtg:5"),
                InlineKeyboardButton(text="+8", callback_data="gtg:8"),
                InlineKeyboardButton(text="+10", callback_data="gtg:10"),
            ],
            [InlineKeyboardButton(text="📊 Обновить статистику", callback_data="gtg:stats")],
        ]
    )


def _format_stats(stats: dict) -> str:
    return (
        "GTG / подтягивания\n"
        f"Сегодня: {stats['today']['reps']} повторов · {stats['today']['sets']} подходов\n"
        f"Неделя: {stats['week']['reps']} повторов · {stats['week']['sets']} подходов"
    )


@router.message(Command("gtg", "gtg_stats"))
async def gtg_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return
    stats = await gtg_stats(message.from_user.id)
    await message.answer(_format_stats(stats), reply_markup=gtg_keyboard())


@router.callback_query(F.data.startswith("gtg:"))
async def gtg_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != settings.admin_id:
        await callback.answer("Access denied", show_alert=True)
        return

    action = (callback.data or "").split(":", 1)[1]
    if action.isdigit():
        reps = int(action)
        await add_gtg_set(callback.from_user.id, reps, source="telegram_button")
        await callback.answer(f"+{reps} сохранено")
    else:
        await callback.answer()

    stats = await gtg_stats(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(_format_stats(stats), reply_markup=gtg_keyboard())
