"""Cancellable conversation admission with real Core, fake model/voice/actions."""
import os
from pathlib import Path
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop UI dependency")
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from desktop.core import DesktopCore  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


FIRST = "Объясни первую сложную идею"


class Voice:
    def __init__(self, on_text, on_state, **_kwargs):
        self.on_text, self.on_state = on_text, on_state
        self.spoken, self.interruptions = [], 0
    def available(self):
        return {"ready": True, "model": True}
    def start(self):
        return True
    def stop(self):
        pass
    def say(self, text):
        self.spoken.append(text)
    def interrupt(self):
        self.interruptions += 1


class Model:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls, self.events = [], []
        self.fail = False
    def answer(self, text, *, on_chunk=None, cancel_event=None, **_kwargs):
        self.calls.append(text)
        self.events.append(cancel_event)
        assert isinstance(cancel_event, threading.Event)
        if text == FIRST:
            if on_chunk:
                on_chunk("Непроверенный старый черновик")
            self.entered.set()
            assert self.release.wait(3)
            if on_chunk:
                on_chunk("ЗАПОЗДАВШИЙ ЧЕРНОВИК")
            if self.fail:
                raise RuntimeError("PRIVATE_OLD_FAILURE")
            return "СТАРЫЙ ОТВЕТ НЕ ДОЛЖЕН БЫТЬ СОХРАНЁН"
        return "СВЕЖИЙ ОТВЕТ: " + text


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
    model, effects = Model(), []
    monkeypatch.setattr("desktop.chat.LocalChat", lambda *_args, **_kwargs: model)
    core = DesktopCore(tmp_path / "state", executor=lambda action: effects.append(action) or "Проверенный результат действия")
    core.store.set_preferences({"local_chat_enabled": True, "close_to_tray": False})
    monkeypatch.setattr(core, "due_events", lambda **_kwargs: [])
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False, enable_integrations=False,
                        quiet_start=True, personal_path=tmp_path / "absent.db")
    widget.model, widget.effects = model, effects
    widget.show()
    app.processEvents()
    yield widget
    model.release.set()
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
    raise AssertionError("Expected cancellation state did not arrive")


def begin_chat(app, window):
    assert window.submit(FIRST)
    assert window.model.entered.wait(1)
    wait_for(app, window.stream_preview.isVisible)


@pytest.mark.parametrize("failure", [False, True])
def test_command_preempts_blocked_model_without_old_reply_error_history_or_actions(app, window, failure):
    window.model.fail = failure
    begin_chat(app, window)
    assert window.command.isEnabled() and window.send_button.isEnabled()
    assert window.submit("открой калькулятор", source="voice")
    wait_for(app, lambda: window.effects == ["calculator"] and not window.busy)
    assert not window.model.release.is_set()  # The command did not wait for model I/O.
    assert window.model.events[0].is_set() and not window.stream_preview.isVisible()
    assert window.voice.spoken == ["Проверенный результат действия"]
    state = window.status.text()
    window.model.release.set()
    wait_for(app, lambda: not window._workers)
    assert window.status.text() == state
    assert window.voice.spoken == ["Проверенный результат действия"]
    assert "СТАРЫЙ ОТВЕТ" not in str(window.core.snapshot()["recent"])
    assert window.effects == ["calculator"] and window.model.calls == [FIRST]


@pytest.mark.parametrize("source", ["button", "typed", "voice"])
def test_stop_is_admitted_during_chat_and_cancels_voice_without_stopping_capture(app, window, source):
    window.start_listening()
    wait_for(app, lambda: window.listening)
    begin_chat(app, window)
    before = window.voice.interruptions
    if source == "button":
        window.stop_response_button.click()
    elif source == "typed":
        window.command.setText("стоп")
        window.submit_current()
        assert window.command.text() == ""
    else:
        window.voice.on_text("стоп")
        app.processEvents()
    assert window.model.events[0].is_set() and not window.busy
    assert window.voice.interruptions > before and window.listening
    assert not window.stream_preview.isVisible()
    state = window.status.text()
    window.model.release.set()
    wait_for(app, lambda: not window._workers)
    assert window.status.text() == state and window.voice.spoken == []
    assert window.effects == [] and "СТАРЫЙ ОТВЕТ" not in str(window.core.snapshot()["recent"])


