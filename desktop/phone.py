"""Opt-in loopback phone companion; HTTPS transport is configured separately.

Plans and optional finite PC control use separate credentials. This transport
never calls Core.handle or a model; the explicit remote controller owns action
policy and one-use confirmations. No server starts on import.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import urlsplit


MAX_BODY = 8192
MAX_RESPONSE = 4 * 1024 * 1024
_CONTROL_POSTS = {"/api/control", "/api/control/confirm", "/api/control/cancel"}
_REJECT_DRAIN_LIMIT = MAX_BODY + 1
_REJECT_DRAIN_SECONDS = 0.25
_STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
           "/index.html": ("index.html", "text/html; charset=utf-8"),
           "/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/style.css": ("style.css", "text/css; charset=utf-8"),
           "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
           "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; worker-src 'self'")
_DELIVERY = {"push": "not_configured", "phone_calls": "not_configured",
             "message": "Push и звонки не подключены. План доступен, пока ноутбук и защищённое соединение работают."}


class OrganizerPhoneAdapter:
    """A finite projection of LocalOrganizer, excluding desktop preferences."""

    def __init__(self, organizer):
        self.organizer = organizer

    def snapshot(self):
        source = self.organizer.snapshot()
        allowed = {"id", "title", "date", "time", "minutes", "kind", "status", "provenance", "reported_minutes"}
        result = {}
        for key in ("today", "tomorrow"):
            day = source[key]
            result[key] = {"date": day["date"], "timezone": day["timezone"],
                           "items": [{name: value for name, value in item.items() if name in allowed}
                                     for item in day["items"]]}
        gym = self.organizer.gym_settings()
        result["gym"] = {name: gym[name] for name in ("days", "time", "minutes", "enabled")}
        result["delivery"] = dict(_DELIVERY)
        return result

    def report(self, item_id, status):
        return self.organizer.report_item(item_id, status=status)

    def add_plan(self, date_key, title):
        return {"item_id": self.organizer.add_item(title, date_key)}

    def set_gym(self, weekdays, time=None):
        # This phone form edits days/time only; retain a desktop-authored duration.
        minutes = self.organizer.gym_settings()["minutes"]
        gym = self.organizer.set_weekly_gym(weekdays, time=time, minutes=minutes, enabled=bool(weekdays))
        return {name: gym[name] for name in ("days", "time", "minutes", "enabled")}


class _RateGate:
    def __init__(self, maximum=180, auth_failures=20, clock=time.monotonic):
        self.maximum, self.auth_failures, self.clock = maximum, auth_failures, clock
        self._requests, self._failures = deque(), deque()
        self._lock = threading.Lock()

    def allow(self, *, failed_auth=False):
        with self._lock:
            now = self.clock()
            values, maximum = (self._failures, self.auth_failures) if failed_auth else (self._requests, self.maximum)
            while values and values[0] <= now - 60:
                values.popleft()
            if len(values) >= maximum:
                return False
            values.append(now)
            return True


class PhoneStore:
    """Durable at-most-once attempts, including crash-uncertain claims.

    Claims are never aged out and are capped at 10,000. After a process crash,
    an unfinished claim is refused rather than risking a duplicated write.
    The caller must inspect the current plan before choosing a new operation.
    Only token/key/request hashes and small API results are stored here.
    """

    def __init__(self, state_dir):
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "phone.sqlite3"
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS phone_claims (
                key_hash TEXT PRIMARY KEY, request_hash TEXT NOT NULL, token_hash TEXT NOT NULL,
                http_status INTEGER, response TEXT, created_at REAL NOT NULL)""")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, key, fingerprint, token_hash):
        key_hash = hashlib.sha256(key.encode("ascii")).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request_hash,http_status,response FROM phone_claims WHERE key_hash=?", (key_hash,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    return "conflict", None
                return ("replay", (row[1], row[2])) if row[1] is not None else ("uncertain", None)
            if db.execute("SELECT count(*) FROM phone_claims").fetchone()[0] >= 10000:
                return "full", None
            db.execute("INSERT INTO phone_claims(key_hash,request_hash,token_hash,created_at) VALUES(?,?,?,?)",
                       (key_hash, fingerprint, token_hash, time.time()))
        return "new", None

    def finish(self, key, status, body):
        if len(body.encode("utf-8")) > 16384:
            raise ValueError("result_limit")
        with self._connection() as db:
            db.execute("UPDATE phone_claims SET http_status=?,response=? WHERE key_hash=? AND http_status IS NULL",
                       (status, body, hashlib.sha256(key.encode("ascii")).hexdigest()))


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(12)
        self._requests_lock, self._requests = threading.Lock(), {}
        super().__init__(address, handler)

    @property
    def drained(self):
        with self._requests_lock:
            for key, (_request, thread, done) in list(self._requests.items()):
                if done and (thread is None or not thread.is_alive()):
                    self._requests.pop(key)
            return not self._requests

    def interrupt_connections(self):
        # Closing owned sockets releases partial-header/body reads. Running
        # adapters finish under their own live authorization/deadline checks.
        with self._requests_lock:
            requests = [value[0] for value in self._requests.values()]
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        _ = self.drained  # Prune completed thread records during normal use.
        with self._requests_lock:
            self._requests[id(request)] = (request, None, False)
        try:
            super().process_request(request, address)
        except Exception:
            with self._requests_lock:
                self._requests.pop(id(request), None)
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        with self._requests_lock:
            self._requests[id(request)] = (request, threading.current_thread(), False)
        try:
            super().process_request_thread(request, address)
        finally:
            with self._requests_lock:
                self._requests[id(request)] = (request, threading.current_thread(), True)
            self._slots.release()

    def handle_error(self, request, client_address):
        pass  # Never log headers, token, plan content or private exception text.


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


