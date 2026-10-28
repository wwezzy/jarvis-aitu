"""Hot ASR mode transitions with owned synthetic listeners; never use devices."""
from collections import deque
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import voice


class Control:
    def __init__(self, value=None):
        self.value, self.enabled, self.visible = value, True, False

    def currentData(self):
        return self.value

    def findData(self, value):
        return value

    def setCurrentIndex(self, value):
        self.value = value

    def setVisible(self, value):
        self.visible = value

    def setEnabled(self, value):
        self.enabled = value

    def setChecked(self, value):
        self.value = value

    def setText(self, value):
        self.value = value

    def text(self):
        return self.value

    def blockSignals(self, _value):
        return False


class Capture:
    """An owned capture that can remain alive after requesting cancellation."""
    def __init__(self):
        self.capture_stopped = True
        self.stop_immediate = True
        self.starts = self.stops = 0
        self.applied = []

    def available(self):
        return {"ready": True}

    def start(self):
        assert self.capture_stopped, "Two capture sessions overlapped"
        self.starts += 1
        self.capture_stopped = False
        return True

    def stop(self):
        self.stops += 1
        if self.stop_immediate:
            self.capture_stopped = True

    def apply_preferences(self, preferences):
        self.applied.append(dict(preferences))

    def prepare(self):
        pass


@pytest.fixture
def ui_harness(monkeypatch):
    pytest.importorskip("PySide6")
    from desktop import ui

    pending = deque()
    monkeypatch.setattr(ui.QTimer, "singleShot", lambda _delay, callback: pending.append(callback))

    class Window:
        _voice_engine_changed = ui.MainWindow._voice_engine_changed
        _resume_voice_after_mode_change = ui.MainWindow._resume_voice_after_mode_change
        _set_checked_silently = staticmethod(ui.MainWindow._set_checked_silently)
        save_preference = ui.MainWindow.save_preference
        stop_listening = ui.MainWindow.stop_listening
        start_listening = ui.MainWindow.start_listening
        toggle_listening = ui.MainWindow.toggle_listening
        _microphone_started = ui.MainWindow._microphone_started
        SETTINGS_PAGE = 6

        def __init__(self, capture=None, *, multilingual=False, engine="windows", enabled=True):
            self.preferences = {"voice_engine": engine, "multilingual_stt_enabled": multilingual,
                                "voice_enabled": enabled, "speech_enabled": False}
            self.voice = capture or Capture()
            self.voice_engine = Control(engine)
            self.voice_character_label, self.voice_character = Control(), Control()
            self.voice_personal_license, self.multilingual_toggle = Control(), Control()
            self.status, self.mic_button, self.voice_sample = Control(), Control(), Control()
            self.orb = SimpleNamespace(active=False, update=lambda: None)
            self.listening = self._capture_requested = self._quitting = self._capture_failed = False
            self._capture_session = 0
            self._capture_error = None
            self.writes, self.heard = [], []
            self.core = SimpleNamespace(store=SimpleNamespace(set_preferences=self.persist))

        def persist(self, updates):
            self.writes.append(dict(updates))
            return self.preferences | updates

        def _cancel_morning_followup(self):
            pass

        def _recognition_callback(self, session_id):
            return lambda text: self.heard.append(text) if session_id == self._capture_session else None

        def _command_callback(self, _session_id):
            return lambda *_: None

        def _run(self, action, completed):
            completed(action())

        def navigate(self, _page):
            pass

        def select_engine(self, engine):
            self.voice_engine.value = engine
            self._voice_engine_changed(0)

    return SimpleNamespace(Window=Window, pending=pending)


def test_openai_transition_restarts_only_the_previously_authorized_capture(ui_harness):
    window = ui_harness.Window()
    window.start_listening()
    old_callback = window.voice.on_text
    window.select_engine("openai")
    assert window.voice.starts == 2 and window.voice.stops == 1 and window.listening
    assert window.preferences["voice_enabled"] is True
    assert window.writes == [{"voice_engine": "openai"}]
    old_callback("obsolete capture")
    window.voice.on_text("current capture")
    assert window.heard == ["current capture"]
    assert window.multilingual_toggle.value is True and not window.multilingual_toggle.enabled
    window.stop_listening()


@pytest.mark.parametrize("multilingual,old,new", [
    (True, "windows", "openai"), (True, "openai", "clone"), (False, "windows", "clone"),
])
def test_engine_changes_with_same_effective_asr_do_not_restart_capture(ui_harness, multilingual, old, new):
    window = ui_harness.Window(multilingual=multilingual, engine=old)
    window.start_listening()
    window.select_engine(new)
    assert window.voice.starts == 1 and window.voice.stops == 0 and window.listening
    window.stop_listening()


@pytest.mark.parametrize("enabled", [False, True])
def test_preference_change_does_not_reenable_a_manually_stopped_microphone(ui_harness, enabled):
    window = ui_harness.Window(enabled=enabled)
    window.select_engine("openai")
    assert window.voice.starts == window.voice.stops == 0
    assert not window.listening and not window._capture_requested and not ui_harness.pending
    assert window.preferences["voice_enabled"] is enabled


