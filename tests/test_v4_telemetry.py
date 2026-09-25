import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from database.v4_models import ProviderUsage
from services import telemetry
from services.commands import route_command
from database.time import utcnow


async def test_reported_usage_cost_and_budget(db, monkeypatch):
    monkeypatch.setenv('AI_PRICE_JSON', '{"openai:test":{"input":2,"output":8}}')
    monkeypatch.setenv('AI_DAILY_BUDGET_USD', '.01')
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=1000))
    async def operation():
        return response
    assert await telemetry.observe('openai', 'test', 'reply', operation()) is response
    data = await telemetry.daily_usage(42)
    assert data['requests'] == 1 and data['configured_price_estimate_usd'] == pytest.approx(.01)
    assert data['budget_warning'] and data['unpriced_requests'] == 0
    assert (await telemetry.daily_usage(43))['requests'] == 0
    async with db() as session:
        row = (await session.scalars(select(ProviderUsage))).one()
        assert row.usage_source == 'reported' and row.error_class is None


async def test_estimates_unknown_prices_and_failure_accounted(db):
    await telemetry.record('gemini', 'test', 'reply', .1,
        SimpleNamespace(text='test'), input_text='abcdefgh')
    async def failure():
        raise TimeoutError('SECRET_MESSAGE')
    with pytest.raises(TimeoutError):
        await telemetry.observe('openai', 'test', 'reply', failure())
    data = await telemetry.daily_usage(42)
    assert data['requests'] == 2 and data['unpriced_requests'] == 2
    assert data['groups']['gemini:test:reply']['estimated_tokens'] == 3
    assert 'SECRET_MESSAGE' not in str(data) + str(telemetry.last_call)


async def test_deterministic_commands_make_zero_ai_calls(db):
    await route_command(42, '/tasks', utcnow())
    await route_command(42, '/plan', utcnow())
    assert (await telemetry.daily_usage(42))['requests'] == 0
    assert 'запросов: 0' in await route_command(42, '/cost', utcnow())


async def test_telemetry_failure_cannot_suppress_provider_response(monkeypatch):
    monkeypatch.setattr(telemetry.engine, 'async_session_factory', lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE_DB')))
    assert await telemetry.observe('openai', 'test', 'reply', asyncio.sleep(0, result='ok')) == 'ok'


async def test_v1_upgrade_preserves_utc_and_usage(db):
    from database.migrations import upgrade
    async with db() as session:
        await session.execute(text('CREATE TABLE jarvis_schema_version (version INTEGER PRIMARY KEY)'))
        await session.execute(text('INSERT INTO jarvis_schema_version VALUES (1)'))
        # Rehearse an already migrated deployment; v2 must never shift dates.
        await session.execute(text("INSERT INTO assignments (user_id,source,title,status,due_at,created_at,updated_at) VALUES (42,'user','kept','done','2030-01-08 10:00:00','2030-01-01 10:00:00','2030-01-01 10:00:00')"))
        connection = await session.connection()
        await connection.run_sync(upgrade)
        await connection.run_sync(upgrade)
        assert str(await session.scalar(text('SELECT due_at FROM assignments'))).startswith('2030-01-08 10:00')
        assert await session.scalar(text('SELECT max(version) FROM jarvis_schema_version')) == 2
