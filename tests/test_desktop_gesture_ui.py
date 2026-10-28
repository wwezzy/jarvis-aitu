"""MainWindow gesture/consent lifecycle with no camera, Hello or OS actions."""
import ctypes
import os
import threading
import time
from ctypes.wintypes import MSG
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop UI dependency")
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from desktop import control_panel, owner_auth  # noqa: E402
from desktop.core import DesktopCore, Reply  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


class Voice:
    def __init__(self, on_text, on_state, model_dir=None):
        self.on_text, self.on_state = on_text, on_state
        self.closed = self.started = self.stopped = 0
        self.spoken = []

    def available(self):
        return {"ready": True, "model": True}

    def apply_preferences(self, preferences):
        self.preferences = preferences

    def prepare(self):
        pass

    def start(self):
        self.started += 1
        return True

    def stop(self):
        self.stopped += 1

    def say(self, text):
        self.spoken.append(text)

    def close(self):
        self.closed += 1


class Verifier:
    def __init__(self):
        self.calls = []

    def verify(self, hwnd, cancel=None, timeout=90):
        self.calls.append(hwnd)
        return owner_auth.AuthResult(True, "verified", "Synthetic verified result")


class Gestures:
    instances = []

    def __init__(self, on_intent, on_state, *, owner_gate):
        self.on_intent, self.on_state, self.owner_gate = on_intent, on_state, owner_gate
        self.starts = self.stops = 0
        self.instances.append(self)

    def start(self):
        self.starts += 1
        if not self.owner_gate(None):
            return False
        self.on_state("Камера включена.")
        return True

    def stop(self):
        self.stops += 1
        self.on_state("Камера выключена.")
        return True


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, condition):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("Gesture integration lifecycle did not settle")


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    Gestures.instances = []
    monkeypatch.setattr(control_panel, "GestureService", Gestures)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "isolated-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "isolated-home"))
    monkeypatch.setattr(DesktopCore, "due_events", lambda *_a, **_kw: [])
    hooks = {"registered": [], "unregistered": []}
    monkeypatch.setattr(owner_auth, "register_session_notifications", lambda hwnd: hooks["registered"].append(hwnd) or True)
    monkeypatch.setattr(owner_auth, "unregister_session_notifications", lambda hwnd: hooks["unregistered"].append(hwnd) or True)
    # Pure message-structure routing below never enters Qt's native base API.
    monkeypatch.setattr(QMainWindow, "nativeEvent", lambda *_a: (False, 0))
    executed, verifier = [], Verifier()
    core = DesktopCore(tmp_path / "state", executor=lambda action: executed.append(action) or "Synthetic action result")
    core.store.set_preferences({"voice_owner_required": True, "gestures_enabled": True})
    core.owner = owner_auth.OwnerSession(True, verifier=verifier, session_check=lambda: True)
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False,
                        enable_integrations=False, quiet_start=True,
                        personal_path=tmp_path / "missing-personal.db")
    widget.test_executed, widget.test_verifier, widget.test_hooks = executed, verifier, hooks
    widget.test_closes = []
    original_close = core.close
    def close():
        widget.test_closes.append(widget.controls.shutdown_complete)
        original_close()
    monkeypatch.setattr(core, "close", close)
    widget.show()
    wait_for(app, lambda: not widget.controls.busy)
    yield widget
    widget._quitting = True
    widget.close()
    wait_for(app, lambda: widget._resources_closed and widget.controls.shutdown_complete)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def enable_gestures(app, window):
    assert window.core.owner.verify(1234).success  # Fake verifier, no Windows API.
    window.controls._poll_owner()
    window.controls.gesture_toggle.setChecked(True)
    wait_for(app, lambda: not window.controls.busy and bool(Gestures.instances))
    return Gestures.instances[-1]


