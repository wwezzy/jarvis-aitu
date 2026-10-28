"""Reply-to-selected-voice regressions with offscreen Qt and fake audio.

No microphone, Windows Hello prompt, media file, model, GPU or OS action is used.
"""
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop reply UI tests run in the desktop job")
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from desktop.core import DesktopCore, Reply  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


class Voice:
    def __init__(self, on_text, on_state, model_dir=None):
        self.on_text, self.on_state = on_text, on_state
        self.spoken, self.cues, self.preferences = [], [], {}
        self.cue_result = False
        self.starts = 0

    def apply_preferences(self, preferences):
        self.preferences = dict(preferences)

    def available(self):
        return {"ready": True, "model": True, "speech_backend": self.preferences.get("voice_engine", "clone")}

    def start(self):
        self.starts += 1
        return True

    def stop(self):
        pass

    def say(self, text):
        self.spoken.append((text, self.preferences.get("voice_engine"), threading.get_ident()))

    def play_cue(self, name):
        self.cues.append(name)
        if isinstance(self.cue_result, Exception):
            raise self.cue_result
        return self.cue_result


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "isolated-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "isolated-home"))
    effects, model_calls = [], []
    core = DesktopCore(tmp_path / "state", executor=lambda action: effects.append(action) or "Fake action")
    core.store.set_preferences({"voice_engine": "clone", "speech_enabled": True,
                               "morning_brief_enabled": False, "evening_plan_enabled": False,
                               "local_chat_enabled": True})
    core.owner._session_check = lambda: pytest.fail("No native owner/session probe")
    monkeypatch.setattr(core, "due_events", lambda **_kwargs: [])
    monkeypatch.setattr(core, "due_reminders", lambda: [])
    def no_model(*_args, **_kwargs):
        model_calls.append(True)
        raise AssertionError("Deterministic reply must not call a model")
    monkeypatch.setattr("desktop.chat.LocalChat", no_model)
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False, personal_path=tmp_path / "absent.db", quiet_start=True)
    widget.effects, widget.model_calls = effects, model_calls
    widget.show()
    app.processEvents()
    yield widget
    widget._quitting = True
    widget.close()
    deadline = time.monotonic() + 3
    while not widget._resources_closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert widget._resources_closed


def settled(app, window):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if not window.busy and not window._workers:
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("Fake reply worker did not settle")


@pytest.mark.parametrize("phrase,fragment", [
    ("как дела", "На связи"), ("Jarvis, how are you?", "На связи"),
    ("что ты умеешь?", "Могу"), ("what are your capabilities", "Могу"),
    ("открой что-нибудь", "Укажи одно действие"),
    ("выключи непонятно что", "Уточни действие"),
    ("добавь задачу Разобрать одну тему", "Добавил задачу"),
])
def test_typed_deterministic_and_clarification_replies_use_selected_voice(app, window, phrase, fragment):
    window.submit(phrase)
    settled(app, window)
    assert len(window.voice.spoken) == 1
    text, engine, thread = window.voice.spoken[0]
    assert fragment in text and engine == "clone" and thread == threading.get_ident()
    assert window.effects == window.model_calls == [] and window.voice.cues == []


def test_recognized_original_voice_command_gets_same_selected_reply_dispatch(app, window):
    window.start_listening()
    settled(app, window)
    assert window.listening and window.voice.starts == 1
    window.voice.on_text("что ты умеешь")
    settled(app, window)
    assert len(window.voice.spoken) == 1 and "Могу" in window.voice.spoken[0][0]
    assert window.voice.spoken[0][1] == "clone"
    assert window.effects == window.model_calls == []


@pytest.mark.parametrize("speech_enabled,speak", [(False, True), (True, False), (False, False)])
def test_explicit_mute_or_speak_false_policy_suppresses_text_and_cue(app, window, speech_enabled, speak):
    window.speech_toggle.setChecked(speech_enabled)
    window._reply(Reply("Только текст по выбранной политике.", speak=speak, media_cue="welcome_home"))
    assert window.voice.spoken == window.voice.cues == []
    assert window.effects == window.model_calls == []


def test_safe_owner_prompt_is_audible_without_history_or_auth_side_effects(app, window):
    window.core.set_owner_required(True)
    window.submit("как дела")
    settled(app, window)
    assert len(window.voice.spoken) == 1 and "Windows Hello" in window.voice.spoken[0][0]
    assert window.core.snapshot()["recent"] == []
    assert window.effects == window.model_calls == []


def test_unrecorded_owner_prompt_remains_visible_when_speech_is_disabled(app, window):
    window.core.set_owner_required(True)
    window.speech_toggle.setChecked(False)
    window.submit("как дела")
    settled(app, window)
    assert any("Windows Hello" in label.text() for label in window.history_scroll.findChildren(QLabel))
    assert window.core.snapshot()["recent"] == [] and not window.voice.spoken


