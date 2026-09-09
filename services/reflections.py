from __future__ import annotations

from datetime import date

from sqlalchemy import select

from database.engine import async_session_factory
from database.models import DailyReflection


async def upsert_reflection(
    user_id: int,
    reflection_date: date,
    *,
    deep_work_hours: float | None = None,
    calories_hit: bool | None = None,
    protein_hit: bool | None = None,
    rir_respected: bool | None = None,
    mood: int | None = None,
    energy: int | None = None,
    notes: str | None = None,
) -> DailyReflection:
    if deep_work_hours is not None and not 0 <= deep_work_hours <= 24:
        raise ValueError("deep_work_hours must be between 0 and 24")
    if mood is not None and not 1 <= mood <= 5:
        raise ValueError("mood must be 1..5")
    if energy is not None and not 1 <= energy <= 5:
        raise ValueError("energy must be 1..5")

    async with async_session_factory() as db:
        result = await db.execute(
            select(DailyReflection).where(
                DailyReflection.user_id == user_id,
                DailyReflection.reflection_date == reflection_date,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DailyReflection(user_id=user_id, reflection_date=reflection_date)
            db.add(row)

        row.deep_work_hours = deep_work_hours
        row.calories_hit = calories_hit
        row.protein_hit = protein_hit
        row.rir_respected = rir_respected
        row.mood = mood
        row.energy = energy
        row.notes = (notes or "").strip()[:4000] or None
        await db.commit()
        await db.refresh(row)
        return row


async def latest_reflection(user_id: int) -> dict | None:
    async with async_session_factory() as db:
        result = await db.execute(
            select(DailyReflection)
            .where(DailyReflection.user_id == user_id)
            .order_by(DailyReflection.reflection_date.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    return {
        "date": row.reflection_date.isoformat(),
        "deep_work_hours": row.deep_work_hours,
        "calories_hit": row.calories_hit,
        "protein_hit": row.protein_hit,
        "rir_respected": row.rir_respected,
        "mood": row.mood,
        "energy": row.energy,
        "notes": row.notes,
    }
