"""Explicit local-only UI mode; fake capture/models, no keys, network or audio."""
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop UI dependency")
from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from desktop.core import DesktopCore, Reply  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


class Voice:
    def __init__(self, on_text, on_state, **kwargs):
        self.on_text, self.on_state, self.options = on_text, on_state, kwargs
        self.starts = self.stops = self.interruptions = self.preparations = 0
        self.applied, self.spoken = [], []
        self.capture_stopped, self.stop_immediate = True, True
        self.command_generation = 0

    def available(self):
        return {"ready": True, "model": True}

    def apply_preferences(self, preferences):
        self.applied.append(dict(preferences))

    def prepare(self):
        self.preparations += 1

    def start(self):
        assert self.capture_stopped
        self.starts += 1
        self.capture_stopped = False
        return True

    def set_command_receiver(self, callback):
        self.on_command = callback

    def stop(self):
        self.stops += 1
        self.command_generation += 1
        if self.stop_immediate:
            self.capture_stopped = True

    def interrupt(self):
        self.interruptions += 1
        self.command_generation += 1

    def say(self, text):
        self.spoken.append(text)

    def close(self):
        self.capture_stopped = True


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("Expected local mode state did not arrive")


@pytest.fixture
def make_window(app, tmp_path, monkeypatch):
    from desktop import owner_auth
    from desktop.openai_mode import OpenAIEnvironment
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty-home"))
    monkeypatch.setattr(owner_auth, "register_session_notifications", lambda _hwnd: False)
    monkeypatch.setattr(owner_auth, "unregister_session_notifications", lambda _hwnd: None)
    monkeypatch.setattr(OpenAIEnvironment, "__getitem__", lambda *_: pytest.fail("Mode UI read a credential/provider flag"))
    widgets = []

    def make(**preferences):
        state = tmp_path / str(len(widgets))
        effects = []
        core = DesktopCore(state, executor=lambda action: effects.append(action) or "Выполнено")
        core.store.set_preferences({"voice_engine": "clone", "voice_rate": 240, "voice_character": "aidar",
                                    "close_to_tray": False, **preferences})
        monkeypatch.setattr(core, "due_events", lambda **_kwargs: [])
        widget = MainWindow(core, voice_factory=Voice, enable_tray=False, enable_integrations=False,
                            quiet_start=True, personal_path=tmp_path / "absent.db")
        widget.effects = effects
        widgets.append(widget)
        return widget

    yield make
    for widget in widgets:
        widget.voice.stop_immediate = True
        widget.quit_application()
        wait_for(app, lambda: widget._resources_closed)
        widget.deleteLater()


def test_local_only_checkbox_is_explicit_default_off_and_persisted_without_changing_voice(make_window):
    window = make_window()
    toggle = window.findChild(QCheckBox, "freeModeToggle")
    assert toggle is window.free_mode_toggle and not toggle.isChecked()
    assert toggle.text() == "Блокировать платные обращения OpenAI"
    before = window.core.store.preferences()
    toggle.setChecked(True)
    after = window.core.store.preferences()
    assert after == before | {"free_mode": True}
    assert after["voice_engine"] == "clone" and after["voice_rate"] == 240 and after["voice_character"] == "aidar"
    assert not after["local_chat_enabled"] and window.voice.starts == 0
    assert window.voice.applied[-1] == after and not window.effects


def test_enabling_from_openai_changes_engine_in_one_atomic_write_only(make_window, monkeypatch):
    window = make_window(voice_engine="openai")
    persist, writes = window.core.store.set_preferences, []

    def record(updates):
        writes.append(dict(updates))
        return persist(updates)

    monkeypatch.setattr(window.core.store, "set_preferences", record)
    window.free_mode_toggle.setChecked(True)
    assert writes == [{"free_mode": True, "voice_engine": "auto"}]
    assert window.preferences["voice_engine"] == window.voice_engine.currentData() == "auto"
    assert window.preferences["voice_rate"] == 240 and window.preferences["voice_character"] == "aidar"
    assert window.multilingual_toggle.isEnabled() and not window.multilingual_toggle.isChecked()
    assert not window.voice_engine.model().item(window.voice_engine.findData("openai")).isEnabled()
    assert window.voice.applied[-1]["free_mode"] is True


def test_openai_cannot_be_selected_programmatically_in_free_mode_and_optout_does_not_select_it(make_window):
    window = make_window(free_mode=True)
    window.voice_engine.setCurrentIndex(window.voice_engine.findData("openai"))
    assert window.voice_engine.currentData() == window.preferences["voice_engine"] == "clone"
    assert "отключён" in window.status.text()
    window.free_mode_toggle.setChecked(False)
    assert window.voice_engine.model().item(window.voice_engine.findData("openai")).isEnabled()
    assert window.voice_engine.currentData() == "clone" and not window.preferences["free_mode"]
    assert not window.preferences["local_chat_enabled"] and window.voice.starts == 0


