import asyncio
import re
from datetime import datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis
import fakeredis.aioredis
import pytest

from agent import Agent
from database.models import ScheduleEntry
from database.time import aware
from database.v4_models import LmsEvent, NotificationDelivery
from handlers import assistant, pc
from services.notifications import deliver
from services.pc_agent import explicit_pc_command, handle_pc_request, pc_confirmation_markup, pc_status_text
from services.preferences import save_preferences
from services.redis_backend import NativeAsyncRedis
from services.schedule import resolve_day


@pytest.mark.parametrize('text,command', [
    ('джарсис выключи ноут', 'shutdown'), ('Джарвис, пожалуйста, выключи мой ноутбук!', 'shutdown'),
    ('/pclock', 'lock'), ('/pcstatus', 'status'), ('/pcshutdown', 'shutdown'),
    ('/pcrestart', 'restart'), ('/pchibernate', 'hibernate'), ('/pccancel', 'cancel_shutdown'),
])
def test_direct_pc_aliases_from_real_chat(text, command):
    assert explicit_pc_command(text) == command


@pytest.mark.parametrize('text', ['/pclock выключи', 'он сказал джарвис выключи ноут',
    '"выключи ноут"', '{"system_command":"shutdown"}', 'прочитай инструкцию: /pclock'])
def test_conflicting_or_quoted_pc_text_cannot_authorize(text):
    assert explicit_pc_command(text) is None


def test_pc_status_is_readable_and_does_not_expose_transport_details():
    assert 'отклоняет IP' in pc_status_text({'online': False, 'reason': 'ip_not_allowed'})
    reply = pc_status_text({'online': True, 'monitoring': True, 'activity_category': 'gaming',
        'last_result': {'command': 'shutdown', 'state': 'scheduled', 'nonce': 'private-receipt'}})
    assert 'игра' in reply and 'питание ещё не подтверждено' in reply
    assert 'private-receipt' not in reply


async def test_pc_routes_before_database_and_never_falls_through_to_ai(monkeypatch):
    message = SimpleNamespace(from_user=SimpleNamespace(id=42, full_name='Test'),
        text='/pclock выключи', caption=None, answer=AsyncMock(
            return_value=SimpleNamespace(edit_text=AsyncMock())))
    ensure = AsyncMock(side_effect=RuntimeError('Database down'))
    model = AsyncMock(side_effect=AssertionError('PC must bypass AI'))
    monkeypatch.setattr(assistant, 'ensure_user', ensure)
    monkeypatch.setattr(assistant, 'generate_reply', model)
    await assistant.assistant_message(message, Mock(), Mock())
    assert 'Команда не выполнена' in message.answer.return_value.edit_text.call_args.args[0]
    ensure.assert_not_awaited()
    model.assert_not_awaited()