def test_new_chat_retains_only_latest_replacement_and_never_starts_two_models(app, window):
    begin_chat(app, window)
    assert window.submit("Второй вопрос")
    assert window.submit("Третий вопрос")
    assert window._chat_pool.maxThreadCount() == 1 and window._chat_pool.activeThreadCount() == 1
    assert window.model.calls == [FIRST] and "worker" not in window._pending_chat
    assert window.command.isEnabled() and "предыдущий" in window.request_hint.text()
    window.model.release.set()
    wait_for(app, lambda: not window.busy and not window._workers)
    assert window.model.calls == [FIRST, "Третий вопрос"]
    assert window.voice.spoken == []  # These replacement questions are written.
    assert "Второй вопрос" not in str(window.core.snapshot()["recent"])
    assert "СТАРЫЙ ОТВЕТ" not in str(window.core.snapshot()["recent"])


def test_microphone_can_be_explicitly_started_while_chat_is_blocked(app, window):
    begin_chat(app, window)
    window.start_listening()
    wait_for(app, lambda: window.listening)
    assert not window.model.release.is_set() and window.command.isEnabled()
    window.voice.on_text("стоп")
    app.processEvents()
    assert window.model.events[0].is_set() and not window.busy


@pytest.mark.parametrize("phrase", ["Джарвис, стоп", "Jarvis, stop"])
def test_wake_prefixed_typed_stop_never_waits_for_or_calls_model(app, window, phrase):
    begin_chat(app, window)
    assert window.submit(phrase)
    assert window.model.events[0].is_set() and not window.busy
    assert window.model.calls == [FIRST] and window.effects == []


def test_command_discards_pending_replacement_chat_before_it_can_start(app, window):
    begin_chat(app, window)
    window.submit("Объясни отложенную теорию")
    assert window._pending_chat is not None
    window.submit("добавь задачу Проверить одну тему")
    wait_for(app, lambda: bool(window.core.snapshot()["tasks"]) and not window.busy)
    window.model.release.set()
    wait_for(app, lambda: not window._workers)
    assert window.model.calls == [FIRST]
    assert window.core.snapshot()["tasks"][0]["title"] == "Проверить одну тему"
    assert window.effects == []


def test_task_completion_button_can_preempt_chat_after_navigating_to_tasks(app, window):
    window.core.handle("добавь задачу Завершить один шаг")
    task_id = window.core.snapshot()["tasks"][0]["id"]
    begin_chat(app, window)
    window.navigate(1)
    complete = window.findChild(QPushButton, f"completeTask_{task_id}")
    assert complete.isEnabled()
    complete.click()
    wait_for(app, lambda: window.core.snapshot()["tasks"][0]["done"])
    assert not window.model.release.is_set() and window.model.events[0].is_set()
    assert window.effects == []


def test_stop_discards_latest_pending_chat_before_any_model_call(app, window):
    begin_chat(app, window)
    window.submit("Отложенный новый вопрос")
    window.stop_response()
    window.model.release.set()
    wait_for(app, lambda: not window._workers)
    assert window.model.calls == [FIRST] and not window.busy and window._pending_chat is None
    assert window.voice.spoken == [] and window.effects == []


