import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config import get_settings
from database.models import Assignment, Reminder, ScheduleEntry
from database.notification_models import NotificationLog
from services import notifications, scheduler


NOW = datetime(2030, 1, 7, 9, tzinfo=get_settings().timezone)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(scheduler, "datetime", FixedDatetime)
    monkeypatch.setattr(notifications, "datetime", FixedDatetime)


class Jobs:
    """Recorder for scheduler registration; callbacks are invoked explicitly."""
    def __init__(self):
        self.jobs = {}

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id):
        del self.jobs[job_id]

    def add_job(self, callback, trigger, **kwargs):
        self.jobs[kwargs["id"]] = SimpleNamespace(callback=callback, trigger=trigger, **kwargs)


async def test_durable_dedup_and_user_isolation(db):
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await notifications.notify_once(bot, 42, "event", "test", now=NOW)
    assert not await notifications.notify_once(bot, 42, "event", "test", now=NOW)
    # A fresh DB session sees the claim; no in-process dedup dictionary is involved.
    async with db() as session:
        row = await session.scalar(select(NotificationLog))
        assert row.kind == "event"
    assert await notifications.notify_once(bot, 43, "event", "test", now=NOW)
    assert bot.send_message.await_count == 2


async def test_concurrent_dispatch_claims_only_once(db):
    bot = SimpleNamespace(send_message=AsyncMock())
    results = await asyncio.gather(*[
        notifications.notify_once(bot, 42, "concurrent", "test", now=NOW) for _ in range(3)
    ])
    assert sum(results) == 1
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("category,block_type", [
    ("study", "fixed"), ("training", "planned"), ("sleep", "planned"), ("deep_work", "planned"),
])
async def test_dnd_during_protected_blocks_with_critical_override(db, category, block_type):
    async with db() as session:
        session.add(ScheduleEntry(user_id=42, weekday=0, start="08:00", end="10:00",
                                  title="Protected", category=category, block_type=block_type))
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    assert not await notifications.notify_once(bot, 42, "event", "test", now=NOW)
    bot.send_message.assert_not_awaited()
    assert await notifications.notify_once(bot, 42, "event", "test", now=NOW, critical=True)


@pytest.mark.parametrize("hour,minute,expected", [(23, 29, False), (23, 30, True), (0, 0, True), (6, 59, True), (7, 0, False)])
async def test_sleep_dnd_spans_midnight(db, hour, minute, expected):
    assert await notifications.is_dnd(42, NOW.replace(hour=hour, minute=minute)) is expected


async def test_delivery_timeout_does_not_cause_duplicate_retry(db):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
    assert not await notifications.notify_once(bot, 42, "event", "test", now=NOW)
    assert not await notifications.notify_once(bot, 42, "event", "test", now=NOW)
    bot.send_message.assert_awaited_once()
    async with db() as session:
        assert (await session.scalar(select(NotificationLog))).event_key == "event"


async def test_class_and_gym_are_warned_60_minutes_before_without_routine_spam(db):
    async with db() as session:
        session.add_all([
            ScheduleEntry(user_id=42, weekday=0, start="10:00", end="11:00", title="Class", category="study", block_type="fixed", notify_before_min=30),
            ScheduleEntry(user_id=42, weekday=0, start="12:00", end="13:00", title="Gym", category="training", notify_before_min=30),
            ScheduleEntry(user_id=42, weekday=0, start="14:00", end="15:00", title="Study", category="study", notify_before_min=30),
        ])
        await session.commit()
    jobs = Jobs()
    assert await scheduler.sync_schedule_jobs(jobs, None, 42) == 2
    assert {(j.hour, j.minute) for j in jobs.get_jobs()} == {(9, 0), (11, 0)}
    assert all(j.args[-1] == 60 for j in jobs.get_jobs())
    assert await scheduler.sync_schedule_jobs(jobs, None, 42) == 2
    assert len(jobs.get_jobs()) == 2


def test_60_minute_warning_rolls_back_to_previous_weekday():
    assert scheduler._notification_trigger(0, "00:30", 60) == ("sun", 23, 30)


