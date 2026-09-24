from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import get_settings
from services.memory import list_memory_facts, delete_memory, correct_memory, upsert_memory_updates
from services.workouts import recent_workouts

router = Router(name="memory")
settings = get_settings()


@router.message(Command("memory"))
async def memory_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return
    parts = (message.text or "").split(maxsplit=3)
    try:
        if len(parts) >= 3 and parts[1] == "delete":
            await delete_memory(message.from_user.id, int(parts[2]))
            await message.answer("Факт удалён.")
            return
        if len(parts) == 4 and parts[1] == "correct":
            await correct_memory(message.from_user.id, int(parts[2]), parts[3])
            await message.answer("Исправление сохранено.")
            return
        if len(parts) == 4 and parts[1] == "remember":
            await upsert_memory_updates(message.from_user.id, [{"key": parts[2], "value": parts[3]}])
            await message.answer("Факт сохранён.")
            return
    except ValueError:
        await message.answer("Проверь ID и текст. /memory correct 12 новое значение; /memory delete 12")
        return

    memories = await list_memory_facts(message.from_user.id, limit=15)
    workouts = await recent_workouts(message.from_user.id, limit=3)

    lines = ["🧠 Long-term memory"]
    if memories:
        lines.extend(f"• #{m['id']} [{m['category']}] {m['key']}: {m['value']} (confidence {m['confidence']})" for m in memories)
    else:
        lines.append("• Durable facts пока не сохранены.")

    lines.append("\n🏋️ Последние структурированные тренировки")
    if workouts:
        for workout in workouts:
            lines.append(f"• {workout['started_at'][:10]} — {workout['title']} ({len(workout['sets'])} sets)")
    else:
        lines.append("• Пока пусто.")

    await message.answer("\n".join(lines)[:4096])