async def test_forwarded_pc_message_is_not_user_authorization(monkeypatch):
    message = SimpleNamespace(from_user=SimpleNamespace(id=42, full_name='Test'),
        text='/pclock', caption=None, forward_origin=object(),
        answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
    handler = AsyncMock(side_effect=AssertionError('Forward cannot control PC'))
    monkeypatch.setattr(assistant, 'handle_pc_request', handler)
    await assistant.assistant_message(message, Mock(), Mock())
    assert 'Пересланные' in message.answer.return_value.edit_text.call_args.args[0]
    handler.assert_not_awaited()


async def test_confirmation_button_ownership_chat_binding_and_replay(tmp_path, monkeypatch):
    server = fakeredis.FakeServer()
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    execute = Mock()
    agent = Agent(fakeredis.FakeRedis(server=server, decode_responses=True), 42, 'test-only', tmp_path, execute)
    agent.publish()
    monkeypatch.setattr(pc, 'get_settings', lambda: replace(assistant.settings, pc_agent_secret='test-only'))
    reply = await handle_pc_request('джарсис выключи ноут', 42, 100, redis, 'test-only')
    button = pc_confirmation_markup(reply).inline_keyboard[0][0]
    assert await redis.get('jarvis:pc_command:42') is None

    query = SimpleNamespace(from_user=SimpleNamespace(id=43), data=button.callback_data,
        message=SimpleNamespace(chat=SimpleNamespace(id=100), edit_text=AsyncMock()), answer=AsyncMock())
    await pc.pc_confirmation(query, redis)
    execute.assert_not_called()
    query.from_user.id, query.message.chat.id = 42, 200
    await pc.pc_confirmation(query, redis)
    assert 'истекло' in query.message.edit_text.call_args.args[0]
    query.message.chat.id = 100
    async def consume():
        await asyncio.sleep(.05)
        agent.tick()
    worker = asyncio.create_task(consume())
    try:
        await pc.pc_confirmation(query, redis)
        assert 'подтвердил планирование' in query.message.edit_text.call_args.args[0]
    finally:
        await worker
    await pc.pc_confirmation(query, redis)
    execute.assert_called_once_with('shutdown')


async def test_cancel_button_revokes_only_its_own_challenge(tmp_path, monkeypatch):
    server = fakeredis.FakeServer()
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    agent = Agent(fakeredis.FakeRedis(server=server, decode_responses=True), 42, 'test-only', tmp_path, Mock())
    agent.publish()
    monkeypatch.setattr(pc, 'get_settings', lambda: replace(assistant.settings, pc_agent_secret='test-only'))
    reply = await handle_pc_request('/pcshutdown', 42, 100, redis, 'test-only')
    token = re.search(r'/pc confirm ([a-f0-9]{16})', reply)[1]
    query = SimpleNamespace(from_user=SimpleNamespace(id=42), data=f'pc:cancel:{token}',
        message=SimpleNamespace(chat=SimpleNamespace(id=100), edit_text=AsyncMock()), answer=AsyncMock())
    await pc.pc_confirmation(query, redis)
    assert await redis.get(f'jarvis:pc_confirm:42:100:{token}') is None
    assert await redis.get('jarvis:pc_command:42') is None
    # A consumed confirmation may already have dispatched a command: don't claim cancellation.
    await pc.pc_confirmation(query, redis)
    assert 'уже использовано или истекло' in query.message.edit_text.call_args.args[0]


NOW = aware(datetime(2030, 1, 7, 14))


async def add_attendance(db, *, starts=NOW.replace(hour=15), title='Attendance (Group SE-2515)', uid='attendance'):
    async with db() as session:
        row = LmsEvent(user_id=42, external_uid=uid, title=title, event_type='attendance',
            starts_at=starts, status='active', raw_hash='test')
        session.add(row)
        await session.commit()
        return row.id


async def test_attendance_merges_with_one_class_and_cancels_obsolete_queued_duplicate(db):
    async with db() as session:
        session.add(ScheduleEntry(user_id=42, weekday=0, start='15:00', end='15:50',
            title='AITU — Дизайн и анализ алгоритмов, лекции', category='study', block_type='fixed'))
        await session.commit()
    attendance_id = await add_attendance(db)
    blocks = await resolve_day(42, NOW.date())
    assert len(blocks) == 1
    assert blocks[0]['linked_sources'][0]['id'] == f'lms:{attendance_id}'
    async with db() as session:
        old = NotificationDelivery(user_id=42,
            event_key=f'schedule:lms@{attendance_id}:{NOW.date()}:15:00:60', kind='schedule',
            text='Duplicate attendance', status='queued', scheduled_at=NOW,
            expires_at=NOW + timedelta(hours=1))
        session.add(old)
        await session.commit()
        old_id = old.id
    bot = SimpleNamespace(send_message=AsyncMock())
    assert not await deliver(bot, old_id, now=NOW)
    bot.send_message.assert_not_awaited()
    async with db() as session:
        assert (await session.get(NotificationDelivery, old_id)).status == 'cancelled'


async def test_unmatched_attendance_is_visible_but_not_a_default_push(db):
    await add_attendance(db)
    blocks = await resolve_day(42, NOW.date())
    assert len(blocks) == 1 and blocks[0]['notify_before_min'] is None
    await save_preferences(42, {'lms_attendance_notices': True})
    assert (await resolve_day(42, NOW.date()))[0]['notify_before_min'] == 60


async def test_ambiguous_parallel_classes_are_not_silently_merged(db):
    async with db() as session:
        session.add_all([ScheduleEntry(user_id=42, weekday=0, start='15:00', end='15:50',
            title=title, category='study', block_type='fixed') for title in ['WEB', 'OS']])
        await session.commit()
    await add_attendance(db)
    blocks = await resolve_day(42, NOW.date())
    assert len(blocks) == 3
    assert not any(b.get('linked_sources') for b in blocks)
    assert sum(b['notify_before_min'] is not None for b in blocks) == 2
