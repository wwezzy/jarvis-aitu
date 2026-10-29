"""Memory permission commits cannot race a new model or document request."""
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


FACT = "SYNTHETIC_OWNER_FACT_TO_FORGET"
QUESTION = "Объясни полезный следующий шаг в обучении"


class Voice:
    def __init__(self, *_args, **_kwargs):
        self.spoken = []

    def available(self):
        return {"ready": True, "model": True}

    def say(self, text):
        self.spoken.append(text)

    def stop(self):
        pass

    def interrupt(self):
        pass

    def close(self):
        pass


class Model:
    last_usage = None

    def __init__(self):
        self.calls = []

    def answer(self, question, **options):
        self.calls.append((question, options))
        return "Синтетический ответ без действий."

    def close(self):
        pass


def wait(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.005)
    raise AssertionError("Expected memory lifecycle state did not arrive")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from desktop import owner_auth
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty-home"))
    monkeypatch.setattr(owner_auth, "register_session_notifications", lambda *_: False)
    monkeypatch.setattr(owner_auth, "unregister_session_notifications", lambda *_: None)
    model, effects, releases = Model(), [], []
    monkeypatch.setattr("desktop.chat.LocalChat", lambda *_args, **_kwargs: model)
    core = DesktopCore(tmp_path / "state", executor=lambda action: effects.append(action) or "Выполнено")
    core.store.set_preferences({"close_to_tray": False, "free_mode": True, "gemini_enabled": False,
                                "local_chat_enabled": True, "personal_context_enabled": True})
    core.memory.upsert("preference", FACT)
    core.due_events = lambda **_kwargs: []
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False, enable_integrations=False,
                        quiet_start=True, personal_path=tmp_path / "absent.db")
    widget.model, widget.effects, widget.test_releases = model, effects, releases
    yield widget
    for release in releases:
        release.set()
    wait(app, lambda: not widget._workers)
    widget.quit_application()
    wait(app, lambda: widget._resources_closed)
    widget.deleteLater()


def block(window, monkeypatch, target, name):
    entered, release = threading.Event(), threading.Event()
    window.test_releases.append(release)
    original = getattr(target, name)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Synthetic blocked commit timed out"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, blocked)
    return entered, release


def assert_model_admission_blocked(window):
    assert not window.submit(QUESTION)
    assert not window.submit(QUESTION, source="voice")
    assert not window.submit("открой калькулятор", attachment_ids=["a" * 32])
    assert not window.model.calls


def test_blocked_forget_does_not_send_old_fact_and_keeps_draft_and_local_actions(app, window, monkeypatch):
    entered, release = block(window, monkeypatch, window.core.memory, "delete")
    window.memory_panel._mutate("delete", "preference")
    assert entered.wait(1) and window._memory_mutations == 1
    assert_model_admission_blocked(window)
    window.command.setText(QUESTION)
    window.submit_current()
    assert window.command.text() == QUESTION and not window.model.calls

    # Memory I/O occupies its own lane; a deterministic user command stays usable.
    assert window.submit("открой калькулятор")
    wait(app, lambda: window.effects == ["calculator"])
    assert not window.model.calls and window._memory_mutations == 1

    release.set()
    wait(app, lambda: not window._workers and window._memory_mutations == 0)
    assert not window.core.memory.list_facts()
    window.submit_current()
    wait(app, lambda: len(window.model.calls) == 1 and not window._workers)
    assert window.command.text() == "" and FACT not in str(window.model.calls)


@pytest.mark.parametrize("enabled", [False, True])
def test_context_permission_transition_blocks_model_until_both_gates_commit(app, window, monkeypatch, enabled):
    if enabled:
        window.core.memory.set_enabled(False)
        window.core.store.set_preferences({"personal_context_enabled": False})
        window.preferences = window.core.store.preferences()
        window._set_checked_silently(window.personal_context_toggle, False)
    entered, release = block(window, monkeypatch, window.core.memory, "set_enabled")
    window.personal_context_toggle.setChecked(enabled)
    assert entered.wait(1) and window._memory_mutations == 1
    assert not window.personal_context_toggle.isEnabled()
    assert_model_admission_blocked(window)
    release.set()
    wait(app, lambda: not window._workers and window._memory_mutations == 0)
    assert window.personal_context_toggle.isEnabled()
    assert window.core.store.preferences()["personal_context_enabled"] is enabled
    assert window.core.memory.status()["enabled"] is enabled
    assert window.submit(QUESTION)
    wait(app, lambda: len(window.model.calls) == 1 and not window._workers)
    assert (FACT in str(window.model.calls)) is enabled


def test_failed_forget_unblocks_requests_without_claiming_fact_was_deleted(app, window, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    window.test_releases.append(release)

    def fail(_key):
        entered.set()
        assert release.wait(3)
        raise OSError("PRIVATE_SYNTHETIC_STORAGE_DETAIL")

    monkeypatch.setattr(window.core.memory, "delete", fail)
    window.memory_panel._mutate("delete", "preference")
    assert entered.wait(1)
    assert_model_admission_blocked(window)
    release.set()
    wait(app, lambda: not window._workers and window._memory_mutations == 0)
    assert window.core.memory.list_facts()[0]["value"] == FACT
    assert "Не удалось" in window.memory_panel.status.text()
    assert "PRIVATE" not in window.memory_panel.status.text()
    assert window.submit(QUESTION)
    wait(app, lambda: len(window.model.calls) == 1 and not window._workers)


def test_partial_disable_failure_leaves_export_revoked_and_releases_admission(app, window, monkeypatch):
    original = window.core.store.set_preferences

    def fail_preference(values):
        if values == {"personal_context_enabled": False}:
            raise OSError("PRIVATE_SYNTHETIC_COMMIT_DETAIL")
        return original(values)

    monkeypatch.setattr(window.core.store, "set_preferences", fail_preference)
    window.personal_context_toggle.setChecked(False)
    wait(app, lambda: not window._workers and window._memory_mutations == 0)
    assert window.core.store.preferences()["personal_context_enabled"] is True
    assert window.core.memory.status()["enabled"] is False
    assert not window.personal_context_toggle.isChecked()
    assert "Не удалось" in window.status.text() and "PRIVATE" not in window.status.text()
    assert window.submit(QUESTION)
    wait(app, lambda: len(window.model.calls) == 1 and not window._workers)
    assert FACT not in str(window.model.calls)


def test_first_of_two_pending_memory_commits_cannot_release_model_admission(app, window, monkeypatch):
    window.core.memory.upsert("second", "SECOND_SYNTHETIC_FACT")
    original = window.core.memory.delete
    entered = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    window.test_releases.extend(releases)

    def delete(key):
        index = 0 if key == "preference" else 1
        entered[index].set()
        assert releases[index].wait(3)
        return original(key)

    monkeypatch.setattr(window.core.memory, "delete", delete)
    window.memory_panel._mutate("delete", "preference")
    window.memory_panel._mutate("delete", "second")
    assert entered[0].wait(1) and window._memory_mutations == 2
    releases[0].set()
    wait(app, lambda: entered[1].is_set() and window._memory_mutations == 1)
    assert_model_admission_blocked(window)
    releases[1].set()
    wait(app, lambda: not window._workers and window._memory_mutations == 0)
    assert not window.core.memory.list_facts()
    assert window.submit(QUESTION)
    wait(app, lambda: len(window.model.calls) == 1 and not window._workers)
    assert FACT not in str(window.model.calls) and "SECOND_SYNTHETIC_FACT" not in str(window.model.calls)
