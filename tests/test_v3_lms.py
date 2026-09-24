from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from database.models import Assignment, LmsSyncState
from database.time import aware
from services import assignments


def calendar(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Jarvis offline test//EN\r\n"
            + "".join(events) + "END:VCALENDAR\r\n").encode()


def event(uid="event-a", title="Assignment A", date="20990108T100000Z", extra=""):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART:{date}\r\n"
            f"CATEGORIES:New semester subject\r\n{extra}END:VEVENT\r\n")


@pytest.fixture
def feed(monkeypatch):
    response = Mock()
    response.raise_for_status = Mock()
    response.read = AsyncMock(return_value=calendar(event()))
    response_context = AsyncMock()
    response_context.__aenter__.return_value = response
    session = Mock()
    session.get.return_value = response_context
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    monkeypatch.setattr(assignments.aiohttp, "ClientSession", Mock(return_value=session_context))
    return response


def test_ical_timezone_and_all_day_deadline():
    parsed = assignments.parse_ical_events(calendar(event()))
    assert parsed[0]["due_at"] == aware(datetime(2099, 1, 8, 15))
    all_day = event().replace("DTSTART:20990108T100000Z", "DTSTART;VALUE=DATE:20990108")
    assert assignments.parse_ical_events(calendar(all_day))[0]["due_at"] == aware(datetime(2099, 1, 8, 23, 59))


async def test_lms_import_update_idempotency_and_completion_preserved(db, feed):
    first = await assignments.sync_lms_ical(42, "offline-feed")
    assert (first["created"], first["updated"]) == (1, 0)
    again = await assignments.sync_lms_ical(42, "offline-feed")
    assert (again["created"], again["unchanged"], again["changed"]) == (0, 1, [])
    assignment_id = first["changed"][0]["id"]
    await assignments.mark_assignment_done(42, assignment_id)
    feed.read.return_value = calendar(event(title="Revised assignment", date="20990109T100000Z"))
    changed = await assignments.sync_lms_ical(42, "offline-feed")
    assert changed["created"] == 0 and changed["updated"] == 1
    async with db() as session:
        rows = (await session.scalars(select(Assignment))).all()
        assert len(rows) == 1 and rows[0].status == "done"
        assert rows[0].title == "Revised assignment" and rows[0].course == "New semester subject"
        assert rows[0].due_at == aware(datetime(2099, 1, 9, 15))


async def test_duplicate_feed_uids_do_not_duplicate_assignments(db, feed):
    feed.read.return_value = calendar(event(), event())
    await assignments.sync_lms_ical(42, "offline-feed")
    async with db() as session:
        assert len((await session.scalars(select(Assignment))).all()) == 1


async def test_explicit_ical_cancellation_stops_reminders(db, feed):
    await assignments.sync_lms_ical(42, "offline-feed")
    feed.read.return_value = calendar(event(extra="STATUS:CANCELLED\r\n"))
    result = await assignments.sync_lms_ical(42, "offline-feed")
    assert result["updated"] == 1
    async with db() as session:
        assert (await session.scalar(select(Assignment))).status == "cancelled"


async def test_sync_failure_preserves_data_and_redacts_diagnostics(db, feed, caplog):
    await assignments.sync_lms_ical(42, "offline-feed")
    marker = "PRIVATE_FEED_CREDENTIAL_DO_NOT_LOG"
    feed.raise_for_status.side_effect = RuntimeError(marker)
    with pytest.raises(RuntimeError, match="LMS sync failed"):
        await assignments.sync_lms_ical(42, "offline-feed")
    async with db() as session:
        state = await session.get(LmsSyncState, 42)
        assert state.last_success_at is not None
        assert state.last_error == "RuntimeError"
        assert len((await session.scalars(select(Assignment))).all()) == 1
    assert marker not in caplog.text


async def test_invalid_feed_does_not_clear_deadlines(db, feed):
    await assignments.sync_lms_ical(42, "offline-feed")
    feed.read.return_value = b"not a calendar"
    with pytest.raises(RuntimeError):
        await assignments.sync_lms_ical(42, "offline-feed")
    async with db() as session:
        assert len((await session.scalars(select(Assignment))).all()) == 1


async def test_assignment_horizon_is_applied_before_limit_and_nulls_last(db):
    async with db() as session:
        session.add_all([
            Assignment(user_id=42, title="Unknown", due_at=None),
            Assignment(user_id=42, title="Due soon", due_at=datetime(2030, 1, 8, 12)),
            Assignment(user_id=42, title="Far away", due_at=datetime(2031, 1, 8, 12)),
        ])
        await session.commit()
    result = await assignments.list_upcoming_assignments(42, now=datetime(2030, 1, 7), limit=1)
    assert [row["title"] for row in result] == ["Due soon"]


async def test_deadline_query_converts_utc_to_local(db):
    async with db() as session:
        session.add(Assignment(user_id=42, title="Expired", due_at=datetime(2030, 1, 6, 23)))
        await session.commit()
    result = await assignments.list_upcoming_assignments(42, now=datetime(2030, 1, 7, 21, tzinfo=timezone.utc))
    assert result == []


@pytest.mark.acceptance_gap
@pytest.mark.xfail(strict=True, reason="Issue #3: feed disappearance has no deletion reconciliation/window contract")
async def test_deleted_lms_event_stops_being_pending(db, feed):
    await assignments.sync_lms_ical(42, "offline-feed")
    feed.read.return_value = calendar()
    await assignments.sync_lms_ical(42, "offline-feed")
    async with db() as session:
        assert (await session.scalar(select(Assignment))).status == "cancelled"
