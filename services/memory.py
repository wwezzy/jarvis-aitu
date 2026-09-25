from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update as sql_update
from sqlalchemy.orm import selectinload

from database.engine import async_session_factory
from database.time import aware, utcnow
from database.models import (
    Assignment,
    DailyReflection,
    GTGSet,
    MemoryFact,
    Workout,
    WorkoutSession,
)

_STOP = {
    "что", "как", "когда", "какой", "какая", "какие", "мне", "мой", "моя", "мои", "это", "там",
    "было", "был", "была", "the", "and", "was", "were", "what", "when", "with", "from", "this", "that",
}

_DAY_NAMES = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[\w+-]{3,}", text or "")
        if token.lower() not in _STOP
    }


def _lexical_score(query_tokens: set[str], text: str) -> int:
    haystack = text.lower()
    return sum(1 for token in query_tokens if token in haystack)


def _is_memory_recall_query(query: str) -> bool:
    """Detect explicit requests to inspect the user's durable profile/memory."""
    value = (query or "").lower()
    markers = (
        "что ты обо мне помнишь",
        "что обо мне помнишь",
        "что ты помнишь обо мне",
        "что ты знаешь обо мне",
        "что знаешь обо мне",
        "моя память",
        "покажи память",
        "покажи что ты помнишь",
        "what do you remember about me",
        "what do you know about me",
        "show my memory",
    )
    return any(marker in value for marker in markers)


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


