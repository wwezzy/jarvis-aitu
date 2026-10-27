"""Opt-in local category hints, never evidence of studying or goal completion.

Only aggregate category seconds in UTC minute buckets are persisted. Executable
basenames are used transiently and discarded; titles, URLs, screenshots,
keystrokes, command lines and full process lists are never read.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

CATEGORIES = ("work", "gaming", "media", "idle", "unknown")
DEFAULT_RULES = {
    "code.exe": "work", "pycharm64.exe": "work", "idea64.exe": "work",
    "winword.exe": "work", "excel.exe": "work", "obsidian.exe": "work", "notion.exe": "work",
    "cs2.exe": "gaming", "dota2.exe": "gaming", "valorant-win64-shipping.exe": "gaming",
    "league of legends.exe": "gaming", "fortniteclient-win64-shipping.exe": "gaming",
    "gta5.exe": "gaming", "eldenring.exe": "gaming", "hl2.exe": "gaming",
    "vlc.exe": "media", "mpv.exe": "media", "wmplayer.exe": "media",
}
SAMPLE_SECONDS = 5
MAX_GAP_SECONDS = 10
RETENTION_SECONDS = 7 * 86400
LOCAL_ZONE = timezone(timedelta(hours=5))
LIMITATIONS = ("Категории — приблизительные подсказки. Рабочая программа не доказывает учёбу, "
               "браузер остаётся неизвестным. Чтение, видео и геймпад могут выглядеть как бездействие. "
               "По компьютеру нельзя подтвердить тренировку; статусы меняет только ваш отчёт.")


def classify_process(basename, idle_seconds=0, *, available=True):
    if type(idle_seconds) not in {int, float} or not math.isfinite(idle_seconds) or idle_seconds < 0:
        return "unknown"
    if idle_seconds >= 300:
        return "idle"
    if not available or not isinstance(basename, str):
        return "unknown"
    return DEFAULT_RULES.get(basename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].casefold(), "unknown")


def foreground_category():
    """Classify one foreground process; no native access happens on import."""
    if os.name != "nt":
        return "unknown"
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInput)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    kernel32.GetTickCount.restype = wintypes.DWORD
    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return "unknown"
    idle = ((kernel32.GetTickCount() - info.dwTime) & 0xffffffff) / 1000
    if idle >= 300:
        return "idle"
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    window = user32.GetForegroundWindow()
    process_id = wintypes.DWORD()
    if not window or not user32.GetWindowThreadProcessId(window, ctypes.byref(process_id)):
        return "unknown"
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not handle:
        return "unknown"
    try:
        path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return "unknown"
        # The full path/basename does not escape this call or enter storage.
        return classify_process(path.value, idle)
    finally:
        kernel32.CloseHandle(handle)


class ObservationMonitor:
    def __init__(self, store, *, sampler=None, wall_clock=None, monotonic_clock=None):
        self.store = store
        self.sampler = sampler if sampler is not None else foreground_category
        self.wall_clock = wall_clock if wall_clock is not None else time.time
        self.monotonic_clock = monotonic_clock if monotonic_clock is not None else time.monotonic
        self._previous = None
        self._last_prune = None
        self._lock = threading.RLock()

    @staticmethod
    def _stamp(value):
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("A finite timestamp is required")
        return float(value)

    def reset(self):
        with self._lock:
            self._previous = None

    def _prune(self, now):
        # A retention check is cheap and happens even while collection is off.
        if self._last_prune is not None and 0 <= now - self._last_prune < 3600:
            return
        with self.store.connection() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM observation_buckets WHERE minute_utc < ?", (now - RETENTION_SECONDS,))
        self._last_prune = now

    def clear(self):
        with self._lock:
            with self.store.connection() as db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("DELETE FROM observation_buckets")
            self._previous = None

    def tick(self, *, enabled, now=None, monotonic=None):
        """Account only bounded adjacent samples; gaps are never invented time."""
        now = self._stamp(self.wall_clock() if now is None else now)
        mono = self._stamp(self.monotonic_clock() if monotonic is None else monotonic)
        with self._lock:
            self._prune(now)
            if enabled is not True:
                self._previous = None
                return False
            previous = self._previous
            if previous is not None and 0 <= mono - previous[1] < SAMPLE_SECONDS:
                return False
            try:
                category = self.sampler()
            except Exception:
                self._previous = None
                return False
            if category not in CATEGORIES:
                self._previous = None
                return False
            self._previous = (now, mono, category)
            if previous is None:
                return False
            wall_delta, elapsed = now - previous[0], mono - previous[1]
            if not (0 < wall_delta <= MAX_GAP_SECONDS and 0 < elapsed <= MAX_GAP_SECONDS
                    and abs(wall_delta - elapsed) <= 1):
                return False
            # Hold the previous sampled category for at most ten seconds.
            # Split boundaries proportionally; per-minute caps prevent clock
            # changes/repeated polls from accumulating impossible durations.
            cursor, written = previous[0], 0.0
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                while cursor < now:
                    minute = math.floor(cursor / 60) * 60
                    until = min(now, minute + 60)
                    amount = (until - cursor) * elapsed / wall_delta
                    used = db.execute("SELECT coalesce(sum(seconds),0) FROM observation_buckets WHERE minute_utc=?", (minute,)).fetchone()[0]
                    amount = min(amount, max(0.0, 60.0 - used))
                    if amount > 0:
                        db.execute("""INSERT INTO observation_buckets(minute_utc,category,seconds) VALUES(?,?,?)
                            ON CONFLICT(minute_utc,category) DO UPDATE SET seconds=min(60,seconds+excluded.seconds)""",
                                   (minute, previous[2], amount))
                        written += amount
                    cursor = until
            return written > 0

    def summary(self, *, enabled=False, now=None):
        """Read aggregates only. This method never queries native processes."""
        now = self._stamp(self.wall_clock() if now is None else now)
        local = datetime.fromtimestamp(now, LOCAL_ZONE)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        end = start + 86400
        with self._lock:
            self._prune(now)
            with self.store.connection() as db:
                rows = db.execute("""SELECT category,sum(seconds) AS seconds FROM observation_buckets
                    WHERE minute_utc>=? AND minute_utc<? GROUP BY category""", (start, end)).fetchall()
                has_records = bool(db.execute("SELECT 1 FROM observation_buckets LIMIT 1").fetchone())
        seconds = dict.fromkeys(CATEGORIES, 0.0)
        for row in rows:
            if row["category"] in seconds:
                seconds[row["category"]] = round(float(row["seconds"]), 3)
        return {"enabled": bool(enabled), "date": local.date().isoformat(), "timezone": "UTC+5",
                "seconds": seconds, "observed_seconds": round(sum(seconds.values()), 3),
                "has_records": has_records,
                "sample_seconds": SAMPLE_SECONDS, "retention_days": 7, "limitations": LIMITATIONS}
