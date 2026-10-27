"""Actual offscreen Qt interactions. No microphone, network or OS effects."""
import os
import time
import threading
import json
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop dependencies are tested in the desktop CI job")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from desktop.core import DesktopCore  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


class FakeVoice:
    def __init__(self, on_text, on_state, model_dir=None):
        self.on_text, self.on_state = on_text, on_state
        self.starts = 0
        self.stops = 0
        self.spoken = []

    def available(self):
        return {"ready": True, "model": True}

    def start(self):
        self.starts += 1
        return True

    def stop(self):
        self.stops += 1

    def say(self, text):
        self.spoken.append(text)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_personal_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-user-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))


@pytest.fixture
def window(app, tmp_path):
    executed = []
    core = DesktopCore(tmp_path, executor=lambda action: executed.append(action) or "Выполнено")
    widget = MainWindow(core, voice_factory=FakeVoice, enable_tray=False, personal_path=tmp_path / "absent.db")
    widget._test_executed = executed
    widget.show()
    app.processEvents()
    yield widget
    widget._quitting = True
    widget.close()
    app.processEvents()


def settled(app, window):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if not window.busy and not window._workers:
            app.processEvents()
            return
        time.sleep(0.01)
    raise AssertionError("Qt worker did not complete")


def test_startup_keeps_microphone_off_and_navigation_simple(app, window):
    assert window.voice.starts == 0
    assert not window.listening
    assert [item.text() for item in window.nav_buttons] == ["Ассистент", "Мои задачи", "Заметки", "Мои данные", "Настройки"]
    window.navigate(1)
    assert window.pages.currentIndex() == 1
    window.navigate(window.SETTINGS_PAGE)
    assert window.pages.currentIndex() == window.SETTINGS_PAGE


def test_real_task_add_and_one_click_completion(app, window):
    window.navigate(1)
    window.task_input.setText("Прочитать главу")
    window.task_add.click()
    settled(app, window)
    task = window.core.snapshot()["tasks"][0]
    assert task["title"] == "Прочитать главу" and not task["done"]
    control = window.findChild(QPushButton, f"completeTask_{task['id']}")
    assert control is not None
    control.click()
    settled(app, window)
    assert window.core.snapshot()["tasks"][0]["done"]
    assert window._test_executed == []


def test_notes_and_chat_render_literal_text(app, window):
    text = '<img src="https://example.test/leak"> Это просто заметка'
    window.note_input.setPlainText(text)
    window.note_save.click()
    settled(app, window)
    assert window.core.snapshot()["notes"][0]["text"] == text
    window.note_search.setText("заметка")
    app.processEvents()
    displayed = [item for item in window.note_layout.parentWidget().findChildren(QLabel) if item.text() == text]
    assert displayed and all(item.textFormat() == Qt.TextFormat.PlainText for item in displayed)
    window.note_search.setText("нет совпадений")
    app.processEvents()
    assert any(item.text() == "Ничего не найдено." for item in window.note_layout.parentWidget().findChildren(QLabel))
    assert not window._test_executed


def test_voice_power_request_requires_visible_button_and_is_one_use(app, window):
    window.mic_button.click()
    settled(app, window)
    window.voice.on_text("выключи ноутбук")
    settled(app, window)
    assert window.confirmation.isVisible()
    assert window._test_executed == []
    token = window._confirmation
    window.confirm_button.click()
    settled(app, window)
    assert window._test_executed == ["shutdown"]
    assert not window.confirmation.isVisible()
    window.core.confirm(token, source="button")
    assert window._test_executed == ["shutdown"]


def test_confirmation_cancel_has_no_effect(app, window):
    window.submit("перезагрузи компьютер")
    settled(app, window)
    assert window.confirmation.isVisible()
    window.cancel_button.click()
    settled(app, window)
    assert not window.confirmation.isVisible()
    assert not window._test_executed


