"""Bounded, domain-separated HTTPS transport for the existing PC mailbox.

This transport cannot write a command. Only the Telegram authorization path can.
"""
import hashlib
import hmac
import re
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request

from services.pc_agent import read_status, signed_status

MAX_RPC_BYTES = 65536


def rpc_secret(secret):
    return hmac.new(secret.encode(), b'jarvis.agent.https.v1', hashlib.sha256).hexdigest()


def permitted_key(operation, key, user_id):
    prefix = 'jarvis:'
    if operation == 'ping':
        return key == ''
    if operation == 'getdel':
        return key == f'{prefix}pc_command:{user_id}'
    if operation == 'get':
        return key in {f'{prefix}pc_status:{user_id}', f'{prefix}activity_ack:{user_id}'}
    if operation == 'set':
        return (key in {f'{prefix}pc_status:{user_id}', f'{prefix}activity:{user_id}'}
                or bool(re.fullmatch(rf'jarvis:pc_result:{user_id}:[a-f0-9]{{32}}', key)))
    return False


class AgentHttps:
    def __init__(self, base_url, user_id, secret):
        parts = urlsplit(base_url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.path not in {'', '/'} or parts.query or parts.fragment or not secret or user_id <= 0):
            raise ValueError('PC_AGENT_SERVER_URL must be an HTTPS origin without credentials/path/query')
        self.url = base_url.rstrip('/') + '/api/agent/v1'
        self.user_id, self.secret = user_id, rpc_secret(secret)

    def request(self, operation, key='', value=None):
        if not isinstance(key, str) or not permitted_key(operation, key, self.user_id):
            raise ValueError('Unsupported agent operation')
        nonce = uuid.uuid4().hex
        payload = signed_status({'user_id': self.user_id, 'seen_at': int(time.time()),
            'rpc': 1, 'nonce': nonce, 'operation': operation, 'key': key, 'value': value}, self.secret).encode()
        if len(payload) > MAX_RPC_BYTES:
            raise ValueError('Agent payload too large')
        # Default certificate/hostname validation; never follows redirect to another origin.
        from urllib.request import HTTPRedirectHandler, build_opener
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = build_opener(NoRedirect())
        request = Request(self.url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        with opener.open(request, timeout=8) as response:
            raw = response.read(MAX_RPC_BYTES + 1)
        if len(raw) > MAX_RPC_BYTES:
            raise ValueError('Agent response too large')
        result = read_status(raw.decode('utf-8'), self.user_id, self.secret)
        if (not result or result.get('rpc') != 1 or result.get('nonce') != nonce
                or abs(time.time() - result['seen_at']) > 30):
            raise ValueError('Agent response not authenticated')
        return result.get('result')

    def ping(self):
        return self.request('ping') is True

    def get(self, key):
        return self.request('get', key)

    def getdel(self, key):
        return self.request('getdel', key)

    def set(self, key, value, *, ex=None):
        # Server fixes TTL by key type; the client cannot request unbounded retention.
        return self.request('set', key, value)
