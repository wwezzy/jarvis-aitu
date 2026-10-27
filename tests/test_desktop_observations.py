"""Synthetic category accounting only; native process APIs are always fake."""
import ctypes
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from desktop.core import DesktopCore
from desktop.observations import ObservationMonitor, classify_process, foreground_category
from desktop.store import LocalStore

ZONE = timezone(timedelta(hours=5))
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=ZONE).timestamp()


@pytest.mark.parametrize("name,idle,available,expected", [
    ("Code.exe", 0, True, "work"),
    (r"C:\private\CODE.EXE", 0, True, "work"),
    ("dota2.exe", 2, True, "gaming"),
    ("vlc.exe", 0, True, "media"),
    ("chrome.exe", 0, True, "unknown"),
    ("msedge.exe", 0, True, "unknown"),
    ("firefox.exe", 0, True, "unknown"),
    ("unclassified.exe", 0, True, "unknown"),
    ("code.exe", 300, True, "idle"),
    ("code.exe", 600, False, "idle"),
    ("code.exe", 0, False, "unknown"),
    ("code.exe", math.nan, True, "unknown"),
])
def test_classification_is_narrow_and_browsers_never_prove_study(name, idle, available, expected):
    assert classify_process(name, idle, available=available) == expected


def monitor(tmp_path, categories=None):
    store = LocalStore(tmp_path)
    clock = {"wall": NOW, "mono": 1000.0}
    calls = []
    values = iter(categories or ["work"] * 100)
    def sample():
        calls.append(True)
        return next(values)
    value = ObservationMonitor(store, sampler=sample, wall_clock=lambda: clock["wall"], monotonic_clock=lambda: clock["mono"])
    return value, store, clock, calls


def advance(clock, seconds):
    clock["wall"] += seconds
    clock["mono"] += seconds


def test_disabled_and_read_only_summary_never_sample(tmp_path):
    value, _store, clock, calls = monitor(tmp_path)
    assert not value.tick(enabled=False)
    assert value.summary()["observed_seconds"] == 0
    assert calls == []
    value.tick(enabled=True)
    assert calls == [True]
    advance(clock, 2)
    assert not value.tick(enabled=True) and calls == [True]


def test_adjacent_samples_count_previous_category_and_respect_five_seconds(tmp_path):
    value, _store, clock, calls = monitor(tmp_path, ["work", "gaming", "media"])
    assert not value.tick(enabled=True)
    advance(clock, 5)
    assert value.tick(enabled=True)
    advance(clock, 5)
    assert value.tick(enabled=True)
    data = value.summary(enabled=True)
    assert data["seconds"]["work"] == 5 and data["seconds"]["gaming"] == 5
    assert data["seconds"]["media"] == 0 and data["observed_seconds"] == 10
    assert len(calls) == 3 and data["date"] == "2026-10-29" and data["timezone"] == "UTC+5"


@pytest.mark.parametrize("wall_delta,mono_delta", [(60, 60), (-5, 5), (5, -5), (8, 5)])
def test_sleep_clock_reversal_and_discontinuity_are_not_invented_time(tmp_path, wall_delta, mono_delta):
    value, _store, clock, _calls = monitor(tmp_path)
    value.tick(enabled=True)
    clock["wall"] += wall_delta
    clock["mono"] += mono_delta
    assert not value.tick(enabled=True)
    assert value.summary()["observed_seconds"] == 0
    advance(clock, 5)
    value.tick(enabled=True)
    assert value.summary()["observed_seconds"] == 5


def test_pause_and_restart_never_bridge_unsampled_gaps(tmp_path):
    value, store, clock, _calls = monitor(tmp_path)
    value.tick(enabled=True)
    advance(clock, 5)
    value.tick(enabled=True)
    value.tick(enabled=False)
    advance(clock, 3600)
    value.tick(enabled=True)
    assert value.summary()["observed_seconds"] == 5
    restarted = ObservationMonitor(store, sampler=lambda: "gaming", wall_clock=lambda: clock["wall"], monotonic_clock=lambda: clock["mono"])
    restarted.tick(enabled=True)
    advance(clock, 5)
    restarted.tick(enabled=True)
    assert restarted.summary()["observed_seconds"] == 10