def test_construction_refresh_and_navigation_never_create_camera_or_prompt_hello(window, app):
    assert Gestures.instances == [] and window.test_verifier.calls == []
    for _ in range(3):
        window.refresh()
        window.navigate(window.CONTROLS_PAGE)
        wait_for(app, lambda: not window.controls.busy)
    assert Gestures.instances == [] and window.test_verifier.calls == []
    assert not window.controls.gesture_toggle.isChecked()
    assert not window.core.store.preferences()["gestures_enabled"]
    assert window.test_executed == []


def test_owner_session_notifications_are_independent_of_phone_integrations(window):
    assert window._integrations_enabled is False
    assert window._session_notifications_registered is True
    assert len(window.test_hooks["registered"]) == 1


def test_failed_session_hook_denies_owner_prompt_and_revokes_existing_grant(window, app):
    assert window.core.owner.verify(1234).success
    before = list(window.test_verifier.calls)
    window._session_notifications_registered = False
    window.controls._poll_owner()
    window.controls.owner_verify.click()
    app.processEvents()
    assert window.test_verifier.calls == before and not window.core.owner.allowed()
    assert "блокировки сеанса" in window.controls.status.text()


def test_windows_lock_event_revokes_owner_before_queued_gesture_dispatch(window, app):
    service = enable_gestures(app, window)
    service.on_intent("volume_up")  # Queued Qt callback; not processed yet.
    message = MSG()
    message.message, message.wParam = 0x02B1, 7  # WM_WTSSESSION_CHANGE / SESSION_LOCK.
    assert window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message)) == (False, 0)
    wait_for(app, lambda: not window.controls.busy and service.stops > 0)
    assert not window.core.owner.allowed()
    assert window.test_executed == []
    assert not window.controls.gesture_toggle.isChecked() and not service.owner_gate(None)


def test_valid_gesture_uses_core_policy_and_reply_then_shutdown_blocks_late_callback(window, app):
    service = enable_gestures(app, window)
    service.on_intent("volume_down")
    wait_for(app, lambda: window.test_executed == ["volume_down"] and not window.controls.busy)
    window._quitting = True
    window.close()
    service.on_intent("volume_up")
    service.on_state("Камера включена.")
    wait_for(app, lambda: window._resources_closed)
    assert window.test_executed == ["volume_down"]
    assert window.voice.closed == 1 and window.test_closes == [True]


def test_main_window_waits_for_actual_controls_thread_then_closes_core_exactly_once(window, app):
    entered, released, exited = threading.Event(), threading.Event(), threading.Event()
    def slow_control():
        entered.set()
        try:
            assert released.wait(2)
            return Reply("Late result must be ignored")
        finally:
            exited.set()
    window.controls._run(slow_control, refresh=False)
    assert entered.wait(1)
    window._quitting = True
    window.close()
    assert not window.controls.busy  # GUI callbacks were canceled immediately.
    assert not window.controls.shutdown_complete and not window._resources_closed
    assert window.test_closes == [] and window.voice.closed == 0
    assert window._close_timer.isActive()
    window._finish_close()
    assert window.test_closes == []
    released.set()
    wait_for(app, lambda: window._resources_closed)
    assert exited.is_set() and window.test_closes == [True]
    assert window.controls.shutdown_complete and window.voice.closed == 1
    assert window.test_hooks["unregistered"] == window.test_hooks["registered"]
    window._finish_close()
    window.close()
    assert window.test_closes == [True] and window.voice.closed == 1
    assert len(window.test_hooks["unregistered"]) == 1


def test_main_window_cancels_hello_and_waits_for_verification_thread_cleanup(window, app):
    entered, released, canceled = threading.Event(), threading.Event(), []
    def verify(hwnd, cancel=None, timeout=90):
        entered.set()
        assert released.wait(2)
        canceled.append(cancel.is_set())
        return owner_auth.AuthResult(False, "canceled", "Synthetic canceled result")
    window.core.owner._verifier.verify = verify
    window.controls.owner_verify.click()
    assert entered.wait(1)
    window._quitting = True
    window.close()
    assert not window.controls.shutdown_complete and window.test_closes == []
    released.set()
    wait_for(app, lambda: window._resources_closed)
    assert canceled == [True] and not window.core.owner.allowed()
    assert window.test_closes == [True]
