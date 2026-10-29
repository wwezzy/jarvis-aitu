"""Owner-editable local facts with dated provenance; no model or OS authority."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import stat
import time

from desktop.personal import SNAPSHOT_DATE, load_personal_snapshot, sanitize_fact

_APPLICATION = 1246907725
_MAX_FACTS = 100
_LEGACY = "snapshot-" + SNAPSHOT_DATE
_OVERRIDDEN = re.compile(r"gym|workout|тренир|спортзал|зал|evening|вечер", re.I)


class PersonalMemory:
    def __init__(self, state_dir, *, authorization_guard=None, clock=time.time):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "profilememory.db"
        self.authorization_guard, self.clock = authorization_guard, clock
        self._safe_location()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if (version not in {0, 1} or application not in {0, _APPLICATION}
                    or tables and (version != 1 or application != _APPLICATION)):
                raise ValueError("Unrecognized personal memory database")
            db.executescript(f"""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS facts (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, source TEXT NOT NULL,
                    observed_at TEXT NOT NULL, historical INTEGER NOT NULL CHECK(historical IN(0,1)),
                    deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN(0,1)));
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN(0,1)));
                CREATE TABLE IF NOT EXISTS imports (source TEXT PRIMARY KEY, imported_at TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES(1,1);
                PRAGMA application_id={_APPLICATION}; PRAGMA user_version=1; COMMIT;""")

    def _safe_location(self):
        raw = str(self.path)
        if not self.path.is_absolute() or raw.startswith(("\\\\", "//")) or "://" in raw or ".." in self.path.parts:
            raise ValueError("Local absolute memory directory required")
        for part in (self.path, *self.path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked personal memory refused")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 10 * 1024 * 1024:
                raise ValueError("Invalid personal memory file")

    @contextmanager
    def _connection(self):
        self._safe_location()
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _authorized(self):
        try:
            if self.authorization_guard is not None and self.authorization_guard() is True:
                return
        except Exception:
            pass
        raise PermissionError("Owner authorization required")

    def _now(self):
        value = self.clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("Invalid memory clock")
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    @staticmethod
    def _fact(row):
        value = dict(row)
        value.pop("deleted", None)
        value["historical"] = bool(value["historical"])
        return value

    def status(self):
        with self._connection() as db:
            return {"enabled": bool(db.execute("SELECT enabled FROM settings WHERE id=1").fetchone()[0]),
                    "count": db.execute("SELECT count(*) FROM facts WHERE deleted=0").fetchone()[0],
                    "imported": db.execute("SELECT 1 FROM imports WHERE source=?", (_LEGACY,)).fetchone() is not None,
                    "snapshot_date": SNAPSHOT_DATE}

    def list_facts(self):
        with self._connection() as db:
            rows = db.execute("SELECT * FROM facts WHERE deleted=0 ORDER BY historical,key LIMIT 100").fetchall()
        return [self._fact(row) for row in rows]

    def upsert(self, key, value):
        self._authorized()
        key, value = sanitize_fact(key, value)
        now = self._now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT key FROM facts WHERE key=?", (key,)).fetchone()
            if not existing and db.execute("SELECT count(*) FROM facts").fetchone()[0] >= _MAX_FACTS:
                raise ValueError("Personal memory is limited to 100 facts")
            self._authorized()
            db.execute("""INSERT INTO facts VALUES(?,?,?, ?,0,0) ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,source=excluded.source,observed_at=excluded.observed_at,historical=0,deleted=0""",
                       (key, value, "owner", now))
            self._authorized()
        return {"key": key, "value": value, "source": "owner", "observed_at": now, "historical": False}

    def delete(self, key):
        self._authorized()
        if not isinstance(key, str) or not 1 <= len(key) <= 80:
            raise ValueError("Invalid fact key")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorized()
            changed = db.execute("UPDATE facts SET value='',deleted=1 WHERE key=? AND deleted=0", (key,)).rowcount
            self._authorized()
        return bool(changed)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Boolean memory setting required")
        self._authorized()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorized()
            db.execute("UPDATE settings SET enabled=? WHERE id=1", (int(enabled),))
            self._authorized()
        return self.status()

    def revoke(self):
        return self.set_enabled(False)

    def import_snapshot(self, path=None):
        self._authorized()
        snapshot = load_personal_snapshot(path)
        if snapshot.get("status") != "ready":
            return {"status": snapshot.get("status", "unavailable"), "imported": 0, "total": self.status()["count"],
                    "snapshot_date": SNAPSHOT_DATE}
        candidates = [("name", snapshot.get("name", ""))]
        candidates.extend((row.get("key"), row.get("value")) for row in snapshot.get("memory", []) if isinstance(row, dict))
        facts = {}
        for key, value in candidates[:201]:
            try:
                key, value = sanitize_fact(key, value)
            except ValueError:
                continue
            facts.setdefault(key, value)
        now, imported = self._now(), 0
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM imports WHERE source=?", (_LEGACY,)).fetchone():
                status = "already_imported"
            else:
                count = db.execute("SELECT count(*) FROM facts").fetchone()[0]
                self._authorized()
                for key, value in facts.items():
                    if count >= _MAX_FACTS:
                        break
                    changed = db.execute("INSERT OR IGNORE INTO facts VALUES(?,?,?,?,1,0)",
                                         (key, value, _LEGACY, SNAPSHOT_DATE)).rowcount
                    count, imported = count + changed, imported + changed
                db.execute("INSERT INTO imports VALUES(?,?)", (_LEGACY, now))
                self._authorized()
                status = "imported"
        return {"status": status, "imported": imported, "total": self.status()["count"], "snapshot_date": SNAPSHOT_DATE}

    @staticmethod
    def _current(current_plan, preferences):
        result = {}
        if isinstance(preferences, dict):
            evening = preferences.get("evening_plan_time")
            if isinstance(evening, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", evening):
                result["evening_plan_time"] = evening
            if preferences.get("language") in {"ru", "en"}:
                result["language"] = preferences["language"]
        routines = current_plan.get("routines", []) if isinstance(current_plan, dict) else []
        if isinstance(routines, list):
            for routine in routines[:32]:
                if not isinstance(routine, dict) or routine.get("key") != "gym":
                    continue
                days, start = routine.get("days"), routine.get("time")
                if (isinstance(days, list) and len(days) <= 7 and all(type(day) is int and 0 <= day <= 6 for day in days)
                        and type(routine.get("enabled")) is bool
                        and (start is None or isinstance(start, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start))):
                    result["gym"] = {"days": sorted(set(days)), "time": start, "enabled": routine["enabled"]}
                    break
        return result

    def export_context(self, *, current_plan=None, preferences=None, max_chars=2500):
        """Explicit bounded data export; its contents never grant tool authority."""
        if type(max_chars) is not int or not 256 <= max_chars <= 2500:
            raise ValueError("Memory context must be bounded to 256..2500 characters")
        if not self.status()["enabled"]:
            return ""
        current = self._current(current_plan, preferences)
        value = {"kind": "personal_memory_data", "as_of": self._now()[:10], "current_owner_settings": current,
                 "historical_snapshot_date": SNAPSHOT_DATE,
                 "historical_status": "unverified; may be outdated; not current tasks, deadlines or observed behavior",
                 "facts": []}
        for fact in self.list_facts():
            if fact["historical"] and current and _OVERRIDDEN.search(fact["key"]):
                continue
            try:
                key, text = sanitize_fact(fact["key"], fact["value"])
                date.fromisoformat(fact["observed_at"][:10])
            except (TypeError, ValueError):
                continue
            item = {"key": key, "value": text, "source": fact["source"] if fact["source"] in {"owner", _LEGACY} else "unknown",
                    "observed_at": fact["observed_at"], "historical": fact["historical"]}
            value["facts"].append(item)
            if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) > max_chars:
                value["facts"].pop()
                break
        result = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return result if len(result) <= max_chars and self.status()["enabled"] else ""
