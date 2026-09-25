import hashlib
import hmac
import json
import time
from dataclasses import replace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest

from services import rate_limit
from services.memory import upsert_memory_updates
from services.schedule import upsert_schedule_entry
from services.tasks import save_task
from webapp import server

original_request = aiohttp.ClientSession._request


def authorization(user_id=42):
    values = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': user_id, 'first_name': 'Test'})}
    key = hmac.new(b'WebAppData', server.settings.bot_token.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k, v in sorted(values.items())).encode(), hashlib.sha256).hexdigest()
    return {'X-Telegram-Init-Data': urlencode(values)}


@pytest.fixture
async def client(db, monkeypatch):
    # Actual HTTP only to TestServer's disposable loopback listener.
    monkeypatch.setattr(aiohttp.ClientSession, '_request', original_request)
    monkeypatch.setattr(server, 'ensure_user', AsyncMock())
    rate_limit._calls.clear()
    async with TestClient(TestServer(server.create_web_app())) as client:
        yield client


async def test_all_api_auth_required_even_with_dev_flag(client, monkeypatch):
    monkeypatch.setattr(server, 'settings', replace(server.settings, miniapp_dev_mode=True))
    for path in ['/api/dashboard', '/api/v4/state', '/api/v4/tasks', '/api/v4/diagnostics']:
        response = await client.get(path)
        assert response.status == 401
        response = await client.get(path, headers=authorization(43))
        assert response.status == 403
    response = await client.post('/api/v4/tasks', json={'title': 'forbidden'})
    assert response.status == 401


async def test_task_crud_filters_and_cross_user_protection(client):
    headers = authorization()
    response = await client.post('/api/v4/tasks', json={'title': 'WEB work', 'course': 'WEB'}, headers=headers)
    assert response.status == 200
    task = await response.json()
    response = await client.get('/api/v4/tasks?course=WEB&status=pending', headers=headers)
    assert [x['id'] for x in await response.json()] == [task['id']]
    foreign = await save_task(43, {'title': 'private'})
    response = await client.patch(f"/api/v4/tasks/{foreign['id']}", json={'title': 'hijack'}, headers=headers)
    assert response.status == 400
    response = await client.patch(f"/api/v4/tasks/{task['id']}", json={'title': 'WEB work', 'status': 'done'}, headers=headers)
    assert response.status == 200 and (await response.json())['progress'] == 100
    response = await client.get('/api/v4/state', headers=headers)
    assert response.status == 200
    data = await response.json()
    assert len(data['week']) == 7 and len(data['tasks']) == 1
    assert response.headers['Cache-Control'] == 'no-store' and response.headers['X-Correlation-ID']


async def test_preferences_dated_override_and_memory_correction(client):
    headers = authorization()
    from datetime import date
    today = date.today()
    block = await upsert_schedule_entry(42, {'weekday': today.weekday(), 'start': '10:00', 'end': '11:00', 'title': 'Gym', 'block_type': 'planned', 'category': 'training'})
    response = await client.post('/api/v4/overrides', json={'date': today.isoformat(), 'entry_id': block['id'], 'changes': {'start': '11:00', 'end': '12:00'}}, headers=headers)
    assert response.status == 200
    response = await client.post('/api/v4/preferences', json={'dnd_start': '22:00', 'morning_brief': False}, headers=headers)
    assert response.status == 200 and not (await response.json())['morning_brief']
    response = await client.post('/api/v4/preferences', json={'sleep_hours': 0}, headers=headers)
    assert response.status == 400
    await upsert_memory_updates(42, [{'key': 'course', 'value': 'old'}])
    state = await (await client.get('/api/v4/state', headers=headers)).json()
    identity = state['memory'][0]['id']
    assert (await client.patch(f'/api/v4/memory/{identity}', json={'value': 'corrected'}, headers=headers)).status == 200
    assert (await client.delete(f'/api/v4/memory/{identity}', headers=headers)).status == 200


async def test_diag_rate_limit_and_invalid_inputs(client):
    headers = authorization()
    rate_limit._calls.clear()
    for _ in range(10):
        assert rate_limit.allow(42, 'diagnostics', limit=10)
    assert (await client.get('/api/v4/diagnostics', headers=headers)).status == 429
    response = await client.post('/api/v4/tasks', json={'title': 'a', 'estimated_minutes': -1}, headers=headers)
    assert response.status == 400


async def test_manual_lms_sync_rebuilds_both_deadline_and_calendar_jobs(client, monkeypatch):
    monkeypatch.setattr(server, 'settings', replace(server.settings, lms_ical_url='configured-in-test'))
    monkeypatch.setattr(server, 'sync_lms_ical', AsyncMock(return_value={'created': 0}))
    calendar_jobs, deadline_jobs = AsyncMock(), AsyncMock()
    monkeypatch.setattr(server, '_resync_schedule_notifications', calendar_jobs)
    monkeypatch.setattr(server, '_resync_assignment_notifications', deadline_jobs)
    response = await client.post('/api/lms/sync', headers=authorization())
    assert response.status == 200
    calendar_jobs.assert_awaited_once()
    deadline_jobs.assert_awaited_once()


def test_structured_logging_never_outputs_sensitive_payloads():
    import logging
    from services.observability import PrivateFormatter, new_correlation, correlation_id
    token = new_correlation()
    try:
        record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'SECRET_TOKEN %s', ('PRIVATE_MESSAGE',), None)
        result = PrivateFormatter().format(record)
        assert 'SECRET_TOKEN' not in result and 'PRIVATE_MESSAGE' not in result
        assert correlation_id.get() in result
    finally:
        correlation_id.reset(token)