def test_explicit_microphone_toggle_and_voice_setting(app, window):
    window.mic_button.click()
    settled(app, window)
    assert window.voice.starts == 1 and window.listening
    window.mic_button.click()
    assert window.voice.stops == 1 and not window.listening
    window.speech_toggle.setChecked(False)
    window.submit("как дела")
    settled(app, window)
    assert window.voice.spoken == []
    assert window.core.snapshot()["preferences"]["speech_enabled"] is False


def test_local_chat_history_requires_opt_in_and_persists_across_restart(app, tmp_path):
    core = DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action"))
    first = MainWindow(core, voice_factory=FakeVoice, enable_tray=False)
    assert not first.local_chat_history_toggle.isChecked()
    assert first.core.snapshot()["preferences"]["local_chat_history"] is False
    first.local_chat_history_toggle.setChecked(True)
    assert first.core.snapshot()["preferences"]["local_chat_history"] is True
    first._quitting = True
    first.close()
    second = MainWindow(DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action")),
                        voice_factory=FakeVoice, enable_tray=False)
    assert second.local_chat_history_toggle.isChecked()
    second.local_chat_history_toggle.setChecked(False)
    assert second.core.snapshot()["preferences"]["local_chat_history"] is False
    assert second.voice.starts == 0 and second.voice.spoken == []
    second._quitting = True
    second.close()


