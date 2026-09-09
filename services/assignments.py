from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, time, timedelta

import aiohttp
from icalendar import Calendar
from sqlalchemy import or_, select

from config import get_settings
from database.engine import async_session_factory
from database.models import Assignment, LmsSyncState

logger = logging.getLogger(__name__)
settings = get_settings()

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _local_naive(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(settings.timezone).replace(tzinfo=None)
    return datetime.combine(value, time(23, 59))


def _parse_local_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _serialize(row: Assignment) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "external_id": row.external_id,
        "course": row.course,
        "title": row.title,
        "due_at": row.due_at.isoformat(sep=" ") if row.due_at else None,
        "status": row.status,
        "url": row.url,
        "notes": row.notes,
    }


def parse_ical_events(raw: bytes | str) -> list[dict]:
    """Parse a Moodle-compatible iCal feed into normalized upcoming calendar events."""
    calendar = Calendar.from_ical(raw)
    events: list[dict] = []

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        summary = str(component.get("summary") or "").strip()
        if not summary:
            continue

        dt_value: date | datetime | None = None
        for key in ("dtstart", "dtend"):
            prop = component.get(key)
            if prop is not None:
                try:
                    candidate = prop.dt
                except AttributeError:
                    candidate = None
                if candidate is not None:
                    dt_value = candidate
                    if key == "dtstart":
                        break
        if dt_value is None:
            continue

        due_at = _local_naive(dt_value)
        uid = str(component.get("uid") or "").strip()
        if not uid:
            uid = hashlib.sha256(f"{summary}|{due_at.isoformat()}".encode("utf-8")).hexdigest()

        categories = component.get("categories")
        course: str | None = None
        if categories is not None:
            cats = getattr(categories, "cats", None)
            if cats:
                course = ", ".join(str(item) for item in cats if str(item).strip()) or None
            else:
                text = str(categories).strip()
                course = text or None

        description = str(component.get("description") or "").strip()
        url = str(component.get("url") or "").strip() or None
        if not url and description:
            match = _URL_RE.search(description)
            if match:
                url = match.group(0).rstrip(").,;")

        events.append(
            {
                "external_id": uid[:255],
                "course": course[:180] if course else None,
                "title": summary[:255],
                "due_at": due_at,
                "url": url,
                "notes": description[:8000] if description else None,
            }
        )

    events.sort(key=lambda item: item["due_at"])
    return events


async def upsert_assignment_payloads(
    user_id: int,
    payloads: list[dict],
    *,
    source: str = "telegram_llm",
) -> list[dict]:
    saved: list[dict] = []
    if not payloads:
        return saved

    async with async_session_factory() as db:
        for payload in payloads[:30]:
            title = str(payload.get("title") or "").strip()[:255]
            if not title:
                continue
            course = str(payload.get("course") or "").strip()[:180] or None
            due_at = _parse_local_datetime(payload.get("due_at"))
            url = str(payload.get("url") or "").strip() or None
            notes = str(payload.get("notes") or "").strip()[:8000] or None

            stmt = select(Assignment).where(
                Assignment.user_id == user_id,
                Assignment.status == "pending",
                Assignment.source == source,
                Assignment.title == title,
            )
            if course:
                stmt = stmt.where(Assignment.course == course)
            result = await db.execute(stmt.order_by(Assignment.id.desc()).limit(1))
            row = result.scalar_one_or_none()

            if row is None:
                row = Assignment(
                    user_id=user_id,
                    source=source,
                    course=course,
                    title=title,
                    due_at=due_at,
                    url=url,
                    notes=notes,
                    status="pending",
                )
                db.add(row)
            else:
                if course:
                    row.course = course
                if due_at is not None:
                    row.due_at = due_at
                if url:
                    row.url = url
                if notes:
                    row.notes = notes

            await db.flush()
            saved.append(_serialize(row))

        await db.commit()

    return saved


async def list_upcoming_assignments(
    user_id: int,
    *,
    now: datetime | None = None,
    days: int = 30,
    include_unknown: bool = True,
    limit: int = 50,
) -> list[dict]:
    local_now = (now or datetime.now(settings.timezone)).replace(tzinfo=None)
    horizon = local_now + timedelta(days=max(1, days))

    async with async_session_factory() as db:
        conditions = [
            Assignment.user_id == user_id,
            Assignment.status == "pending",
        ]
        if include_unknown:
            conditions.append(or_(Assignment.due_at.is_(None), Assignment.due_at >= local_now - timedelta(days=1)))
        else:
            conditions.append(Assignment.due_at.is_not(None))
            conditions.append(Assignment.due_at >= local_now - timedelta(days=1))

        result = await db.execute(
            select(Assignment)
            .where(*conditions)
            .order_by(Assignment.due_at.asc(), Assignment.id.asc())
            .limit(max(1, min(limit, 200)))
        )
        rows = list(result.scalars().all())

    filtered = [
        row
        for row in rows
        if row.due_at is None or row.due_at <= horizon
    ]
    return [_serialize(row) for row in filtered]