def test_delayed_shutdown_is_observed_before_exactly_one_new_listener(ui_harness):
    window = ui_harness.Window()
    window.start_listening()
    window.voice.stop_immediate = False
    window.select_engine("openai")
    assert window.voice.starts == 1 and not window.listening and len(ui_harness.pending) == 1
    ui_harness.pending.popleft()()
    assert window.voice.starts == 1 and len(ui_harness.pending) == 1
    window.voice.capture_stopped = True
    ui_harness.pending.popleft()()
    assert window.voice.starts == 2 and window.listening and not ui_harness.pending
    window.voice.stop_immediate = True
    window.stop_listening()


@pytest.mark.parametrize("cancel", ["stop", "close", "replace_voice"])
def test_delayed_restart_loses_consent_on_user_stop_close_or_voice_replacement(ui_harness, cancel):
    window = ui_harness.Window()
    capture = window.voice
    window.start_listening()
    capture.stop_immediate = False
    window.select_engine("openai")
    if cancel == "stop":
        window.stop_listening()
    elif cancel == "close":
        window._quitting = True
    else:
        window.voice = Capture()
    capture.capture_stopped = True
    ui_harness.pending.popleft()()
    assert capture.starts == 1 and not window.listening and not ui_harness.pending


def test_repeated_engine_choices_while_stopping_keep_latest_mode_and_only_one_restart(ui_harness):
    window = ui_harness.Window()
    window.start_listening()
    window.voice.stop_immediate = False
    window.select_engine("openai")
    window.select_engine("clone")
    window.voice.capture_stopped = True
    while ui_harness.pending:
        ui_harness.pending.popleft()()
    assert window.preferences["voice_engine"] == "clone"
    assert window.voice.starts == 2 and window.listening
    assert window.voice.applied[-1]["voice_engine"] == "clone"
    window.voice.stop_immediate = True
    window.stop_listening()


def test_failed_setting_resumes_old_authorized_mode_without_changing_capture_preference(ui_harness):
    window = ui_harness.Window()
    window.start_listening()
    def reject(_updates):
        raise ValueError("Synthetic invalid preference")
    window.core.store.set_preferences = reject
    window.select_engine("openai")
    assert window.voice_engine.currentData() == window.preferences["voice_engine"] == "windows"
    assert window.listening and window.voice.starts == 2 and window.voice.stops == 1
    assert window.preferences["voice_enabled"] is True and not window.voice.applied


def test_unfinished_old_capture_hits_bounded_deadline_without_restart(ui_harness, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    window = ui_harness.Window()
    window.start_listening()
    window.voice.stop_immediate = False
    window.select_engine("openai")
    now[0] = 110.1
    ui_harness.pending.popleft()()
    assert window.voice.starts == 1 and not window.listening and not ui_harness.pending
    assert "прежний сеанс" in window.status.text().casefold()


def test_effective_openai_mode_is_stable_when_stored_multilingual_flag_is_false():
    service = voice.VoiceService(lambda _: None, lambda _: None)
    try:
        service.apply_preferences({"voice_engine": "openai", "multilingual_stt_enabled": False})
        epoch, generation = service._audio_epoch, service._speech_generation
        service.apply_preferences({"voice_engine": "openai", "multilingual_stt_enabled": False})
        assert service.multilingual_stt_enabled is True
        assert service._audio_epoch == epoch and service._speech_generation == generation
        service.apply_preferences({"voice_engine": "windows", "multilingual_stt_enabled": False})
        assert service.multilingual_stt_enabled is False and service._audio_epoch == epoch + 1
    finally:
        service.close()


@pytest.mark.parametrize("engine,expected", [("openai", []), ("windows", ["открой калькулятор"])])
def test_generic_unprepared_openai_decoder_never_reports_raw_vosk_as_provider_success(engine, expected):
    heard = []
    service = voice.VoiceService(heard.append, lambda _: None)
    service.multilingual_stt_enabled = True
    service.speech_engine, service.speech_enabled = engine, False
    try:
        service._recognized("джарвис открой калькулятор", b"00" * 8000, 16000)
        assert heard == expected and service.diagnostics()["decoder_requests"] == 0
        if engine == "openai":
            assert service.recognition_status()["reason"] == "backend_unavailable"
            assert service.recognition_status()["provider"] is None
    finally:
        service.close()


def test_unprepared_provider_still_allows_an_acoustically_verified_literal_utility():
    heard = []
    service = voice.VoiceService(heard.append, lambda _: None)
    service.apply_preferences({"voice_engine": "openai", "speech_enabled": False})
    try:
        text = "джарвис доброе утро"
        service._recognized(text, b"00" * 8000, 16000,
                            words=[{"word": word, "conf": 1.0} for word in text.split()])
        assert heard == ["доброе утро"]
        assert service.recognition_status()["provider"] == "vosk"
    finally:
        service.close()


def test_capture_stopped_is_a_passive_actual_thread_check():
    service = voice.VoiceService(lambda _: None, lambda _: None)
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(2))
    service._thread = thread
    try:
        assert service.capture_stopped
        thread.start()
        assert not service.capture_stopped
        release.set()
        thread.join(1)
        assert service.capture_stopped
    finally:
        release.set()
        service.close()


