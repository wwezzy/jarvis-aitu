from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from database.assistant_models import GoalCheckIn, AssistantState, FocusSession
from database.time import aware
from database.v4_models import NotificationDelivery
from services import autonomy, notifications
from services.autonomy import (active_focus, assistant_tick, check_in, ensure_starter_goals,
    finish_focus, goals_snapshot, next_action, save_goal, start_focus, autonomy_command)
from services.direct_intents import try_direct_answer
from services.preferences import save_preferences
from services.schedule import upsert_schedule_entry
from services.tasks import save_task

NOW = aware(datetime(2030, 1, 7, 9, 5))


async def test_starter_goals_edit_archive_and_restart_do_not_reset_history(db):
    await ensure_starter_goals(42)
    goals = await goals_snapshot(42, NOW)
    assert len(goals) == 3
    goal = goals[0]
    await check_in(42, goal['id'], 'done', goal['minimum_minutes'], now=NOW)
    await check_in(42, goal['id'], 'done', goal['minimum_minutes'], now=NOW)
    await ensure_starter_goals(42)
    snapshot = await goals_snapshot(42, NOW)
    assert next(g for g in snapshot if g['id'] == goal['id'])['week_completed'] == 1
    await save_goal(42, {'enabled': False}, goal['id'])
    await ensure_starter_goals(42)
    assert len(await goals_snapshot(42, NOW)) == 2
    async with db() as session:
        assert len(list(await session.scalars(select(GoalCheckIn)))) == 1


async def test_unknown_skipped_and_completed_are_distinct_and_cross_user_protected(db):
    await ensure_starter_goals(42)
    goal = (await goals_snapshot(42, NOW))[0]
    assert goal['today'] == 'unknown' and goal['week_completed'] == 0
    with pytest.raises(ValueError):
        await check_in(43, goal['id'], 'done', 30, now=NOW)
    with pytest.raises(ValueError):
        await check_in(42, goal['id'], 'done', 0, now=NOW)
    await check_in(42, goal['id'], 'skipped', obstacle='needed rest', now=NOW)
    goal = (await goals_snapshot(42, NOW))[0]
    assert goal['today'] == 'skipped' and goal['week_completed'] == 0
    await check_in(42, goal['id'], 'done', 20, now=NOW + timedelta(days=1))
    assert (await goals_snapshot(42, NOW + timedelta(days=1)))[0]['week_completed'] == 1


@pytest.mark.parametrize('text', ['/next', '/bored', 'мне скучно', 'не могу начать', '/goals', '/activity', '/autopilot'])
async def test_assistant_routes_are_deterministic_with_no_providers(db, monkeypatch, text):
    from services import llm
    model = AsyncMock(side_effect=AssertionError('No provider permitted'))
    monkeypatch.setattr(llm, 'generate_reply', model)
    await ensure_starter_goals(42)
    reply = await try_direct_answer(42, text, NOW)
    assert reply and 'Traceback' not in reply
    model.assert_not_awaited()


async def test_next_step_prioritizes_imminent_task_without_filling_rest(db):
    await ensure_starter_goals(42)
    task = await save_task(42, {'title': 'Submit', 'due_at': (NOW + timedelta(hours=3)).isoformat(),
        'estimated_minutes': 30, 'importance': 9})
    action = await next_action(42, NOW)
    assert action['kind'] == 'task' and action['task_id'] == task['id']
    await upsert_schedule_entry(42, {'weekday': 0, 'start': '09:00', 'end': '10:00', 'title': 'Rest', 'category': 'rest', 'block_type': 'planned'})
    assert (await next_action(42, NOW))['kind'] == 'rest'


async def test_focus_restart_recovery_dnd_and_explicit_report(db):
    await ensure_starter_goals(42)
    goal = (await goals_snapshot(42, NOW))[0]
    focus = await start_focus(42, 25, goal_id=goal['id'], now=NOW)
    assert (await active_focus(42))['id'] == focus['id']
    assert await notifications.is_dnd(42, NOW + timedelta(minutes=2))
    with pytest.raises(ValueError, match='текущий фокус'):
        await start_focus(42, 10, now=NOW)
    with pytest.raises(ValueError, match='прошедшего'):
        await finish_focus(42, 20, NOW + timedelta(minutes=2))
    assert (await next_action(42, NOW + timedelta(minutes=30)))['kind'] == 'review'
    assert (await goals_snapshot(42, NOW))[0]['week_completed'] == 0
    await finish_focus(42, 20, NOW + timedelta(minutes=30))
    assert await active_focus(42) is None
    assert (await goals_snapshot(42, NOW))[0]['week_completed'] == 0
    async with db() as session:
        stored = await session.get(FocusSession, focus['id'])
        assert stored.status == 'reported' and stored.reported_minutes == 20


