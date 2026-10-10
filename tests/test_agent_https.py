import asyncio
import json
import time
import uuid
from dataclasses import replace
from unittest.mock import Mock

import fakeredis.aioredis
import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import Agent
from services.agent_http import AgentHttps, rpc_secret
from services.pc_agent import build_signed_command, read_status, signed_status
from services.redis_backend import NativeAsyncRedis
from webapp import agent_api, server

SECRET = 'offline-test-only'
REAL_AIOHTTP_REQUEST = aiohttp.ClientSession._request


@pytest.fixture(autouse=True)
def allow_only_disposable_http(monkeypatch, no_external_network):
    from urllib.parse import urlsplit
    async def local_request(session, method, url, **kwargs):
        assert urlsplit(str(url)).hostname == '127.0.0.1'
        kwargs['allow_redirects'] = False
        return await REAL_AIOHTTP_REQUEST(session, method, url, **kwargs)
    monkeypatch.setattr(aiohttp.ClientSession, '_request', local_request)


def request_body(operation='ping', key='', value=None, **updates):
    data = {'user_id': 42, 'seen_at': int(time.time()), 'rpc': 1, 'nonce': uuid.uuid4().hex,
        'operation': operation, 'key': key, 'value': value}
    data.update(updates)
    return signed_status(data, rpc_secret(SECRET))


@pytest.fixture
async def http_mailbox(monkeypatch):
    monkeypatch.setenv('ENABLE_PC_AGENT_HTTP', '1')
    monkeypatch.setattr(agent_api, 'get_settings', lambda: replace(server.settings, pc_agent_secret=SECRET))
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    async with TestClient(TestServer(server.create_web_app(pc_redis=redis))) as client:
        yield client, redis


async def test_http_is_opt_in_and_absent_redis_degrades(monkeypatch):
    async with TestClient(TestServer(server.create_web_app())) as client:
        assert (await client.post('/api/agent/v1', data=request_body())).status == 404
        monkeypatch.setenv('ENABLE_PC_AGENT_HTTP', '1')
        monkeypatch.setattr(agent_api, 'get_settings', lambda: replace(server.settings, pc_agent_secret=SECRET))
        assert (await client.post('/api/agent/v1', data=request_body())).status == 503


@pytest.mark.parametrize('updates', [dict(user_id=43), dict(seen_at_offset=-60),
    dict(seen_at_offset=60), dict(nonce='invalid'), dict(rpc=2)])
async def test_wrong_owner_expiry_future_nonce_protocol_rejected(http_mailbox, updates):
    client, redis = http_mailbox
    updates = dict(updates)
    if 'seen_at_offset' in updates:
        updates['seen_at'] = int(time.time()) + updates.pop('seen_at_offset')
    await redis.set('jarvis:pc_command:42', 'untouched')
    response = await client.post('/api/agent/v1', data=request_body('getdel', 'jarvis:pc_command:42', **updates))
    assert response.status == 401
    assert await redis.get('jarvis:pc_command:42') == 'untouched'


async def test_auth_domain_separation_and_atomic_rpc_replay(http_mailbox):
    client, _ = http_mailbox
    body = request_body()
    raw = json.loads(body)
    forged = signed_status(json.loads(raw['body']), SECRET)
    assert (await client.post('/api/agent/v1', data=forged)).status == 401
    first = await client.post('/api/agent/v1', data=body)
    verified = read_status(await first.text(), 42, rpc_secret(SECRET))
    assert verified['result'] is True and verified['nonce'] == json.loads(raw['body'])['nonce']
    assert (await client.post('/api/agent/v1', data=body)).status == 409


@pytest.mark.parametrize('operation,key', [('set', 'jarvis:pc_command:42'),
    ('getdel', 'jarvis:pc_command:43'), ('get', 'jarvis:chat:42'),
    ('set', 'jarvis:activity_ack:42'), ('delete', 'jarvis:pc_status:42')])
async def test_no_command_creation_or_general_redis_access(http_mailbox, operation, key):
    client, redis = http_mailbox
    response = await client.post('/api/agent/v1', data=request_body(operation, key, 'arbitrary'))
    assert response.status == 403
    assert await redis.get('jarvis:pc_command:42') is None


async def test_signed_observations_and_receipt_identity_required(http_mailbox):
    client, redis = http_mailbox
    key = 'jarvis:pc_result:42:' + 'a'*32
    wrong = signed_status({'user_id':42,'seen_at':int(time.time()),'last_result':{'nonce':'b'*32}}, SECRET)
    assert (await client.post('/api/agent/v1',data=request_body('set',key,wrong))).status == 400
    assert (await client.post('/api/agent/v1',data=request_body('set','jarvis:pc_status:42','forged'))).status == 400
    assert await redis.get(key) is None
    assert (await client.post('/api/agent/v1',data=b'x'*65537)).status == 413