def test_minute_and_local_midnight_boundaries_are_split_without_raw_names(tmp_path):
    value, store, clock, _calls = monitor(tmp_path)
    clock["wall"] = datetime(2026, 10, 29, 23, 59, 58, tzinfo=ZONE).timestamp()
    value.tick(enabled=True)
    advance(clock, 5)
    value.tick(enabled=True)
    assert value.summary()["seconds"]["work"] == 3
    old_day = value.summary(now=clock["wall"] - 4)
    assert old_day["seconds"]["work"] == 2
    with store.connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM observation_buckets ORDER BY minute_utc")]
        columns = [row[1] for row in db.execute("PRAGMA table_info(observation_buckets)")]
    assert columns == ["minute_utc", "category", "seconds"]
    assert [row["seconds"] for row in rows] == [2, 3]
    assert all(row["minute_utc"] % 60 == 0 for row in rows)
    assert "exe" not in str(rows) and "private" not in str(rows)


def test_per_minute_total_is_capped_across_clock_overlap(tmp_path):
    value, store, clock, _calls = monitor(tmp_path)
    minute = int(NOW // 60 * 60)
    with store.connection() as db:
        db.execute("INSERT INTO observation_buckets VALUES(?,?,?)", (minute, "gaming", 58))
    value.tick(enabled=True)
    advance(clock, 5)
    value.tick(enabled=True)
    data = value.summary()
    assert data["observed_seconds"] == 60 and data["seconds"]["work"] == 2


def test_seven_day_retention_and_clear_preserve_goals_and_notes(tmp_path):
    value, store, _clock, _calls = monitor(tmp_path)
    task_id = store.add_task("Keep my task")
    note_id = store.capture_note("Keep my note")
    with store.connection() as db:
        db.execute("INSERT INTO observation_buckets VALUES(?,?,?)", (int((NOW - 8 * 86400) // 60 * 60), "gaming", 50))
        db.execute("INSERT INTO observation_buckets VALUES(?,?,?)", (int((NOW - 60) // 60 * 60), "work", 10))
    assert value.summary()["observed_seconds"] == 10
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM observation_buckets").fetchone()[0] == 1
    value.clear()
    assert value.summary()["observed_seconds"] == 0 and not value.summary()["has_records"]
    assert store.snapshot()["tasks"][0]["id"] == task_id
    assert store.snapshot()["notes"][0]["id"] == note_id


def test_backup_omits_short_lived_observations_but_preserves_personal_content(tmp_path):
    store = LocalStore(tmp_path)
    store.add_task("Preserve this")
    with store.connection() as db:
        db.execute("INSERT INTO observation_buckets VALUES(?,?,?)", (int(NOW // 60 * 60), "gaming", 35))
    LocalStore(tmp_path)
    backup = next((tmp_path / "backups").glob("*-schema3.db"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT count(*) FROM observation_buckets").fetchone()[0] == 0
        assert db.execute("SELECT title FROM tasks").fetchone()[0] == "Preserve this"
        assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0
        # A previously created current-day backup is scrubbed as well.
        db.execute("INSERT INTO observation_buckets VALUES(?,?,?)", (int(NOW // 60 * 60), "work", 20))
    LocalStore(tmp_path)
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT count(*) FROM observation_buckets").fetchone()[0] == 0
    with store.connection() as db:
        assert db.execute("SELECT seconds FROM observation_buckets").fetchone()[0] == 35


def test_sampler_failure_and_invalid_categories_do_not_invent_data(tmp_path):
    value, _store, clock, _calls = monitor(tmp_path)
    value.tick(enabled=True)
    advance(clock, 5)
    value.sampler = lambda: {"process": "private.exe"}
    assert not value.tick(enabled=True)
    assert value.summary()["observed_seconds"] == 0
    def failure():
        raise OSError("private native detail")
    value.sampler = failure
    advance(clock, 5)
    assert not value.tick(enabled=True)


def test_core_constructor_snapshot_and_disabled_poll_do_not_query_native_state(tmp_path, monkeypatch):
    import desktop.observations as observations
    calls = []
    def forbidden():
        calls.append(True)
        raise AssertionError("No live native observation in tests")
    monkeypatch.setattr(observations, "foreground_category", forbidden)
    core = DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action"))
    assert core.snapshot()["observations"]["enabled"] is False
    assert not core.poll_observations()
    assert "не" in core.handle("учусь ли я").text.lower()
    assert calls == []


def test_core_observations_are_hints_not_completion_or_gym_evidence(tmp_path):
    core = DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action"))
    clock = {"wall": NOW, "mono": 1000.0}
    core.observations = ObservationMonitor(core.store, sampler=lambda: "gaming", wall_clock=lambda: clock["wall"], monotonic_clock=lambda: clock["mono"])
    core.store.set_preferences({"activity_monitor_enabled": True})
    core.organizer.add_item("Учебная цель", "2026-10-29")
    core.poll_observations()
    advance(clock, 5)
    assert core.poll_observations()
    data = core.snapshot()["observations"]
    assert data["seconds"]["gaming"] == 5
    assert core.organizer.day("2026-10-29")["items"][0]["status"] == "unknown"
    text = core.handle("моя активность").text
    assert "не доказывает учёбу" in text and "не подтверждает зал" in text
    core.clear_observations()
    assert core.snapshot()["observations"]["observed_seconds"] == 0


class NativeCall:
    def __init__(self, function):
        self.function = function
    def __call__(self, *args):
        return self.function(*args)


def fake_native(monkeypatch, *, idle_seconds=10, query_ok=True, open_ok=True):
    import desktop.observations as observations
    monkeypatch.setattr(observations, "os", SimpleNamespace(name="nt"))
    closed, queried = [], []
    def last_input(pointer):
        pointer._obj.dwTime = int(600000 - idle_seconds * 1000)
        return 1
    def process_id(_window, pointer):
        pointer._obj.value = 42
        return 1
    def image_name(_handle, _flags, buffer, size):
        queried.append(True)
        buffer.value = r"C:\DO_NOT_STORE\Code.exe"
        size._obj.value = len(buffer.value)
        return int(query_ok)
    user = SimpleNamespace(GetLastInputInfo=NativeCall(last_input), GetForegroundWindow=NativeCall(lambda: 10),
                           GetWindowThreadProcessId=NativeCall(process_id))
    kernel = SimpleNamespace(GetTickCount=NativeCall(lambda: 600000),
        OpenProcess=NativeCall(lambda *_args: 501 if open_ok else 0),
        QueryFullProcessImageNameW=NativeCall(image_name), CloseHandle=NativeCall(lambda handle: closed.append(handle) or 1))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_kwargs: user if name == "user32" else kernel, raising=False)
    return queried, closed


def test_native_reader_uses_only_foreground_basename_and_closes_handle(monkeypatch):
    queried, closed = fake_native(monkeypatch)
    assert foreground_category() == "work"
    assert queried == [True] and closed == [501]


def test_native_query_failure_still_closes_process_handle(monkeypatch):
    _queried, closed = fake_native(monkeypatch, query_ok=False)
    assert foreground_category() == "unknown" and closed == [501]


def test_idle_native_sample_does_not_read_process_name(monkeypatch):
    queried, closed = fake_native(monkeypatch, idle_seconds=301)
    assert foreground_category() == "idle" and not queried and not closed


def test_nonwindows_reader_does_not_load_native_libraries(monkeypatch):
    import desktop.observations as observations
    monkeypatch.setattr(observations, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: pytest.fail("Native API"), raising=False)
    assert foreground_category() == "unknown"
