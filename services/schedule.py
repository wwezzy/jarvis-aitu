from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from sqlalchemy import delete, func, select

from config import get_settings
from database.engine import async_session_factory
from database.models import ScheduleEntry
from database.time import aware
from database.v4_models import LmsEvent, PersonalCalendarEvent, ScheduleOverride

settings = get_settings()

DAY_NAMES_RU = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}

DAY_FOCUS = {
    0: "Gym A + AITU",
    1: "Deep Work + AITU + Pool",
    2: "Gym B + Blog + AITU",
    3: "Academic Focus",
    4: "Gym C + AITU",
    5: "Async Study + Algorithms",
    6: "Weekly Reset",
}

ALLOWED_CATEGORIES = {
    "routine",
    "recovery",
    "training",
    "study",
    "deep_work",
    "content",
    "nutrition",
    "commute",
    "rest",
    "reflection",
    "sleep",
    "chores",
    "weekly_review",
    "flex",
}
ALLOWED_BLOCK_TYPES = {"fixed", "planned", "flex"}


@dataclass(frozen=True, slots=True)
class DefaultBlock:
    start: str
    end: str
    title: str
    category: str
    block_type: str = "planned"
    notify_before_min: int | None = 30


def _b(
    start: str,
    end: str,
    title: str,
    category: str,
    block_type: str = "planned",
    notify: int | None = 30,
) -> DefaultBlock:
    return DefaultBlock(start, end, title, category, block_type, notify)