async def upsert_memory_updates(user_id: int, updates: list[dict]) -> int:
    if not updates:
        return 0
    saved = 0
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
            confidence = float(update.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                raise ValueError("confidence must be 0..1")
            row.confidence = confidence
            row.provenance = str(update.get("provenance") or "user statement")[:120]
            expiry = update.get("expires_at")
            row.expires_at = aware(datetime.fromisoformat(expiry)) if expiry else None
            saved += 1
        await db.commit()
    return saved


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
            "confidence": row.confidence,
            "provenance": row.provenance,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


async def delete_memory(user_id: int, memory_id: int):
    async with async_session_factory() as db:
        row = await db.get(MemoryFact, memory_id)
        if row is None or row.user_id != user_id:
            raise ValueError("Memory not found")
        await db.delete(row)
        await db.commit()


async def correct_memory(user_id: int, memory_id: int, value: str):
    if not value.strip() or len(value) > 8000:
        raise ValueError("Memory value must contain 1..8000 characters")
    async with async_session_factory() as db:
        row = await db.get(MemoryFact, memory_id)
        if row is None or row.user_id != user_id:
            raise ValueError("Memory not found")
        row.value, row.confidence, row.provenance, row.expires_at = value.strip(), 1.0, "user correction", None
        await db.commit()


async def build_memory_context(user_id: int, query: str, now: datetime) -> str:
    query_tokens = _tokens(query)
    local_now = aware(now)
    week_start = local_now - timedelta(days=7)
    assignment_horizon = local_now + timedelta(days=30)

    async with async_session_factory() as db:
        memory_result = await db.execute(
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id,
                   or_(MemoryFact.expires_at.is_(None), MemoryFact.expires_at > local_now))
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

        assignment_result = await db.execute(
            select(Assignment)
            .where(
                Assignment.user_id == user_id,
                Assignment.status == "pending",
                or_(
                    Assignment.due_at.is_(None),
                    Assignment.due_at >= local_now - timedelta(days=1),
                ),
            )
            .order_by(Assignment.due_at.asc(), Assignment.id.asc())
            .limit(60)
        )
        assignment_rows = [
            row
            for row in assignment_result.scalars().all()
            if row.due_at is None or row.due_at <= assignment_horizon
        ]

    if _is_memory_recall_query(query):
        # Explicit "what do you remember about me?" requests should inspect the
        # durable profile itself, not depend on lexical overlap with arbitrary
        # extractor-generated keys such as "device_model" or "education_level".
        ranked_memories = list(memory_rows[:30])
    else:
        ranked_memories = sorted(
            [row for row in memory_rows if _lexical_score(query_tokens, f"{row.category} {row.key} {row.value}") > 0],
            key=lambda row: (_lexical_score(query_tokens, f"{row.category} {row.key} {row.value}"), row.importance),
            reverse=True,
        )[:18]
        from services.semantic import semantic_ids
        related_ids = await semantic_ids(query, [{"id": r.id, "key": r.key, "value": r.value} for r in memory_rows])
        for row in memory_rows:
            if row.id in related_ids and row not in ranked_memories:
                ranked_memories.append(row)
        ranked_memories = ranked_memories[:18]
    if ranked_memories:
        async with async_session_factory() as db:
            await db.execute(sql_update(MemoryFact).where(MemoryFact.user_id == user_id,
                MemoryFact.id.in_([r.id for r in ranked_memories])).values(last_used_at=utcnow(), updated_at=MemoryFact.updated_at))
            await db.commit()

    def session_text(row: WorkoutSession) -> str:
        details = " | ".join(
            f"{s.exercise_name}: {s.weight_kg if s.weight_kg is not None else '-'}kg x "
            f"{s.reps if s.reps is not None else '-'} @RIR {s.rir if s.rir is not None else '-'}"
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

    day_sections: list[str] = []
    for offset in range(7):
        target = local_now.date() + timedelta(days=offset)
        from services.schedule import resolve_day
        rows = await resolve_day(user_id, target)
        label = f"{target.isoformat()} {_DAY_NAMES[target.weekday()]}"
        block_lines = [
            f"  - {row['start']}-{row['end']} {row['title']} ({row['block_type']}/{row['category']})"
            for row in rows
        ]
        day_sections.append(label + ("\n" + "\n".join(block_lines) if block_lines else "\n  - no blocks"))
    parts.append("UPCOMING 7-DAY SCHEDULE:\n" + "\n".join(day_sections))


    if assignment_rows:
        deadline_lines: list[str] = []
        for row in assignment_rows:
            due = row.due_at.strftime("%Y-%m-%d %H:%M") if row.due_at else "deadline unknown"
            course = f"[{row.course}] " if row.course else ""
            deadline_lines.append(
                f"- #{row.id} {due} {course}{row.title} (source={row.source})"
            )
        parts.append("UPCOMING ASSIGNMENTS / DEADLINES:\n" + "\n".join(deadline_lines))

    if ranked_memories:
        parts.append(
            "PERSISTENT FACTS:\n"
            + "\n".join(f"- [{row.category}; confidence={row.confidence}; source={row.provenance}] {row.key}: {row.value}" for row in ranked_memories)
        )

    if selected_sessions:
        parts.append(
            "STRUCTURED WORKOUT HISTORY:\n"
            + "\n".join(f"- {session_text(row)}" for row in selected_sessions)
        )
    elif legacy_rows:
        parts.append(
            "LEGACY WORKOUT NOTES:\n"
            + "\n".join(
                f"- {row.workout_date} {row.workout_type}: {row.notes or 'no notes'}"
                for row in legacy_rows
            )
        )

    if gtg_rows:
        week_reps = sum(row.reps for row in gtg_rows)
        parts.append(f"GTG LAST 7 DAYS: {week_reps} pull-up reps across {len(gtg_rows)} sets.")

    if reflection_rows:
        parts.append(
            "RECENT REFLECTIONS:\n"
            + "\n".join(
                f"- {row.reflection_date}: deep_work={row.deep_work_hours}h, protein={row.protein_hit}, "
                f"calories={row.calories_hit}, RIR={row.rir_respected}, mood={row.mood}, "
                f"energy={row.energy}, notes={row.notes or '-'}"
                for row in reflection_rows
            )
        )

    if any(word in query.lower() for word in ("study", "учеб", "защит", "defense", "quiz", "explain")):
        from services.study import study_context
        topics = await study_context(user_id)
        if topics:
            parts.append("STUDY (self-reported, not verified mastery):\n" + str(topics))
    return "\n\n".join(parts) if parts else "No durable user data stored yet."
