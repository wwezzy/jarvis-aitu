"""Small local store independent of the Telegram/Render database.

Each operation opens its own connection, so the UI and command worker never
share sqlite handles. Personal content stays on disk and is never evaluated.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PREFERENCES = {
    "speech_enabled": True,
    "voice_enabled": False,
    "close_to_tray": True,
    "launch_at_login": False,
    "voice_rate": 185,
    "voice_engine": "auto",
    "language": "ru",
    "hide_advanced": True,
    "local_chat_enabled": False,
    "local_chat_history": False,
    "local_chat_model": "qwen3:4b-instruct-2507-q4_K_M",
}


class LocalStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "desktop.db"
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Local database is newer than this application")
            if version:
                self._daily_backup(db, version)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
                    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
                    created_at REAL NOT NULL, completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS confirmations (
                    token TEXT PRIMARY KEY, action TEXT NOT NULL, issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('reminder','timer','focus')),
                    due_at REAL NOT NULL, created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','delivered','cancelled','expired')),
                    delivered_at REAL, cancelled_at REAL
                );
                CREATE INDEX IF NOT EXISTS reminders_status_due ON reminders(status,due_at);
                PRAGMA user_version=2;
                COMMIT;
            """)

    def _daily_backup(self, source, version):
        """SQLite online backup includes committed WAL data, before upgrades.

        One snapshot per UTC date/schema is retained; no private backup is
        removed automatically. A failed backup prevents a schema migration.
        """
        directory = self.state_dir / "backups"
        directory.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        destination = directory / f"desktop-{stamp}-schema{version}.db"
        if destination.exists():
            with closing(sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)) as existing:
                if (existing.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                        or existing.execute("PRAGMA user_version").fetchone()[0] != version):
                    raise RuntimeError("Existing daily backup integrity check failed")
            return
        temporary = directory / f"desktop-{stamp}-{uuid.uuid4().hex}.partial"
        try:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Local backup integrity check failed")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.create_function("casefold", 1, lambda value: value.casefold(), deterministic=True)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _content(value: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
            raise ValueError("Empty or oversized content")
        return value.strip()

    def add_task(self, title: str) -> int:
        title = self._content(title, 1000)
        with self.connection() as db:
            return db.execute("INSERT INTO tasks(title,created_at) VALUES(?,?)", (title, time.time())).lastrowid

    def complete_task(self, task_id: int) -> str:
        with self.connection() as db:
            row = db.execute("SELECT done FROM tasks WHERE id=? AND archived=0", (task_id,)).fetchone()
            if row is None:
                return "missing"
            if row["done"]:
                return "already_done"
            db.execute("UPDATE tasks SET done=1,completed_at=? WHERE id=?", (time.time(), task_id))
            return "completed"

    def archive_task(self, task_id: int) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET archived=1 WHERE id=? AND archived=0", (task_id,)).rowcount)

    def capture_note(self, text: str) -> int:
        text = self._content(text, 20000)
        with self.connection() as db:
            return db.execute("INSERT INTO notes(text,created_at) VALUES(?,?)", (text, time.time())).lastrowid

    def search_notes(self, query: str, limit: int = 50) -> list[dict]:
        query = self._content(query, 500)
        # instr is a literal Unicode search: %, _, quotes and SQL are data.
        with self.connection() as db:
            rows = db.execute("SELECT id,text,created_at FROM notes WHERE instr(casefold(text),casefold(?))>0 ORDER BY id DESC LIMIT ?",
                              (query, max(1, min(limit, 200)))).fetchall()
        return [dict(row) for row in rows]

    def preferences(self) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT value FROM preferences WHERE id=1").fetchone()
        if row is None:
            return dict(DEFAULT_PREFERENCES)
        return {**DEFAULT_PREFERENCES, **json.loads(row["value"])}

    def set_preferences(self, updates: dict) -> dict:
        if not isinstance(updates, dict) or set(updates) - DEFAULT_PREFERENCES.keys():
            raise ValueError("Unknown setting")
        for key, value in updates.items():
            if isinstance(DEFAULT_PREFERENCES[key], bool) and type(value) is not bool:
                raise ValueError("Boolean setting required")
        if "voice_rate" in updates and (type(updates["voice_rate"]) is not int or not 90 <= updates["voice_rate"] <= 240):
            raise ValueError("Voice rate must be 90..240")
        if "voice_engine" in updates and (not isinstance(updates["voice_engine"], str)
                                          or updates["voice_engine"] not in {"auto", "windows"}):
            raise ValueError("Unsupported speech engine")
        if "language" in updates and updates["language"] not in {"ru", "en"}:
            raise ValueError("Unsupported language")
        if "local_chat_model" in updates:
            model = updates["local_chat_model"]
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", model) or "cloud" in model.casefold() or "://" in model:
                raise ValueError("A local Ollama model name is required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM preferences WHERE id=1").fetchone()
            value = {**DEFAULT_PREFERENCES, **(json.loads(row["value"]) if row else {}), **updates}
            db.execute("INSERT INTO preferences(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                       (json.dumps(value, ensure_ascii=False),))
        return value

    def record(self, role: str, text: str) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("Invalid history role")
        text = self._content(text, 24000)
        with self.connection() as db:
            record_id = db.execute("INSERT INTO history(role,text,timestamp) VALUES(?,?,?)", (role, text, time.time())).lastrowid
            db.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 250)")
        return record_id

    def model_chat_history(self, *, before_id: int) -> list[dict]:
        """Only completed local-model exchanges before the current input.

        Deterministic command results, captured notes, reminders and personal
        projections lack this fixed assistant prefix and are never included.
        The prefix is provenance of the existing app response, not a tool grant.
        """
        from desktop.chat import bounded_history
        with self.connection() as db:
            rows = db.execute("SELECT role,text FROM history WHERE id<? ORDER BY id DESC LIMIT 250", (before_id,)).fetchall()
        prefix = "Локальная модель: "
        question = None
        pairs = []
        for row in reversed(rows):
            if row["role"] == "user":
                question = row["text"]
            else:
                answer = row["text"]
                if question and answer.startswith(prefix) and answer[len(prefix):].strip():
                    pairs.extend([{"role": "user", "content": question},
                                  {"role": "assistant", "content": answer[len(prefix):]}])
                question = None
        return bounded_history(pairs[-6:])

    def create_confirmation(self, token: str, action: str, now: float, ttl: int = 60) -> None:
        with self.connection() as db:
            db.execute("UPDATE confirmations SET consumed=1 WHERE consumed=0")
            db.execute("DELETE FROM confirmations WHERE expires_at<?", (now - 86400,))
            db.execute("INSERT INTO confirmations(token,action,issued_at,expires_at) VALUES(?,?,?,?)",
                       (token, action, now, now + ttl))

    def claim_confirmation(self, token: str, now: float) -> str | None:
        with self.connection() as db:
            row = db.execute("""UPDATE confirmations SET consumed=1
                WHERE token=? AND consumed=0 AND issued_at<=? AND expires_at>=? AND issued_at>=?
                RETURNING action""", (token, now + 5, now, now - 60)).fetchone()
        return row["action"] if row else None

    def cancel_confirmation(self, token: str) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE confirmations SET consumed=1 WHERE token=? AND consumed=0", (token,)).rowcount)

    @staticmethod
    def _moment(value):
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("A finite UTC timestamp is required")
        return float(value)

    def create_reminder(self, text: str, due_at: float, kind="reminder", *, now=None) -> int:
        text = self._content(text, 1000)
        now = self._moment(time.time() if now is None else now)
        due_at = self._moment(due_at)
        if kind not in {"reminder", "timer", "focus"} or not 1 <= due_at - now <= 7 * 86400:
            raise ValueError("Reminder duration must be between one second and seven days")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE reminders SET status='expired' WHERE status='pending' AND due_at<?", (now - 86400,))
            if db.execute("SELECT count(*) FROM reminders WHERE status='pending'").fetchone()[0] >= 20:
                raise ValueError("Only twenty active reminders are allowed")
            return db.execute("INSERT INTO reminders(text,kind,due_at,created_at) VALUES(?,?,?,?)",
                              (text, kind, due_at, now)).lastrowid

    def cancel_reminder(self, reminder_id: int, *, now=None) -> bool:
        now = self._moment(time.time() if now is None else now)
        with self.connection() as db:
            return bool(db.execute("""UPDATE reminders SET status='cancelled',cancelled_at=?
                WHERE id=? AND status='pending'""", (now, reminder_id)).rowcount)

    def claim_due_reminders(self, *, now=None) -> list[dict]:
        """At most once. A crash after this claim may miss display; no replay."""
        now = self._moment(time.time() if now is None else now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE reminders SET status='expired' WHERE status='pending' AND due_at<?", (now - 86400,))
            rows = db.execute("""UPDATE reminders SET status='delivered',delivered_at=?
                WHERE status='pending' AND due_at<=? AND due_at>=?
                RETURNING id,text,kind,due_at""", (now, now, now - 86400)).fetchall()
        return sorted((dict(row) for row in rows), key=lambda row: (row["due_at"], row["id"]))

    def snapshot(self) -> dict:
        with self.connection() as db:
            tasks = [dict(row) for row in db.execute("SELECT id,title,done,created_at,completed_at FROM tasks WHERE archived=0 ORDER BY done,id LIMIT 1000")]
            notes = [dict(row) for row in db.execute("SELECT id,text,created_at FROM notes ORDER BY id DESC LIMIT 200")]
            recent = [dict(row) for row in db.execute("SELECT role,text,timestamp FROM history ORDER BY id DESC LIMIT 40")]
            reminders = [dict(row) for row in db.execute("SELECT id,text,kind,due_at FROM reminders WHERE status='pending' ORDER BY due_at,id LIMIT 20")]
        for task in tasks:
            task["done"] = bool(task["done"])
        return {"tasks": tasks, "notes": notes, "recent": list(reversed(recent)),
                "reminders": reminders, "preferences": self.preferences()}

    def close(self):
        # Connections are scoped to operations; checkpoint is safe at shutdown.
        with self.connection() as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