async def test_schedule_callback_dedup_and_disabled_notice(db):
    async with db() as session:
        row = ScheduleEntry(user_id=42, weekday=0, start="10:00", end="11:00", title="Class",
                            category="study", block_type="fixed", notify_before_min=30)
        session.add(row)
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduler.send_schedule_notice(bot, 42, row.id, "10:00", "Class", "study", 60)
    await scheduler.send_schedule_notice(bot, 42, row.id, "10:00", "Class", "study", 60)
    bot.send_message.assert_awaited_once()
    assert "60" in bot.send_message.call_args.args[1]
    async with db() as session:
        saved = await session.get(ScheduleEntry, row.id)
        saved.notify_before_min = None
        await session.commit()
    # A separate bot verifies this callback is silent even without relying on its mock history.
    bot.send_message.reset_mock()
    await scheduler.send_schedule_notice(bot, 42, row.id, "10:00", "Class", "study", 60)
    bot.send_message.assert_not_awaited()


async def test_normal_deadline_only_24h_and_3h(db):
    async with db() as session:
        session.add(Assignment(user_id=42, title="Task", due_at=(NOW + timedelta(days=2)).replace(tzinfo=None)))
        await session.commit()
    jobs = Jobs()
    assert await scheduler.sync_assignment_jobs(jobs, None, 42) == 2
    assert {j.args[-1] for j in jobs.get_jobs()} == {1440, 180}


async def test_project_deadline_has_three_day_warning(db):
    async with db() as session:
        session.add(Assignment(user_id=42, title="Semester project", due_at=(NOW + timedelta(days=4)).replace(tzinfo=None)))
        await session.commit()
    jobs = Jobs()
    assert await scheduler.sync_assignment_jobs(jobs, None, 42) == 3
    assert {j.args[-1] for j in jobs.get_jobs()} == {4320, 1440, 180}


def test_schedule_lead_honors_opt_out_and_zero():
    row = SimpleNamespace(category="study", block_type="fixed", notify_before_min=None)
    assert scheduler._smart_schedule_lead(row) is None
    row.notify_before_min = 0
    assert scheduler._smart_schedule_lead(row) == 0


@pytest.mark.parametrize("change", ["done", "cancelled", "rescheduled", "deleted", "other_user"])
async def test_stale_assignment_callback_is_silent(db, change):
    due = datetime(2030, 1, 8, 9)
    async with db() as session:
        row = Assignment(user_id=42, title="Task", due_at=due)
        session.add(row)
        await session.commit()
        row_id = row.id
        if change in {"done", "cancelled"}:
            row.status = change
        elif change == "rescheduled":
            row.due_at += timedelta(days=1)
        elif change == "deleted":
            await session.delete(row)
        else:
            row.user_id = 43
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduler.send_assignment_notice(bot, 42, row_id, "Task", None, due.isoformat(), 1440)
    bot.send_message.assert_not_awaited()


async def test_repeated_assignment_callback_sends_once(db):
    due = datetime(2030, 1, 8, 9)
    async with db() as session:
        row = Assignment(user_id=42, title="Task", due_at=due)
        session.add(row)
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    for _ in range(2):
        await scheduler.send_assignment_notice(bot, 42, row.id, "Task", None, due.isoformat(), 1440)
    bot.send_message.assert_awaited_once()


async def test_sent_reminder_is_not_sent_again(db):
    async with db() as session:
        row = Reminder(user_id=42, text="test", remind_at=NOW.replace(tzinfo=None), is_sent=True)
        session.add(row)
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduler.send_saved_reminder(bot, 42, "old text", row.id)
    bot.send_message.assert_not_awaited()


async def test_resync_does_not_remove_another_users_jobs(db):
    jobs = Jobs()
    jobs.add_job(None, "date", id="deadline:item:43:1:180")
    jobs.add_job(None, "cron", id="schedule:block:43:1")
    await scheduler.sync_assignment_jobs(jobs, None, 42)
    await scheduler.sync_schedule_jobs(jobs, None, 42)
    assert len(jobs.get_jobs()) == 2