def test_actual_listener_restart_prepares_facade_and_english_before_new_capture(ui_harness, monkeypatch, tmp_path):
    lifecycle, backends, capture_count = [], [], [0]
    entered = threading.Semaphore(0)

    class Recognizer:
        def SetWords(self, _value):
            pass

        def Reset(self):
            pass

    class Input:
        def __enter__(self):
            capture_count[0] += 1
            assert capture_count[0] == 1, "Native capture lifetime overlapped"
            lifecycle.append("capture")
            entered.release()
            return self

        def __exit__(self, *_args):
            capture_count[0] -= 1
            lifecycle.append("capture_closed")

    class Decoder:
        reason = "ready"

        def prepare(self):
            lifecycle.append("facade_prepare")
            return True

        def close(self):
            lifecycle.append("facade_closed")

    class English:
        def prepare(self, *_args, **_kwargs):
            lifecycle.append("english_prepare")
            return True

        def close(self):
            lifecycle.append("english_closed")

    def factory():
        backend = Decoder()
        backends.append(backend)
        return backend

    vosk = SimpleNamespace(SetLogLevel=lambda _: None, Model=lambda _: object(),
                           KaldiRecognizer=lambda *_: Recognizer())
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 16000},
                            RawInputStream=lambda **_: Input())
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: vosk if name == "vosk" else sound)
    monkeypatch.setitem(sys.modules, "desktop.english_wake", SimpleNamespace(EnglishWakeDetector=English))
    service = voice.VoiceService(lambda _: None, lambda _: None, tmp_path / "no-model", transcriber_factory=factory)
    service.available = lambda: {"ready": True}
    service.prepare = lambda: None
    service.apply_preferences({"speech_enabled": False})
    window = ui_harness.Window(service)
    try:
        window.start_listening()
        assert entered.acquire(timeout=1) and not backends
        old_generation = service._speech_generation
        window.select_engine("openai")
        assert entered.acquire(timeout=1)
        assert lifecycle[:4] == ["capture", "capture_closed", "facade_prepare", "english_prepare"]
        assert len(backends) == 1 and service._stt is backends[0]
        assert service._speech_generation > old_generation and service.multilingual_stt_enabled
        assert not service.capture_stopped and not ui_harness.pending
    finally:
        window.stop_listening()
        service.close()
    assert capture_count[0] == 0 and service.capture_stopped
    assert lifecycle.count("capture") == lifecycle.count("capture_closed") == 2
    assert "english_closed" in lifecycle


def test_user_stop_during_decoder_preparation_prevents_late_capture(ui_harness, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    capture_attempts = []

    class Decoder:
        reason = "ready"

        def prepare(self):
            entered.set()
            assert release.wait(2)
            return True

        def close(self):
            pass

    vosk = SimpleNamespace(SetLogLevel=lambda _: None, Model=lambda _: object(),
                           KaldiRecognizer=lambda *_: SimpleNamespace(SetWords=lambda _: None))
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 16000},
                            RawInputStream=lambda **_: capture_attempts.append("capture"))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: vosk if name == "vosk" else sound)
    service = voice.VoiceService(lambda _: None, lambda _: None, tmp_path / "no-model", transcriber_factory=Decoder)
    service.available = lambda: {"ready": True}
    service.prepare = lambda: None
    service.apply_preferences({"voice_engine": "openai", "speech_enabled": False})
    window = ui_harness.Window(service, engine="openai")
    stopping = None
    try:
        window.start_listening()
        assert entered.wait(1)
        old_callback = service.on_text
        stopping = threading.Thread(target=window.stop_listening)
        stopping.start()
        assert service._stop.wait(1)
        release.set()
        stopping.join(1)
        assert not stopping.is_alive() and service.capture_stopped
        old_callback("obsolete prepared decoder")
        assert not window.heard and not capture_attempts and not window.listening and not ui_harness.pending
        assert window.preferences["voice_enabled"] is True  # Manual stop preserves the existing preference.
    finally:
        release.set()
        if stopping is not None:
            stopping.join(2)
        service.close()


@pytest.mark.parametrize("reason", ["credit_balance_exhausted", "insufficient_quota", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "rate_limit_exceeded", "slow_down"])
def test_fixed_billing_diagnostics_survive_without_provider_error_text(reason):
    service = voice.VoiceService(lambda _: None, lambda _: None)
    try:
        service._decoder_status(SimpleNamespace(reason=reason, last_error_stage="cloud_decode", last_error_provider="openai"),
                                fallback="transcription_failed", stage="decode")
        assert service.recognition_status()["reason"] == reason
        assert reason not in json.dumps(service.diagnostics())
    finally:
        service.close()
