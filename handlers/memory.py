from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import get_settings
from services.memory import list_memory_facts
from services.workouts import recent_workouts

router = Router(name="memory")
settings = get_settings()


@router.message(Command("memory"))
async def memory_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return

    memories = await list_memory_facts(message.from_user.id, limit=15)
    workouts = await recent_workouts(message.from_user.id, limit=3)

    lines = ["🧠 Long-term memory"]
    if memories:
        lines.extend(f"• [{m['category']}] {m['key']}: {m['value']}" for m in memories)
    else:
        lines.append("• Durable facts пока не сохранены.")

    lines.append("\n🏋️ Последние структурированные тренировки")
    if workouts:
        for workout in workouts:
            lines.append(f"• {workout['started_at'][:10]} — {workout['title']} ({len(workout['sets'])} sets)")
    else:
        lines.append("• Пока пусто.")

    await message.answer("\n".join(lines)[:4096])
