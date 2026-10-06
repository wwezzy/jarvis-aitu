"""Opt-in foreground/idle category accounting, with a durable bounded offline spool.

No window titles, keyboard data, screenshots, URLs or command lines are collected.
"""
import ctypes
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from services.pc_agent import read_status, signed_status

CATEGORIES = {"work", "gaming", "media", "other", "idle", "unknown"}
DEFAULT_RULES = {
    "code.exe": "work", "pycharm64.exe": "work", "idea64.exe": "work", "winword.exe": "work",
    "excel.exe": "work", "obsidian.exe": "work", "notion.exe": "work",
    "cs2.exe": "gaming", "dota2.exe": "gaming", "valorant-win64-shipping.exe": "gaming",
    "league of legends.exe": "gaming", "fortniteclient-win64-shipping.exe": "gaming",
    "gta5.exe": "gaming", "eldenring.exe": "gaming", "hl2.exe": "gaming",
    "vlc.exe": "media", "mpv.exe": "media", "wmplayer.exe": "media",
    # A browser may mean work, anime or anything else. It must remain unknown.
}


def load_rules():
    extra = json.loads(os.getenv("PC_ACTIVITY_RULES_JSON", "{}"))
    if not isinstance(extra, dict) or len(extra) > 200 or any(
        not isinstance(k, str) or not re.fullmatch(r"[\w .-]{1,100}\.exe", k.lower())
        or value not in CATEGORIES - {"idle"} for k, value in extra.items()):
        raise ValueError("PC_ACTIVITY_RULES_JSON: executable basename -> category")
    return {**DEFAULT_RULES, **{key.lower(): value for key, value in extra.items()}}