async def mark_assignment_done(user_id: int, assignment_id: int) -> dict:
    async with async_session_factory() as db:
        result = await db.execute(
            select(Assignment).where(
                Assignment.id == assignment_id,
                Assignment.user_id == user_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ValueError("assignment not found")
        row.status = "done"
        await db.commit()
        await db.refresh(row)
        return _serialize(row)


async def assignment_counts(user_id: int, *, now: datetime | None = None) -> dict:
    upcoming = await list_upcoming_assignments(user_id, now=now, days=60, limit=200)
    with_deadline = sum(1 for item in upcoming if item["due_at"])
    unknown = sum(1 for item in upcoming if not item["due_at"])
    return {
        "pending": len(upcoming),
        "with_deadline": with_deadline,
        "unknown_deadline": unknown,
    }


async def _update_lms_state(
    user_id: int,
    *,
    success: bool,
    error: str | None = None,
    created: int = 0,
    updated: int = 0,
) -> None:
    now = datetime.now(settings.timezone).replace(tzinfo=None)
    async with async_session_factory() as db:
        result = await db.execute(select(LmsSyncState).where(LmsSyncState.user_id == user_id))
        row = result.scalar_one_or_none()
        if row is None:
            row = LmsSyncState(user_id=user_id)
            db.add(row)

        row.last_attempt_at = now
        row.last_created = created
        row.last_updated = updated
        if success:
            row.last_success_at = now
            row.last_error = None
        else:
            row.last_error = (error or "Unknown LMS sync error")[:4000]

        await db.commit()


async def sync_lms_ical(user_id: int, url: str | None = None) -> dict:
    feed_url = (url or settings.lms_ical_url or "").strip()
    if not feed_url:
        return {
            "configured": False,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "changed": [],
        }
    if feed_url.startswith("webcal://"):
        feed_url = "https://" + feed_url[len("webcal://"):]

    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(feed_url, allow_redirects=True) as response:
                response.raise_for_status()
                raw = await response.read()

        parsed = parse_ical_events(raw)
        now = datetime.now(settings.timezone).replace(tzinfo=None)
        lower_bound = now - timedelta(days=2)

        created = 0
        updated = 0
        unchanged = 0
        changed: list[dict] = []

        async with async_session_factory() as db:
            for item in parsed:
                due_at = item["due_at"]
                if due_at < lower_bound:
                    continue

                result = await db.execute(
                    select(Assignment).where(
                        Assignment.user_id == user_id,
                        Assignment.source == "lms_ical",
                        Assignment.external_id == item["external_id"],
                    )
                )
                row = result.scalar_one_or_none()

                if row is None:
                    row = Assignment(
                        user_id=user_id,
                        source="lms_ical",
                        external_id=item["external_id"],
                        course=item["course"],
                        title=item["title"],
                        due_at=due_at,
                        status="pending",
                        url=item["url"],
                        notes=item["notes"],
                    )
                    db.add(row)
                    await db.flush()
                    created += 1
                    changed.append(_serialize(row))
                    continue

                before = (
                    row.course,
                    row.title,
                    row.due_at,
                    row.url,
                    row.notes,
                )
                row.course = item["course"]
                row.title = item["title"]
                row.due_at = due_at
                row.url = item["url"]
                row.notes = item["notes"]
                after = (
                    row.course,
                    row.title,
                    row.due_at,
                    row.url,
                    row.notes,
                )
                if before != after:
                    updated += 1
                    changed.append(_serialize(row))
                else:
                    unchanged += 1

            await db.commit()

        await _update_lms_state(
            user_id,
            success=True,
            created=created,
            updated=updated,
        )
        return {
            "configured": True,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "changed": changed[:20],
            "events_seen": len(parsed),
        }
    except Exception as exc:
        logger.exception("LMS iCal sync failed")
        await _update_lms_state(user_id, success=False, error=f"{type(exc).__name__}: {exc}")
        raise


async def lms_status(user_id: int) -> dict:
    async with async_session_factory() as db:
        result = await db.execute(select(LmsSyncState).where(LmsSyncState.user_id == user_id))
        row = result.scalar_one_or_none()

    return {
        "configured": bool(settings.lms_ical_url),
        "sync_minutes": settings.lms_sync_minutes,
        "last_attempt_at": row.last_attempt_at.isoformat(sep=" ") if row and row.last_attempt_at else None,
        "last_success_at": row.last_success_at.isoformat(sep=" ") if row and row.last_success_at else None,
        "last_error": row.last_error if row else None,
        "last_created": row.last_created if row else 0,
        "last_updated": row.last_updated if row else 0,
    }


def format_deadlines(items: list[dict], *, title: str = "📚 Ближайшие дедлайны") -> str:
    if not items:
        return f"{title}\n\nАктивных заданий пока нет."

    lines = [title]
    for item in items[:20]:
        due = item.get("due_at")
        if due:
            try:
                dt = datetime.fromisoformat(due)
                due_text = dt.strftime("%d.%m %H:%M")
            except ValueError:
                due_text = str(due)
        else:
            due_text = "дата не уточнена"

        course = f"[{item['course']}] " if item.get("course") else ""
        source = "LMS" if item.get("source") == "lms_ical" else "Jarvis"
        lines.append(f"#{item['id']} · {due_text} · {course}{item['title']} · {source}")

    return "\n".join(lines)