def test_worker_failure_speaks_fixed_feedback_without_database_refresh(app, window, monkeypatch):
    calls = []
    def unavailable(*_args, **_kwargs):
        raise OSError("PRIVATE_ERROR_PATH_AND_SECRET")
    def forbidden_refresh(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Error handler must not retry broken database")
    monkeypatch.setattr(window.core, "handle", unavailable)
    monkeypatch.setattr(window.core, "snapshot", forbidden_refresh)
    monkeypatch.setattr(window, "refresh", forbidden_refresh)
    window.submit("как дела")
    settled(app, window)
    assert calls == [] and len(window.voice.spoken) == 1
    text, engine, _thread = window.voice.spoken[0]
    assert "Не удалось выполнить действие" in text and "PRIVATE" not in text and engine == "clone"
    assert window.status.text() == "Не удалось выполнить действие"
    assert window.command.isEnabled() and window.send_button.isEnabled() and window.mic_button.isEnabled()
    assert not window._confirmation and window.stream_text.text() == ""


@pytest.mark.parametrize("phrase", ["Jarvis wake up daddy’s home", "JARVIS, WAKE UP, DADDY'S HOME!",
    "wake up daddys home", "Джарвис, wake up daddy's home"])
def test_exact_welcome_phrase_is_a_finite_cue_with_no_model_or_os_action(window, phrase):
    reply = window.core.handle(phrase)
    assert reply.media_cue == "welcome_home" and reply.text == "Добро пожаловать домой, сэр."
    assert reply.speak and reply.action is None and reply.confirmation_id is None
    assert window.effects == window.model_calls == []


@pytest.mark.parametrize("phrase", ['Say "wake up daddy\'s home"', "don't wake up daddy's home",
    "wake up daddy's home and open calculator", "цитата «Jarvis wake up daddy's home»"])
def test_quotes_negations_and_compound_requests_do_not_trigger_welcome(window, phrase):
    window.core.store.set_preferences({"local_chat_enabled": False})
    reply = window.core.handle(phrase)
    assert reply.media_cue is None and reply.action is None and reply.confirmation_id is None
    assert window.effects == window.model_calls == []


def test_ui_queues_only_the_named_available_cue_without_double_speaking(app, window):
    window.voice.cue_result = True
    window.submit("Jarvis wake up daddy’s home")
    settled(app, window)
    assert window.voice.cues == ["welcome_home"] and window.voice.spoken == []
    assert window.effects == window.model_calls == []


@pytest.mark.parametrize("result", [False, None, 1, RuntimeError("private cue failure")])
def test_unavailable_or_failed_cue_falls_back_to_selected_text_voice(app, window, result):
    window.voice.cue_result = result
    window.submit("Jarvis wake up daddy’s home")
    settled(app, window)
    assert window.voice.cues == ["welcome_home"]
    assert len(window.voice.spoken) == 1
    assert window.voice.spoken[0][:2] == ("Добро пожаловать домой, сэр.", "clone")
    assert window.effects == window.model_calls == []


def test_voice_backend_without_optional_cue_api_still_speaks_selected_text(app, window, monkeypatch):
    monkeypatch.setattr(window.voice, "play_cue", None)
    window.submit("Jarvis wake up daddy’s home")
    settled(app, window)
    assert not window.voice.cues and window.voice.spoken[0][:2] == ("Добро пожаловать домой, сэр.", "clone")


@pytest.mark.parametrize("cue", ["../clip.wav", "https://example.invalid/audio", "not_registered"])
def test_arbitrary_cue_names_are_never_sent_to_media_backend(window, cue):
    window.voice.cue_result = True
    window._reply(Reply("Безопасный текст ответа.", media_cue=cue))
    assert not window.voice.cues and window.voice.spoken[0][:2] == ("Безопасный текст ответа.", "clone")


def test_old_reply_objects_without_cue_metadata_still_work(window):
    reply = SimpleNamespace(text="Совместимый ответ.", action=None, confirmation_id=None, speak=True)
    window._reply(reply)
    assert not window.voice.cues and window.voice.spoken[0][:2] == ("Совместимый ответ.", "clone")


def test_expired_owner_confirmation_has_visible_and_selected_voice_feedback(window, monkeypatch):
    window.core.owner.set_enabled(True)
    monkeypatch.setattr(window.core.owner, "allowed", lambda: False)
    reply = window.core.confirm("a" * 32)
    window._reply(reply)
    assert reply.speak is True
    assert window.voice.spoken[0][:2] == (reply.text, "clone")
    assert "Windows Hello" in reply.text and window.effects == []