def foreground_sample():
    """Read session-specific Win32 state. Process names are immediately classified."""
    if os.name != "nt":
        return {"process": "", "idle_seconds": 0, "available": False}
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetTickCount.restype = wintypes.DWORD
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInput)]
    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return {"process": "", "idle_seconds": 0, "available": False}
    idle = ((kernel32.GetTickCount() - info.dwTime) & 0xffffffff) / 1000
    hwnd = user32.GetForegroundWindow()
    process_id = wintypes.DWORD()
    if not hwnd or not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
        return {"process": "", "idle_seconds": idle, "available": False}
    handle = kernel32.OpenProcess(0x1000, False, process_id.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return {"process": "", "idle_seconds": idle, "available": False}
    try:
        path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return {"process": "", "idle_seconds": idle, "available": False}
        return {"process": path.value.rsplit("\\", 1)[-1].lower(), "idle_seconds": idle, "available": True}
    finally:
        kernel32.CloseHandle(handle)


class ActivityJournal:
    def __init__(self, path, rules=None, sample=foreground_sample):
        self.path, self.rules, self.sample = str(path), rules or load_rules(), sample
        self.previous = None
        self.high_water = None
        self.current = "unknown"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, starts INTEGER UNIQUE, totals TEXT NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0)")

    def tick(self, now=None):
        now = int(time.time() if now is None else now)
        if self.high_water is not None and now <= self.high_water:
            return
        self.high_water = now
        observation = self.sample()
        category = ("idle" if observation.get("idle_seconds", 0) >= 300 else
                    self.rules.get(observation.get("process", "").lower(), "unknown") if observation.get("available") else "unknown")
        if self.previous is not None:
            # Never bridge sleep, transport backoff, process restarts, or clock reversal.
            elapsed = now - self.previous[0]
            if 0 < elapsed <= 10:
                with closing(sqlite3.connect(self.path)) as db, db:
                    for second in range(self.previous[0], now):
                        start = second // 60 * 60
                        row = db.execute("SELECT id, totals FROM activity WHERE starts=?", (start,)).fetchone()
                        identity, totals = (row[0], json.loads(row[1])) if row else (uuid.uuid4().hex, {})
                        totals[self.previous[1]] = totals.get(self.previous[1], 0) + 1
                        db.execute("INSERT INTO activity VALUES (?, ?, ?, 0) ON CONFLICT(starts) DO UPDATE SET totals=excluded.totals, uploaded=0",
                                   (identity, start, json.dumps(totals)))
                    db.execute("DELETE FROM activity WHERE starts < ?", (now - 7 * 86400,))
        self.previous, self.current = (now, category), category

    def pending(self, now=None):
        now = int(time.time() if now is None else now)
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = db.execute("SELECT id, starts, totals FROM activity WHERE uploaded=0 AND starts < ? ORDER BY starts LIMIT 64", (now // 60 * 60,)).fetchall()
        return [{"id": r[0], "starts": r[1], "seconds": json.loads(r[2])} for r in rows]

    def acknowledge(self, identities):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("UPDATE activity SET uploaded=1 WHERE id=?", [(value,) for value in identities])

    def clear(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM activity")
        self.previous = None
        self.high_water = None

    def sync(self, redis, user_id, secret):
        raw = redis.get(f"jarvis:activity_ack:{user_id}")
        data = read_status(raw, user_id, secret) if raw else None
        if data and isinstance(data.get("ids"), list):
            self.acknowledge([value for value in data["ids"][:64] if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value)])
        redis.set(f"jarvis:activity:{user_id}", signed_status({"user_id": user_id,
            "seen_at": int(time.time()), "buckets": self.pending()}, secret), ex=600)


async def ingest_activity(redis, user_id, secret, now=None):
    # The local Windows agent imports ActivityJournal without bot credentials/DB.
    from sqlalchemy import delete, select
    from sqlalchemy.exc import IntegrityError
    from database import engine
    from database.assistant_models import ActivityReceipt
    from database.assistant_models import AssistantState
    from database.time import aware
    from services.preferences import get_preferences
    prefs = await get_preferences(user_id)
    if not prefs.activity_enabled or redis is None or not secret:
        return 0
    now = aware(now or datetime.now().astimezone())
    raw = await redis.get(f"jarvis:activity:{user_id}")
    data = read_status(raw, user_id, secret) if raw and len(raw) <= 50000 else None
    if not data or data["seen_at"] < now.timestamp() - 600:
        return 0
    buckets = data.get("buckets")
    if not isinstance(buckets, list) or len(buckets) > 64:
        return 0
    valid = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            return 0
        key, start, seconds = bucket.get("id"), bucket.get("starts"), bucket.get("seconds")
        if (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key) or type(start) is not int
                or not now.timestamp() - 7 * 86400 <= start < now.timestamp()
                or start % 60 or not isinstance(seconds, dict) or not seconds
                or set(seconds) - CATEGORIES or any(type(s) is not int or not 0 <= s <= 60 for s in seconds.values())
                or sum(seconds.values()) > 60):
            return 0
        valid.append((key, start, seconds))
    async with engine.async_session_factory() as db:
        state = await db.get(AssistantState, user_id)
        cutoff = state.activity_reset_at.timestamp() if state and state.activity_reset_at else 0
        discarded = {key for key, start, _ in valid if start < cutoff}
        for key, start, seconds in valid:
            if key in discarded:
                continue
            if await db.scalar(select(ActivityReceipt.id).where(ActivityReceipt.user_id == user_id, ActivityReceipt.bucket_id == key)) is None:
                db.add(ActivityReceipt(user_id=user_id, bucket_id=key,
                    starts_at=datetime.fromtimestamp(start, timezone.utc), seconds=seconds))
        await db.execute(delete(ActivityReceipt).where(ActivityReceipt.user_id == user_id, ActivityReceipt.starts_at < now - timedelta(days=7)))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            # A concurrent importer may have committed. Only acknowledge committed IDs.
        committed = set(await db.scalars(select(ActivityReceipt.bucket_id).where(ActivityReceipt.user_id == user_id,
            ActivityReceipt.bucket_id.in_([v[0] for v in valid]))))
    await redis.set(f"jarvis:activity_ack:{user_id}", signed_status({"user_id": user_id, "seen_at": int(now.timestamp()),
        "ids": sorted(committed | discarded)}, secret), ex=600)
    return len(committed)


