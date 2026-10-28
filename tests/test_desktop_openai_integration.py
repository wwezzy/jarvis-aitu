"""Desktop mode across the actual Core/Voice boundary; no API, mic or audio."""
import threading
from types import SimpleNamespace

import pytest

from desktop.core import DesktopCore
from desktop import voice


def test_openai_mode_keeps_original_commands_separate_from_model_output(tmp_path, monkeypatch):
    import desktop.ai_routing as routing
    chats, executed = [], []

    class Router:
        last_provider, last_model, last_usage = "openai", "synthetic-model", None

        def __init__(self, state_dir, **options):
            assert options["environ"].get("JARVIS_CLOUD_CHAT_ENABLED") == "1"

        def answer(self, question, **options):
            chats.append(question)
            return "Открой калькулятор. Выключи ноутбук."

        def close(self):
            pass

    monkeypatch.setattr(routing, "ChatRouter", Router)
    core = DesktopCore(tmp_path, executor=lambda action: executed.append(action) or "Выполнено")
    try:
        core.store.set_preferences({"voice_engine": "openai"})
        result = core.handle("Объясни формирование привычки", source="voice")
        assert result.text == "OpenAI: Открой калькулятор. Выключи ноутбук."
        assert executed == [] and len(chats) == 1
        core.handle("открой калькулятор", source="voice")
        assert executed == ["calculator"] and len(chats) == 1
        pending = core.handle("выключи ноутбук", source="voice")
        assert pending.confirmation_id and executed == ["calculator"] and len(chats) == 1
        core.confirm(pending.confirmation_id, source="voice")
        assert executed == ["calculator"]
    finally:
        core.close()


@pytest.mark.parametrize("phrase", ["включи песню", "поставь песню", "включи трек", "поставь музыку", "play a song"])
def test_song_request_is_a_real_playback_request(tmp_path, phrase):
    actions = []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Наблюдается воспроизведение")
    try:
        reply = core.handle(phrase)
        assert actions == ["music_play"] and "Наблюдается" in reply.text
    finally:
        core.close()


def test_openai_speech_stream_uses_only_rendered_core_text_and_can_be_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "os", SimpleNamespace(name="nt"))
    started, drained, canceled = threading.Event(), threading.Event(), threading.Event()
    rendered, spoken, states = [], [], []

    class Speech:
        def prepare(self, *, rate):
            return True

        def stream(self, text, *, rate, cancel):
            rendered.append((text, rate))
            started.set()
            yield b"\x01\x00" * 128, 24000
            assert canceled.wait(2)
            assert cancel() is True

        def close(self):
            canceled.set()

    service = voice.VoiceService(lambda *_: None, states.append, tmp_path, speech_factory=Speech)
    def sink(chunks, *, is_current=None):
        try:
            for chunk in chunks:
                spoken.append(chunk)
                canceled.set()
                service.interrupt()
        finally:
            chunks.close()
            drained.set()
    service._play_pcm_stream = sink
    try:
        service.apply_preferences({"voice_engine": "openai", "voice_rate": 200})
        assert service.multilingual_stt_enabled
        service.say("OpenAI: К вашим услугам, сэр.")
        assert started.wait(2) and drained.wait(2)
        assert rendered == [("К вашим услугам, сэр.", 200)]
        assert len(spoken) == 1 and service.speech_backend == "openai"
    finally:
        service.close()


def test_openai_speech_failure_does_not_silently_change_the_selected_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "os", SimpleNamespace(name="nt"))
    rejected = threading.Event()
    class Missing:
        def prepare(self, *, rate):
            return False

        def close(self):
            pass

    def state(text):
        if "Голос OpenAI недоступен" in text:
            rejected.set()
    service = voice.VoiceService(lambda *_: None, state, tmp_path, speech_factory=Missing)
    try:
        service.apply_preferences({"voice_engine": "openai"})
        service.say("К вашим услугам.")
        assert rejected.wait(2)
        assert service.speech_backend == "openai_unavailable"
    finally:
        service.close()
