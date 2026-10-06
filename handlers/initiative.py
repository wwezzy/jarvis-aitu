"""One-tap feedback for initiative; every mutation is an authenticated user tap."""
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import get_settings
from services.autonomy import next_action, check_in, goals_snapshot, start_focus, autonomy_command, touch

router = Router(name="initiative")


def initiative_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать один шаг", callback_data="assist:start"),
         InlineKeyboardButton(text="Другой вариант", callback_data="assist:bored")],
        [InlineKeyboardButton(text="Отдых на час", callback_data="assist:pause"),
         InlineKeyboardButton(text="Сделал минимум", callback_data="assist:check")],
    ])


@router.callback_query(F.data.startswith("assist:"))
async def feedback(query: CallbackQuery):
    settings = get_settings()
    if not query.from_user or query.from_user.id != settings.admin_id or not query.message:
        await query.answer("Нет доступа", show_alert=True)
        return
    user_id, action = query.from_user.id, query.data.split(":", 1)[1]
    now = datetime.now(settings.timezone)
    try:
        if action == "start":
            proposed = await next_action(user_id, now)
            if proposed["kind"] not in {"goal", "task"}:
                result = proposed["text"]
            else:
                field = "goal_id" if proposed["kind"] == "goal" else "task_id"
                focus = await start_focus(user_id, proposed["minutes"], now=now, **{field: proposed[field]})
                result = f"Один шаг до {datetime.fromisoformat(focus['end']):%H:%M}: {focus['title']}. /focus cancel — отменить."
        elif action in {"pause", "bored"}:
            if action == "pause":
                from services.autonomy import active_focus, finish_focus
                if await active_focus(user_id):
                    await finish_focus(user_id, now=now)
            result = await autonomy_command(user_id, "/autopilot pause 1" if action == "pause" else "/bored", now)
        elif action == "check":
            goals = await goals_snapshot(user_id, now)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"{g['title'][:35]} · минимум {g['minimum_minutes']} мин", callback_data=f"assist:done:{g['id']}")]
                for g in goals[:10]])
            await query.message.answer("Выбери только действительно выполненный шаг:", reply_markup=keyboard)
            await query.answer()
            return
        elif action.startswith("done:"):
            identity = int(action.split(":")[1])
            goal = next((g for g in await goals_snapshot(user_id, now) if g["id"] == identity), None)
            if not goal:
                raise ValueError("Цель отключена или не найдена")
            result = await check_in(user_id, identity, "done", goal["minimum_minutes"], now=now)
        else:
            raise ValueError("Неизвестное действие")
        await touch(user_id, now)
        await query.message.answer(result)
        await query.answer()
    except (ValueError, KeyError):
        await query.answer("Действие устарело или конфликтует с текущим состоянием. /next — обновить.", show_alert=True)
