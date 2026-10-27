"""Opt-in owner-only delivery; importing/constructing never sends anything.

Providers acknowledge API acceptance, not that the owner read/heard a reminder.
Only Telegram's explicit rate-limit rejection is retried (max three attempts).
Ambiguous POST outcomes remain uncertain forever, especially paid phone calls.
Pending events without a fresh current-event validator are discarded on restart.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


_ZONE = timezone(timedelta(hours=5))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _audio_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443) or parsed.hostname.lower() == "localhost"):
            return None
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return None
        except ValueError:
            if "." not in parsed.hostname or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
                return None
        return value
    except ValueError:
        return None


class PhoneDelivery:
    """Finite Telegram/Twilio adapters; every recipient comes only from env.

    ``current_validator(event_id, text)`` may revalidate persisted events after
    restart. Without it, only the in-memory ``is_current()`` closure supplied
    when enqueueing can authorize an attempt. Neither callback grants PC access.
    TWILIO_PERSONAL_AUDIO_URL is a fixed pre-rendered recording, not dynamic TTS.
    """

    def __init__(self, state_dir, *, environ=None, opener=None, clock=None, current_validator=None):
        env = os.environ if environ is None else environ
        self._clock = clock or time.time
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self._lookup = current_validator if callable(current_validator) else None
        self._guards, self._lock = {}, threading.RLock()
        token, chat = env.get("DESKTOP_TELEGRAM_BOT_TOKEN", ""), env.get("DESKTOP_TELEGRAM_CHAT_ID", "")
        self._telegram = ((token, chat) if env.get("DESKTOP_TELEGRAM_ENABLED") == "1"
            and re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{30,128}", token)
            and re.fullmatch(r"[1-9][0-9]{0,19}", chat) else None)
        sid, secret = env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", "")
        caller, recipient = env.get("TWILIO_FROM", ""), env.get("TWILIO_TO", "")
        requested_audio = env.get("TWILIO_PERSONAL_AUDIO_URL", "")
        self._audio = _audio_url(requested_audio)
        self._twilio = ((sid, secret, caller, recipient) if env.get("JARVIS_PHONE_CALLS_ENABLED") == "1"
            and re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) and re.fullmatch(r"[0-9a-fA-F]{32}", secret)
            and all(re.fullmatch(r"\+[1-9][0-9]{7,14}", number) for number in (caller, recipient))
            and (not requested_audio or self._audio) else None)
        self._path = Path(state_dir) / "phone_delivery.sqlite3"
        self._ready = False
        if self._telegram or self._twilio:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._db() as db:
                    db.execute("""CREATE TABLE IF NOT EXISTS outbox(
                        provider TEXT, event_id TEXT, text TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                        next_at REAL NOT NULL, provider_id TEXT, error_class TEXT,
                        PRIMARY KEY(provider,event_id))""")
                    db.execute("CREATE TABLE IF NOT EXISTS call_days(day TEXT PRIMARY KEY)")
                    db.execute("UPDATE outbox SET state='uncertain' WHERE state='claimed'")
                self._ready = True
            except Exception:
                pass  # Optional delivery cannot prevent the local assistant.

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self._path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self):
        counts = {}
        if self._ready:
            try:
                with self._db() as db:
                    counts = dict(db.execute("SELECT state,count(*) FROM outbox GROUP BY state"))
            except Exception:
                return {"telegram": "unavailable", "calls": "unavailable", "voice": "unavailable", "counts": {}}
        return {"telegram": "configured" if self._ready and self._telegram else "not_configured",
                "calls": "configured" if self._ready and self._twilio else "not_configured",
                "voice": "personal_audio" if self._audio and self._twilio else "provider_fallback",
                "counts": counts}

    def enqueue(self, event_id, text, *, is_current=None, call=False):
        if (not self._ready or not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", event_id)
                or not isinstance(text, str) or not text.strip() or len(text) > 1500
                or any(ord(c) < 32 and c not in "\n\t" for c in text) or type(call) is not bool):
            return False
        providers = (["telegram"] if self._telegram else []) + (["twilio"] if call and self._twilio else [])
        try:
            fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
            now, inserted = self._clock(), False
            with self._lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT count(*) FROM outbox").fetchone()[0] >= 10000:
                    return False
                for provider in providers:
                    added = db.execute("""INSERT OR IGNORE INTO outbox(provider,event_id,text,fingerprint,state,created,next_at)
                        VALUES(?,?,?,?,'pending',?,?)""", (provider, event_id, text, fingerprint, now, now)).rowcount
                    if added and callable(is_current):
                        self._guards[(provider, event_id)] = is_current
                    inserted = inserted or bool(added)
            return inserted
        except Exception:
            return False

    def _current(self, row):
        guard = self._guards.get((row["provider"], row["event_id"]))
        try:
            return (guard() if guard else self._lookup(row["event_id"], row["text"]) if self._lookup else False) is True
        except Exception:
            return False

    def _request(self, row):
        if row["provider"] == "telegram":
            token, chat = self._telegram
            data = json.dumps({"chat_id": chat, "text": row["text"],
                               "link_preview_options": {"is_disabled": True}}, ensure_ascii=False).encode("utf-8")
            return Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                           headers={"Content-Type": "application/json"}, method="POST")
        sid, secret, caller, recipient = self._twilio
        speech = (f"<Play>{escape(self._audio)}</Play>" if self._audio else
                  f'<Say voice="Polly.Tatyana" language="ru-RU">{escape(row["text"])}</Say>')
        twiml = "<Response>" + speech + "</Response>"
        if len(twiml) > 4000:
            raise ValueError("speech_limit")
        data = urlencode({"From": caller, "To": recipient, "Twiml": twiml,
                          "Timeout": "20", "TimeLimit": "60", "Record": "false"}).encode("ascii")
        auth = base64.b64encode((sid + ":" + secret).encode("ascii")).decode("ascii")
        return Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", data=data,
                       headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": "Basic " + auth}, method="POST")

    def _post(self, row, request):
        try:
            response = self._opener.open(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            code = response.code
            if response.geturl() != request.full_url or code in (301, 302, 303, 307, 308):
                raise ValueError("redirect_refused")
            body = response.read(16385)
            if len(body) > 16384 or response.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("response_invalid")
            result = json.loads(body.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("response_invalid")
        if row["provider"] == "telegram":
            if code in (200, 429) and result.get("ok") is False and result.get("error_code") == 429:
                delay = result.get("parameters", {}).get("retry_after", 60)
                return "retry", max(60, min(3600, delay)) if type(delay) is int else 60
            message = result.get("result", {})
            if (200 <= code < 300 and result.get("ok") is True and isinstance(message, dict)
                    and type(message.get("message_id")) is int and message["message_id"] > 0
                    and str(message.get("chat", {}).get("id")) == self._telegram[1]):
                return "provider_accepted", str(message["message_id"])
        elif (200 <= code < 300 and re.fullmatch(r"CA[0-9a-fA-F]{32}", str(result.get("sid", "")))
              and result.get("account_sid") == self._twilio[0] and result.get("to") == self._twilio[3]):
            return "provider_accepted", result["sid"]
        return "failed" if code in (400, 401, 403, 404, 422, 429) else "uncertain", None

    def process_pending(self):
        if not self._ready:
            return self.status()
        try:
            with self._lock:
                with self._db() as db:
                    rows = db.execute("SELECT * FROM outbox WHERE state='pending' AND next_at<=? ORDER BY created LIMIT 4",
                                      (self._clock(),)).fetchall()
                for row in rows:
                    now, key = self._clock(), (row["provider"], row["event_id"])
                    state, detail, error_class = "discarded", None, None
                    if now - row["created"] >= 86400 or not self._current(row):
                        self._update(key, state)
                        continue
                    local = datetime.fromtimestamp(now, _ZONE)
                    if row["provider"] == "twilio" and not 480 <= local.hour * 60 + local.minute < 1350:
                        next_day = local.date() + timedelta(days=local.hour * 60 + local.minute >= 1350)
                        opening = datetime.combine(next_day, datetime.min.time(), _ZONE) + timedelta(hours=8)
                        self._update(key, "pending", next_at=opening.timestamp())
                        continue
                    try:
                        request = self._request(row)  # Validate before a durable attempt claim.
                        with self._db() as db:
                            db.execute("BEGIN IMMEDIATE")
                            if row["provider"] == "twilio" and not db.execute(
                                    "INSERT OR IGNORE INTO call_days(day) VALUES(?)", (local.date().isoformat(),)).rowcount:
                                db.execute("UPDATE outbox SET state='daily_limit' WHERE provider=? AND event_id=? AND state='pending'", key)
                                continue
                            claimed = db.execute("""UPDATE outbox SET state='claimed',attempts=attempts+1
                                WHERE provider=? AND event_id=? AND state='pending' AND attempts=? AND next_at<=?""",
                                                 (*key, row["attempts"], now)).rowcount
                        if not claimed:
                            continue
                        if not self._current(row):
                            self._update(key, "discarded")
                            continue
                        state, detail = self._post(row, request)
                    except Exception as error:
                        state, error_class = "uncertain", type(error).__name__
                    if state == "retry":
                        if row["attempts"] + 1 < 3 and now + detail < row["created"] + 86400:
                            self._update(key, "pending", next_at=now + max(detail, 60 * 2 ** row["attempts"]))
                        else:
                            self._update(key, "failed")
                    else:
                        self._update(key, state, provider_id=detail, error_class=error_class)
                    if state not in ("retry", "pending"):
                        self._guards.pop(key, None)
        except Exception:
            pass  # A local outbox/storage outage must not break deterministic UI.
        return self.status()

    def _update(self, key, state, *, next_at=0, provider_id=None, error_class=None):
        with self._db() as db:
            db.execute("UPDATE outbox SET state=?,next_at=?,provider_id=?,error_class=? WHERE provider=? AND event_id=?",
                       (state, next_at, provider_id, error_class, *key))
        if state not in ("pending", "claimed"):
            self._guards.pop(key, None)
