from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from config import get_settings
from database.engine import async_session_factory
from database.models import GTGSet

settings = get_settings()


def _local_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(settings.timezone) if now.tzinfo else now.replace(tzinfo=settings.timezone)
    start = datetime.combine(local.date(), time.min)
    return start, start + timedelta(days=1)


def _week_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(settings.timezone) if now.tzinfo else now.replace(tzinfo=settings.timezone)
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(monday, time.min)
    return start, start + timedelta(days=7)


async def add_gtg_set(user_id: int, reps: int, source: str = "button", when: datetime | None = None) -> GTGSet:
    if reps < 1 or reps > 100:
        raise ValueError("GTG reps must be between 1 and 100")
    item = GTGSet(
        user_id=user_id,
        reps=reps,
        source=source,
        logged_at=when or datetime.now(settings.timezone).replace(tzinfo=None),
    )
    async with async_session_factory() as session:
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def gtg_stats(user_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(settings.timezone)
    today_start, today_end = _local_day_bounds(now)
    week_start, week_end = _week_bounds(now)

    async with async_session_factory() as session:
        day_result = await session.execute(
            select(func.coalesce(func.sum(GTGSet.reps), 0), func.count(GTGSet.id)).where(
                GTGSet.user_id == user_id,
                GTGSet.logged_at >= today_start,
                GTGSet.logged_at < today_end,
            )
        )
        week_result = await session.execute(
            select(func.coalesce(func.sum(GTGSet.reps), 0), func.count(GTGSet.id)).where(
                GTGSet.user_id == user_id,
                GTGSet.logged_at >= week_start,
                GTGSet.logged_at < week_end,
            )
        )
        latest_result = await session.execute(
            select(GTGSet)
            .where(GTGSet.user_id == user_id)
            .order_by(GTGSet.logged_at.desc(), GTGSet.id.desc())
            .limit(8)
        )
        latest = latest_result.scalars().all()

    day_reps, day_sets = day_result.one()
    week_reps, week_sets = week_result.one()
    return {
        "today": {"reps": int(day_reps or 0), "sets": int(day_sets or 0)},
        "week": {"reps": int(week_reps or 0), "sets": int(week_sets or 0)},
        "recent": [
            {"id": row.id, "reps": row.reps, "logged_at": row.logged_at.isoformat(), "source": row.source}
            for row in latest
        ],
    }
