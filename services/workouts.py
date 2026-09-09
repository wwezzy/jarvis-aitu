from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.engine import async_session_factory
from database.models import ExerciseSet, WorkoutSession


def _clean_float(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if value is None or value == "":
        return None
    parsed = float(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"value must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"value must be <= {maximum}")
    return parsed


def _clean_int(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"value must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"value must be <= {maximum}")
    return parsed


async def log_workout(
    user_id: int,
    title: str,
    sets: list[dict[str, Any]],
    *,
    notes: str | None = None,
    source: str = "telegram",
    started_at: datetime | None = None,
) -> WorkoutSession:
    clean_title = (title or "Workout").strip()[:120] or "Workout"
    if not sets:
        raise ValueError("At least one exercise set is required")
    if len(sets) > 100:
        raise ValueError("Too many sets in one workout")

    session_row = WorkoutSession(
        user_id=user_id,
        title=clean_title,
        started_at=started_at or datetime.now(),
        completed_at=datetime.now(),
        notes=(notes or "").strip()[:4000] or None,
        source=source[:32],
    )

    counters: defaultdict[str, int] = defaultdict(int)
    for raw in sets:
        exercise = str(raw.get("exercise_name") or raw.get("exercise") or "").strip()
        if not exercise:
            continue
        counters[exercise] += 1
        set_number = _clean_int(raw.get("set_number"), minimum=1, maximum=100) or counters[exercise]
        reps = _clean_int(raw.get("reps"), minimum=0, maximum=1000)
        weight_kg = _clean_float(raw.get("weight_kg"), minimum=0, maximum=2000)
        rir = _clean_float(raw.get("rir"), minimum=0, maximum=10)
        technique_ok = raw.get("technique_ok")
        if technique_ok is not None:
            technique_ok = bool(technique_ok)

        session_row.sets.append(
            ExerciseSet(
                exercise_name=exercise[:160],
                set_number=set_number,
                weight_kg=weight_kg,
                reps=reps,
                rir=rir,
                technique_ok=technique_ok,
                notes=(str(raw.get("notes") or "").strip()[:1000] or None),
            )
        )

    if not session_row.sets:
        raise ValueError("No valid exercise sets supplied")

    async with async_session_factory() as db:
        db.add(session_row)
        await db.commit()
        await db.refresh(session_row)
        return session_row


def _serialize_session(row: WorkoutSession) -> dict:
    sets = []
    volume = 0.0
    for item in row.sets:
        if item.weight_kg is not None and item.reps is not None:
            volume += item.weight_kg * item.reps
        sets.append(
            {
                "id": item.id,
                "exercise_name": item.exercise_name,
                "set_number": item.set_number,
                "weight_kg": item.weight_kg,
                "reps": item.reps,
                "rir": item.rir,
                "technique_ok": item.technique_ok,
                "notes": item.notes,
            }
        )
    return {
        "id": row.id,
        "title": row.title,
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "notes": row.notes,
        "source": row.source,
        "volume_kg": round(volume, 1),
        "sets": sets,
    }


async def recent_workouts(user_id: int, limit: int = 8) -> list[dict]:
    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkoutSession)
            .options(selectinload(WorkoutSession.sets))
            .where(WorkoutSession.user_id == user_id)
            .order_by(WorkoutSession.started_at.desc(), WorkoutSession.id.desc())
            .limit(max(1, min(limit, 30)))
        )
        rows = result.scalars().unique().all()
    return [_serialize_session(row) for row in rows]


async def latest_exercise_history(user_id: int, exercise_name: str, limit: int = 4) -> list[dict]:
    async with async_session_factory() as db:
        result = await db.execute(
            select(ExerciseSet, WorkoutSession.started_at)
            .join(WorkoutSession, ExerciseSet.workout_session_id == WorkoutSession.id)
            .where(
                WorkoutSession.user_id == user_id,
                ExerciseSet.exercise_name.ilike(f"%{exercise_name.strip()}%"),
            )
            .order_by(WorkoutSession.started_at.desc(), ExerciseSet.set_number.asc())
            .limit(max(1, min(limit * 6, 60)))
        )
        rows = result.all()

    grouped: dict[str, dict] = {}
    for item, started_at in rows:
        key = started_at.isoformat()
        group = grouped.setdefault(key, {"started_at": key, "sets": []})
        group["sets"].append(
            {"weight_kg": item.weight_kg, "reps": item.reps, "rir": item.rir, "set_number": item.set_number}
        )
        if len(grouped) >= limit and key not in list(grouped.keys())[:limit]:
            break
    return list(grouped.values())[:limit]
