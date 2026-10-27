"""Actual offscreen Qt interactions. No microphone, network or OS effects."""
import os
import time
import threading
import json
from types import SimpleNamespace
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
    def slow(text, source="typed"):
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