async def test_focus_rejects_conflicts_foreign_records_and_night(db):
    await upsert_schedule_entry(42, {'weekday': 0, 'start': '09:00', 'end': '10:00', 'title': 'Class', 'category': 'study', 'block_type': 'fixed'})
    with pytest.raises(ValueError, match='пересекается'):
        await start_focus(42, 10, now=NOW)
    with pytest.raises(ValueError, match='рабочий день'):
        await start_focus(42, 10, now=NOW.replace(hour=2))
    goal = await save_goal(43, {'title': 'Private', 'next_step': 'Secret'})
    with pytest.raises(ValueError, match='not found'):
        await start_focus(42, 10, goal_id=goal['id'], now=NOW.replace(hour=11))


async def test_initiative_is_deduplicated_bounded_backed_off_and_mutable(db):
    await ensure_starter_goals(42)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await assistant_tick(bot, 42, NOW)
    assert not await assistant_tick(bot, 42, NOW)
    assert await assistant_tick(bot, 42, NOW.replace(hour=11))
    assert not await assistant_tick(bot, 42, NOW.replace(hour=14))  # two unanswered -> backoff
    assert not await assistant_tick(bot, 42, NOW.replace(hour=3))
    assert bot.send_message.await_count == 2
    await autonomy_command(42, '/autopilot pause 24', NOW)
    assert not await assistant_tick(bot, 42, NOW.replace(hour=21))
    await autonomy_command(42, '/autopilot resume', NOW)
    await save_preferences(42, {'autonomy_mode': 'off'})
    assert not await assistant_tick(bot, 42, NOW.replace(hour=21))


async def test_focus_expiry_prompts_once_and_never_counts_as_done(db):
    await ensure_starter_goals(42)
    await start_focus(42, 5, now=NOW)
    bot = SimpleNamespace(send_message=AsyncMock())
    await assistant_tick(bot, 42, NOW + timedelta(minutes=6))
    await assistant_tick(bot, 42, NOW + timedelta(minutes=10))
    bot.send_message.assert_awaited_once()
    assert all(g['week_completed'] == 0 for g in await goals_snapshot(42, NOW))


async def test_queued_initiative_cancelled_after_opt_out(db, monkeypatch):
    await ensure_starter_goals(42)
    bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(notifications, 'is_dnd', AsyncMock(return_value=True))
    await notifications.notify_once(bot, 42, 'autonomy:2030-01-07:9', 'Proposed step', now=NOW, expires_at=NOW + timedelta(minutes=30))
    await save_preferences(42, {'autonomy_mode': 'off'})
    monkeypatch.setattr(notifications, 'is_dnd', AsyncMock(return_value=False))
    await notifications.flush_queued(bot, 42, NOW + timedelta(minutes=5))
    bot.send_message.assert_not_awaited()
    async with db() as session:
        assert (await session.scalar(select(NotificationDelivery))).status == 'cancelled'


async def test_recent_reply_prevents_unnecessary_nudge(db):
    await ensure_starter_goals(42)
    await autonomy.touch(42, NOW - timedelta(minutes=10))
    bot = SimpleNamespace(send_message=AsyncMock())
    assert not await assistant_tick(bot, 42, NOW)
    bot.send_message.assert_not_awaited()
    async with db() as session:
        assert (await session.get(AssistantState, 42)).last_interaction_at


async def test_abandoned_focus_eventually_releases_but_never_claims_completion(db):
    await ensure_starter_goals(42)
    focus = await start_focus(42, 5, now=NOW)
    bot = SimpleNamespace(send_message=AsyncMock())
    await assistant_tick(bot, 42, NOW + timedelta(hours=9))
    assert await active_focus(42) is None
    async with db() as session:
        row = await session.get(FocusSession, focus['id'])
        assert row.status == 'unreported' and row.reported_minutes is None


async def test_game_drift_is_factual_once_per_focus_and_never_closes_process(db):
    from services.autonomy import focus_drift_notice
    from database.assistant_models import ActivityReceipt
    await save_preferences(42, {'activity_enabled': True, 'autonomy_mode': 'active'})
    await start_focus(42, 25, now=NOW.replace(minute=0))
    async with db() as session:
        for offset in (1, 2, 3):
            session.add(ActivityReceipt(user_id=42, bucket_id=str(offset), starts_at=NOW.replace(minute=offset), seconds={'gaming': 60}))
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await focus_drift_notice(bot, 42, NOW.replace(minute=4))
    assert not await focus_drift_notice(bot, 42, NOW.replace(minute=4))
    bot.send_message.assert_awaited_once()
    assert 'активного окна игры' in bot.send_message.call_args.args[1]
    await save_preferences(42, {'autonomy_mode': 'off'})
    assert not await focus_drift_notice(bot, 42, NOW.replace(minute=5))
