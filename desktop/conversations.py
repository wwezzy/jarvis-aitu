"""Private, separate conversation histories; imported legacy rows grant no context."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid


MAX_MESSAGES = 300
MAX_TEXT = 24000
MAX_DATABASE_BYTES = 128 * 1024 * 1024  # Three bounded threads may contain four-byte UTF-8 characters.
_APPLICATION = 0x4A435653
_SCHEMA = {
    "threads": "CREATE TABLE threads(id TEXT PRIMARY KEY,channel TEXT UNIQUE NOT NULL,readonly INTEGER NOT NULL)",
    "messages": "CREATE TABLE messages(id INTEGER PRIMARY KEY,thread_id TEXT NOT NULL REFERENCES threads(id),role TEXT NOT NULL,text TEXT NOT NULL,provenance TEXT NOT NULL,exchange_id TEXT,attachments TEXT NOT NULL,created_at REAL NOT NULL,context_epoch INTEGER NOT NULL DEFAULT 0)",
    "metadata": "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
}


class ConversationStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "conversations.db"

    def _safe_location(self):
        raw = str(self.path)
        if not self.path.is_absolute() or raw.startswith(("\\\\", "//")) or "://" in raw or ".." in self.path.parts:
            raise ValueError("Local absolute conversation directory required")
        for entry in (self.path, *self.path.parents):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked conversation database refused")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_DATABASE_BYTES:
                raise ValueError("Invalid conversation database file")

    @staticmethod
    def _canonical(sql):
        return re.sub(r"\s+", "", sql or "").casefold().replace("ifnotexists", "")

    @contextmanager
    def _connection(self):
        self._safe_location()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._safe_location()
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            self._safe_location()
            db.execute("PRAGMA trusted_schema=OFF")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            schema = {row["name"]: row["sql"] for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            unsafe = db.execute("SELECT 1 FROM sqlite_master WHERE type IN('trigger','view') LIMIT 1").fetchone()
            if (version not in {0, 1} or application not in {0, _APPLICATION}
                    or schema and (version != 1 or application != _APPLICATION or set(schema) != set(_SCHEMA))
                    or unsafe):
                raise ValueError("Unsupported conversation database")
            if schema and any(self._canonical(schema[name]) != self._canonical(sql) for name, sql in _SCHEMA.items()):
                raise ValueError("Unsupported conversation database schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                for sql in _SCHEMA.values():
                    db.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
                db.execute("INSERT OR IGNORE INTO metadata VALUES('context_epoch','0')")
                db.execute(f"PRAGMA application_id={_APPLICATION}")
                db.execute("PRAGMA user_version=1")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Invalid conversation identity")
        return value

    def threads(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for channel in ("text", "voice"):
                db.execute("INSERT OR IGNORE INTO threads VALUES(?,?,0)", (uuid.uuid4().hex, channel))
            return {row["channel"]: row["id"] for row in db.execute("SELECT id,channel FROM threads")}

    def import_legacy(self, rows):
        """One bounded read-only copy; source channel and model provenance unknown."""
        if not isinstance(rows, list):
            raise ValueError("Legacy messages must be a list")
        self.threads()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                return False
            imported = []
            for row in rows[-MAX_MESSAGES:]:
                if (isinstance(row, dict) and row.get("role") in {"user", "assistant"}
                        and isinstance(row.get("text"), str) and row["text"].strip()):
                    imported.append((row["role"], row["text"][:MAX_TEXT].replace("\x00", "")))
            if imported:
                thread = uuid.uuid4().hex
                db.execute("INSERT INTO threads VALUES(?,'imported',1)", (thread,))
                db.executemany("INSERT INTO messages(thread_id,role,text,provenance,attachments,created_at) VALUES(?,?,?,'imported','[]',?)",
                               [(thread, role, text, time.time()) for role, text in imported])
            db.execute("INSERT INTO metadata VALUES('legacy_imported','1')")
            return bool(imported)

    def append(self, thread_id, role, text, *, provenance="command", exchange_id=None, attachments=None):
        thread_id = self._id(thread_id)
        if (role not in {"user", "assistant"} or provenance not in {"command", "model", "analysis", "notification"}
                or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text):
            raise ValueError("Invalid conversation message")
        if exchange_id is not None:
            self._id(exchange_id)
        ids = attachments or []
        if not isinstance(ids, list) or len(ids) > 4:
            raise ValueError("Invalid attachments")
        for identity in ids:
            self._id(identity)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            thread = db.execute("SELECT readonly FROM threads WHERE id=?", (thread_id,)).fetchone()
            if thread is None or thread[0]:
                raise ValueError("Conversation unavailable or read-only")
            epoch = int(db.execute("SELECT value FROM metadata WHERE key='context_epoch'").fetchone()[0])
            identity = db.execute("INSERT INTO messages(thread_id,role,text,provenance,exchange_id,attachments,created_at,context_epoch) VALUES(?,?,?,?,?,?,?,?)",
                                  (thread_id, role, text, provenance, exchange_id, json.dumps(ids), time.time(), epoch)).lastrowid
            db.execute("DELETE FROM messages WHERE thread_id=? AND id NOT IN (SELECT id FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?)",
                       (thread_id, thread_id, MAX_MESSAGES))
            return identity

    def messages(self, thread_id, limit=200):
        thread_id = self._id(thread_id)
        if type(limit) is not int:
            raise ValueError("Invalid message limit")
        with self._connection() as db:
            rows = db.execute("SELECT id,role,text,provenance,exchange_id,attachments,created_at,context_epoch FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                              (thread_id, max(1, min(limit, MAX_MESSAGES)))).fetchall()
        return [dict(row) | {"attachments": json.loads(row["attachments"])} for row in reversed(rows)]

    def reset_model_context(self):
        """Revoke automatic replay while preserving both visible transcripts."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = int(db.execute("SELECT value FROM metadata WHERE key='context_epoch'").fetchone()[0]) + 1
            db.execute("UPDATE metadata SET value=? WHERE key='context_epoch'", (str(epoch),))
            return epoch

    def model_history(self, thread_id, max_messages=6):
        """Only completed model pairs from this exact channel, bounded to 4000 chars."""
        if type(max_messages) is not int:
            raise ValueError("Invalid history limit")
        if max_messages <= 0:
            return []
        with self._connection() as db:
            epoch = int(db.execute("SELECT value FROM metadata WHERE key='context_epoch'").fetchone()[0])
        pending, pairs = {}, []
        for row in self.messages(thread_id, MAX_MESSAGES):
            exchange = row["exchange_id"]
            if row["provenance"] != "model" or exchange is None or row["context_epoch"] != epoch:
                continue
            if row["role"] == "user":
                pending[exchange] = row["text"]
            elif exchange in pending:
                answer = row["text"]
                for prefix in ("Локальная модель: ", "OpenAI: ", "Gemini: "):
                    if answer.startswith(prefix):
                        answer = answer[len(prefix):]
                        break
                pairs.append((pending.pop(exchange), answer))
        result, remaining = [], 4000
        for question, answer in reversed(pairs[-max(0, min(max_messages, 6)) // 2:]):
            if remaining < 2:
                break
            question, answer = question[:remaining // 2], answer[:remaining - min(len(question), remaining // 2)]
            result[:0] = [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
            remaining -= len(question) + len(answer)
        return result
