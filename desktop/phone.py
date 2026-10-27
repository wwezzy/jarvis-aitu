"""Opt-in loopback phone companion; HTTPS transport is configured separately.

This adapter exposes only user-authored plans and explicit self-reports. It
cannot dispatch PC actions, model tools, calls or push notifications. No server
starts on import, and missing credentials leave the desktop fully functional.
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
import sqlite3
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 8192
MAX_RESPONSE = 4 * 1024 * 1024
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
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
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

    def __init__(self, api, state_dir, *, port=8767, environ=None):
        env = os.environ if environ is None else environ
        token = env.get("JARVIS_PHONE_TOKEN", "")
        enabled = env.get("JARVIS_PHONE_ENABLED") == "1"
        valid_token = (isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token)
                       and len(set(token)) >= 8)
        requested_origin = env.get("JARVIS_PHONE_ORIGIN", "")
        self.origin = _origin(requested_origin)
        self.enabled = bool(enabled and valid_token and (not requested_origin or self.origin))
        self._token_hash = hashlib.sha256(token.encode("utf-8")).digest() if self.enabled else None
        self.api, self.state_dir, self.port = api, Path(state_dir), port
        self._server = self._thread = self._store = None
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
            server = None
            try:
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
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(timeout=3)
            self._reason = "stopped" if self.enabled else "configuration_required"

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
                if len(self.path) > 2048 or sum(len(k) + len(v) for k, v in self.headers.items()) > 16384:
                    self._error(431, "request_limit")
                    return False
                for name in ("Host", "Origin", "Authorization", "Content-Type", "Content-Length", "Idempotency-Key"):
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
                value = self.headers.get("Authorization", "")
                presented = value[7:] if value.startswith("Bearer ") and len(value) <= 263 else ""
                digest = hashlib.sha256(presented.encode("utf-8")).digest()
                if not presented or not hmac.compare_digest(digest, owner._token_hash):
                    self._error(401 if owner._gate.allow(failed_auth=True) else 429, "authorization_required")
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
                if self.path != "/api/plan":
                    self._error(404, "not_found")
                    return
                if not self._authorized():
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
                if self.path not in {"/api/report", "/api/plan", "/api/gym"}:
                    self._error(404, "not_found")
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
                    call, arguments = _validate(self.path, payload, owner.api)
                    canonical = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
                    fingerprint = hashlib.sha256((self.path + "\n" + canonical).encode("utf-8")).hexdigest()
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    self._error(400, "invalid_request")
                    return
                try:
                    outcome, cached = owner._store.claim(key, fingerprint, owner._token_hash.hex())
                    if outcome == "replay":
                        self._send(cached[0], cached[1].encode("utf-8"), replay=True)
                        return
                    if outcome != "new":
                        self._error(507 if outcome == "full" else 409,
                                    {"full": "request_store_full", "conflict": "idempotency_conflict",
                                     "uncertain": "result_uncertain"}[outcome])
                        return
                    try:
                        result, status = call(**arguments), 200
                    except ValueError:
                        result, status = {"error": "plan_item_invalid"}, 400
                    except Exception:
                        result, status = {"error": "operation_failed_inspect_plan"}, 503
                    body = _json(result)
                    owner._store.finish(key, status, body)
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
