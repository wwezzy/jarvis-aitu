"""Cloud primary skips eager local startup; no GUI, sockets, model or process."""
import json
import sqlite3
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

import desktop_app
from desktop import model_runtime


@pytest.mark.parametrize("preferences,environ,expected", [
    ({"local_chat_enabled": True}, {}, True),
    ({"local_chat_enabled": True, "voice_engine": "windows"}, {}, True),
    ({"local_chat_enabled": True, "voice_engine": "clone"}, {}, True),
    ({"local_chat_enabled": True, "voice_engine": "openai"}, {}, False),
    ({"local_chat_enabled": True, "voice_engine": "clone"}, {"JARVIS_CLOUD_CHAT_ENABLED": "1"}, False),
    ({"local_chat_enabled": True}, {"JARVIS_CLOUD_CHAT_ENABLED": "0"}, True),
    ({"local_chat_enabled": False}, {}, False),
    ({"local_chat_enabled": 1}, {}, False),
    ({"local_chat_enabled": "true"}, {}, False),
    ({}, {}, False), (None, {}, False), ([], {}, False),
])
def test_shared_primary_predicate_is_strict_and_preserves_explicit_local_mode(preferences, environ, expected):
    assert model_runtime.should_start_local_model(preferences, environ) is expected


def test_predicate_reads_only_provider_flag_without_key_or_profile_discovery(monkeypatch):
    class Environment:
        def get(self, key):
            assert key == "JARVIS_CLOUD_CHAT_ENABLED"
            return "1"
    def forbidden(*_args, **_kwargs):
        pytest.fail("Primary selection performed I/O")
    monkeypatch.setattr(model_runtime.sqlite3, "connect", forbidden)
    monkeypatch.setattr(model_runtime.hashlib, "file_digest", forbidden)
    assert model_runtime.should_start_local_model({"local_chat_enabled": True}, Environment()) is False


@pytest.mark.parametrize("cloud", ["voice_mode", "explicit_cloud_flag"])
def test_actual_runtime_cloud_primary_never_checks_listener_manifest_hash_or_spawn(tmp_path, monkeypatch, cloud):
    preferences = {"local_chat_enabled": True, "voice_engine": "openai" if cloud == "voice_mode" else "clone"}
    path = tmp_path / "desktop.db"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE preferences(id INTEGER PRIMARY KEY,value TEXT)")
        database.execute("INSERT INTO preferences VALUES(1,?)", (json.dumps(preferences),))
    original = path.read_bytes()
    calls = []
    def forbidden(*_args, **_kwargs):
        calls.append(True)
        pytest.fail("Cloud primary performed eager local runtime work")
    class Manifest(dict):
        get = forbidden
    monkeypatch.setattr(model_runtime.hashlib, "file_digest", forbidden)
    environment = {"JARVIS_CLOUD_CHAT_ENABLED": "1"} if cloud == "explicit_cloud_flag" else {}
    assert model_runtime.ensure_model_runtime(tmp_path, Manifest(), connector=forbidden,
                                              spawn=forbidden, environ=environment) == "disabled"
    assert not calls and path.read_bytes() == original


@pytest.mark.parametrize("cloud", ["voice_mode", "explicit_cloud_flag", "local_primary", "gemini_primary", "gemini_disabled", "gemini_truthy"])
def test_actual_entrypoint_uses_same_gate_without_starting_gui_or_model(tmp_path, monkeypatch, cloud):
    assert desktop_app.should_start_local_model is model_runtime.should_start_local_model
    preferences = {"local_chat_enabled": True, "voice_engine": "openai" if cloud == "voice_mode" else "clone",
                   "local_chat_model": "synthetic-local-model"}
    if cloud.startswith("gemini_"):
        preferences.update(free_mode=True, gemini_enabled={"gemini_primary": True, "gemini_disabled": False, "gemini_truthy": 1}[cloud])
        # Service admission remains true for an available on-demand fallback.
        # Only entrypoint model weight preloading is suppressed for Gemini.
        assert model_runtime.should_start_local_model(preferences, {}) is True
    started = []
    if cloud == "explicit_cloud_flag":
        monkeypatch.setenv("JARVIS_CLOUD_CHAT_ENABLED", "1")
    else:
        monkeypatch.delenv("JARVIS_CLOUD_CHAT_ENABLED", raising=False)
    class Thread:
        def __init__(self, *, target, args, name, daemon):
            assert target is desktop_app._warm_local_chat and name == "JarvisModelWarmup" and daemon is True
            self.args = args

        def start(self):
            started.append(self.args)
    monkeypatch.setattr(threading, "Thread", Thread)
    class Application:
        @staticmethod
        def instance():
            return Application()

        def setApplicationName(self, _value):
            pass

        def setOrganizationName(self, _value):
            pass

        def exec(self):
            return 0
    class Socket:
        def connectToServer(self, _name):
            pass

        def waitForConnected(self, _timeout):
            return False
    class Server:
        SocketOption = SimpleNamespace(UserAccessOption=1)

        def __init__(self, _app):
            self.newConnection = SimpleNamespace(connect=lambda _callback: None)

        @staticmethod
        def removeServer(_name):
            pass

        def setSocketOptions(self, _options):
            pass

        def listen(self, _name):
            return True

        def close(self):
            pass
    class Core:
        def __init__(self, *_args, **_kwargs):
            self.store = SimpleNamespace(preferences=lambda: dict(preferences))
    class Window:
        def __init__(self, *_args, **_kwargs):
            pass

        def show(self):
            pass
    def module(name, **values):
        result = ModuleType(name)
        result.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, result)
        return result
    module("PySide6", __path__=[])
    module("PySide6.QtCore", QTimer=SimpleNamespace())
    module("PySide6.QtNetwork", QLocalServer=Server, QLocalSocket=Socket)
    module("PySide6.QtWidgets", QApplication=Application, QMessageBox=SimpleNamespace(
        critical=lambda *_args: pytest.fail("Entrypoint unexpectedly failed")))
    module("desktop.core", DesktopCore=Core)
    module("desktop.ui", MainWindow=Window)
    module("desktop.runtime_env", create_phone_environment=lambda _state: None, load_phone_environment=lambda _state: None)
    module("desktop.personal", default_snapshot_path=lambda: tmp_path / "absent-archive.db")
    assert desktop_app.main(["--state-dir", str(tmp_path), "--quiet-start"]) == 0
    assert started == ([("synthetic-local-model",)] if cloud in {"local_primary", "gemini_disabled", "gemini_truthy"} else [])
