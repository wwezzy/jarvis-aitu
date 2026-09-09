from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.engine import async_session_factory
from database.models import DailyReflection, GTGSet, MemoryFact, ScheduleEntry, Workout, WorkoutSession

_STOP = {
    "что", "как", "когда", "какой", "какая", "какие", "мне", "мой", "моя", "мои", "это", "там",
    "было", "был", "была", "the", "and", "was", "were", "what", "when", "with", "from", "this", "that",
}


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9_+-]{3,}", text or "")
        if token.lower() not in _STOP
    }


def _lexical_score(query_tokens: set[str], text: str) -> int:
    haystack = text.lower()
    return sum(1 for token in query_tokens if token in haystack)


def merge_preferences(existing: str | None, new_text: str | None) -> str | None:
    if not new_text:
        return existing
    clean = new_text.strip()
    if not clean:
        return existing
    if not existing:
        return clean
    if clean.lower() in existing.lower():
        return existing
    return f"{existing.rstrip()}\n{clean}"[-12000:]


async def upsert_memory_updates(user_id: int, updates: list[dict]) -> None:
    if not updates:
        return
    async with async_session_factory() as db:
        for update in updates[:30]:
            key = str(update.get("key") or "").strip()[:120]
            value = str(update.get("value") or "").strip()[:8000]
            if not key or not value:
                continue
            result = await db.execute(
                select(MemoryFact).where(MemoryFact.user_id == user_id, MemoryFact.key == key)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = MemoryFact(user_id=user_id, key=key, value=value)
                db.add(row)
            row.category = str(update.get("category") or "other")[:50]
            row.value = value
            row.importance = max(1, min(int(update.get("importance") or 5), 10))
        await db.commit()


async def list_memory_facts(user_id: int, limit: int = 30) -> list[dict]:
    async with async_session_factory() as db:
        result = await db.execute(
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id)
            .order_by(MemoryFact.importance.desc(), MemoryFact.updated_at.desc())
            .limit(max(1, min(limit, 100)))
        )
        rows = result.scalars().all()
    return [
        {
            "id": row.id,
            "category": row.category,
            "key": row.key,
            "value": row.value,
            "importance": row.importance,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


async def build_memory_context(user_id: int, query: str, now: datetime) -> str:
    query_tokens = _tokens(query)
    week_start = now.replace(tzinfo=None) - timedelta(days=7)

    async with async_session_factory() as db:
        memory_result = await db.execute(
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id)
            .order_by(MemoryFact.importance.desc(), MemoryFact.updated_at.desc())
            .limit(100)
        )
        memory_rows = memory_result.scalars().all()

        sessions_result = await db.execute(
            select(WorkoutSession)
            .options(selectinload(WorkoutSession.sets))
            .where(WorkoutSession.user_id == user_id)
            .order_by(WorkoutSession.started_at.desc(), WorkoutSession.id.desc())
            .limit(100)
        )
        session_rows = sessions_result.scalars().unique().all()

        legacy_result = await db.execute(
            select(Workout)
            .where(Workout.user_id == user_id)
            .order_by(Workout.workout_date.desc(), Workout.id.desc())
            .limit(10)
        )
        legacy_rows = legacy_result.scalars().all()

        reflection_result = await db.execute(
            select(DailyReflection)
            .where(DailyReflection.user_id == user_id)
            .order_by(DailyReflection.reflection_date.desc())
            .limit(3)
        )
        reflection_rows = reflection_result.scalars().all()

        gtg_result = await db.execute(
            select(GTGSet)
            .where(GTGSet.user_id == user_id, GTGSet.logged_at >= week_start)
            .order_by(GTGSet.logged_at.desc())
        )
        gtg_rows = gtg_result.scalars().all()

        schedule_result = await db.execute(
            select(ScheduleEntry)
            .where(
                ScheduleEntry.user_id == user_id,
                ScheduleEntry.weekday == now.weekday(),
                ScheduleEntry.enabled.is_(True),
            )
            .order_by(ScheduleEntry.start.asc(), ScheduleEntry.sort_order.asc())
        )
        schedule_rows = schedule_result.scalars().all()

    ranked_memories = sorted(
        memory_rows,
        key=lambda row: (_lexical_score(query_tokens, f"{row.category} {row.key} {row.value}"), row.importance),
        reverse=True,
    )[:18]

    def session_text(row: WorkoutSession) -> str:
        details = " | ".join(
            f"{s.exercise_name}: {s.weight_kg if s.weight_kg is not None else '-'}kg x {s.reps if s.reps is not None else '-'} @RIR {s.rir if s.rir is not None else '-'}"
            for s in row.sets
        )
        return f"{row.started_at.date()} {row.title}: {details}"

    newest = session_rows[:3]
    relevant = sorted(
        session_rows[3:],
        key=lambda row: _lexical_score(query_tokens, session_text(row)),
        reverse=True,
    )
    relevant = [row for row in relevant if _lexical_score(query_tokens, session_text(row)) > 0][:5]
    selected_sessions = newest + [row for row in relevant if row not in newest]

    parts: list[str] = []
    if schedule_rows:
        parts.append(
            "TODAY PLAN:\n"
            + "\n".join(f"- {row.start}-{row.end} {row.title} ({row.block_type})" for row in schedule_rows)
        )

    if ranked_memories:
        parts.append("PERSISTENT FACTS:\n" + "\n".join(
            f"- [{row.category}] {row.key}: {row.value}" for row in ranked_memories
        ))

    if selected_sessions:
        parts.append("STRUCTURED WORKOUT HISTORY:\n" + "\n".join(f"- {session_text(row)}" for row in selected_sessions))
    elif legacy_rows:
        parts.append("LEGACY WORKOUT NOTES:\n" + "\n".join(
            f"- {row.workout_date} {row.workout_type}: {row.notes or 'no notes'}" for row in legacy_rows
        ))

    if gtg_rows:
        week_reps = sum(row.reps for row in gtg_rows)
        parts.append(f"GTG LAST 7 DAYS: {week_reps} pull-up reps across {len(gtg_rows)} sets.")

    if reflection_rows:
        parts.append("RECENT REFLECTIONS:\n" + "\n".join(
            f"- {row.reflection_date}: deep_work={row.deep_work_hours}h, protein={row.protein_hit}, "
            f"calories={row.calories_hit}, RIR={row.rir_respected}, mood={row.mood}, energy={row.energy}, notes={row.notes or '-'}"
            for row in reflection_rows
        ))

    return "\n\n".join(parts) if parts else "No durable user data stored yet."