async def test_real_agent_exchange_executes_only_pre_authorized_mailbox_once(http_mailbox, monkeypatch, tmp_path):
    client, redis = http_mailbox
    loop = asyncio.get_running_loop()
    import urllib.request
    class Response:
        def __init__(self, raw):
            self.raw = raw
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, maximum):
            return self.raw[:maximum]
    class Bridge:
        def open(self, request, timeout):
            async def exchange():
                response = await client.post('/api/agent/v1', data=request.data)
                assert response.status == 200
                return await response.read()
            return Response(asyncio.run_coroutine_threadsafe(exchange(), loop).result(timeout=timeout))
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Bridge())
    transport = AgentHttps('https://staging.example', 42, SECRET)
    execute = Mock()
    agent = Agent(transport, 42, SECRET, tmp_path, execute)
    await asyncio.to_thread(agent.tick)
    execute.assert_not_called()
    payload = build_signed_command('lock', 42, SECRET)
    await redis.set('jarvis:pc_command:42', payload, ex=20)
    await asyncio.to_thread(agent.tick)
    await asyncio.to_thread(agent.tick)
    execute.assert_called_once_with('lock')
    heartbeat = read_status(await redis.get('jarvis:pc_status:42'), 42, SECRET)
    assert heartbeat['last_result']['state'] == 'completed'


@pytest.mark.parametrize('url', ['http://staging.example', 'https://user:password@staging.example',
    'https://staging.example/path', 'https://staging.example?token=unsafe'])
def test_agent_rejects_insecure_or_credential_bearing_origins(url):
    with pytest.raises(ValueError):
        AgentHttps(url, 42, SECRET)


def test_forged_server_response_cannot_authorize_client_execution(monkeypatch):
    import urllib.request
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, maximum):
            return signed_status({'user_id':42,'seen_at':int(time.time()),'rpc':1,
                'nonce':'f'*32,'result':'forged command'}, rpc_secret(SECRET)).encode()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Mock(open=Mock(return_value=Response())))
    with pytest.raises(ValueError, match='not authenticated'):
        AgentHttps('https://staging.example',42,SECRET).getdel('jarvis:pc_command:42')


async def test_observation_upload_and_signed_ack_survive_http_transport(http_mailbox, tmp_path):
    client, redis = http_mailbox
    from services.activity import ActivityJournal
    journal = ActivityJournal(tmp_path / 'activity.db')
    observation = signed_status({'user_id':42,'seen_at':int(time.time()),'buckets':journal.pending()}, SECRET)
    response = await client.post('/api/agent/v1',data=request_body('set','jarvis:activity:42',observation))
    assert response.status == 200
    assert await redis.get('jarvis:activity:42') == observation
    assert 0 < await redis.ttl('jarvis:activity:42') <= 600
    ack = signed_status({'user_id':42,'seen_at':int(time.time()),'ids':[]}, SECRET)
    await redis.set('jarvis:activity_ack:42',ack,ex=600)
    response = await client.post('/api/agent/v1',data=request_body('get','jarvis:activity_ack:42'))
    assert read_status(await response.text(),42,rpc_secret(SECRET))['result'] == ack


async def test_rate_limit_and_backend_failure_preserve_commands(http_mailbox, monkeypatch):
    client, redis = http_mailbox
    await redis.set('jarvis:pc_command:42','untouched')
    monkeypatch.setattr(agent_api,'allow',lambda *args,**kwargs:False)
    assert (await client.post('/api/agent/v1',data=request_body('getdel','jarvis:pc_command:42'))).status == 429
    assert await redis.get('jarvis:pc_command:42') == 'untouched'
    monkeypatch.setattr(agent_api,'allow',lambda *args,**kwargs:True)
    async def unavailable(*args,**kwargs):
        raise RuntimeError('PRIVATE-CONNECTION-DETAILS')
    monkeypatch.setattr(redis,'set',unavailable)
    response = await client.post('/api/agent/v1',data=request_body())
    assert response.status == 503 and 'PRIVATE' not in await response.text()


async def test_actual_http_redirect_is_not_followed(monkeypatch):
    from aiohttp import web
    from urllib.error import HTTPError
    from urllib.request import Request as RealRequest
    from services import agent_http
    reached = []
    async def redirect(request):
        raise web.HTTPFound('/sink')
    async def sink(request):
        reached.append(True)
        return web.Response(text='must not be reached')
    app = web.Application()
    app.router.add_post('/api/agent/v1',redirect)
    app.router.add_route('*','/sink',sink)
    async with TestServer(app) as destination:
        def loopback_request(url, **kwargs):
            assert url == 'https://staging.example/api/agent/v1'
            return RealRequest(str(destination.make_url('/api/agent/v1')),**kwargs)
        monkeypatch.setattr(agent_http,'Request',loopback_request)
        with pytest.raises(HTTPError) as failed:
            await asyncio.to_thread(AgentHttps('https://staging.example',42,SECRET).ping)
        assert failed.value.code == 302 and reached == []