def test_worker_failure_is_visible_and_does_not_claim_success(app, window, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("sensitive adapter detail")
    monkeypatch.setattr(window.core, "handle", fail)
    window.submit("открой калькулятор")
    settled(app, window)
    assert window.status.text() == "Не удалось выполнить действие"
    assert window.command.isEnabled() and window.send_button.isEnabled()
    assert "sensitive" not in window.status.text()


def test_restart_retains_user_data_but_never_starts_microphone(app, tmp_path):
    first = MainWindow(DesktopCore(tmp_path, executor=lambda _: None), voice_factory=FakeVoice, enable_tray=False)
    first.core.handle("добавь задачу Сохранённая задача")
    first.core.store.set_preferences({"speech_enabled": False})
    first._quitting = True
    first.close()
    second = MainWindow(DesktopCore(tmp_path, executor=lambda _: None), voice_factory=FakeVoice, enable_tray=False)
    assert second.core.snapshot()["tasks"][0]["title"] == "Сохранённая задача"
    assert not second.speech_toggle.isChecked()
    assert second.voice.starts == 0
    second._quitting = True
    second.close()


def test_smoke_entrypoint_does_not_touch_personal_profile(app, tmp_path, monkeypatch):
    # The subprocess-based packaged executable check is performed by the build smoke test.
    # Here we verify isolated construction and screenshot rendering with an injected executor.
    path = tmp_path / "screen.png"
    widget = MainWindow(DesktopCore(tmp_path / "isolated", executor=lambda _: pytest.fail("OS action")), voice_factory=FakeVoice, enable_tray=False)
    widget.show()
    app.processEvents()
    assert widget.grab().save(str(path))
    assert Path(path).stat().st_size > 1000
    assert widget.voice.starts == 0
    widget._quitting = True
    widget.close()


def test_personal_page_reports_historical_copy_and_renders_literal_data(app, tmp_path):
    import sqlite3
    source = tmp_path / "personal.db"
    text = '<a href="https://private.invalid">Важный факт</a>'
    with sqlite3.connect(source) as db:
        db.executescript("""CREATE TABLE users(telegram_id INTEGER,name TEXT); INSERT INTO users VALUES(42,'Test');
            CREATE TABLE memory_facts(id INTEGER,user_id INTEGER,key TEXT,value TEXT);""")
        db.execute("INSERT INTO memory_facts VALUES(1,42,'idea',?)", (text,))
    window = MainWindow(DesktopCore(tmp_path / "app", executor=lambda _: pytest.fail("OS effect")),
                        voice_factory=FakeVoice, enable_tray=False, personal_path=source)
    window.navigate(3)
    window.show()
    app.processEvents()
    assert "исторический" in window.personal_status.text()
    labels = window.personal_layout.parentWidget().findChildren(QLabel)
    matching = [item for item in labels if "Важный факт" in item.text()]
    assert matching and all(item.textFormat() == Qt.TextFormat.PlainText for item in matching)
    assert all("private.invalid" not in item.text() for item in labels)
    assert window.voice.starts == 0
    window._quitting = True
    window.close()


def test_async_capture_failure_resets_controls_and_survives_next_reply(app, window):
    window.mic_button.click()
    settled(app, window)
    assert window.listening
    error = "Голос недоступен (AudioError). Текстовый ввод работает."
    window.voice.on_state(error)
    app.processEvents()
    assert not window.listening and not window.orb.active
    assert window.mic_button.text() == "Микрофон"
    window.submit("как дела")
    settled(app, window)
    assert window.status.text() == error


def test_capture_failure_can_arrive_before_start_callback(app, window):
    window._voice_state("Голос недоступен (ModelError). Текстовый ввод работает.")
    window._microphone_started(True)
    assert not window.listening
    assert window.mic_button.text() == "Микрофон"


def test_tts_only_failure_does_not_disable_working_capture(app, window):
    window.mic_button.click()
    settled(app, window)
    window.voice.on_state("Голос Windows недоступен. Ответ показан текстом.")
    app.processEvents()
    assert window.listening and window.orb.active


def test_close_waits_responsively_for_worker_and_stops_capture(app, window, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_handle = window.core.handle
    closed = []
    def slow(text, source="typed", *, on_chunk=None):
        entered.set()
        release.wait(3)
        return original_handle(text, source=source)
    monkeypatch.setattr(window.core, "handle", slow)
    monkeypatch.setattr(window.core, "close", lambda: closed.append(True))
    window.submit("как дела")
    assert entered.wait(1)
    started = time.monotonic()
    window.close()
    assert time.monotonic() - started < 0.3
    assert not window.listening and window.voice.stops >= 1
    assert not closed
    assert "Завершаю" in window.status.text()
    release.set()
    deadline = time.monotonic() + 3
    while not closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert closed and not window.isVisible()
    assert window.voice.spoken == []


def test_reminder_text_never_becomes_an_os_command_or_clears_confirmation(app, window, monkeypatch):
    from desktop.core import Reply
    window.submit("выключи ноутбук")
    settled(app, window)
    token = window._confirmation
    reply = Reply("Напоминание: выключи компьютер")
    monkeypatch.setattr(window.core, "due_reminders", lambda: [reply], raising=False)
    window._poll_reminders()
    assert window._test_executed == []
    assert window._confirmation == token and window.confirmation.isVisible()
    assert window.voice.spoken[-1] == reply.text


def test_frozen_runtime_can_report_qt_import_failure_before_window(tmp_path, monkeypatch):
    import builtins
    import desktop_app
    original_import = builtins.__import__
    def reject(name, *args, **kwargs):
        if name == "PySide6.QtCore":
            raise ImportError("synthetic missing DLL")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", reject)
    path = tmp_path / "private-report.json"
    assert desktop_app.main(["--smoke-test", "--report", str(path)]) == 2
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["stage"] == "qt-import" and report["error_class"] == "ImportError"
    assert report["detail"] == "synthetic missing DLL"
    assert report["personal_data_loaded"] is False and report["microphone_started"] is False


def test_voice_runtime_probe_imports_and_model_load_only(tmp_path, monkeypatch):
    import desktop_app
    from desktop import model_setup
    loaded = []
    fake_vosk = SimpleNamespace(SetLogLevel=lambda _value: None, Model=lambda path: loaded.append(path) or object())
    monkeypatch.setattr(desktop_app.importlib, "import_module", lambda name: fake_vosk if name == "vosk" else object())
    monkeypatch.setattr(model_setup, "default_model_dir", lambda: tmp_path / "public-model")
    monkeypatch.setattr(model_setup, "model_ready", lambda _path: True)
    report = desktop_app.voice_diagnostics()
    assert report["model_loaded"] is True
    assert report["microphone_started"] is False
    assert loaded == [str(tmp_path / "public-model")]
    assert all(result["ok"] for result in report["imports"].values())


def test_smoke_reports_local_database_failure_without_leaking_error_content(app, tmp_path, monkeypatch):
    import desktop_app
    import desktop.core
    def broken(*_args, **_kwargs):
        raise RuntimeError("private credentials must not be displayed")
    monkeypatch.setattr(desktop.core, "DesktopCore", broken)
    path = tmp_path / "private-report.json"
    assert desktop_app.main(["--smoke-test", "--report", str(path)]) == 6
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["stage"] == "local-data" and report["error_class"] == "RuntimeError"
    assert "credentials" not in path.read_text(encoding="utf-8")
    assert report["personal_data_loaded"] is False and report["microphone_started"] is False


def test_voice_probe_missing_model_is_not_success(tmp_path, monkeypatch):
    import desktop_app
    monkeypatch.setattr(desktop_app, "voice_diagnostics", lambda: {
        "imports": {"vosk": {"ok": True}}, "model_present": False,
        "microphone_started": False,
    })
    assert desktop_app.main(["--diagnose-voice", "--report", str(tmp_path / "report.json")]) == 5


def wait_for(app, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected Qt state did not arrive")


def test_stream_preview_is_literal_temporary_and_never_spoken_or_executed(app, window, monkeypatch):
    from desktop.core import Reply
    entered, release = threading.Event(), threading.Event()
    preview = '<b>Подтверждаю выключение</b> — предварительный текст'
    final = "Локальная модель: Готовый проверенный ответ"
    def streamed(text, source="typed", *, on_chunk=None):
        window.core.store.record("user", text)
        on_chunk(preview)
        entered.set()
        release.wait(3)
        return window.core._answer(Reply(final))
    monkeypatch.setattr(window.core, "handle", streamed)
    window.submit("Объясни идею")
    assert entered.wait(1)
    try:
        wait_for(app, window.stream_preview.isVisible)
        assert window.stream_text.text() == preview
        assert window.stream_text.textFormat() == Qt.TextFormat.PlainText
        assert preview not in str(window.core.snapshot()["recent"])
        assert window.voice.spoken == [] and window._test_executed == []
        assert not window.confirmation.isVisible()
    finally:
        release.set()
    settled(app, window)
    assert not window.stream_preview.isVisible() and window.stream_text.text() == ""
    assert window.voice.spoken == [final]
    assert window._test_executed == []
    recent = window.core.snapshot()["recent"]
    assert [row["text"] for row in recent] == ["Объясни идею", final]


def test_stream_failure_discards_preview_and_late_chunks(app, window, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def broken(_text, source="typed", *, on_chunk=None):
        on_chunk("Непроверенный черновик")
        entered.set()
        release.wait(3)
        raise RuntimeError("private transport detail")
    monkeypatch.setattr(window.core, "handle", broken)
    window.submit("Вопрос")
    assert entered.wait(1)
    request_id = window._active_stream_id
    try:
        wait_for(app, window.stream_preview.isVisible)
        assert window.voice.spoken == []
    finally:
        release.set()
    settled(app, window)
    window.events.stream_chunk.emit(request_id, "поздний старый фрагмент")
    app.processEvents()
    assert not window.stream_preview.isVisible()
    assert window.stream_text.text() == ""
    assert window.status.text() == "Не удалось выполнить действие"
    assert not window.core.snapshot()["recent"]
    assert window.voice.spoken == [] and window._test_executed == []


def test_stream_preview_is_bounded_and_old_request_cannot_append(app, window, monkeypatch):
    from desktop.core import Reply
    entered, release = threading.Event(), threading.Event()
    def streamed(_text, source="typed", *, on_chunk=None):
        on_chunk("x" * (window.MAX_PREVIEW_CHARS + 1000))
        entered.set()
        release.wait(3)
        return Reply("Ответ", speak=False)
    monkeypatch.setattr(window.core, "handle", streamed)
    window.submit("Вопрос")
    assert entered.wait(1)
    request_id = window._active_stream_id
    try:
        wait_for(app, window.stream_preview.isVisible)
        window.events.stream_chunk.emit(request_id - 1, "old")
        window.events.stream_chunk.emit(request_id, "excess")
        app.processEvents()
        assert len(window._stream_buffer) == window.MAX_PREVIEW_CHARS
        assert len(window.stream_text.text()) == window.MAX_PREVIEW_CHARS
        assert window.voice.spoken == []
    finally:
        release.set()
    settled(app, window)


def test_deterministic_action_still_runs_once_without_preview(app, window):
    window.submit("открой калькулятор")
    settled(app, window)
    assert window._test_executed == ["calculator"]
    assert not window.stream_preview.isVisible()
    assert len([row for row in window.core.snapshot()["recent"] if row["role"] == "user"]) == 1


def test_voice_engine_tempo_sample_and_cleanup_are_explicit_and_persistent(app, tmp_path):
    class PreferenceVoice(FakeVoice):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.applied = []
            self.prepared = 0
            self.closed = 0
        def apply_preferences(self, preferences):
            self.applied.append(dict(preferences))
        def prepare(self):
            self.prepared += 1
        def close(self):
            self.closed += 1
    widget = MainWindow(DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action")),
                        voice_factory=PreferenceVoice, enable_tray=False)
    assert widget.voice.prepared == 1 and widget.voice.starts == 0
    assert widget.voice.spoken == []
    widget.voice_engine.setCurrentIndex(widget.voice_engine.findData("windows"))
    widget.voice_rate.setValue(195)
    widget.voice_rate.setValue(200)
    widget.voice_rate.setValue(205)
    wait_for(app, lambda: widget.core.snapshot()["preferences"]["voice_rate"] == 205)
    prefs = widget.core.snapshot()["preferences"]
    assert prefs["voice_engine"] == "windows" and prefs["voice_rate"] == 205
    assert widget.voice.applied[-1]["voice_rate"] == 205
    assert widget.voice.prepared == 3
    widget.voice_sample.click()
    assert widget.voice.spoken == ["К вашим услугам, сэр. С чего начнём?"]
    assert widget.voice.starts == 0
    widget.speech_toggle.setChecked(False)
    assert not widget.voice_sample.isEnabled()
    widget.voice_rate.setValue(180)
    widget._quitting = True
    widget.close()
    assert widget.voice.closed == 1
    second = MainWindow(DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action")),
                        voice_factory=PreferenceVoice, enable_tray=False)
    assert second.voice_engine.currentData() == "windows"
    assert second.voice_rate.value() == 180 and not second.voice_sample.isEnabled()
    assert second.voice.starts == 0 and second.voice.spoken == []
    second._quitting = True
    second.close()


def test_settings_remain_scrollable_to_bottom_at_minimum_window_size(app, window):
    window.resize(860, 620)
    window.navigate(window.SETTINGS_PAGE)
    app.processEvents()
    scroll = window.pages.currentWidget()
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.ensureWidgetVisible(window.chat_model)
    app.processEvents()
    position = window.chat_model.mapTo(scroll.viewport(), window.chat_model.rect().topLeft())
    assert 0 <= position.y() < scroll.viewport().height()
    assert position.y() + window.chat_model.height() <= scroll.viewport().height()


def test_reported_voice_backend_can_differ_from_auto_preference(app, window, monkeypatch):
    monkeypatch.setattr(window.voice, "available", lambda: {"ready": True, "model": True, "speech_backend": "windows"})
    window.refresh()
    assert window.voice_engine.currentData() == "auto"
    assert window.voice_backend_status.text() == "Используется установленный голос Windows."
    monkeypatch.setattr(window.voice, "available", lambda: {"ready": True, "model": True, "speech_backend": "neural"})
    window.voice.on_state("Говорю…")
    app.processEvents()
    assert window.voice_backend_status.text() == "Используется локальный нейроголос."


def test_new_request_interrupts_old_speech_without_new_actions(app, window, monkeypatch):
    interrupted = []
    monkeypatch.setattr(window.voice, "interrupt", lambda: interrupted.append(True), raising=False)
    window.submit("как дела")
    settled(app, window)
    assert interrupted == [True]
    assert window._test_executed == []


def test_close_requests_stream_cancellation_and_does_not_speak_partial(app, window, monkeypatch):
    from desktop.core import Reply
    entered, cancelled = threading.Event(), threading.Event()
    def streaming(_text, source="typed", *, on_chunk=None):
        on_chunk("Начало предварительного ответа")
        entered.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if on_chunk(" следующий фрагмент") is False:
                cancelled.set()
                raise RuntimeError("cancelled")
            time.sleep(0.01)
        return Reply("Stream did not stop")
    monkeypatch.setattr(window.core, "handle", streaming)
    window.submit("Вопрос")
    assert entered.wait(1)
    window.close()
    wait_for(app, lambda: cancelled.is_set() and not window.isVisible(), timeout=1.5)
    assert window._resources_closed
    assert window.voice.spoken == [] and window._test_executed == []
    assert window.stream_text.text() == ""


def test_late_recognition_after_stop_or_restart_cannot_dispatch(app, window):
    window.mic_button.click()
    settled(app, window)
    old_callback = window.voice.on_text
    window.stop_listening()
    old_callback("открой калькулятор")
    app.processEvents()
    assert not window.busy and window._test_executed == []
    window.mic_button.click()
    settled(app, window)
    assert window.listening
    old_callback("открой блокнот")
    app.processEvents()
    assert not window.busy and window._test_executed == []
    window.voice.on_text("открой калькулятор")
    settled(app, window)
    assert window._test_executed == ["calculator"]


def test_recognition_is_rejected_before_activation_and_during_shutdown(app, window):
    window._voice_command("открой калькулятор")
    assert not window.busy and window._test_executed == []
    window.mic_button.click()
    settled(app, window)
    window._quitting = True
    window.voice.on_text("открой калькулятор")
    app.processEvents()
    assert not window.busy and window._test_executed == []


def test_auto_listen_opt_in_starts_now_and_manual_stop_keeps_preference(app, window):
    assert not window.auto_listen_toggle.isChecked() and not window.listening
    window.auto_listen_toggle.setChecked(True)
    settled(app, window)
    assert window.listening and window.voice.starts == 1
    assert window.core.snapshot()["preferences"]["voice_enabled"] is True
    window.stop_listening()
    assert not window.listening and window.auto_listen_toggle.isChecked()
    assert window.core.snapshot()["preferences"]["voice_enabled"] is True
    window.auto_listen_toggle.setChecked(False)
    assert not window.listening
    assert window.core.snapshot()["preferences"]["voice_enabled"] is False


def test_stored_auto_listen_works_without_tray_or_manual_morning_click(app, tmp_path):
    core = DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action"))
    core.store.set_preferences({"voice_enabled": True})
    widget = MainWindow(core, voice_factory=FakeVoice, enable_tray=False)
    widget.show()
    wait_for(app, lambda: widget.listening)
    assert widget.voice.starts == 1 and widget.auto_listen_toggle.isChecked()
    widget._quitting = True
    widget.close()


def test_close_before_auto_listen_callback_does_not_start_microphone(app, tmp_path):
    core = DesktopCore(tmp_path, executor=lambda _: pytest.fail("OS action"))
    core.store.set_preferences({"voice_enabled": True})
    widget = MainWindow(core, voice_factory=FakeVoice, enable_tray=False)
    widget._quitting = True
    widget.close()
    app.processEvents()
    assert widget.voice.starts == 0


def test_auto_listen_opt_out_stops_capture_even_if_preference_write_fails(app, window, monkeypatch):
    window.auto_listen_toggle.setChecked(True)
    settled(app, window)
    assert window.listening
    def broken(_updates):
        raise OSError("private disk error")
    monkeypatch.setattr(window.core.store, "set_preferences", broken)
    window.auto_listen_toggle.setChecked(False)
    assert not window.listening
    assert window.auto_listen_toggle.isChecked()  # honest: saved preference remains enabled
    assert "Микрофон выключен" in window.status.text()
    assert "private" not in window.status.text()


def test_auto_listen_opt_out_invalidates_a_start_queued_behind_chat(app, window, monkeypatch):
    from desktop.core import Reply
    entered, release = threading.Event(), threading.Event()
    def slow(_text, source="typed", *, on_chunk=None):
        entered.set()
        release.wait(3)
        return Reply("Готово", speak=False)
    monkeypatch.setattr(window.core, "handle", slow)
    window.submit("Вопрос")
    assert entered.wait(1)
    window.auto_listen_toggle.setChecked(True)
    assert window._capture_requested
    window.auto_listen_toggle.setChecked(False)
    release.set()
    settled(app, window)
    assert window.voice.starts == 0 and not window.listening
    assert window.core.snapshot()["preferences"]["voice_enabled"] is False


def startup_backend(monkeypatch, function):
    module = ModuleType("desktop.startup")
    module.set_startup_enabled = function
    monkeypatch.setitem(sys.modules, "desktop.startup", module)


def test_startup_preference_is_saved_only_after_verified_backend_success(app, window, monkeypatch):
    calls = []
    startup_backend(monkeypatch, lambda enabled: calls.append(enabled) or SimpleNamespace(success=True, message="Готово"))
    assert window.core.snapshot()["preferences"]["launch_at_login"] is False
    window.launch_at_login_toggle.setChecked(True)
    settled(app, window)
    assert calls == [True]
    assert window.core.snapshot()["preferences"]["launch_at_login"] is True
    assert window.launch_at_login_toggle.isChecked() and window.launch_at_login_toggle.isEnabled()
    window.launch_at_login_toggle.setChecked(False)
    settled(app, window)
    assert calls == [True, False]
    assert window.core.snapshot()["preferences"]["launch_at_login"] is False
    assert window.voice.starts == 0 and window._test_executed == []


def test_startup_failure_restores_checkbox_without_persisting_success(app, window, monkeypatch):
    startup_backend(monkeypatch, lambda _enabled: SimpleNamespace(success=False, message="Автозапуск недоступен"))
    window.launch_at_login_toggle.setChecked(True)
    settled(app, window)
    assert window.core.snapshot()["preferences"]["launch_at_login"] is False
    assert not window.launch_at_login_toggle.isChecked() and window.launch_at_login_toggle.isEnabled()
    assert window.status.text() == "Автозапуск недоступен"


def test_startup_rolls_back_owned_setting_if_local_preference_cannot_be_saved(app, window, monkeypatch):
    calls = []
    startup_backend(monkeypatch, lambda enabled: calls.append(enabled) or SimpleNamespace(success=True, message="Готово"))
    def broken(_updates):
        raise OSError("private disk error")
    monkeypatch.setattr(window.core.store, "set_preferences", broken)
    window.launch_at_login_toggle.setChecked(True)
    settled(app, window)
    assert calls == [True, False]
    assert window.core.snapshot()["preferences"]["launch_at_login"] is False
    assert not window.launch_at_login_toggle.isChecked()
    assert "возвращён" in window.status.text()
    assert "private" not in window.status.text()


def test_startup_exception_has_safe_error_and_restores_controls(app, window, monkeypatch):
    def broken(_enabled):
        raise RuntimeError("private command detail")
    startup_backend(monkeypatch, broken)
    window.launch_at_login_toggle.setChecked(True)
    settled(app, window)
    assert not window.launch_at_login_toggle.isChecked() and window.launch_at_login_toggle.isEnabled()
    assert window.core.snapshot()["preferences"]["launch_at_login"] is False
    assert "private" not in window.status.text()
