import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis
import fakeredis.aioredis
import pytest

from agent import Agent, doctor
from services.pc_agent import handle_pc_request, pc_status, signed_status, read_status, wait_receipt
from services.redis_backend import NativeAsyncRedis, safe_transport_reason


@pytest.mark.parametrize('text', ['выключить', 'выключить ноутбук', 'выключи ноут', 'Джарвис, выключи компьютер!', '/pc shutdown'])
async def test_offline_never_reports_command_sent_or_generates_confirmation(text):
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    reply = await handle_pc_request(text, 42, 42, redis, 'test-only', wait_seconds=0)
    assert 'не отправлена' in reply and '/pc confirm' not in reply
    assert await redis.get('jarvis:pc_command:42') is None


async def test_allowlist_error_is_actionable_and_redacted():
    redis = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError('Client IP address is not in the allowlist redis://SECRET')),
                            set=AsyncMock())
    status = await pc_status(redis, 42, 'secret')
    assert status['reason'] == 'ip_not_allowed'
    reply = await handle_pc_request('/pc lock', 42, 42, redis, 'secret')
    assert 'IP' in reply and 'SECRET' not in reply
    redis.set.assert_not_awaited()
    assert safe_transport_reason(RuntimeError('WRONGPASS PRIVATE')) == 'authentication_failed'


@pytest.mark.parametrize('execution,state,word', [(None, 'completed', 'выполнение'), (OSError('PRIVATE'), 'failed', 'ошибку')])
async def test_end_to_end_correlated_agent_result_without_real_os_effect(tmp_path, execution, state, word):
    import asyncio
    server = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    execute = Mock(side_effect=execution)
    agent = Agent(sync, 42, 'test-only', tmp_path, execute)
    agent.publish()
    async def consume():
        await asyncio.sleep(0.05)
        agent.tick()
    worker = asyncio.create_task(consume())
    try:
        reply = await handle_pc_request('/pc lock', 42, 42, redis, 'test-only', wait_seconds=1)
        assert word in reply and 'PRIVATE' not in reply
        assert agent.last['state'] == state
        nonce = agent.last['nonce']
        receipt = read_status(sync.get(f'jarvis:pc_result:42:{nonce}'), 42, 'test-only')
        assert receipt['last_result']['state'] == state
        execute.assert_called_once_with('lock')
    finally:
        await worker


async def test_unrelated_and_tampered_result_never_counts_as_ack():
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await redis.set('jarvis:pc_result:42:expected', signed_status({'user_id': 42, 'seen_at': int(time.time()),
        'last_result': {'nonce': 'wrong', 'state': 'completed'}}, 'test-only'))
    assert await wait_receipt(redis, 42, 'test-only', 'expected', 0) is None


def test_ack_publish_failure_prevents_os_effect_and_replay(tmp_path):
    from services.pc_agent import build_signed_command
    redis = fakeredis.FakeRedis(decode_responses=True)
    execute = Mock()
    agent = Agent(redis, 42, 'test-only', tmp_path, execute)
    payload = build_signed_command('shutdown', 42, 'test-only')
    redis.set('jarvis:pc_command:42', payload)
    original = agent.publish
    calls = 0
    def publish():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError()
        original()
    agent.publish = publish
    with pytest.raises(TimeoutError):
        agent.tick()
    redis.set('jarvis:pc_command:42', payload)
    Agent(redis, 42, 'test-only', tmp_path, execute).tick()
    execute.assert_not_called()


def test_read_only_doctor_does_not_consume_or_expose_configuration(monkeypatch, tmp_path):
    import agent
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('ADMIN_ID', '42')
    monkeypatch.setenv('PC_AGENT_SECRET', 'PRIVATE_SECRET')
    monkeypatch.setattr(agent, 'load_dotenv', Mock())
    redis = Mock(ping=Mock(side_effect=RuntimeError('IP address is not in the allowlist PRIVATE_URL')))
    monkeypatch.setattr(agent, 'create_redis', lambda **kwargs: redis)
    report = doctor()
    assert report['reason'] == 'ip_not_allowed'
    assert 'PRIVATE' not in json.dumps(report)
    redis.getdel.assert_not_called()
    redis.set.assert_not_called()
