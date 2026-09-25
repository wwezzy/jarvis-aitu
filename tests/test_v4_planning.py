from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from database.models import Assignment, ScheduleEntry
from database.time import aware
from services.planner import make_plan, risk_score, sleep_plan
from services.preferences import save_preferences
from services.schedule import resolve_day, save_override
from services.tasks import list_tasks, parse_task_command, save_task


NOW = aware(datetime(2030, 1, 7, 8))


async def test_dated_override_does_not_change_next_week_or_other_user(db):
    async with db() as session:
        row = ScheduleEntry(user_id=42, weekday=0, start="08:30", end="10:00", title="Gym", category="training")
        session.add(row)
        await session.commit()
        row_id = row.id
    await save_override(42, NOW.date(), row_id, {"start": "11:00", "end": "12:30"})
    assert (await resolve_day(42, NOW.date()))[0]["start"] == "11:00"
    assert (await resolve_day(42, NOW.date() + timedelta(days=7)))[0]["start"] == "08:30"
    with pytest.raises(ValueError):
        await save_override(43, NOW.date(), row_id, {}, cancelled=True)
    await save_override(42, NOW.date(), row_id, {}, cancelled=True)
    assert await resolve_day(42, NOW.date()) == []


async def test_plan_respects_fixed_rest_meals_and_buffer_and_replans_from_now(db):
    await save_task(42, {"title": "Project", "due_at": NOW + timedelta(hours=12), "estimated_minutes": 150})
    async with db() as session:
        session.add_all([
            ScheduleEntry(user_id=42, weekday=0, start="09:00", end="11:00", title="Class", category="study", block_type="fixed"),
            ScheduleEntry(user_id=42, weekday=0, start="12:00", end="13:00", title="Meal", category="nutrition"),
            ScheduleEntry(user_id=42, weekday=0, start="15:00", end="18:00", title="Rest", category="rest", block_type="flex"),
        ])
        await session.commit()
    plan = await make_plan(42, NOW)
    assert sum(b["minutes"] for b in plan["blocks"]) == 150
    for block in plan["blocks"]:
        start, end = datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])
        assert end <= NOW.replace(hour=19)
        for a, b in ((9, 11), (12, 13), (15, 18)):
            assert end <= NOW.replace(hour=a) or start >= NOW.replace(hour=b)
    missed = await make_plan(42, NOW.replace(hour=18))
    assert all(datetime.fromisoformat(b["start"]) >= NOW.replace(hour=18) for b in missed["blocks"])
    assert missed["overloaded"] and missed["unscheduled"]


async def test_unknown_deadline_and_effort_are_not_invented(db):
    task = await save_task(42, parse_task_command("/task SDP sometime next week"))
    assert task["due_at"] is None and task["estimated_minutes"] is None
    plan = await make_plan(42, NOW)
    assert not plan["blocks"]
    assert plan["unscheduled"][0]["reason"] == "effort unknown"
    with pytest.raises(ValueError):
        parse_task_command("/task WEB | due 2030-01-08")


async def test_lms_deadline_requires_explicit_override_and_done_cancels(db):
    async with db() as session:
        session.add(Assignment(user_id=42, title="LMS task", source="lms_ical", due_at=NOW))
        await session.commit()
        task = await session.scalar(select(Assignment))
        task_id = task.id
    with pytest.raises(ValueError, match="teacher override"):
        await save_task(42, {"title": "LMS task", "due_at": NOW + timedelta(days=1)}, task_id)
    updated = await save_task(42, {"title": "LMS task", "due_at": NOW + timedelta(days=1), "teacher_override": True}, task_id)
    assert datetime.fromisoformat(updated["due_at"]) == NOW + timedelta(days=1)
    assert await list_tasks(43) == []


def test_risk_explains_urgency_progress_capacity_and_unknowns():
    task = {"status": "pending", "importance": 10, "consequence": 10, "estimated_minutes": 180,
            "progress": 20, "due_at": (NOW + timedelta(hours=3)).isoformat(), "testing_buffer_minutes": 60}
    risk = risk_score(task, NOW, free_minutes=90)
    assert risk["score"] >= 70
    assert risk["components"]["capacity_gap"] > 0
    assert risk["remaining_minutes"] == 144
    assert risk_score({**task, "status": "done"}, NOW)["score"] == 0


async def test_sleep_uses_dated_first_class_and_user_preparation(db):
    async with db() as session:
        row = ScheduleEntry(user_id=42, weekday=1, start="10:00", end="11:00", title="Class", category="study", block_type="fixed", commute_minutes=30)
        session.add(row)
        await session.commit()
        row_id = row.id
    await save_override(42, date(2030, 1, 8), row_id, {"start": "12:00", "end": "13:00", "location": "online", "commute_minutes": 0})
    await save_preferences(42, {"sleep_hours": 8, "breakfast_minutes": 20, "shower_minutes": 10, "preparation_minutes": 15})
    plan = await sleep_plan(42, NOW.replace(hour=23))
    assert datetime.fromisoformat(plan["wake_at"]).strftime("%H:%M") == "11:15"
    assert datetime.fromisoformat(plan["bedtime"]).strftime("%H:%M") == "03:15"
