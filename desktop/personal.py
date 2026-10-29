"""Read-only projection of a recovered personal snapshot, never configuration.

This module has no dependency on the Telegram bot, env secrets or cloud clients.
Rows from recovered materials remain data and can never authorize PC actions.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SNAPSHOT_DATE = "2026-10-10"
_MAX_ROWS = 200
_URL = re.compile(r"\b(?:https?|rediss?|postgres(?:ql)?(?:\+asyncpg)?)://[^\s<>]+", re.I)
_SENSITIVE_KEY = re.compile(r"token|secret|password|api[\s_-]*key|парол|токен|ключ\s+api|(?:lms|calendar).*url", re.I)
_CREDENTIAL = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|\d{6,12}:[A-Za-z0-9_-]{25,})\b")
_AQ_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9_-])AQ\.[A-Za-z0-9_-]{20,197}(?![A-Za-z0-9_-])")
_ASSIGNMENT = re.compile(r"\b(?:[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))\s*=\s*\S+", re.I)
_FACT_SECRET = re.compile(r"token|secret|password|credential|authorization|cookie|session|парол|токен|api[\s_-]*key|lms|moodle|url|ссылк", re.I)
_FACT_URL = re.compile(r"\b(?:[a-z][a-z0-9+.-]{1,20}://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/?:#]\S*)?)", re.I)


def safe_context_text(value, maximum=2000):
    """Bounded model-facing data: redact bare domains and credential values."""
    if type(maximum) is not int or not 1 <= maximum <= 50000:
        raise ValueError("Invalid context text limit")
    clean = _FACT_URL.sub("[ссылка скрыта]", safe_text(value, maximum))
    return re.sub(r"(?i)\b(?:bearer|password|пароль|secret|token|токен)\s*[:= ]\s*\S+", "[секрет скрыт]", clean)[:maximum]


def sanitize_fact(key, value, maximum=500):
    """Explicit profile projection, never a prompt or execution instruction."""
    if (not isinstance(key, str) or not key.strip() or len(key) > 80 or _FACT_SECRET.search(key)
            or not isinstance(value, str) or not value.strip() or len(value) > 2000):
        raise ValueError("Invalid or sensitive personal fact")
    clean_key = " ".join(safe_text(key, 80).split())
    clean = safe_context_text(value, 2000)
    clean = " ".join("".join(char if ord(char) >= 32 else " " for char in clean).split())
    if not clean or not clean_key:
        raise ValueError("Empty personal fact")
    return clean_key, clean[:maximum]


def default_snapshot_path() -> Path:
    primary = Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "Jarvis" / "recovered-20261010.sqlite3"
    # A packaged developer terminal can virtualize AppData. The operator's
    # explicit recovery handoff lives outside that virtualization and outside
    # the public installer; only this known local file is considered.
    handoff = Path.home() / "Downloads" / "Jarvis-Recovery" / "recovered-20261010.sqlite3"
    return primary if primary.is_file() or not handoff.is_file() else handoff


def safe_text(value, maximum: int = 2000) -> str:
    text = str(value or "").replace("\x00", "")
    text = _AQ_CREDENTIAL.sub("[секрет скрыт]", text)
    text = _URL.sub("[ссылка скрыта]", text)
    text = _CREDENTIAL.sub("[секрет скрыт]", text)
    text = _ASSIGNMENT.sub("[настройка скрыта]", text)
    return text[:maximum] + ("…" if len(text) > maximum else "")


def display_due(value) -> str:
    if not value:
        return "Срок не указан"
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone(timedelta(hours=5))).strftime("%d.%m.%Y · %H:%M")
    except (TypeError, ValueError):
        return "Дата требует проверки"


def load_personal_snapshot(path: Path | None = None) -> dict:
    """Return only one identified user's whitelisted public-facing columns.

    A missing/partial old table is reported per section. Multiple users require
    an explicit future owner-selection flow; no identity is guessed. Source
    SQLite is opened read-only and is not migrated, created or modified.
    """
    result = {"status": "missing", "snapshot_date": SNAPSHOT_DATE, "name": "",
              "schedule": [], "assignments": [], "memory": [], "tasks": [],
              "unavailable": [], "truncated": []}
    source = Path(path) if path is not None else default_snapshot_path()
    if not source.is_file():
        return result
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            steps = 0
            def bounded_query():
                nonlocal steps
                steps += 1
                return int(steps > 1000)
            db.set_progress_handler(bounded_query, 1000)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "users" not in tables:
                result["status"] = "identity_missing"
                return result
            users = db.execute("SELECT telegram_id,name FROM users LIMIT 2").fetchall()
            if len(users) != 1:
                result["status"] = "selection_required" if users else "identity_missing"
                return result
            user_id = users[0]["telegram_id"]
            if not isinstance(user_id, int) or user_id <= 0:
                result["status"] = "identity_missing"
                return result
            result["name"] = safe_text(users[0]["name"], 100)
            queries = {
                "schedule": ("schedule_entries", "SELECT id,weekday,start,end,substr(title,1,2001) AS title FROM schedule_entries WHERE user_id=? AND enabled=1 ORDER BY weekday,start,id LIMIT 201"),
                "assignments": ("assignments", "SELECT id,substr(title,1,2001) AS title,substr(course,1,301) AS course,due_at,status FROM assignments WHERE user_id=? AND status NOT IN ('done','completed','cancelled','archived','deleted') ORDER BY due_at IS NULL,due_at,id LIMIT 201"),
                "memory": ("memory_facts", "SELECT id,substr(key,1,301) AS key,substr(value,1,2001) AS value FROM memory_facts WHERE user_id=? ORDER BY id LIMIT 201"),
                "tasks": ("tasks", "SELECT id,substr(title,1,2001) AS title,status FROM tasks WHERE user_id=? AND status NOT IN ('done','completed','cancelled','archived','deleted') ORDER BY id LIMIT 201"),
            }
            for section, (table, query) in queries.items():
                if table not in tables:
                    # `tasks` is optional in imported layouts; current bot tasks
                    # live in `assignments`, not a second copy of those rows.
                    if section != "tasks":
                        result["unavailable"].append(section)
                    continue
                try:
                    rows = db.execute(query, (user_id,)).fetchall()
                except sqlite3.DatabaseError:
                    result["unavailable"].append(section)
                    continue
                if len(rows) > _MAX_ROWS:
                    result["truncated"].append(section)
                for row in rows[:_MAX_ROWS]:
                    if section == "memory" and _SENSITIVE_KEY.search(str(row["key"])):
                        continue
                    item = dict(row)
                    for key in item:
                        if isinstance(item[key], str):
                            item[key] = safe_text(item[key])
                    result[section].append(item)
            result["status"] = "ready"
    except (sqlite3.DatabaseError, OSError, ValueError):
        result["status"] = "unavailable"
    return result