# Real AITU timetable + training plan + protected recovery blocks.
# Green "Online" classes from the university schedule are treated as asynchronous content.
DEFAULT_WEEK_SCHEDULE: dict[int, tuple[DefaultBlock, ...]] = {
    0: (
        _b("07:30", "08:00", "Подъём, вода, завтрак", "routine", notify=None),
        _b("08:00", "08:20", "Самокоррекция / ЛФК", "recovery", notify=None),
        _b("08:30", "10:00", "Full-body A", "training"),
        _b("10:00", "10:45", "Душ + восстановление + еда", "recovery", notify=None),
        _b("11:00", "12:30", "ДЗ / лабораторные / подготовка к парам", "study"),
        _b("12:30", "13:10", "Обед", "nutrition", notify=None),
        _b("13:15", "13:45", "Дорога в AITU", "commute", notify=None),
        _b("14:00", "15:50", "AITU — WEB технологии 1 (Фронтенд), практика", "study", "fixed"),
        _b("16:00", "17:50", "AITU — Операционные системы, практика", "study", "fixed"),
        _b("18:00", "18:30", "Дорога домой", "commute", notify=None),
        _b("18:30", "19:30", "Ужин + полный reset", "recovery", notify=None),
        _b("19:30", "20:45", "Разбор пар / мелкое ДЗ", "study"),
        _b("20:45", "22:00", "Свободное время / книга", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Вечерняя рефлексия", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
    1: (
        _b("07:45", "08:20", "Утро + самокоррекция", "recovery", notify=None),
        _b("08:30", "10:30", "Deep Work — Jarvis / backend / новое", "deep_work"),
        _b("10:45", "12:35", "Async: Казахский (русский) — 2 лекции", "study", "flex"),
        _b("12:35", "13:15", "Обед", "nutrition", notify=None),
        _b("13:20", "13:50", "Дорога в AITU", "commute", notify=None),
        _b("14:00", "15:50", "AITU — Казахский (русский) язык 1 (B1), практика", "study", "fixed"),
        _b("16:30", "18:00", "Бассейн", "training", "planned"),
        _b("18:00", "19:15", "Дорога + еда + восстановление", "recovery", notify=None),
        _b("19:30", "21:00", "ДЗ / лабораторные", "study"),
        _b("21:00", "21:30", "Книга", "rest", "flex", notify=None),
        _b("21:30", "22:30", "Свободное время", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Вечерняя рефлексия", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
    2: (
        _b("07:45", "08:20", "Утро + самокоррекция", "recovery", notify=None),
        _b("08:30", "10:00", "Full-body B", "training"),
        _b("10:00", "10:45", "Душ + восстановление + еда", "recovery", notify=None),
        _b("11:00", "11:50", "Async: Казахский (русский) — лекция 3", "study", "flex"),
        _b("12:00", "13:30", "Blog Lock-in — сценарий / съёмка / монтаж", "content"),
        _b("13:30", "14:10", "Обед", "nutrition", notify=None),
        _b("14:20", "14:50", "Дорога в AITU", "commute", notify=None),
        _b("15:00", "15:50", "AITU — WEB технологии 1 (Фронтенд), практика", "study", "fixed"),
        _b("16:00", "17:50", "AITU — Шаблон проектирования ПО, практика", "study", "fixed"),
        _b("18:00", "18:30", "Дорога домой", "commute", notify=None),
        _b("18:30", "19:30", "Ужин + отдых", "recovery", notify=None),
        _b("20:00", "21:30", "ДЗ / подготовка к четвергу", "study"),
        _b("21:30", "22:15", "Книга / отдых", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Вечерняя рефлексия", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
    3: (
        _b("07:45", "08:15", "Утро + самокоррекция", "recovery", notify=None),
        _b("08:30", "10:20", "Async: Операционные системы — 2 лекции", "study", "flex"),
        _b("10:20", "11:00", "Еда + подготовка", "nutrition", notify=None),
        _b("11:15", "11:45", "Дорога в AITU", "commute", notify=None),
        _b("12:00", "13:55", "AITU — Дизайн и анализ алгоритмов, практика", "study", "fixed"),
        _b("14:00", "15:50", "AITU — Шаблон проектирования ПО, лекции", "study", "fixed"),
        _b("16:00", "16:40", "Дорога домой", "commute", notify=None),
        _b("16:40", "17:40", "Еда + полный отдых", "recovery", notify=None),
        _b("17:45", "19:30", "Deep Study — Algorithms / SDP", "deep_work"),
        _b("19:30", "20:00", "Прогулка + mobility", "recovery", notify=None),
        _b("20:00", "21:30", "Блог / монтаж", "content"),
        _b("21:30", "22:30", "Свободное время / книга", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Вечерняя рефлексия", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
    4: (
        _b("07:45", "08:20", "Утро + самокоррекция", "recovery", notify=None),
        _b("08:30", "10:00", "Full-body C", "training"),
        _b("10:00", "10:45", "Душ + восстановление + еда", "recovery", notify=None),
        _b("11:00", "12:30", "Weekly Deadline Block — лабы / задания", "study"),
        _b("12:30", "13:30", "Обед", "nutrition", notify=None),
        _b("13:30", "14:10", "Admin / подготовка к парам", "flex", "flex", notify=None),
        _b("14:20", "14:50", "Дорога в AITU", "commute", notify=None),
        _b("15:00", "15:50", "AITU — Шаблон проектирования ПО, практика", "study", "fixed"),
        _b("16:00", "16:50", "AITU — Операционные системы, практика", "study", "fixed"),
        _b("17:00", "17:50", "AITU — Дизайн и анализ алгоритмов, практика", "study", "fixed"),
        _b("18:00", "18:30", "Дорога домой", "commute", notify=None),
        _b("18:30", "19:30", "Ужин + восстановление", "recovery", notify=None),
        _b("19:30", "22:30", "Защищённый свободный вечер", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Вечерняя рефлексия", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
    5: (
        _b("08:30", "08:50", "Самокоррекция", "recovery", notify=None),
        _b("09:00", "10:50", "Async: WEB технологии 1 — 2 лекции", "study", "flex"),
        _b("11:00", "12:30", "Personal project / новый материал", "deep_work"),
        _b("12:30", "13:15", "Обед", "nutrition", notify=None),
        _b("13:15", "14:00", "Algorithms — подготовка / review недели", "study"),
        _b("14:20", "14:50", "Дорога в AITU", "commute", notify=None),
        _b("15:00", "16:50", "AITU — Дизайн и анализ алгоритмов, лекции", "study", "fixed"),
        _b("17:00", "17:30", "Дорога / reset", "commute", notify=None),
        _b("17:30", "19:00", "Опционально: бассейн / длинная прогулка", "training", "flex", notify=None),
        _b("19:00", "22:30", "Свободный вечер", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Вечерняя рефлексия", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
    6: (
        _b("08:30", "09:30", "Спокойный подъём + завтрак", "routine", notify=None),
        _b("09:30", "12:00", "Полная уборка + стирка", "chores"),
        _b("12:00", "13:30", "Закуп продуктов", "chores"),
        _b("13:30", "14:30", "Еда + разложить продукты + стирка", "recovery", notify=None),
        _b("14:30", "15:15", "Weekly Review с Jarvis", "weekly_review"),
        _b("15:15", "16:45", "Только важные хвосты + подготовка к понедельнику", "study"),
        _b("17:00", "18:00", "Прогулка", "recovery", "flex", notify=None),
        _b("18:00", "22:30", "Защищённый отдых / друзья / книга / фильм", "rest", "flex", notify=None),
        _b("22:30", "23:00", "Weekly Reflection", "reflection", notify=None),
        _b("23:30", "23:59", "Full Sleep Mode", "sleep", notify=None),
    ),
}


def _clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _validate_clock(value: str, field: str) -> str:
    value = str(value or "").strip()
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"{field} must use HH:MM") from exc
    return datetime.strptime(value, "%H:%M").strftime("%H:%M")


def _validate_entry_payload(payload: dict) -> dict:
    try:
        weekday = int(payload.get("weekday"))
    except (TypeError, ValueError) as exc:
        raise ValueError("weekday must be 0..6") from exc
    if weekday not in range(7):
        raise ValueError("weekday must be 0..6")

    start = _validate_clock(payload.get("start"), "start")
    end = _validate_clock(payload.get("end"), "end")
    if _clock(end) <= _clock(start):
        raise ValueError("end must be later than start")

    title = str(payload.get("title") or "").strip()[:180]
    if not title:
        raise ValueError("title is required")

    category = str(payload.get("category") or "flex").strip()
    if category not in ALLOWED_CATEGORIES:
        raise ValueError("unsupported category")

    block_type = str(payload.get("block_type") or "planned").strip()
    if block_type not in ALLOWED_BLOCK_TYPES:
        raise ValueError("unsupported block_type")

    notify_raw = payload.get("notify_before_min")
    if notify_raw is None or notify_raw == "" or notify_raw is False:
        notify_before_min = None
    else:
        try:
            notify_before_min = int(notify_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("notify_before_min must be an integer") from exc
        if not 0 <= notify_before_min <= 180:
            raise ValueError("notify_before_min must be 0..180")

    extras = {}
    for key, low, high, default in (("commute_minutes", 0, 240, 0), ("preparation_minutes", 0, 240, 0), ("importance", 1, 10, 5)):
        extras[key] = int(payload.get(key, default))
        if not low <= extras[key] <= high:
            raise ValueError(f"Invalid {key}")
    return {
        "weekday": weekday,
        "start": start,
        "end": end,
        "title": title,
        "category": category,
        "block_type": block_type,
        "notify_before_min": notify_before_min,
        "location": str(payload.get("location") or "").strip()[:255] or None,
        **extras,
    }


def _row_to_dict(row: ScheduleEntry, now: datetime | None = None) -> dict:
    payload = {
        "id": row.id,
        "weekday": row.weekday,
        "start": row.start,
        "end": row.end,
        "title": row.title,
        "category": row.category,
        "block_type": row.block_type,
        "notify_before_min": row.notify_before_min,
        "enabled": row.enabled,
        "sort_order": row.sort_order,
        "location": row.location,
        "commute_minutes": row.commute_minutes,
        "preparation_minutes": row.preparation_minutes,
        "importance": row.importance,
        "provenance": row.provenance,
    }
    if now is not None and row.weekday == now.weekday():
        current_time = now.time().replace(tzinfo=None)
        start_t = _clock(row.start)
        end_t = _clock(row.end)
        if start_t <= current_time < end_t:
            payload["status"] = "current"
        elif current_time < start_t:
            payload["status"] = "upcoming"
        else:
            payload["status"] = "done"
    else:
        payload["status"] = "idle"
    return payload


async def ensure_default_schedule(user_id: int, *, force: bool = False) -> int:
    async with async_session_factory() as db:
        if force:
            await db.execute(delete(ScheduleEntry).where(ScheduleEntry.user_id == user_id))
        else:
            count = await db.scalar(select(func.count()).select_from(ScheduleEntry).where(ScheduleEntry.user_id == user_id))
            if count:
                return int(count)

        rows: list[ScheduleEntry] = []
        for weekday, blocks in DEFAULT_WEEK_SCHEDULE.items():
            for order, block in enumerate(blocks):
                rows.append(
                    ScheduleEntry(
                        user_id=user_id,
                        weekday=weekday,
                        start=block.start,
                        end=block.end,
                        title=block.title,
                        category=block.category,
                        block_type=block.block_type,
                        notify_before_min=block.notify_before_min,
                        sort_order=order,
                        enabled=True,
                    )
                )
        db.add_all(rows)
        await db.commit()
        return len(rows)


async def list_schedule_entries(user_id: int, weekday: int | None = None) -> list[ScheduleEntry]:
    async with async_session_factory() as db:
        stmt = select(ScheduleEntry).where(ScheduleEntry.user_id == user_id, ScheduleEntry.enabled.is_(True))
        if weekday is not None:
            stmt = stmt.where(ScheduleEntry.weekday == weekday)
        stmt = stmt.order_by(ScheduleEntry.weekday.asc(), ScheduleEntry.start.asc(), ScheduleEntry.sort_order.asc(), ScheduleEntry.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())


async def week_schedule(user_id: int) -> dict:
    rows = await list_schedule_entries(user_id)
    days = []
    for weekday in range(7):
        blocks = [_row_to_dict(row) for row in rows if row.weekday == weekday]
        days.append(
            {
                "weekday": weekday,
                "name": DAY_NAMES_RU[weekday],
                "focus": DAY_FOCUS[weekday],
                "blocks": blocks,
            }
        )
    return {"days": days}


async def today_schedule(user_id: int, now: datetime | None = None) -> dict:
    local = now or datetime.now(settings.timezone)
    if local.tzinfo is None:
        local = local.replace(tzinfo=settings.timezone)
    else:
        local = local.astimezone(settings.timezone)

    rows = await list_schedule_entries(user_id, local.weekday())
    return {
        "date": local.date().isoformat(),
        "weekday": local.weekday(),
        "weekday_name": DAY_NAMES_RU[local.weekday()],
        "focus": DAY_FOCUS[local.weekday()],
        "blocks": await resolve_day(user_id, local.date(), rows=rows, now=local),
    }


async def resolve_day(user_id: int, on_date: date, *, rows=None, now=None) -> list[dict]:
    rows = rows if rows is not None else await list_schedule_entries(user_id, on_date.weekday())
    blocks = {row.id: _row_to_dict(row) for row in rows}
    left = aware(datetime.combine(on_date, time.min))
    right = left + timedelta(days=1)
    async with async_session_factory() as db:
        overrides = list(await db.scalars(select(ScheduleOverride).where(
            ScheduleOverride.user_id == user_id, ScheduleOverride.on_date == on_date)))
        lms_events = list(await db.scalars(select(LmsEvent).where(
            LmsEvent.user_id == user_id, LmsEvent.event_type.in_(["class_event", "attendance"]),
            LmsEvent.status == "active", LmsEvent.starts_at >= left, LmsEvent.starts_at < right)))
        personal = list(await db.scalars(select(PersonalCalendarEvent).where(
            PersonalCalendarEvent.user_id == user_id, PersonalCalendarEvent.starts_at < right,
            PersonalCalendarEvent.ends_at > left)))
    for override in overrides:
        if override.entry_id is not None:
            if override.cancelled:
                blocks.pop(override.entry_id, None)
            elif override.entry_id in blocks:
                blocks[override.entry_id].update(override.changes, override_id=override.id,
                                                  provenance=override.provenance)
        elif not override.cancelled:
            blocks[f"dated:{override.id}"] = dict(override.changes, id=f"dated:{override.id}",
                override_id=override.id, provenance=override.provenance)
    for row in lms_events + personal:
        start = max(left, row.starts_at)
        # Unknown class duration remains explicit; use a one-hour planning reserve.
        end = min(right - timedelta(minutes=1), row.ends_at or start + timedelta(hours=1))
        identity = f"lms:{row.id}" if isinstance(row, LmsEvent) else f"google:{row.id}"
        if any(b["title"] == row.title and b["start"] == start.strftime("%H:%M") for b in blocks.values()):
            continue
        blocks[identity] = dict(id=identity, start=start.strftime("%H:%M"), end=end.strftime("%H:%M"),
            title=row.title, category="study" if isinstance(row, LmsEvent) else "flex", block_type="fixed",
            notify_before_min=60, commute_minutes=0, preparation_minutes=0, importance=5,
            provenance="LMS" if isinstance(row, LmsEvent) else "Google Calendar",
            duration_estimated=row.ends_at is None, enabled=True)
    result = sorted(blocks.values(), key=lambda b: (b["start"], str(b["id"])))
    for block in result:
        block["date"] = on_date.isoformat()
        block["weekday"] = on_date.weekday()
        block["status"] = "idle"
        if now and aware(now).date() == on_date:
            clock = aware(now).strftime("%H:%M")
            block["status"] = "current" if block["start"] <= clock < block["end"] else "upcoming" if clock < block["start"] else "elapsed"
    return result


async def save_override(user_id: int, on_date: date, entry_id: int | None, changes: dict, *, cancelled=False):
    if not isinstance(on_date, date):
        raise ValueError("Invalid override date")
    allowed = {"start", "end", "title", "category", "block_type", "notify_before_min", "location", "commute_minutes", "preparation_minutes", "importance"}
    if set(changes) - allowed:
        raise ValueError("Unsupported override fields")
    async with async_session_factory() as db:
        entry = await db.get(ScheduleEntry, entry_id) if entry_id else None
        if entry_id and (not entry or entry.user_id != user_id or entry.weekday != on_date.weekday()):
            raise ValueError("Schedule occurrence not found")
        if not cancelled:
            _validate_entry_payload({**(_row_to_dict(entry) if entry else {}), **changes, "weekday": on_date.weekday()})
        row = await db.scalar(select(ScheduleOverride).where(ScheduleOverride.user_id == user_id,
            ScheduleOverride.entry_id == entry_id, ScheduleOverride.on_date == on_date)) if entry_id else None
        if row is None:
            row = ScheduleOverride(user_id=user_id, entry_id=entry_id, on_date=on_date)
            db.add(row)
        row.changes, row.cancelled = changes, bool(cancelled)
        await db.commit()
        return {"id": row.id, "date": on_date.isoformat(), "entry_id": entry_id, "cancelled": row.cancelled}


async def resolved_week(user_id: int, start: date) -> list[dict]:
    return [{"date": (start + timedelta(days=n)).isoformat(),
             "blocks": await resolve_day(user_id, start + timedelta(days=n))} for n in range(7)]


async def upsert_schedule_entry(user_id: int, payload: dict) -> dict:
    values = _validate_entry_payload(payload)
    entry_id = payload.get("id")

    async with async_session_factory() as db:
        row: ScheduleEntry | None = None
        if entry_id not in (None, ""):
            try:
                entry_id = int(entry_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid schedule id") from exc
            result = await db.execute(
                select(ScheduleEntry).where(ScheduleEntry.id == entry_id, ScheduleEntry.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise ValueError("schedule block not found")

        if row is None:
            row = ScheduleEntry(user_id=user_id, **values)
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
            row.enabled = True

        await db.commit()
        await db.refresh(row)
        return _row_to_dict(row)


async def delete_schedule_entry(user_id: int, entry_id: int) -> None:
    async with async_session_factory() as db:
        result = await db.execute(
            select(ScheduleEntry).where(ScheduleEntry.id == entry_id, ScheduleEntry.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ValueError("schedule block not found")
        await db.delete(row)
        await db.commit()


async def reset_schedule(user_id: int) -> int:
    return await ensure_default_schedule(user_id, force=True)


def default_blocks_for_weekday(weekday: int) -> tuple[DefaultBlock, ...]:
    return DEFAULT_WEEK_SCHEDULE.get(weekday, ())


def default_schedule_count() -> int:
    return sum(len(items) for items in DEFAULT_WEEK_SCHEDULE.values())
