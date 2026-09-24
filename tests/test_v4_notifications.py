from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from database.models import Assignment, ScheduleEntry
from database.time import aware
from database.v4_models import NotificationDelivery
from services.assignments import mark_assignment_done
from services.notifications import flush_queued, notification_feedback, notify_once
from services.schedule import save_override
from services.scheduler import send_dated_notice, sync_schedule_jobs
from test_v3_notifications import Jobs

NOW = aware(datetime(2030, 1, 7, 9))


async def protect(db):
    async with db() as session:
        session.add(ScheduleEntry(user_id=42, weekday=0, start="09:00", end="10:00",
                                  title="Class", category="study", block_type="fixed"))
        await session.commit()


async def test_dnd_is_queued_and_flushed_once(db):
    await protect(db)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert not await notify_once(bot, 42, "test:queued", "Queued", now=NOW)
    async with db() as session:
        assert (await session.scalar(select(NotificationDelivery))).status == "queued"
    assert await flush_queued(bot, 42, NOW + timedelta(hours=1)) == 1
    assert await flush_queued(bot, 42, NOW + timedelta(hours=1)) == 0
    bot.send_message.assert_awaited_once()


async def test_done_cancels_pending_dnd_notice(db):
    await protect(db)
    due = NOW + timedelta(hours=3)
    async with db() as session:
        task = Assignment(user_id=42, title="Deadline", due_at=due)
        session.add(task)
        await session.commit()
        task_id = task.id
    bot = SimpleNamespace(send_message=AsyncMock())
    await notify_once(bot, 42, f"deadline:{task_id}:{due.isoformat()}:180", "Deadline", now=NOW)
    await mark_assignment_done(42, task_id)
    assert await flush_queued(bot, 42, NOW + timedelta(hours=1)) == 0
    bot.send_message.assert_not_awaited()


async def test_expired_schedule_notice_does_not_arrive_after_class(db):
    await protect(db)
    bot = SimpleNamespace(send_message=AsyncMock())
    await notify_once(bot, 42, "event:expired", "Old notice", now=NOW, expires_at=NOW + timedelta(minutes=30))
    assert await flush_queued(bot, 42, NOW + timedelta(hours=1)) == 0


async def test_rescheduled_deadline_invalidates_dnd_queue(db):
    await protect(db)
    due = NOW + timedelta(hours=3)
    async with db() as session:
        task = Assignment(user_id=42, title="Moved", due_at=due)
        session.add(task)
        await session.commit()
        task_id = task.id
    bot = SimpleNamespace(send_message=AsyncMock())
    await notify_once(bot, 42, f"deadline:{task_id}:{due.isoformat()}:180", "Old deadline", now=NOW)
    async with db() as session:
        task = await session.get(Assignment, task_id)
        task.due_at = due + timedelta(days=1)
        await session.commit()
    assert await flush_queued(bot, 42, NOW + timedelta(hours=1)) == 0
    bot.send_message.assert_not_awaited()


async def test_feedback_owned_snooze_idempotent_and_mute_persists(db):
    bot = SimpleNamespace(send_message=AsyncMock())
    await notify_once(bot, 42, "test:feedback", "Notice", now=NOW)
    async with db() as session:
        notice_id = await session.scalar(select(NotificationDelivery.id))
    with pytest.raises(ValueError):
        await notification_feedback(43, notice_id, "snooze", NOW)
    for _ in range(2):
        await notification_feedback(42, notice_id, "snooze", NOW)
    async with db() as session:
        assert len(list(await session.scalars(select(NotificationDelivery)))) == 2
    await notification_feedback(42, notice_id, "mute", NOW)
    assert await flush_queued(bot, 42, NOW + timedelta(hours=1)) == 0


async def test_override_changes_notice_and_cancellation_revalidates(db, monkeypatch):
    from services import scheduler
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(scheduler, "datetime", Clock)
    async with db() as session:
        row = ScheduleEntry(user_id=42, weekday=0, start="11:00", end="12:00", title="Gym", category="training", notify_before_min=60)
        session.add(row)
        await session.commit()
        row_id = row.id
    await save_override(42, NOW.date(), row_id, {"start": "13:00", "end": "14:00"})
    jobs = Jobs()
    await sync_schedule_jobs(jobs, None, 42)
    dated = [j for j in jobs.get_jobs() if j.trigger == "date"]
    assert dated[0].run_date.hour == 12
    await save_override(42, NOW.date(), row_id, {}, cancelled=True)
    bot = SimpleNamespace(send_message=AsyncMock())
    await send_dated_notice(bot, 42, NOW.date().isoformat(), row_id, "13:00", 60)
    bot.send_message.assert_not_awaited()