def _origin(value):
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port == 0 or any(char.isspace() for char in value)):
            return None
        return "https://" + parsed.netloc.lower()
    except ValueError:
        return None


class PhoneServer:
    """Explicit start/stop; always bind 127.0.0.1, never expose a public socket."""

    def __init__(self, api, state_dir, *, port=8767, environ=None, remote=None):
        env = os.environ if environ is None else environ
        self._environ = env
        token = env.get("JARVIS_PHONE_TOKEN", "")
        enabled = env.get("JARVIS_PHONE_ENABLED") == "1"
        valid_token = (isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token)
                       and len(set(token)) >= 8)
        requested_origin = env.get("JARVIS_PHONE_ORIGIN", "")
        self.origin = _origin(requested_origin)
        self.enabled = bool(enabled and valid_token and (not requested_origin or self.origin))
        self._token_hash = hashlib.sha256(token.encode("utf-8")).digest() if self.enabled else None
        self.api, self.state_dir, self.port = api, Path(state_dir), port
        self.remote = remote
        self._server = self._thread = self._store = None
        self._stopping, self._retired = threading.Event(), []
        self._gate, self._lock = _RateGate(), threading.Lock()
        self._reason = "not_started" if self.enabled else "configuration_required"

    def status(self):
        port = self._server.server_port if self._server else self.port
        return {"enabled": self.enabled, "running": self._server is not None,
                "url": f"http://127.0.0.1:{port}" if self._server else None,
                "phone_origin": self.origin,
                "transport": "https_proxy_not_verified" if self.origin else "loopback_only",
                "reason": self._reason, "delivery": dict(_DELIVERY)}

    def start(self):
        with self._lock:
            if not self.enabled or self._server is not None:
                return self.status()
            if any(not server.drained for server in self._retired):
                self._reason = "stopping"
                return self.status()
            self._retired.clear()
            server = None
            try:
                self._stopping.clear()
                if type(self.port) is not int or not 0 <= self.port <= 65535:
                    raise ValueError("port_invalid")
                store = PhoneStore(self.state_dir)
                server = _LoopbackServer(("127.0.0.1", self.port), self._handler())
                self._store, self._server = store, server
                self._thread = threading.Thread(target=server.serve_forever, name="jarvis-phone", daemon=True)
                self._thread.start()
                self._reason = "ready"
            except Exception:
                if server:
                    server.server_close()
                self._store = self._server = self._thread = None
                self._reason = "unavailable"
            return self.status()

    def stop(self):
        self.prevent_requests()
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            if server:
                self._retired.append(server)
                server.shutdown()
                server.interrupt_connections()
                server.server_close()
            if thread:
                thread.join(timeout=3)
            self._reason = "stopped" if self.enabled else "configuration_required"

    def prevent_requests(self):
        """Nonblocking admission revocation, including while provider I/O runs."""
        self._stopping.set()

    @property
    def shutdown_complete(self):
        with self._lock:
            self._retired = [server for server in self._retired if not server.drained]
            return self._stopping.is_set() and self._server is None and not self._retired

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            server_version = "JarvisPhone"
            sys_version = ""

            def setup(self):
                super().setup()
                self.connection.settimeout(3)
                self._body_read = False

            def log_message(self, *args):
                pass

            def _send(self, status, value, *, content_type="application/json; charset=utf-8", replay=False):
                body = value if isinstance(value, bytes) else _json(value).encode("utf-8")
                self.send_response(status)
                for key, val in {"Content-Type": content_type, "Content-Length": str(len(body)),
                                 "Cache-Control": "no-store", "Content-Security-Policy": _CSP,
                                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                 "Referrer-Policy": "no-referrer", "Connection": "close",
                                 "Permissions-Policy": "microphone=(), camera=(), geolocation=()"}.items():
                    self.send_header(key, val)
                if status == 429:
                    self.send_header("Retry-After", "60")
                if replay:
                    self.send_header("Idempotency-Replayed", "true")
                self.end_headers()
                self.close_connection = True
                if self.command != "HEAD":
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass

            def _error(self, status, code):
                self._discard_rejected_body()
                self._send(status, {"error": code})

            def _discard_rejected_body(self):
                # Closing a Windows socket with an unread POST body can replace
                # the HTTP error with a TCP reset. Discard only bounded raw
                # bytes: rejected content never reaches JSON or an adapter.
                if self._body_read:
                    return
                self._body_read = True
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                    return
                remaining = min(int(lengths[0]), _REJECT_DRAIN_LIMIT)
                deadline = time.monotonic() + _REJECT_DRAIN_SECONDS
                previous_timeout = self.connection.gettimeout()
                try:
                    while remaining:
                        budget = deadline - time.monotonic()
                        if budget <= 0:
                            break
                        self.connection.settimeout(budget)
                        chunk = self.rfile.read1(min(remaining, 1024))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        self.connection.settimeout(previous_timeout)
                    except OSError:
                        pass

            def _guard(self):
                if owner._stopping.is_set():
                    self._error(503, "server_stopping")
                    return False
                if len(self.path) > 2048 or sum(len(k) + len(v) for k, v in self.headers.items()) > 16384:
                    self._error(431, "request_limit")
                    return False
                for name in ("Host", "Origin", "Authorization", "Content-Type", "Content-Length", "Idempotency-Key", "X-Jarvis-Control"):
                    if len(self.headers.get_all(name, [])) > 1:
                        self._error(400, "duplicate_header")
                        return False
                hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                origins = {f"http://{host}" for host in hosts}
                if owner.origin:
                    hosts.add(urlsplit(owner.origin).netloc)
                    origins.add(owner.origin)
                if self.headers.get("Host", "").lower() not in hosts:
                    self._error(403, "host_refused")
                    return False
                if self.headers.get("Origin") is not None and self.headers["Origin"] not in origins:
                    self._error(403, "origin_refused")
                    return False
                if not owner._gate.allow():
                    self._error(429, "rate_limit")
                    return False
                try:
                    parsed = urlsplit(self.path)
                except ValueError:
                    self._error(400, "path_refused")
                    return False
                if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                    self._error(400, "path_refused")
                    return False
                return True

            def _authorized(self):
                try:
                    live_token = owner._environ.get("JARVIS_PHONE_TOKEN", "")
                    live_origin = owner._environ.get("JARVIS_PHONE_ORIGIN", "")
                    current = (owner._environ.get("JARVIS_PHONE_ENABLED") == "1"
                        and isinstance(live_token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", live_token)
                        and owner._token_hash is not None
                        and hmac.compare_digest(hashlib.sha256(live_token.encode("ascii")).digest(), owner._token_hash)
                        and (not live_origin or _origin(live_origin) is not None)
                        and _origin(live_origin) == owner.origin)
                except Exception:
                    current = False
                if not current:
                    self._error(401, "authorization_required")
                    return False
                value = self.headers.get("Authorization", "")
                presented = value[7:] if value.startswith("Bearer ") and len(value) <= 263 else ""
                digest = hashlib.sha256(presented.encode("utf-8")).digest()
                if not presented or not hmac.compare_digest(digest, owner._token_hash):
                    self._error(401 if owner._gate.allow(failed_auth=True) else 429, "authorization_required")
                    return False
                return True

            def _control_authorized(self):
                if owner.remote is None:
                    self._error(503, "control_unavailable")
                    return False
                presented = self.headers.get("X-Jarvis-Control", "")
                try:
                    accepted = (bool(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", presented))
                                and owner.remote.authorize(presented) is True)
                except Exception:
                    accepted = False
                if not accepted:
                    self._error(403 if owner._gate.allow(failed_auth=True) else 429, "control_authorization_required")
                    return False
                return True

            def do_GET(self):
                if not self._guard():
                    return
                if self.path in _STATIC:
                    filename, content_type = _STATIC[self.path]
                    try:
                        value = (Path(__file__).parent / "phone_web" / filename).read_bytes()
                    except OSError:
                        self._error(503, "web_unavailable")
                        return
                    self._send(200, value, content_type=content_type)
                    return
                if self.path not in {"/api/plan", "/api/control"}:
                    self._error(404, "not_found")
                    return
                if not self._authorized():
                    return
                if self.path == "/api/control":
                    if not self._control_authorized():
                        return
                    try:
                        body = _json(_remote_snapshot(owner.remote.snapshot())).encode("utf-8")
                        if len(body) > MAX_RESPONSE:
                            raise ValueError("response_limit")
                        self._send(200, body)
                    except Exception:
                        self._error(503, "control_unavailable")
                    return
                try:
                    body = _json(owner.api.snapshot()).encode("utf-8")
                    if len(body) > MAX_RESPONSE:
                        raise ValueError("response_limit")
                    self._send(200, body)
                except Exception:
                    self._error(503, "plan_unavailable")

            def do_POST(self):
                if not self._guard() or not self._authorized():
                    return
                control = self.path in _CONTROL_POSTS
                if self.path not in {"/api/report", "/api/plan", "/api/gym"} | _CONTROL_POSTS:
                    self._error(404, "not_found")
                    return
                if control and not self._control_authorized():
                    return
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
                    self._error(400, "encoding_refused")
                    return
                if self.headers.get("Content-Type", "").lower() != "application/json":
                    self._error(415, "json_required")
                    return
                length = self.headers.get("Content-Length", "")
                if not re.fullmatch(r"[0-9]{1,5}", length):
                    self._error(411, "length_required")
                    return
                if not 0 < int(length) <= MAX_BODY:
                    self._error(413, "body_limit")
                    return
                key = self.headers.get("Idempotency-Key", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
                    self._error(400, "idempotency_key_required")
                    return
                try:
                    raw = self.rfile.read(int(length))
                    self._body_read = True
                    if len(raw) != int(length):
                        raise ValueError("incomplete_body")
                    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant_refused")))
                    call, arguments = (_validate_control(self.path, payload, owner.remote,
                                                        control_token=self.headers.get("X-Jarvis-Control", "")) if control
                                       else _validate(self.path, payload, owner.api))
                    canonical = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
                    fingerprint = hashlib.sha256((self.path + "\n" + canonical).encode("utf-8")).hexdigest()
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    self._error(400, "invalid_request")
                    return
                try:
                    outcome, cached = owner._store.claim(key, fingerprint, owner._token_hash.hex())
                    if outcome == "replay":
                        if control:
                            self._send(409, {"error": "result_uncertain"}, replay=True)
                        else:
                            self._send(cached[0], cached[1].encode("utf-8"), replay=True)
                        return
                    if outcome != "new":
                        self._error(507 if outcome == "full" else 409,
                                    {"full": "request_store_full", "conflict": "idempotency_conflict",
                                     "uncertain": "result_uncertain"}[outcome])
                        return
                    try:
                        if owner._stopping.is_set():
                            raise RuntimeError("server_stopping")
                        if not self._authorized():
                            return
                        if control and not self._control_authorized():
                            return
                        result, status = call(**arguments), 200
                        if control:
                            try:
                                result = _remote_result(result)
                            except (ValueError, TypeError):
                                raise RuntimeError("control_result_uncertain") from None
                    except ValueError:
                        result, status = {"error": "control_request_invalid" if control else "plan_item_invalid"}, 400
                    except Exception:
                        result, status = {"error": "control_result_uncertain" if control else "operation_failed_inspect_plan"}, 503
                    body = _json(result)
                    # A remote challenge/result lives only in this response.
                    # Durable replays can never recover a token or repeat an
                    # action; even an apparently successful POST stays generic.
                    owner._store.finish(key, 409 if control else status,
                                        _json({"error": "result_uncertain"}) if control else body)
                    self._send(status, body.encode("utf-8"))
                except Exception:
                    # The durable claim remains; a retry cannot repeat a possibly
                    # completed mutation if saving the response itself failed.
                    self._error(503, "result_uncertain")

            def do_OPTIONS(self):
                if self._guard():
                    self._error(405, "method_refused")

            do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS

        return Handler


def _remote_alias(value):
    return (isinstance(value, str) and 1 <= len(value) <= 80 and value == value.strip()
            and not value.startswith((".", "-")) and not value.endswith(".")
            and all(unicodedata.category(char)[0] in {"L", "M", "N"} or char in " ._-+" for char in value))


def _remote_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError("control_snapshot_invalid")
    from desktop.actions import ALLOWED_ACTIONS
    result = {key: value.get(key) is True for key in ("configured", "approved", "available")}
    reason = value.get("reason")
    result["reason"] = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason) else "unavailable"
    result.update(actions=[], projects=[], apps=[])
    if not result["available"]:
        return result
    for item in value.get("actions", [])[:100]:
        if (isinstance(item, dict) and item.get("name") in ALLOWED_ACTIONS
                and isinstance(item.get("title"), str) and 1 <= len(item["title"]) <= 200):
            result["actions"].append({"name": item["name"], "title": item["title"],
                                      "requires_confirmation": item.get("requires_confirmation") is True})
    for key, maximum in (("projects", 200), ("apps", 100)):
        for item in value.get(key, [])[:maximum]:
            if isinstance(item, dict) and _remote_alias(item.get("name")):
                aliases = item.get("aliases", [])
                result[key].append({"name": item["name"], "aliases": [alias for alias in aliases[:10] if _remote_alias(alias)]
                                    if isinstance(aliases, list) else []})
    return result


def _remote_result(value):
    if not isinstance(value, dict):
        raise ValueError("control_result_invalid")
    from desktop.actions import ALLOWED_ACTIONS
    status, text = value.get("status"), value.get("text")
    challenge, expires = value.get("confirmation_id"), value.get("expires_in", 0)
    if (type(value.get("ok")) is not bool or not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,64}", status)
            or not isinstance(text, str) or not 1 <= len(text) <= 2000
            or any(ord(char) < 32 and char not in "\n\t" for char in text)
            or re.search(r"[A-Za-z]:[\\/]|\\\\|(?:https?|file)://|/(?:home|Users|tmp|etc)/", text, re.I)
            or value.get("action") is not None and value["action"] not in ALLOWED_ACTIONS
            or challenge is not None and (not isinstance(challenge, str) or not re.fullmatch(r"[a-f0-9]{32}", challenge))
            or type(expires) is not int or not 0 <= expires <= 60 or challenge is not None and expires == 0):
        raise ValueError("control_result_invalid")
    return {"ok": value["ok"], "status": status, "text": text, "action": value.get("action"),
            "confirmation_id": challenge, "expires_in": expires}


def _validate_control(path, payload, remote, *, control_token):
    if not isinstance(payload, dict):
        raise ValueError("control_object_required")
    if path == "/api/control":
        if set(payload) != {"kind", "name"} or payload["kind"] not in ("action", "project", "app"):
            raise ValueError("control_request_invalid")
        if payload["kind"] == "action":
            from desktop.actions import ALLOWED_ACTIONS
            if not isinstance(payload["name"], str) or payload["name"] not in ALLOWED_ACTIONS:
                raise ValueError("control_action_invalid")
        elif not _remote_alias(payload["name"]):
            raise ValueError("control_alias_invalid")
        return lambda: remote.request(dict(payload), control_token=control_token), {}
    if (set(payload) != {"token"} or not isinstance(payload["token"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", payload["token"])):
        raise ValueError("control_confirmation_invalid")
    return remote.confirm if path == "/api/control/confirm" else remote.cancel, {
        "token": payload["token"], "control_token": control_token}


def _validate(path, payload, api):
    if not isinstance(payload, dict):
        raise ValueError("object_required")
    if path == "/api/report":
        if (set(payload) != {"item_id", "status"} or not isinstance(payload["item_id"], str)
                or not 1 <= len(payload["item_id"]) <= 100
                or payload["status"] not in ("done", "missed", "unknown")):
            raise ValueError("report_invalid")
        payload["item_id"].encode("utf-8")
        return api.report, payload
    if path == "/api/plan":
        if (set(payload) != {"date_key", "title"} or not isinstance(payload["date_key"], str)
                or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", payload["date_key"])
                or not isinstance(payload["title"], str) or not payload["title"].strip()
                or len(payload["title"]) > 1000 or "\x00" in payload["title"]):
            raise ValueError("plan_invalid")
        from datetime import date
        parsed = date.fromisoformat(payload["date_key"])
        if not 1900 <= parsed.year <= 2200:
            raise ValueError("date_invalid")
        payload["title"].encode("utf-8")
        return api.add_plan, payload
    if (set(payload) - {"weekdays", "time"} or "weekdays" not in payload
            or not isinstance(payload["weekdays"], list) or len(payload["weekdays"]) > 7
            or any(type(day) is not int or not 0 <= day <= 6 for day in payload["weekdays"])
            or len(set(payload["weekdays"])) != len(payload["weekdays"])
            or payload.get("time") is not None and (not isinstance(payload["time"], str)
                or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", payload["time"]))):
        raise ValueError("gym_invalid")
    return api.set_gym, {"weekdays": payload["weekdays"], "time": payload.get("time")}
