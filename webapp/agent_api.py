"""Optional authenticated mailbox access, never an OS-command creation endpoint."""
import os
import re
import time

from aiohttp import web

from config import get_settings
from services.agent_http import MAX_RPC_BYTES, permitted_key, rpc_secret
from services.pc_agent import read_status, signed_status
from services.rate_limit import allow

AGENT_REDIS_KEY = web.AppKey('agent_mailbox', object)


async def agent_exchange(request):
    if os.getenv('ENABLE_PC_AGENT_HTTP', '').lower() not in {'1', 'true', 'yes'}:
        raise web.HTTPNotFound(text='Agent HTTPS disabled')
    settings = get_settings()
    secret = settings.pc_agent_secret
    if not secret:
        raise web.HTTPServiceUnavailable(text='Agent unavailable')
    raw = await request.read()
    if len(raw) > MAX_RPC_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_RPC_BYTES, actual_size=len(raw))
    try:
        data = read_status(raw.decode('utf-8'), settings.admin_id, rpc_secret(secret))
    except UnicodeError:
        data = None
    if (not data or data.get('rpc') != 1 or abs(time.time() - data['seen_at']) > 30
            or not isinstance(data.get('nonce'), str) or not re.fullmatch(r'[a-f0-9]{32}', data['nonce'])):
        raise web.HTTPUnauthorized(text='Invalid agent authentication')
    operation, key = data.get('operation'), data.get('key')
    if not isinstance(key, str) or not permitted_key(operation, key, settings.admin_id):
        raise web.HTTPForbidden(text='Unsupported agent operation')
    if not allow(settings.admin_id, 'agent_https', limit=300):
        raise web.HTTPTooManyRequests(text='Agent rate limit')
    redis = request.app.get(AGENT_REDIS_KEY)
    if redis is None:
        raise web.HTTPServiceUnavailable(text='Mailbox unavailable')
    try:
        claimed = await redis.set(f"jarvis:agent_rpc_nonce:{settings.admin_id}:{data['nonce']}", '1', ex=90, nx=True)
        if not claimed:
            raise web.HTTPConflict(text='Agent request already used')
        if operation == 'ping':
            result = bool(await redis.ping())
        elif operation == 'get':
            result = await redis.get(key)
        elif operation == 'getdel':
            result = await redis.getdel(key)
        else:
            value = data.get('value')
            inner = read_status(value, settings.admin_id, secret)
            if not inner or abs(time.time() - inner['seen_at']) > 45:
                raise web.HTTPBadRequest(text='Invalid signed observation')
            if key.startswith('jarvis:pc_result:'):
                last = inner.get('last_result')
                if not isinstance(last, dict) or last.get('nonce') != key.rsplit(':', 1)[1]:
                    raise web.HTTPBadRequest(text='Invalid receipt identity')
            result = bool(await redis.set(key, value, ex=600 if key.startswith('jarvis:activity:') else 86400))
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPServiceUnavailable(text='Mailbox unavailable') from None
    body = signed_status({'user_id': settings.admin_id, 'seen_at': int(time.time()),
        'rpc': 1, 'nonce': data['nonce'], 'result': result}, rpc_secret(secret))
    # JSON is already serialized and signed; do not reinterpret private payloads.
    return web.Response(text=body, content_type='application/json')


def register(app, redis):
    app[AGENT_REDIS_KEY] = redis
    app.router.add_post('/api/agent/v1', agent_exchange)