def test_failed_mode_write_restores_controls_without_partial_voice_change(make_window, monkeypatch):
    window = make_window(voice_engine="openai")
    before = window.core.store.preferences()
    applied = list(window.voice.applied)

    def fail(_updates):
        raise OSError("Synthetic private database path")

    monkeypatch.setattr(window.core.store, "set_preferences", fail)
    window.free_mode_toggle.setChecked(True)
    assert window.core.store.preferences() == before
    assert not window.free_mode_toggle.isChecked() and window.voice_engine.currentData() == "openai"
    assert window.voice_engine.model().item(window.voice_engine.findData("openai")).isEnabled()
    assert window.voice.applied == applied and "private" not in window.status.text()


@pytest.mark.parametrize("kind", ["command", "chat"])
def test_mode_change_cancels_inflight_response_and_cannot_surface_late_paid_result(app, make_window, monkeypatch, kind):
    window = make_window()
    entered, release = threading.Event(), threading.Event()
    events = []

    def handle(_text, source="typed", *, on_chunk=None, cancel_event=None, conversation_id=None, conversation_history=None):
        events.append(cancel_event)
        on_chunk("Незавершённый старый ответ")
        entered.set()
        assert release.wait(3)
        on_chunk("Поздний фрагмент")
        return Reply("СТАРЫЙ ПЛАТНЫЙ ОТВЕТ")

    monkeypatch.setattr(window.core, "request_kind", lambda *_args, **_kwargs: kind)
    monkeypatch.setattr(window.core, "handle", handle)
    assert window.submit("Синтетический запрос") and entered.wait(1)
    try:
        window.free_mode_toggle.setChecked(True)
        assert events[0].is_set() and not window.stream_preview.isVisible()
        assert window.voice.interruptions >= 2
    finally:
        release.set()
    wait_for(app, lambda: not window._workers)
    assert not window.voice.spoken and not window.effects
    assert window.preferences["free_mode"] and "СТАРЫЙ" not in str(window.core.snapshot()["recent"])


def test_free_mode_restarts_only_existing_capture_after_actual_stop_and_fences_old_session(app, make_window, monkeypatch):
    from desktop import ui
    window = make_window(multilingual_stt_enabled=True)
    window.start_listening()
    wait_for(app, lambda: window.listening)
    old_callback = window.voice.on_text
    window.voice.stop_immediate = False
    pending = []
    monkeypatch.setattr(ui.QTimer, "singleShot", lambda _delay, callback: pending.append(callback))
    window.free_mode_toggle.setChecked(True)
    assert window.voice.starts == 1 and not window.listening and len(pending) == 1
    window.voice.capture_stopped = True
    pending.pop(0)()
    wait_for(app, lambda: window.listening)
    assert window.voice.starts == 2 and window.voice.applied[-1]["free_mode"] is True
    old_callback("открой калькулятор")
    app.processEvents()
    assert not window.effects
    window.voice.on_text("открой калькулятор")
    wait_for(app, lambda: window.effects == ["calculator"])


def test_user_stop_cancels_delayed_mode_restart(app, make_window, monkeypatch):
    from desktop import ui
    window = make_window()
    window.start_listening()
    wait_for(app, lambda: window.listening)
    window.voice.stop_immediate = False
    pending = []
    monkeypatch.setattr(ui.QTimer, "singleShot", lambda _delay, callback: pending.append(callback))
    window.free_mode_toggle.setChecked(True)
    window.stop_listening()
    window.voice.capture_stopped = True
    pending.pop(0)()
    app.processEvents()
    assert window.voice.starts == 1 and not window.listening and not pending


def test_default_transcriber_factory_reads_current_local_language_lazily(make_window, monkeypatch):
    from desktop import ai_routing, voice
    calls = []
    window = make_window()
    monkeypatch.setattr(voice, "VoiceService", Voice)
    monkeypatch.setattr(ai_routing, "FallbackTranscription", lambda state, **kwargs: SimpleNamespace(state=state, **kwargs))
    monkeypatch.setitem(sys.modules, "desktop.multilingual_stt", SimpleNamespace(
        MultilingualSTT=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(**kwargs)))
    window.voice_factory = None
    default = window._new_voice()
    assert not calls
    facade = default.options["transcriber_factory"]()
    assert not calls  # Facade construction also leaves the model unloaded.
    assert facade.local_factory().preferred_language is None
    window.core.store.set_preferences({"free_mode": True, "language": "ru"})
    assert facade.local_factory().preferred_language == "ru"
    window.core.store.set_preferences({"language": "en"})
    assert facade.local_factory().preferred_language == "en"
    window.core.store.set_preferences({"free_mode": False})
    assert facade.local_factory().preferred_language is None
    assert calls == [{"preferred_language": None}, {"preferred_language": "ru"},
                     {"preferred_language": "en"}, {"preferred_language": None}]
