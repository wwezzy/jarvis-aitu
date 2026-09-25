from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.time import aware
from services import calendar, research


def enable_calendar(monkeypatch):
    for name in ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN', 'GOOGLE_CALENDAR_ID']:
        monkeypatch.setenv(name, 'offline')
    monkeypatch.setenv('ENABLE_GOOGLE_CALENDAR', 'true')
    monkeypatch.setenv('ENABLE_GOOGLE_CALENDAR_WRITE', 'true')


async def test_unconfigured_features_make_no_network(db):
    assert not (await calendar.sync_calendar(42))['configured']
    assert 'недоступно' in await research.research('/research latest Python news')
    with pytest.raises(calendar.CalendarUnavailable):
        await calendar.save_event(42, {})


async def test_calendar_snapshot_stable_ids_failure_preserves_and_cancel_reconciles(db, monkeypatch):
    enable_calendar(monkeypatch)
    now = aware(datetime(2030, 1, 8, 8))
    item = {'id': 'event1', 'summary': 'Real meeting', 'start': {'dateTime': '2030-01-08T09:00:00+05:00'},
            'end': {'dateTime': '2030-01-08T10:00:00+05:00'}}
    adapter = SimpleNamespace(events=AsyncMock(return_value=[item]))
    assert (await calendar.sync_calendar(42, adapter=adapter, now=now))['events'] == 1
    assert len(await calendar.list_events(42)) == 1
    await calendar.sync_calendar(42, adapter=adapter, now=now)
    assert len(await calendar.list_events(42)) == 1
    adapter.events.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await calendar.sync_calendar(42, adapter=adapter, now=now)
    assert len(await calendar.list_events(42)) == 1
    adapter.events.side_effect = None
    adapter.events.return_value = []
    await calendar.sync_calendar(42, adapter=adapter, now=now)
    assert await calendar.list_events(42) == []


async def test_calendar_write_ownership_and_conflicts(db, monkeypatch):
    enable_calendar(monkeypatch)
    adapter = SimpleNamespace(token=AsyncMock(return_value='offline'), request=AsyncMock(return_value={'id': 'new'}))
    payload = {'title': 'Study', 'start': '2030-01-08T13:00:00+05:00', 'end': '2030-01-08T14:00:00+05:00',
               'operation_id': '550e8400-e29b-41d4-a716-446655440000'}
    result = await calendar.save_event(42, payload, adapter=adapter)
    assert result['saved'] and adapter.request.call_args.args[0] == 'POST'
    assert adapter.request.call_args.kwargs['payload']['extendedProperties']['private']['jarvis_owner'] == '42'
    with pytest.raises(ValueError, match='Only Jarvis-owned'):
        await calendar.save_event(42, {**payload, 'id': 'foreign'}, adapter=adapter)
    adapter.request.reset_mock()
    result = await calendar.save_event(42, payload, adapter=adapter)
    assert not result['saved'] and result['conflicts']
    adapter.request.assert_not_awaited()


async def test_google_pagination_and_oauth_mock_transport(monkeypatch):
    enable_calendar(monkeypatch)
    adapter = calendar.GoogleCalendar()
    monkeypatch.setattr(adapter, 'token', AsyncMock(return_value='offline'))
    monkeypatch.setattr(adapter, 'request', AsyncMock(side_effect=[{'items': [{'id': 'one'}], 'nextPageToken': 'next'}, {'items': [{'id': 'two'}]}]))
    start = aware(datetime(2030, 1, 8))
    assert len(await adapter.events(start, start.replace(day=9))) == 2
    assert adapter.request.call_args.kwargs['params']['pageToken'] == 'next'


async def test_research_only_fresh_public_queries_with_real_citations(monkeypatch):
    monkeypatch.setenv('ENABLE_WEB_RESEARCH', 'true')
    monkeypatch.setenv('WEB_RESEARCH_API_KEY', 'offline')
    provider = SimpleNamespace(search=AsyncMock(return_value=[{'title': 'Official', 'url': 'https://example.org/reference', 'excerpt': 'Source excerpt'}]))
    for query in ['my deadlines today', 'мой LMS сегодня', 'explain recursion']:
        await research.research(query, provider=provider)
    provider.search.assert_not_awaited()
    answer = await research.research('/research latest public Python news', provider=provider)
    assert 'https://example.org/reference' in answer and 'Source excerpt' in answer
    provider.search.side_effect = TimeoutError()
    assert 'недоступно' in await research.research('latest public news', provider=provider)
