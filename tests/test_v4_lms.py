from datetime import datetime

import pytest
from sqlalchemy import select

from database.models import Assignment
from database.time import aware
from database.v4_models import LmsEvent
from services.assignments import _serialize
from services.lms import classify, reconcile, snapshot
from test_v3_lms import calendar, event


@pytest.mark.parametrize("title,expected", [
    ("Attendance assignment", "attendance"), ("Посещаемость", "attendance"),
    ("WEB Quiz opens", "quiz_open"), ("OS тест закрывается", "quiz_close"),
    ("SDP assignment due", "assignment_due"), ("WEB lecture", "class_event"),
    ("Course announcement", "other"),
])
def test_classification(title, expected):
    assert classify(title) == expected


async def ingest(db, raw, window=None):
    items, parsed_window = snapshot(raw, now=aware(datetime(2099, 1, 7)))
    async with db() as session:
        result = await reconcile(session, 42, items, window or parsed_window,
                                 aware(datetime(2099, 1, 7)), _serialize)
        await session.commit()
        return result


async def test_only_assignment_due_and_quiz_close_create_tasks(db):
    data = calendar(*(event(str(i), title) for i, title in enumerate(
        ["Attendance", "Quiz opens", "Quiz closes", "Assignment due", "WEB lecture", "Announcement"])))
    result = await ingest(db, data)
    assert result["created"] == 2
    async with db() as session:
        assert len(list(await session.scalars(select(LmsEvent)))) == 6
        assert {t.title for t in await session.scalars(select(Assignment))} == {"Quiz closes", "Assignment due"}


async def test_missing_requires_repeated_authoritative_snapshots_and_preserves_done(db):
    await ingest(db, calendar(event("pending"), event("done", "Assignment complete")))
    async with db() as session:
        completed = await session.scalar(select(Assignment).where(Assignment.external_id == "done"))
        completed.status = "done"
        await session.commit()
    for _ in range(4):
        await ingest(db, calendar())  # Unknown window cannot cancel anything.
    outside = (aware(datetime(2100, 1, 1)), aware(datetime(2100, 2, 1)))
    for _ in range(4):
        await ingest(db, calendar(), outside)
    window = (aware(datetime(2099, 1, 1)), aware(datetime(2099, 2, 1)))
    for _ in range(2):
        await ingest(db, calendar(), window)
    async with db() as session:
        pending = await session.scalar(select(Assignment).where(Assignment.external_id == "pending"))
        assert pending.status == "pending"
    await ingest(db, calendar(), window)
    async with db() as session:
        assert (await session.scalar(select(Assignment).where(Assignment.external_id == "pending"))).status == "cancelled"
        assert (await session.scalar(select(Assignment).where(Assignment.external_id == "done"))).status == "done"


def test_recurrence_exception_override_and_html_sanitization():
    master = event("weekly", "WEB lecture", "20300107T100000Z",
                   "RRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE:20300114T100000Z\r\nDESCRIPTION:<p>Read chapter</p><script>bad()</script>\r\nX-TEACHER:Teacher A\r\n")
    override = event("weekly", "Moved lecture", "20300121T120000Z", "RECURRENCE-ID:20300121T100000Z\r\n")
    items, _ = snapshot(calendar(master, override), now=aware(datetime(2030, 1, 1)))
    assert len(items) == 2
    assert items[0]["description"] == "Read chapter" and items[0]["teacher"] == "Teacher A"
    assert items[1]["starts_at"].hour == 17
    assert items[1]["external_uid"].endswith("15:00:00+05:00")


async def test_seen_again_resets_missing_count_and_teacher_override_is_preserved(db):
    data = calendar(event())
    await ingest(db, data)
    window = (aware(datetime(2099, 1, 1)), aware(datetime(2099, 2, 1)))
    await ingest(db, calendar(), window)
    await ingest(db, data, window)
    async with db() as session:
        assert (await session.scalar(select(LmsEvent))).missing_sync_count == 0
        task = await session.scalar(select(Assignment))
        task.deadline_overridden = True
        task.due_at = aware(datetime(2099, 1, 20))
        await session.commit()
    await ingest(db, calendar(event(date="20990109T100000Z")))
    async with db() as session:
        assert (await session.scalar(select(Assignment))).due_at.day == 20


def test_malformed_event_rejects_whole_snapshot():
    with pytest.raises(ValueError):
        snapshot(calendar(event().replace("DTSTART:20990108T100000Z\r\n", "")))
