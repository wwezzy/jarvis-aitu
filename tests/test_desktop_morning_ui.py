"""Trusted morning cue continuation: fake playback, real nonce, no devices."""
import os
from pathlib import Path
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop UI dependency")
from PySide6.QtWidgets import QApplication  # noqa: E402

from desktop.core import DesktopCore  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


class Voice:
    def __init__(self, on_text, on_state, **_kwargs):
        self.on_text, self.on_state = on_text, on_state
        self.callbacks, self.spoken, self.cues = [], [], []
        self.accept = True
    def available(self):
        return {"ready": True, "model": True}
    def start(self):
        return True
    def stop(self):
        pass
    def say(self, text):
        self.spoken.append(text)
    def interrupt(self):
        pass
    def play_cue(self, name, *, on_complete=None):
        self.cues.append(name)
        if on_complete is not None:
            self.callbacks.append(on_complete)
        return self.accept


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from desktop import owner_auth
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty-home"))
    monkeypatch.setattr(owner_auth, "register_session_notifications", lambda _hwnd: False)
    monkeypatch.setattr(owner_auth, "unregister_session_notifications", lambda _hwnd: None)
    effects = []
    def execute(action):
        effects.append((action, threading.get_ident()))
        return "Проверенный результат"
    core = DesktopCore(tmp_path / "state", executor=execute)
    core.store.set_preferences({"close_to_tray": False})
    monkeypatch.setattr(core, "due_events", lambda **_kwargs: [])
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False, enable_integrations=False,
                        quiet_start=True, personal_path=tmp_path / "absent.db")
    widget.effects = effects
    widget.show()
    app.processEvents()
    yield widget
    widget.quit_application()
    wait_for(app, lambda: widget._resources_closed)
    widget.deleteLater()


def wait_for(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            app.processEvents()
            return
        time.sleep(.005)
    raise AssertionError("Expected morning UI state did not arrive")


def begin(app, window):
    count = len(window.voice.callbacks)
    assert window.submit("доброе утро")
    wait_for(app, lambda: len(window.voice.callbacks) > count and not window.busy)
    assert window.effects == [] and window.voice.spoken == []
    token = window._morning_followup["token"]
    assert token not in str(window.core.snapshot()["recent"])
    return window.voice.callbacks[-1], token


def test_full_cue_callback_is_queued_and_only_then_dispatches_once_on_action_lane(app, window):
    completed, _token = begin(app, window)
    completed(True)
    assert window.effects == []  # Even a GUI-thread callback only enqueues a signal.
    completed(True)  # Duplicate/misbehaving backend callbacks cannot repeat music.
    wait_for(app, lambda: not window._workers and bool(window.effects))
    assert window.effects == [("music_morning", window.effects[0][1])]
    assert window.effects[0][1] != threading.get_ident()
    assert window.voice.spoken == []
    completed(True)
    app.processEvents()
    assert len(window.effects) == 1


def test_speech_worker_never_calls_core_or_os_directly(app, window, monkeypatch):
    completed, _token = begin(app, window)
    callback_thread = []
    core_threads = []
    original = window.core.complete_morning
    def complete(token, *, cancel_event=None):
        core_threads.append(threading.get_ident())
        return original(token, cancel_event=cancel_event)
    monkeypatch.setattr(window.core, "complete_morning", complete)
    def playback_thread():
        callback_thread.append(threading.get_ident())
        completed(True)
    thread = threading.Thread(target=playback_thread)
    thread.start()
    thread.join(1)
    assert core_threads == window.effects == []
    wait_for(app, lambda: bool(window.effects))
    assert core_threads[0] != threading.get_ident() and core_threads[0] != callback_thread[0]


@pytest.mark.parametrize("played", [False, None, 1])
def test_failed_cancelled_or_nonboolean_playback_revokes_followup(app, window, played):
    completed, token = begin(app, window)
    completed(played)
    wait_for(app, lambda: window._morning_followup is None and not window._workers)
    assert window.effects == []
    assert "отменён" in window.core.complete_morning(token).text
    assert window.effects == []


@pytest.mark.parametrize("reason", ["request", "stop", "mute", "close", "microphone_stop"])
def test_cancelled_context_cannot_resume_music_from_late_completed_callback(app, window, reason):
    completed, _token = begin(app, window)
    if reason == "request":
        window.submit("как дела")
    elif reason == "stop":
        window.stop_response_button.click()
    elif reason == "mute":
        window.speech_toggle.setChecked(False)
    elif reason == "close":
        window.quit_application()
    else:
        window.stop_listening()
    completed(True)
    wait_for(app, lambda: not window._workers and window.pool.activeThreadCount() == 0)
    assert window.effects == [] and window._morning_followup is None


@pytest.mark.parametrize("reason", ["request", "stop", "mute", "close"])
def test_queued_morning_continuation_cannot_execute_after_cancel(app, window, reason):
    completed, _token = begin(app, window)
    entered, release = threading.Event(), threading.Event()
    def block_lane():
        entered.set()
        assert release.wait(2)
    window._run(block_lane, lambda _value: None)
    try:
        assert entered.wait(1)
        completed(True)
        app.processEvents()
        assert window._action_request.get("morning") is True and window.effects == []
        if reason == "request":
            assert window.submit("открой калькулятор")
        elif reason == "stop":
            window.stop_response()
        elif reason == "mute":
            window.speech_toggle.setChecked(False)
        else:
            window.quit_application()
    finally:
        release.set()
    wait_for(app, lambda: not window._workers and window.pool.activeThreadCount() == 0)
    assert [action for action, _thread in window.effects] == (["calculator"] if reason == "request" else [])


def test_old_morning_completion_cannot_resume_a_new_morning_session(app, window):
    first, token = begin(app, window)
    second, new_token = begin(app, window)
    assert new_token != token
    first(True)
    app.processEvents()
    assert window.effects == []
    second(True)
    wait_for(app, lambda: bool(window.effects))
    assert [action for action, _thread in window.effects] == ["music_morning"]


def test_unavailable_cue_uses_selected_text_without_any_morning_continuation(app, window):
    window.voice.accept = False
    window.submit("доброе утро")
    wait_for(app, lambda: not window.busy and not window._workers)
    assert window.voice.spoken == ["Доброе утро, сэр."] and window.effects == []
    assert window._morning_followup is None
    window.voice.callbacks[0](True)
    app.processEvents()
    assert window.effects == []


def test_initially_muted_morning_request_never_queues_cue_or_continuation(app, window):
    window.speech_toggle.setChecked(False)
    assert window.submit("доброе утро")
    wait_for(app, lambda: not window.busy and not window._workers)
    assert window.voice.cues == window.voice.callbacks == window.voice.spoken == window.effects == []
    assert window._morning_followup is None


def test_external_reply_does_not_authorize_an_opaque_followup(app, window):
    reply = window.core.handle("доброе утро")
    window._reply(reply)
    wait_for(app, lambda: not window._workers)
    assert window.voice.cues == ["welcome_home"] and window.voice.callbacks == []
    assert window._morning_followup is None and window.effects == []
