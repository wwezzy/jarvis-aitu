"""Opt-in finite phone control; no networking, shell, models or OS executor.

The injected action handler MUST propose risky actions on its first call,
returning Reply(action=..., confirmation_id=...). Only its token-confirmation
call may dispatch them. Phone proxy challenges are separate from core tokens;
SQLite stores hashes only, and a restart cannot revive an unfinished request.
HTTP callers must authorize every operation and durably claim writes first.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import hmac
import ipaddress
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from desktop.actions import ALLOWED_ACTIONS, RISKY_ACTIONS


_TITLES = {
    "calculator": "Калькулятор", "notepad": "Блокнот", "explorer": "Проводник",
    "youtube": "YouTube", "browser": "Браузер", "chrome": "Google Chrome",
    "volume_up": "Громче", "volume_down": "Тише", "volume_mute": "Без звука",
    "volume_unmute": "Включить звук", "minimize": "Свернуть окно", "minimize_all": "Свернуть всё",
    "maximize": "Развернуть окно", "maximize_youtube": "Развернуть YouTube",
    "music_open": "Яндекс Музыка", "music_play": "Продолжить музыку", "music_morning": "Утренняя музыка",
    "lock": "Блокировка компьютера", "shutdown": "Выключение компьютера", "restart": "Перезагрузка",
    "hibernate": "Гибернация", "cancel_shutdown": "Отменить выключение",
}
_ACTIONS = frozenset(_TITLES).intersection(ALLOWED_ACTIONS)
_MESSAGES = {
    "not_configured": "Удалённое управление не настроено. Локальный помощник продолжает работать.",
    "approval_required": "На компьютере требуется разрешить удалённое управление и подтвердить владельца.",
    "authorization_refused": "Ключ удалённого управления изменён или отозван. Повтори привязку устройства.",
    "invalid_request": "Нужна одна команда из разрешённого списка или точный псевдоним из каталога.",
    "catalog_unavailable": "Разрешённый каталог сейчас недоступен.",
    "alias_refused": "Не найден единственный разрешённый объект с таким псевдонимом.",
    "confirmation_required": "Подтверди выбранное действие в течение 60 секунд. Сохрани открытые документы.",
    "confirmation_refused": "Подтверждение истекло, отменено или уже использовано. Запроси действие заново.",
    "result_uncertain": "Результат не подтверждён. Проверь компьютер; запрос автоматически не повторяется.",
    "store_unavailable": "Не удалось безопасно сохранить запрос. Действие не подтверждено.",
    "reported": "Обработчик завершил попытку. Проверь результат на компьютере.",
    "requested": "Запрос открытия передан разрешённому обработчику.",
    "canceled": "Запрос отменён до подтверждения.",
    "failed": "Действие не выполнено. Проверь компьютер.",
}
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_CHALLENGE = re.compile(r"[a-f0-9]{32}\Z")


def _strong(value):
    return isinstance(value, str) and bool(_TOKEN.fullmatch(value)) and len(set(value)) >= 8


def _hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _presented_matches(token, configuration):
    # None is the existing trusted in-process API. HTTP must require a bearer
    # token and explicitly bind the presented value on every mutating call.
    return token is None or _strong(token) and hmac.compare_digest(_hash(token), configuration[0])


def _https_origin(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or not value.isascii() or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        try:
            address = ipaddress.ip_address(host)
            netloc = f"[{host}]" if address.version == 6 else host
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host):
                return False
            netloc = host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return bool(parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None
                    and not parsed.path and not parsed.query and not parsed.fragment
                    and parsed.netloc == netloc and (parsed.port is None or 1 <= parsed.port <= 65535)
                    and not any(char in value for char in "\\%"))
    except ValueError:
        return False


def _alias(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 80 or value != value.strip()
            or value.startswith((".", "-")) or value.endswith(".")
            or any(not (unicodedata.category(char)[0] in {"L", "M", "N"} or char in " ._-+") for char in value)):
        raise ValueError("alias_invalid")
    return unicodedata.normalize("NFC", value)


def _text(value, fallback):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2000
            or any(ord(char) < 32 and char not in "\n\t" for char in value)
            or re.search(r"[A-Za-z]:[\\/]|\\\\|(?:https?|file)://|/(?:home|Users|tmp|etc)/", value, re.I)):
        return _MESSAGES[fallback]
    return value


def _answer(status, *, action=None, text=None, challenge=None):
    return {"ok": status in {"reported", "requested", "canceled", "confirmation_required"}, "status": status,
            "text": _text(text, status) if text is not None else _MESSAGES[status], "action": action,
            "confirmation_id": challenge, "expires_in": 60 if challenge else 0}


def _reply(value):
    if isinstance(value, dict):
        return {key: value.get(key) for key in ("text", "action", "confirmation_id")}
    return {key: getattr(value, key, None) for key in ("text", "action", "confirmation_id")}


class RemoteController:
    def __init__(self, state_dir, *, action_handler, project_catalog=None, app_catalog=None,
                 approved, environ=None, clock=None, project_editor="explorer"):
        if not callable(action_handler) or not callable(approved) or project_editor not in {None, "explorer", "vscode", "pycharm", "idea"}:
            raise ValueError("remote_adapter_invalid")
        self.state_dir, self.path = Path(state_dir), Path(state_dir) / "remote_control.sqlite3"
        self.action_handler, self.project_catalog, self.app_catalog = action_handler, project_catalog, app_catalog
        self.project_editor, self.approved = project_editor, approved
        self._environ, self._clock = environ if environ is not None else os.environ, clock or time.monotonic
        self._lock, self._pending, self._identity = threading.RLock(), {}, None

    def _configuration(self):
        try:
            values = self._environ
            control, phone, origin = values.get("JARVIS_REMOTE_TOKEN"), values.get("JARVIS_PHONE_TOKEN"), values.get("JARVIS_PHONE_ORIGIN")
            if (values.get("JARVIS_REMOTE_ENABLED") != "1" or not _strong(control) or not _strong(phone)
                    or hmac.compare_digest(_hash(control), _hash(phone)) or not _https_origin(origin)):
                return None
            return _hash(control), _hash(control + phone + origin)
        except Exception:
            return None

    def _state(self):
        with self._lock:
            configuration = self._configuration()
            identity = configuration[1] if configuration else None
            if identity != self._identity:
                self._pending.clear()
                self._identity = identity
            try:
                approved = configuration is not None and self.approved() is True
            except Exception:
                approved = False
            if not approved:
                self._pending.clear()
            reason = "ready" if approved else "approval_required" if configuration else "not_configured"
            return configuration, approved, reason

    def status(self):
        configuration, approved, reason = self._state()
        return {"configured": configuration is not None, "approved": approved, "available": approved, "reason": reason}

    def authorize(self, raw_control_token):
        configuration, approved, _ = self._state()
        if not approved or not _strong(raw_control_token):
            return False
        return hmac.compare_digest(_hash(raw_control_token), configuration[0])

    @staticmethod
    def _catalog_items(catalog, kind):
        if catalog is None:
            return []
        rows = catalog.projects() if kind == "project" else catalog.apps()
        if not isinstance(rows, list) or len(rows) > (200 if kind == "project" else 100):
            raise ValueError("catalog_invalid")
        items = []
        for row in rows:
            if not isinstance(row, dict) or row.get("available") is False:
                continue
            aliases = row.get("aliases", [])
            if not isinstance(aliases, list) or len(aliases) > 10:
                continue
            try:
                items.append({"name": _alias(row["name"]), "aliases": [_alias(alias) for alias in aliases]})
            except (KeyError, ValueError):
                continue
        return items

    def snapshot(self):
        state = self.status()
        result = {**state, "actions": [], "projects": [], "apps": []}
        if not state["available"]:
            return result
        result["actions"] = [{"name": name, "title": _TITLES[name], "requires_confirmation": name in RISKY_ACTIONS}
                             for name in sorted(_ACTIONS)]
        for key, catalog, kind in (("projects", self.project_catalog, "project"), ("apps", self.app_catalog, "app")):
            try:
                result[key] = self._catalog_items(catalog, kind)
            except Exception:
                pass
        return result

    def _now(self):
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("clock_invalid")
        return value

    @contextmanager
    def _database(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=3)) as database:
            database.execute("CREATE TABLE IF NOT EXISTS remote_challenges (challenge_hash TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, action TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)")
            with database:
                yield database

    def request(self, payload, *, control_token=None):
        configuration, approved, reason = self._state()
        if not approved:
            return _answer(reason)
        if not _presented_matches(control_token, configuration):
            return _answer("authorization_refused")
        if not isinstance(payload, dict) or set(payload) != {"kind", "name"}:
            return _answer("invalid_request")
        kind, name = payload["kind"], payload["name"]
        if kind not in ("action", "project", "app"):
            return _answer("invalid_request")
        if kind == "action":
            if not isinstance(name, str) or name not in _ACTIONS:
                return _answer("invalid_request")
            try:
                current, allowed, reason = self._state()
                if not allowed or current != configuration:
                    return _answer(reason if not allowed else "approval_required")
                response = _reply(self.action_handler(name))
                if response["action"] != name:
                    return _answer("result_uncertain", action=name)
                core_token = response["confirmation_id"]
                if name in RISKY_ACTIONS:
                    if not isinstance(core_token, str) or not _CHALLENGE.fullmatch(core_token):
                        return _answer("result_uncertain", action=name)
                    return self._propose(name, core_token, configuration)
                if core_token is not None:
                    return _answer("result_uncertain", action=name)
                return _answer("reported", action=name, text=response["text"])
            except Exception:
                return _answer("result_uncertain", action=name)
        catalog = self.project_catalog if kind == "project" else self.app_catalog
        if catalog is None:
            return _answer("catalog_unavailable")
        try:
            name = _alias(name)
            resolved = catalog.resolve(name)
            if not isinstance(resolved, dict) or resolved.get("status") != "found":
                return _answer("alias_refused")
            current, allowed, reason = self._state()
            if not allowed or current != configuration:
                return _answer(reason if not allowed else "approval_required")
            def still_approved():
                current, allowed, _reason = self._state()
                return allowed and current == configuration
            response = (catalog.open_project(name, editor=self.project_editor, authorization_guard=still_approved)
                        if kind == "project" else catalog.open_app(name, authorization_guard=still_approved))
            if not isinstance(response, dict):
                return _answer("result_uncertain")
            if response.get("status") == "denied":
                current, allowed, reason = self._state()
                return _answer(reason if not allowed else "approval_required" if current != configuration else "alias_refused")
            status = "requested" if response.get("status") == "requested" else "failed"
            return _answer(status, text=response.get("message"))
        except ValueError:
            return _answer("invalid_request")
        except Exception:
            return _answer("result_uncertain")

    def _propose(self, action, core_token, configuration):
        with self._lock:
            try:
                current, approved, reason = self._state()
                if not approved or current != configuration:
                    return _answer(reason if not approved else "approval_required")
                now = self._now()
                self._pending = {key: value for key, value in self._pending.items() if now < value[2]}
                if len(self._pending) >= 64:
                    return _answer("store_unavailable", action=action)
                token = secrets.token_hex(16)
                digest = _hash(token)
                with self._database() as database:
                    database.execute("BEGIN IMMEDIATE")
                    if database.execute("SELECT count(*) FROM remote_challenges").fetchone()[0] >= 10000:
                        return _answer("store_unavailable", action=action)
                    database.execute("INSERT INTO remote_challenges VALUES(?,?,?,?,?,0)", (digest, configuration[0], action, now, now + 60))
                self._pending[digest] = (action, core_token, now + 60)
                return _answer("confirmation_required", action=action, challenge=token)
            except Exception:
                return _answer("store_unavailable", action=action)

    def _finish(self, token, *, cancel, control_token=None):
        configuration, approved, reason = self._state()
        if not approved:
            return _answer(reason)
        if not _presented_matches(control_token, configuration):
            return _answer("authorization_refused")
        if not isinstance(token, str) or not _CHALLENGE.fullmatch(token):
            return _answer("confirmation_refused")
        digest, action = _hash(token), None
        with self._lock:
            try:
                now = self._now()
                if not self.path.is_file():
                    return _answer("confirmation_refused")
                with self._database() as database:
                    database.execute("BEGIN IMMEDIATE")
                    row = database.execute("SELECT owner_hash,action,issued,expires,consumed FROM remote_challenges WHERE challenge_hash=?", (digest,)).fetchone()
                    if row is None or row[4] or not hmac.compare_digest(row[0], configuration[0]) or not row[2] <= now < row[3] <= row[2] + 60:
                        self._pending.pop(digest, None)
                        return _answer("confirmation_refused")
                    action = row[1]
                    database.execute("UPDATE remote_challenges SET consumed=1 WHERE challenge_hash=? AND consumed=0", (digest,))
                pending = self._pending.pop(digest, None)
                if (pending is None or pending[0] != action or action not in RISKY_ACTIONS
                        or not now < pending[2] or pending[2] != row[3]):
                    return _answer("result_uncertain", action=action if action in _ACTIONS else None)
                current, allowed, reason = self._state()
                if not allowed or current != configuration:
                    return _answer(reason if not allowed else "approval_required", action=action)
                response = _reply(self.action_handler(action, token=pending[1], cancel=cancel))
                if response["confirmation_id"] is not None or not cancel and response["action"] != action:
                    return _answer("result_uncertain", action=action)
                return _answer("canceled" if cancel else "reported", action=action, text=response["text"])
            except Exception:
                # A durable claim is consumed before dispatch. Never repeat an
                # operation whose callback or result may have been interrupted.
                self._pending.pop(digest, None)
                return _answer("result_uncertain", action=action)

    def confirm(self, token, *, control_token=None):
        return self._finish(token, cancel=False, control_token=control_token)

    def cancel(self, token, *, control_token=None):
        return self._finish(token, cancel=True, control_token=control_token)