def test_plain_chat_keeps_original_power_confirmation_and_stop_revokes_it(app, window):
    window.submit("выключи ноутбук")
    wait_for(app, lambda: window.confirmation.isVisible() and not window.busy)
    token, prompt = window._confirmation, window.confirm_text.text()
    begin_chat(app, window)
    assert window._confirmation == token and window.confirm_text.text() == prompt
    window.stop_response_button.click()
    wait_for(app, lambda: window._confirmation is None and not window.confirmation.isVisible() and not window._action_request)
    # Drain the explicit nonce revocation on the action pool while chat stays blocked.
    wait_for(app, lambda: window.pool.activeThreadCount() == 0)
    result = window.core.confirm(token, source="button")
    assert "истекло" in result.text and window.effects == []
    window.model.release.set()
    wait_for(app, lambda: not window._workers)
    assert not window.confirmation.isVisible() and window._confirmation is None


def test_chat_final_never_relabels_or_clears_existing_trusted_power_card(app, window):
    window.submit("перезагрузи ноутбук")
    wait_for(app, lambda: window.confirmation.isVisible() and not window.busy)
    token, prompt = window._confirmation, window.confirm_text.text()
    window.submit("Объясни короткую идею")
    wait_for(app, lambda: not window.busy and not window._workers)
    assert window._confirmation == token and window.confirm_text.text() == prompt
    assert window.confirmation.isVisible() and window.effects == []


def test_visible_confirmation_can_preempt_chat_but_still_requires_explicit_button(app, window):
    window.submit("выключи ноутбук")
    wait_for(app, lambda: window.confirmation.isVisible() and not window.busy)
    begin_chat(app, window)
    assert window.effects == []
    window.confirm_button.click()
    wait_for(app, lambda: window.effects == ["shutdown"] and not window.busy)
    assert window.model.events[0].is_set()
    window.model.release.set()
    wait_for(app, lambda: not window._workers)
    assert window.effects == ["shutdown"] and not window.confirmation.isVisible()


def test_second_action_is_refused_without_losing_input_and_stop_never_claims_rollback(app, window):
    entered, release = threading.Event(), threading.Event()
    def execute(action):
        window.effects.append(action)
        entered.set()
        assert release.wait(2)
        return "Действие завершилось после нажатия стоп"
    window.core.executor = execute
    window.submit("открой калькулятор")
    try:
        assert entered.wait(1)
        window.command.setText("  открой блокнот  ")
        window.submit_current()
        assert window.command.text() == "  открой блокнот  "
        assert window.effects == ["calculator"]
        assert window.command.isEnabled() and window.stop_response_button.isEnabled()
        window.stop_response()
        assert "могла выполниться" in window.status.text()
        assert window.busy
    finally:
        release.set()
    wait_for(app, lambda: not window.busy and not window._workers)
    assert window.effects == ["calculator"] and window.voice.spoken == []


def test_asr_and_cold_voice_status_do_not_hide_cancellable_chat_progress(app, window):
    begin_chat(app, window)
    for state in ("Распознаю речь…", "Голос по образцу готовится. Ответ ожидает озвучки."):
        window.voice.on_state(state)
        app.processEvents()
        marker = state + " текстовый фрагмент"
        window.events.stream_chunk.emit(window._active_stream_id, marker)
        wait_for(app, lambda: marker in window.stream_text.text())
        assert window.status.text() == state and window.request_hint.isVisible()
        assert window.command.isEnabled() and window.stop_response_button.isEnabled()
    window.stop_response()


def test_close_cancels_chat_and_waits_actual_lane_exit_before_core_close(app, window, monkeypatch):
    begin_chat(app, window)
    original = window.core.close
    closed = []
    def close():
        assert window._chat_pool.activeThreadCount() == 0 and window.pool.activeThreadCount() == 0
        closed.append(True)
        original()
    monkeypatch.setattr(window.core, "close", close)
    started = time.monotonic()
    window.quit_application()
    assert time.monotonic() - started < .3
    assert window.model.events[0].is_set() and not closed
    window.model.release.set()
    wait_for(app, lambda: window._resources_closed)
    assert closed == [True] and window.voice.spoken == []
