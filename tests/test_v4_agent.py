import json
import re
from unittest.mock import Mock

import fakeredis
import fakeredis.aioredis
import pytest

from agent import Agent, SingleInstance
from services.pc_agent import build_signed_command, handle_pc_request, pc_status, read_status, signed_status
from services.redis_backend import NativeAsyncRedis


async def test_confirmation_bound_to_user_chat_and_one_use():
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    reply = await handle_pc_request('/pc shutdown', 42, 100, redis, 'test-only')
    assert await redis.get('jarvis:pc_command:42') is None
    token = re.search(r'confirm ([a-f0-9]+)', reply)[1]
    assert 'истекло' in await handle_pc_request('/pc confirm ' + token, 43, 100, redis, 'test-only')
    assert 'истекло' in await handle_pc_request('/pc confirm ' + token, 42, 101, redis, 'test-only')
    assert 'queued' in await handle_pc_request('/pc confirm ' + token, 42, 100, redis, 'test-only')
    assert 'истекло' in await handle_pc_request('/pc confirm ' + token, 42, 100, redis, 'test-only')
    assert json.loads(await redis.get('jarvis:pc_command:42'))['cmd'] == 'shutdown'
    assert 'ожидает' in await handle_pc_request('/pc lock', 42, 100, redis, 'test-only')


def test_agent_ack_failure_replay_and_reconnect_state(tmp_path):
    redis = fakeredis.FakeRedis(decode_responses=True)
    execute = Mock()
    agent = Agent(redis, 42, 'test-only', tmp_path, execute)
    payload = build_signed_command('lock', 42, 'test-only')
    redis.set('jarvis:pc_command:42', payload)
    agent.tick()
    state = read_status(redis.get('jarvis:pc_status:42'), 42, 'test-only')
    assert state['last_result']['state'] == 'completed'
    restarted = Agent(redis, 42, 'test-only', tmp_path, execute)
    redis.set('jarvis:pc_command:42', payload)
    restarted.tick()
    execute.assert_called_once_with('lock')
    execute.side_effect = OSError('PRIVATE_PAYLOAD')
    redis.set('jarvis:pc_command:42', build_signed_command('sleep', 42, 'test-only'))
    restarted.tick()
    assert restarted.last['error'] == 'OSError'
    assert 'PRIVATE_PAYLOAD' not in redis.get('jarvis:pc_status:42')


async def test_status_verifies_signature_and_offline_age(monkeypatch):
    from services import pc_agent
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(pc_agent.time, 'time', lambda: 1000)
    for seen, online in [(990, True), (900, False)]:
        await redis.set('jarvis:pc_status:42', signed_status({'user_id': 42, 'seen_at': seen}, 'test-only'))
        assert (await pc_status(redis, 42, 'test-only'))['online'] is online
    assert not (await pc_status(redis, 42, 'wrong'))['online']


def test_single_instance_lock_released(tmp_path):
    instance = SingleInstance(tmp_path / 'lock')
    try:
        with pytest.raises((RuntimeError, OSError)):
            SingleInstance(tmp_path / 'lock')
    finally:
        instance.close()
    SingleInstance(tmp_path / 'lock').close()
