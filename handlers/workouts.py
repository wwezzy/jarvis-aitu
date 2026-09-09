from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import get_settings
from services.progression import double_progression_recommendation
from services.workouts import recent_workouts

router = Router(name="workouts")
settings = get_settings()


@router.message(Command("workouts"))
async def workouts_command(message: Message) -> None:
    if not message.from_user or message.from_user.id != settings.admin_id:
        return

    workouts = await recent_workouts(message.from_user.id, limit=5)
    if not workouts:
        await message.answer("Структурированный журнал тренировок пока пуст. Запишите тренировку сообщением или через Mini App.")
        return

    lines = ["🏋️ Последние тренировки"]
    for workout in workouts:
        lines.append(f"\n{workout['started_at'][:10]} · {workout['title']} · volume {workout['volume_kg']:.0f} кг")
        grouped: dict[str, list[dict]] = {}
        for item in workout["sets"]:
            grouped.setdefault(item["exercise_name"], []).append(item)
        for exercise, sets in grouped.items():
            compact = ", ".join(
                f"{s['weight_kg'] if s['weight_kg'] is not None else '-'}×{s['reps'] if s['reps'] is not None else '-'} @RIR {s['rir'] if s['rir'] is not None else '-'}"
                for s in sets
            )
            lines.append(f"• {exercise}: {compact}")
            recommendation = double_progression_recommendation(sets)
            if recommendation["action"] != "insufficient_data":
                lines.append(f"  ↳ {recommendation['action']}: {recommendation['reason']}")

    await message.answer("\n".join(lines)[:4096])
