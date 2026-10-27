"""No real network, process, credentials or installed Ollama are used."""
import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing

import pytest

from desktop.model_runtime import ensure_model_runtime


def settings(tmp_path, value=True):
    database = tmp_path / "desktop.db"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE preferences(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO preferences VALUES(1,?)", (json.dumps({"local_chat_enabled": value}),))
    return database


def no_service(*args, **kwargs):
    assert args == (("127.0.0.1", 11434),)
    assert kwargs == {"timeout": 0.35}
    raise ConnectionRefusedError()


def forbidden(*args, **kwargs):
    pytest.fail("Unexpected process/network operation")


def manifest(tmp_path):
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"synthetic setup-verified executable")
    return {"ollamaExe": str(executable),
            "ollamaSha256": hashlib.sha256(executable.read_bytes()).hexdigest()}


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_explicit_boolean_preference_required_without_network_or_spawn(tmp_path, value):
    database = settings(tmp_path, value)
    before = database.read_bytes()
    assert ensure_model_runtime(tmp_path, {}, connector=forbidden, spawn=forbidden) == "disabled"
    assert database.read_bytes() == before


def test_missing_or_corrupt_preferences_fail_closed_and_never_create_database(tmp_path):
    assert ensure_model_runtime(tmp_path, {}, connector=forbidden, spawn=forbidden) == "disabled"
    assert list(tmp_path.iterdir()) == []
    settings(tmp_path)
    with closing(sqlite3.connect(tmp_path / "desktop.db")) as db, db:
        db.execute("UPDATE preferences SET value='not JSON'")
    assert ensure_model_runtime(tmp_path, {}, connector=forbidden, spawn=forbidden) == "unavailable"


def test_existing_loopback_listener_skips_manifest_and_is_not_stopped(tmp_path):
    settings(tmp_path)
    closed = []
    class Connection:
        def close(self):
            closed.append(True)
    def healthy(address, timeout):
        assert address == ("127.0.0.1", 11434) and timeout == 0.35
        return Connection()
    assert ensure_model_runtime(tmp_path, {}, connector=healthy, spawn=forbidden) == "already_listening"
    assert closed == [True]


@pytest.mark.parametrize("data", [{}, None, {"ollamaExe": "relative/ollama.exe", "ollamaSha256": "a" * 64},
    {"ollamaExe": r"\\server\share\ollama.exe", "ollamaSha256": "a" * 64},
    {"ollamaExe": "//server/share/ollama.exe", "ollamaSha256": "a" * 64},
    {"ollamaExe": "https://external.example/ollama.exe", "ollamaSha256": "a" * 64}])
def test_untrusted_manifest_network_or_relative_executable_never_spawns(tmp_path, data):
    settings(tmp_path)
    assert ensure_model_runtime(tmp_path, data, connector=no_service, spawn=forbidden) == "refused"


def test_approved_basename_and_matching_file_hash_are_both_required(tmp_path):
    settings(tmp_path)
    data = manifest(tmp_path)
    data["ollamaSha256"] = "0" * 64
    assert ensure_model_runtime(tmp_path, data, connector=no_service, spawn=forbidden) == "refused"
    data = manifest(tmp_path)
    other = tmp_path / "other.exe"
    other.write_bytes((tmp_path / "ollama.exe").read_bytes())
    data["ollamaExe"] = str(other)
    assert ensure_model_runtime(tmp_path, data, connector=no_service, spawn=forbidden) == "refused"


def test_verified_serve_is_hidden_local_only_and_creates_no_logs_or_environment_mutations(tmp_path, monkeypatch):
    settings(tmp_path)
    data = manifest(tmp_path)
    before = {x.name for x in tmp_path.iterdir()}
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:1234")
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "0")
    monkeypatch.setenv("OLLAMA_DEBUG", "1")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    calls = []
    def spawn(args, **kwargs):
        calls.append((args, kwargs))
    assert ensure_model_runtime(tmp_path, data, connector=no_service, spawn=spawn) == "started"
    args, options = calls[0]
    assert args == [data["ollamaExe"], "serve"] and options["shell"] is False
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["creationflags"] == 0x08000200
    assert options["env"]["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert options["env"]["OLLAMA_NO_CLOUD"] == "1" and options["env"]["OLLAMA_DEBUG"] == "0"
    assert options["env"]["OLLAMA_MAX_LOADED_MODELS"] == options["env"]["OLLAMA_NUM_PARALLEL"] == "1"
    import os
    assert os.environ["OLLAMA_HOST"] == "0.0.0.0:1234" and os.environ["OLLAMA_NO_CLOUD"] == "0"
    assert {x.name for x in tmp_path.iterdir()} == before


def test_optional_spawn_error_is_fixed_status_and_does_not_expose_error_contents(tmp_path):
    settings(tmp_path)
    def denied(*args, **kwargs):
        raise OSError("private token and local path must not reach UI")
    assert ensure_model_runtime(tmp_path, manifest(tmp_path), connector=no_service, spawn=denied) == "unavailable"
