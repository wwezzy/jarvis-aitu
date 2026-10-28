"""Explicit synthetic key sources and shared-ledger migration; no live API."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from desktop import cloud_ai, openai_mode


KEY = "sk-" + "synthetic_NEVER_REAL_012345678901234567890"
OTHER_KEY = "sk-" + "synthetic_REVOKED_01234567890123456789"
FLAGS = ("JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED")


def configured(tmp_path, *, content=None, config=None):
    state = tmp_path / "state"
    state.mkdir()
    chosen = tmp_path / "explicit-owner-choice.env"
    chosen.write_text(content if content is not None else "OPENAI_API_KEY=" + KEY + "\n", encoding="utf-8")
    value = {"version": 1, "env_file": str(chosen), "env_name": "OPENAI_API_KEY"}
    if config:
        value.update(config)
    manifest = state / "openai-key-source.json"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    preferences = {"voice_engine": "auto"}
    environment = openai_mode.OpenAIEnvironment(state, lambda: dict(preferences), environ={})
    return SimpleNamespace(env=environment, chosen=chosen, manifest=manifest, prefs=preferences)


def test_construction_nonkey_lookup_and_mode_change_never_read_key_files(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Credential files accessed before explicit key lookup")
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    preferences = {"voice_engine": "auto"}
    env = openai_mode.OpenAIEnvironment(tmp_path, lambda: preferences, environ={"PUBLIC_OPTION": "fixed"})
    assert env.get("PUBLIC_OPTION") == "fixed" and env.get("UNKNOWN") is None
    assert all(env.get(flag) is None for flag in FLAGS)
    preferences["voice_engine"] = "openai"
    assert all(env[flag] == "1" for flag in FLAGS)
    preferences["voice_engine"] = "windows"
    assert all(env.get(flag) is None for flag in FLAGS)


def test_process_key_has_priority_without_file_io_and_is_live_revocable(tmp_path, monkeypatch):
    raw = {"OPENAI_API_KEY": KEY}
    env = openai_mode.OpenAIEnvironment(tmp_path, lambda: {}, environ=raw)
    def forbidden(*_args, **_kwargs):
        pytest.fail("Explicit process key searched private files")
    monkeypatch.setattr(Path, "lstat", forbidden)
    assert env["OPENAI_API_KEY"] == KEY
    raw["OPENAI_API_KEY"] = OTHER_KEY
    assert env["OPENAI_API_KEY"] == OTHER_KEY


def test_empty_mapping_and_complete_mapping_obey_key_iteration_contract(tmp_path):
    env = openai_mode.OpenAIEnvironment(tmp_path, lambda: {}, environ={})
    assert list(env) == [] and len(env) == 0 and dict(env) == {}
    preferences = {"voice_engine": "openai"}
    env = openai_mode.OpenAIEnvironment(tmp_path, lambda: preferences, environ={"OPENAI_API_KEY": KEY, "PUBLIC": "value"})
    assert dict(env) == {"OPENAI_API_KEY": KEY, "PUBLIC": "value", **dict.fromkeys(FLAGS, "1")}
    assert len(env) == 5 and all(key in env for key in env)


@pytest.mark.parametrize("line", ["OPENAI_API_KEY={key}", " export OPENAI_API_KEY = '{key}' ", 'OPENAI_API_KEY="{key}"'])
def test_only_explicit_manifest_source_is_read_without_writing_or_evaluation(tmp_path, line, capsys):
    content = "# PRIVATE unrelated comment\nUNRELATED=$(do-not-run)\n" + line.format(key=KEY) + "\n"
    value = configured(tmp_path, content=content)
    before = {path: path.read_bytes() for path in (value.chosen, value.manifest)}
    assert value.env["OPENAI_API_KEY"] == KEY
    assert dict(value.env) == {"OPENAI_API_KEY": KEY}
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("content", [
    "OPENAI_API_KEY={key}\nOPENAI_API_KEY={key}\n",
    "OPENAI_API_KEY=invalid\nOPENAI_API_KEY={key}\n",
    "OPENAI_API_KEY={key}\nexport OPENAI_API_KEY=$(do-not-run)\n",
    'OPENAI_API_KEY="{key}\n', "OPENAI_API_KEY={key} # ambiguous suffix\n",
])
def test_duplicate_or_malformed_key_assignments_fail_closed(tmp_path, content):
    value = configured(tmp_path, content=content.format(key=KEY))
    assert value.env.get("OPENAI_API_KEY") is None and "OPENAI_API_KEY" not in list(value.env)


@pytest.mark.parametrize("config", [
    {"version": True}, {"version": 2}, {"env_name": "OTHER_SECRET"}, {"env_file": "relative.env"},
    {"extra": "not-allowed"}, {"env_file": 123},
])
def test_invalid_manifest_never_resolves_a_key(tmp_path, config):
    value = configured(tmp_path, config=config)
    assert value.env.get("OPENAI_API_KEY") is None


def test_duplicate_json_field_and_invalid_encoding_refuse_without_raw_error(tmp_path, capsys):
    value = configured(tmp_path)
    value.manifest.write_text('{"version":1,"version":1,"env_file":' + json.dumps(str(value.chosen)) + ',"env_name":"OPENAI_API_KEY"}', encoding="utf-8")
    assert value.env.get("OPENAI_API_KEY") is None
    value.manifest.write_bytes(b"\xff\xfe")
    assert value.env.get("OPENAI_API_KEY") is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("target", ["manifest", "chosen"])
def test_oversized_or_empty_private_files_are_rejected(tmp_path, target):
    value = configured(tmp_path)
    path = getattr(value, target)
    path.write_bytes(b" " * (8193 if target == "chosen" else 4097))
    assert value.env.get("OPENAI_API_KEY") is None
    path.write_bytes(b"")
    assert value.env.get("OPENAI_API_KEY") is None


@pytest.mark.parametrize("target", ["manifest", "chosen"])
def test_hardlinked_private_files_are_rejected_without_modification(tmp_path, target):
    value = configured(tmp_path)
    path = getattr(value, target)
    original = path.read_bytes()
    duplicate = tmp_path / (target + ".hardlink")
    os.link(path, duplicate)
    assert value.env.get("OPENAI_API_KEY") is None
    assert path.read_bytes() == duplicate.read_bytes() == original


@pytest.mark.parametrize("target", ["manifest", "chosen"])
def test_windows_reparse_metadata_is_rejected_before_read(tmp_path, monkeypatch, target):
    value = configured(tmp_path)
    path = getattr(value, target)
    original = Path.lstat
    def marked(actual, *args, **kwargs):
        info = original(actual, *args, **kwargs)
        if actual == path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", marked)
    assert value.env.get("OPENAI_API_KEY") is None


def test_unconfigured_neighbor_env_and_network_path_are_never_discovered(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=" + KEY, encoding="utf-8")
    env = openai_mode.OpenAIEnvironment(tmp_path, lambda: {}, environ={})
    assert env.get("OPENAI_API_KEY") is None
    def forbidden(*_args, **_kwargs):
        pytest.fail("Network path metadata was touched")
    monkeypatch.setattr(Path, "lstat", forbidden)
    with pytest.raises(ValueError):
        openai_mode._regular(Path("//untrusted.invalid/share/secret.env"), 8192)


def test_file_rotation_deletion_and_manifest_revocation_clear_cached_key(tmp_path):
    value = configured(tmp_path)
    assert value.env["OPENAI_API_KEY"] == KEY
    value.chosen.write_text("OPENAI_API_KEY=" + OTHER_KEY, encoding="utf-8")
    assert value.env["OPENAI_API_KEY"] == OTHER_KEY
    value.chosen.unlink()
    assert value.env.get("OPENAI_API_KEY") is None
    value.chosen.write_text("OPENAI_API_KEY=" + KEY, encoding="utf-8")
    assert value.env["OPENAI_API_KEY"] == KEY
    value.manifest.unlink()
    assert value.env.get("OPENAI_API_KEY") is None


def legacy_ledger(state, *, invalid=False):
    path = state / "cloud-ai.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE reservations (
            id TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('chat','stt')),
            model TEXT NOT NULL, reserved INTEGER NOT NULL CHECK(reserved>0),
            settled INTEGER CHECK(settled>=0), outcome TEXT NOT NULL DEFAULT 'unknown'
            CHECK(outcome IN ('unknown','completed')), input_tokens INTEGER, output_tokens INTEGER)""")
        db.execute("CREATE INDEX reservation_day ON reservations(day)")
        db.execute(f"PRAGMA application_id={cloud_ai._APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        day = cloud_ai._day()
        rows = [("pending", day, "stt", "gpt-transcribe", 900, None, "unknown", None, None),
                ("settled", day, "chat", "gpt-6.1-terra", 2000, 200, "completed", 100, 20),
                ("older", "2025-01-01", "chat", "gpt-6-luna", 1000, 5, "completed", 10, 8)]
        db.executemany("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)", rows)
        if invalid:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE reservations SET reserved=-1 WHERE id='pending'")
    return path, rows


def ledger_state(path):
    with sqlite3.connect(path) as db:
        return (db.execute("PRAGMA user_version").fetchone()[0],
                db.execute("SELECT * FROM reservations ORDER BY id").fetchall(),
                db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())


def test_budget_schema_two_migration_retains_all_rows_and_shared_speech_accounting(tmp_path):
    path, rows = legacy_ledger(tmp_path)
    budget = cloud_ai._Budget(tmp_path, {})
    assert budget.available() and ledger_state(path)[0] == 1
    token = budget.reserve("speech", "gpt-4o-mini-tts", 300)
    version, current, tables = ledger_state(path)
    assert version == 2 and tables == [("reservations",)]
    assert [row for row in current if row[0] != token] == sorted(rows)
    assert budget.snapshot()["requests"] == 3
    assert budget.snapshot()["reserved_or_known_micro_usd"] == 1400
    restarted = cloud_ai._Budget(tmp_path, {})
    restarted.settle(token, 50, None, None)
    assert restarted.snapshot()["reserved_or_known_micro_usd"] == 1150
    assert ledger_state(path)[0] == 2


def test_denied_first_postmigration_reservation_does_not_lose_unknown_or_settled_rows(tmp_path):
    path, rows = legacy_ledger(tmp_path)
    budget = cloud_ai._Budget(tmp_path, {"JARVIS_CLOUD_DAILY_BUDGET_USD": "0.0011"})
    with pytest.raises(cloud_ai.CloudBudgetExceeded, match="budget_exceeded"):
        budget.reserve("speech", "gpt-4o-mini-tts", 1)
    assert ledger_state(path) == (2, sorted(rows), [("reservations",)])


def test_failed_migration_rolls_back_schema_and_rows_atomically(tmp_path):
    path, _ = legacy_ledger(tmp_path, invalid=True)
    before = ledger_state(path)
    with pytest.raises(cloud_ai.CloudBudgetExceeded, match="budget_unavailable"):
        cloud_ai._Budget(tmp_path, {}).reserve("speech", "gpt-4o-mini-tts", 1)
    assert ledger_state(path) == before


def test_concurrent_migration_and_mixed_requests_share_one_atomic_cap(tmp_path):
    path, original = legacy_ledger(tmp_path)
    barrier = threading.Barrier(6)
    env = {"JARVIS_CLOUD_DAILY_BUDGET_USD": "0.0019", "JARVIS_CLOUD_DAILY_REQUEST_LIMIT": "4"}
    def reserve(index):
        barrier.wait(timeout=3)
        try:
            cloud_ai._Budget(tmp_path, env).reserve(("chat", "stt", "speech")[index % 3], "synthetic-known-request", 400)
            return True
        except cloud_ai.CloudBudgetExceeded as error:
            assert error.reason == "budget_exceeded"
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(6))) == 2
    version, current, _ = ledger_state(path)
    assert version == 2 and all(row in current for row in original)
    snapshot = cloud_ai._Budget(tmp_path, env).snapshot()
    assert snapshot["requests"] == 4 and snapshot["reserved_or_known_micro_usd"] == 1900
