"""Fence old ASR work after Stop, including work not yet registered by its router.

Real voice/router control flow; synthetic PCM and fake CPU decoder only. No
microphone, audio output, model, GPU, network, owner prompt or PC action is used.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from desktop.ai_routing import FallbackTranscription
from desktop.voice import VoiceService


PCM = b"\x00\x04" * 8000
PHRASE = "джарвис открой ютуб"


class LocalDecoder:
    def __init__(self):
        self.calls = []
        self.result, self.error = "Open YouTube", None
        self.before = lambda: None
        self.last_language, self.last_confidence = "en", .99

    def prepare(self):
        return True

    def transcribe(self, pcm, rate):
        self.calls.append((pcm, rate))
        self.before()
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def recognition(tmp_path):
    heard, states, spoken = [], [], []
    service = VoiceService(heard.append, states.append, tmp_path / "no-model")
    service.speech_enabled = False
    service.say = spoken.append
    service.multilingual_stt_enabled = True
    local = LocalDecoder()
    router = FallbackTranscription(tmp_path / "state", environ={}, local_factory=lambda: local)
    service._stt = router
    yield SimpleNamespace(service=service, router=router, local=local,
                          heard=heard, states=states, spoken=spoken)
    service.close()
    router.close()


@contextmanager
def paused_call(monkeypatch, target, name, function):
    """Pause before the named operation without sleeps or abandoned workers."""
    entered, release = threading.Event(), threading.Event()
    original = getattr(target, name)
    errors, results = [], []

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release the synthetic ASR boundary"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, paused)

    def execute():
        try:
            results.append(function())
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute, name="SyntheticASRCancelReview")
    worker.start()
    try:
        assert entered.wait(3), "Synthetic ASR never reached the requested boundary"
        yield results
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive(), "Synthetic ASR worker failed to exit"
    if errors:
        raise errors[0]


@pytest.mark.parametrize("boundary", ["before_registration", "local_decode"])
def test_interrupt_drops_old_command_even_before_router_can_register_it(recognition, monkeypatch, boundary):
    value = recognition
    target, method = (value.router, "_begin") if boundary == "before_registration" else (value.local, "transcribe")
    with paused_call(monkeypatch, target, method,
                     lambda: value.service._recognized(PHRASE, PCM, 16000)):
        value.service.interrupt()
    assert value.heard == value.spoken == []
    assert value.service.gate.deadline == 0


@pytest.mark.parametrize("change", ["interrupt", "audio_epoch", "decoder", "disable_multilingual"])
def test_direct_decoder_result_is_fenced_by_the_original_capture_request(recognition, monkeypatch, change):
    value = recognition
    with paused_call(monkeypatch, value.router, "_begin",
                     lambda: value.service._decode_command("открой ютуб", PCM, 16000)) as results:
        if change == "interrupt":
            value.service.interrupt()
        elif change == "audio_epoch":
            value.service._audio_epoch += 1
        elif change == "decoder":
            value.service._stt = None
        else:
            value.service.multilingual_stt_enabled = False
    assert results == [None]
    assert value.heard == value.spoken == []


@pytest.mark.parametrize("result", ["Jarvis", "", "Open YouTube", None])
def test_canceled_decode_never_rearms_acknowledges_or_requests_retry(recognition, monkeypatch, result):
    value = recognition
    if result is None:
        value.local.error = RuntimeError("synthetic decoder failure")
    else:
        value.local.result = result
    with paused_call(monkeypatch, value.router, "_begin",
                     lambda: value.service._recognized(PHRASE, PCM, 16000)):
        value.service.interrupt()
        state_count = len(value.states)
    assert value.heard == value.spoken == []
    assert value.service.gate.deadline == 0
    assert value.states[state_count:] == []


def test_confident_alias_does_not_bypass_an_interrupt_at_dispatch(recognition, monkeypatch):
    value = recognition
    words = [{"word": word, "conf": .99} for word in PHRASE.split()]
    assert value.service._confident_alias(PHRASE, "открой ютуб", words)
    with paused_call(monkeypatch, value.service, "_confident_alias",
                     lambda: value.service._recognized(PHRASE, PCM, 16000, words=words)):
        value.service.interrupt()
    assert value.heard == value.spoken == value.local.calls == []


def test_an_explicit_fresh_wake_still_works_after_canceling_old_decode(recognition, monkeypatch):
    value = recognition
    with monkeypatch.context() as scoped:
        with paused_call(scoped, value.router, "_begin",
                         lambda: value.service._recognized(PHRASE, PCM, 16000)):
            value.service.interrupt()
    assert value.heard == []
    value.service._recognized(PHRASE, PCM, 16000)
    assert value.heard == ["Open YouTube"]
    assert value.service.gate.deadline == 0


def test_captured_listener_generation_cannot_start_new_decode_after_stop(recognition):
    value = recognition
    captured_generation = value.service.command_generation
    value.service.interrupt()
    value.service._recognized(PHRASE, PCM, 16000, expected_generation=captured_generation)
    assert value.heard == value.spoken == value.local.calls == []
    assert value.service.gate.deadline == 0


def test_command_callback_carries_only_the_current_generation_and_has_no_legacy_duplicate(recognition):
    value = recognition
    callbacks = []
    value.service._on_command = lambda text, generation: callbacks.append((text, generation))
    old_generation = value.service.command_generation
    value.service.interrupt()
    value.service._emit_command("открой ютуб", old_generation)
    assert callbacks == value.heard == []
    current_generation = value.service.command_generation
    value.service._emit_command("открой ютуб", current_generation)
    assert callbacks == [("открой ютуб", current_generation)] and value.heard == []
    value.service._stop.set()
    value.service._emit_command("открой ютуб", current_generation)
    assert len(callbacks) == 1


@pytest.mark.parametrize("boundary", ["before_decoder_registration", "queued_gui_callback"])
def test_typed_stop_in_the_window_rejects_late_asr_from_the_same_capture_session(tmp_path, monkeypatch, boundary):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6", reason="Optional desktop UI regression")
    from PySide6.QtWidgets import QApplication
    from desktop import owner_auth
    from desktop.core import DesktopCore
    from desktop.ui import MainWindow

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty-home"))
    monkeypatch.setattr(owner_auth, "register_session_notifications", lambda _hwnd: False)
    monkeypatch.setattr(owner_auth, "unregister_session_notifications", lambda _hwnd: None)
    local, effects = LocalDecoder(), []

    class FakeCaptureVoice(VoiceService):
        def __init__(self, on_text, on_state, **_kwargs):
            super().__init__(on_text, on_state, tmp_path / "no-model", on_command=_kwargs.get("on_command"))
            self._stt = FallbackTranscription(tmp_path / "state", environ={}, local_factory=lambda: local)

        def available(self):
            return {"ready": True, "model": True}

        def prepare(self):
            pass

        def start(self):
            return True  # No capture thread or native input device.

        def say(self, text):
            pytest.fail("Speech is disabled in this synthetic capture test")

    core = DesktopCore(tmp_path / "state", executor=lambda action: effects.append(action) or "Fake action")
    core.store.set_preferences({"speech_enabled": False, "multilingual_stt_enabled": True,
                               "close_to_tray": False, "morning_brief_enabled": False,
                               "evening_plan_enabled": False})
    monkeypatch.setattr(core, "due_events", lambda **_kwargs: [])
    monkeypatch.setattr(core, "due_reminders", lambda: [])
    # Keep MainWindow's actual default on_command/Qt generation bridge while
    # substituting only capture/model preparation and all native audio output.
    monkeypatch.setattr("desktop.voice.VoiceService", FakeCaptureVoice)
    app = QApplication.instance() or QApplication([])
    widget = MainWindow(core, enable_tray=False,
                        enable_integrations=False, quiet_start=True, personal_path=tmp_path / "absent.db")

    def wait_for(condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.processEvents()
            if condition():
                return
            time.sleep(.005)
        raise AssertionError("Offscreen cancellation did not settle")

    try:
        widget.start_listening()
        wait_for(lambda: widget.listening)
        capture_session = widget._capture_session
        if boundary == "before_decoder_registration":
            with paused_call(monkeypatch, widget.voice._stt, "_begin",
                             lambda: widget.voice._recognized(PHRASE, PCM, 16000)):
                widget.command.setText("стоп")
                widget.submit_current()
                assert widget.command.text() == ""
        else:
            generation = widget.voice.command_generation
            worker = threading.Thread(target=lambda: widget.voice._emit_command("открой ютуб", generation))
            worker.start()
            worker.join(3)
            assert not worker.is_alive()
            # The callback has been queued to Qt but must not be delivered
            # until after this genuine typed Stop changes its generation.
            assert effects == [] and widget._action_request is None
            widget.command.setText("стоп")
            widget.submit_current()
            assert widget.command.text() == ""
        # Stop cancels response/ASR without disguising this as a mic restart.
        assert widget._capture_session == capture_session and widget.listening
        wait_for(lambda: not widget.busy and not widget._workers)
        app.processEvents()
        assert effects == []
        assert all("ютуб" not in row["text"].casefold() and "youtube" not in row["text"].casefold()
                   for row in core.snapshot()["recent"])
        # This positive control is essential: rejecting every product callback
        # because its session was bound before start would hide a broken mic UI.
        service = widget.voice
        current_generation = service.command_generation
        fresh = threading.Thread(target=lambda: service._emit_command("открой калькулятор", current_generation))
        fresh.start()
        fresh.join(3)
        assert not fresh.is_alive()
        wait_for(lambda: effects == ["calculator"] and not widget.busy)
    finally:
        widget.quit_application()
        wait_for(lambda: widget._resources_closed)
        widget.deleteLater()
