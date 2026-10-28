"""Adversarial OpenAI integration: fake audio/providers, no credentials or API."""
import os
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from desktop import voice


def worker_for(factory, states):
    service = voice.VoiceService(lambda *_args: None, states.append, speech_factory=factory)
    service.speech_engine = "openai"
    worker = threading.Thread(target=service._speak_loop, daemon=True)
    service._speaker = worker
    worker.start()
    return service, worker


@pytest.mark.parametrize("change", ["interrupt", "mute", "switch"])
def test_canceled_openai_prepare_cannot_overwrite_current_voice_status(change):
    entered, release = threading.Event(), threading.Event()
    states = []
    class Speech:
        def prepare(self, *, rate):
            entered.set()
            assert release.wait(2)
            return False

        def close(self):
            pass
    service, worker = worker_for(Speech, states)
    try:
        service._speech.put((service._speech_generation, "obsolete answer"))
        assert entered.wait(1)
        if change == "interrupt":
            service.interrupt()
        elif change == "mute":
            service.apply_preferences({"speech_enabled": False})
        else:
            service.apply_preferences({"voice_engine": "windows"})
        release.set()
        service._speech.put(None)
        worker.join(2)
        assert not worker.is_alive()
        assert not any("OpenAI недоступен" in state for state in states)
        assert service.speech_backend != "openai_unavailable"
    finally:
        release.set()
        service.close()
        worker.join(1)


@pytest.mark.parametrize("failure_after_pcm", [False, True])
def test_openai_stream_error_never_starts_an_alternate_voice_or_repeats_text(monkeypatch, failure_after_pcm):
    states, imports, writes, rendered = [], [], [], []
    closed, finished = threading.Event(), threading.Event()
    class Speech:
        def prepare(self, *, rate):
            return True

        def stream(self, text, *, rate, cancel):
            rendered.append(text)
            try:
                if failure_after_pcm:
                    yield b"00" * 128, 24000
                raise RuntimeError("PRIVATE provider error /owner/key.env")
            finally:
                finished.set()

        def close(self):
            closed.set()
    class Output:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def write(self, pcm):
            writes.append(pcm)
    def fake_import(name):
        imports.append(name)
        assert name == "sounddevice", "Selected OpenAI voice attempted a different renderer"
        return SimpleNamespace(RawOutputStream=Output)
    monkeypatch.setattr(voice.importlib, "import_module", fake_import)
    service, worker = worker_for(Speech, states)
    try:
        service._speech.put((service._speech_generation, "one answer"))
        assert finished.wait(1)
        service._speech.put(None)
        worker.join(2)
        assert not worker.is_alive() and closed.is_set()
        assert rendered == ["one answer"]
        assert len(writes) == int(failure_after_pcm)
        assert imports == (["sounddevice"] if failure_after_pcm else [])
        assert all("PRIVATE" not in state and "key.env" not in state for state in states)
        assert any("прервалось" in state if failure_after_pcm else "недоступен" in state for state in states)
    finally:
        service.close()
        worker.join(1)


def test_interrupt_after_first_pcm_closes_generator_and_writes_no_late_chunk(monkeypatch):
    finished, canceled = threading.Event(), threading.Event()
    writes = []
    class Speech:
        def prepare(self, *, rate):
            return True

        def stream(self, text, *, rate, cancel):
            try:
                yield b"00" * 128, 24000
                if cancel():
                    canceled.set()
                    return
                yield b"11" * 128, 24000
            finally:
                finished.set()

        def close(self):
            pass
    class Output:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def write(self, pcm):
            writes.append(pcm)
            service.interrupt()

        def abort(self):
            pass
    def fake_import(name):
        assert name == "sounddevice"
        return SimpleNamespace(RawOutputStream=Output)
    monkeypatch.setattr(voice.importlib, "import_module", fake_import)
    service, worker = worker_for(Speech, [])
    try:
        service._speech.put((service._speech_generation, "one answer"))
        assert finished.wait(1) and canceled.is_set()
        service._speech.put(None)
        worker.join(2)
        assert not worker.is_alive() and writes == [b"00" * 128]
    finally:
        service.close()
        worker.join(1)


def test_default_main_window_factories_share_live_explicit_environment_without_eager_clients(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from desktop import ai_routing, owner_auth
    from desktop.core import DesktopCore
    from desktop.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    clients, services = [], []
    class Speech:
        def __init__(self, *, state_dir, environ):
            clients.append(("speech", state_dir, environ))
    class Decoder:
        def __init__(self, state_dir, *, environ):
            clients.append(("stt", state_dir, environ))
    class FakeVoice:
        def __init__(self, on_text, on_state, model_dir=None, **options):
            self.options = options
            services.append(self)

        def apply_preferences(self, preferences):
            self.preferences = preferences

        def prepare(self):
            pass

        def available(self):
            return {"ready": True, "speech_backend": "openai_unavailable"}

        def close(self):
            pass

        def stop(self):
            pass
    stub = ModuleType("desktop.cloud_speech")
    stub.OpenAIRealtimeSpeech = Speech
    monkeypatch.setitem(sys.modules, "desktop.cloud_speech", stub)
    monkeypatch.setattr(voice, "VoiceService", FakeVoice)
    monkeypatch.setattr(ai_routing, "FallbackTranscription", Decoder)
    monkeypatch.setattr(owner_auth, "register_session_notifications", lambda *_args: True)
    monkeypatch.setattr(owner_auth, "unregister_session_notifications", lambda *_args: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "isolated-home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "isolated-profile"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for flag in ("JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED"):
        monkeypatch.delenv(flag, raising=False)
    core = DesktopCore(tmp_path / "state", executor=lambda _action: pytest.fail("No OS action"))
    core.store.set_preferences({"voice_engine": "openai", "speech_enabled": False,
                               "morning_brief_enabled": False, "evening_plan_enabled": False})
    widget = MainWindow(core, voice_factory=None, quiet_start=True, enable_integrations=False,
                        enable_tray=False, personal_path=tmp_path / "missing.db")
    try:
        assert len(services) == 1 and not clients
        service = services[0]
        service.options["transcriber_factory"]()
        service.options["speech_factory"]()
        assert [entry[:2] for entry in clients] == [("stt", core.state_dir), ("speech", core.state_dir)]
        environment = clients[0][2]
        assert clients[1][2] is environment
        assert environment.get("JARVIS_CLOUD_STT_ENABLED") == environment.get("JARVIS_CLOUD_SPEECH_ENABLED") == "1"
        assert environment.get("OPENAI_API_KEY") is None
        assert not (core.state_dir / "openai-key-source.json").exists()
        core.store.set_preferences({"voice_engine": "windows"})
        assert environment.get("JARVIS_CLOUD_STT_ENABLED") is None
        assert environment.get("JARVIS_CLOUD_SPEECH_ENABLED") is None
        widget._update_voice_backend_status({"speech_backend": "PRIVATE_ERROR", "recognition": {
            "reason": "PRIVATE_RECOGNIZED_WORDS", "error": "private-key.env", "transcript": "private words"}})
        assert widget.recognition_backend_status.text() == ""
        assert "PRIVATE" not in widget.voice_backend_status.text()
    finally:
        widget._quitting = True
        widget.close()
        deadline = time.monotonic() + 3
        while not widget._resources_closed and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert widget._resources_closed
