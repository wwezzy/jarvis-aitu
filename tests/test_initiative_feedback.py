from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.time import aware
from handlers import initiative
from services.autonomy import active_focus, ensure_starter_goals, goals_snapshot, start_focus


async def test_feedback_rejects_other_users_and_records_only_explicit_tap(db):
    now = aware(datetime(2030, 1, 7, 10))
    await ensure_starter_goals(42)
    goals = await goals_snapshot(42, now)
    goal = goals[0]
    message = SimpleNamespace(answer=AsyncMock())
    query = SimpleNamespace(from_user=SimpleNamespace(id=43), message=message,
        answer=AsyncMock(), data=f"assist:done:{goal['id']}")
    await initiative.feedback(query)
    message.answer.assert_not_awaited()
    query.answer.assert_awaited_once()


async def test_intentional_rest_cancels_focus_without_completion(db, monkeypatch):
    now = aware(datetime(2030, 1, 7, 10))
    await start_focus(42, 25, now=now)
    class Clock:
        @staticmethod
        def now(zone):
            return now
    monkeypatch.setattr(initiative, 'datetime', Clock)
    query = SimpleNamespace(from_user=SimpleNamespace(id=42), message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(), data='assist:pause')
    await initiative.feedback(query)
    assert await active_focus(42) is None


async def test_owned_explicit_completion_tap_is_idempotent(db, monkeypatch):
    now = aware(datetime(2030, 1, 7, 10))
    await ensure_starter_goals(42)
    goal = (await goals_snapshot(42, now))[0]
    class Clock:
        @staticmethod
        def now(zone):
            return now
    monkeypatch.setattr(initiative, 'datetime', Clock)
    query = SimpleNamespace(from_user=SimpleNamespace(id=42), message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(), data=f"assist:done:{goal['id']}")
    await initiative.feedback(query)
    await initiative.feedback(query)
    result = next(g for g in await goals_snapshot(42, now) if g['id'] == goal['id'])
    assert result['week_completed'] == 1 and result['today'] == 'done'
