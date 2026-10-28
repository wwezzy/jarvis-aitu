"""Real VoiceService call graph; fake decoders/devices, no mic/API/speech."""
import sys
from types import SimpleNamespace

import pytest

from desktop import voice


class Decoder:
    def __init__(self):
        self.local_calls, self.cloud_calls = [], []
        self.canceled = self.closed = self.prepared = 0
        self.local_result = "Jarvis, explain binary search."
        self.cloud_result = "Джарвис, расскажи о следующем шаге."
        self.local_error = False
        self.before_cloud = lambda: None

    def prepare(self):
        self.prepared += 1
        return True

    def transcribe_local(self, pcm, rate):
        self.local_calls.append((pcm, rate))
        if self.local_error:
            raise RuntimeError("synthetic local-only failure")
        return self.local_result

    def transcribe(self, pcm, rate):
        self.cloud_calls.append((pcm, rate))
        self.before_cloud()
        return self.cloud_result

    def cancel(self):
        self.canceled += 1

    def close(self):
        self.closed += 1


@pytest.fixture
def service(tmp_path, monkeypatch):
    heard, states = [], []
    instance = voice.VoiceService(heard.append, states.append, tmp_path)
    instance.speech_enabled = False
    instance.say = lambda *_args: pytest.fail("Boundary test attempted speech output")
    instance.multilingual_stt_enabled = True
    instance._stt = Decoder()
    monkeypatch.setattr(voice.time, "monotonic", lambda: 100.0)
    return SimpleNamespace(instance=instance, decoder=instance._stt, heard=heard, states=states,
                           pcm=b"\x01\x00" * 800, rate=16000)


def test_idle_english_candidate_uses_only_local_method_and_reuses_its_complete_result(service):
    value = service
    value.instance._recognized("", value.pcm, value.rate, english_candidate=True)
    assert value.decoder.local_calls == [(value.pcm, value.rate)]
    assert value.decoder.cloud_calls == []
    assert value.heard == ["explain binary search."] and value.instance.gate.deadline == 0


@pytest.mark.parametrize("decoded", ["ordinary television speech", "He mentioned Jarvis in passing", "", "x" * 501])
def test_idle_unverified_english_recording_cannot_reach_cloud_or_command_dispatch(service, decoded):
    value = service
    value.decoder.local_result = decoded
    value.instance._recognized("", value.pcm, value.rate, english_candidate=True)
    assert len(value.decoder.local_calls) == 1 and not value.decoder.cloud_calls
    assert value.heard == [] and value.instance.gate.deadline == 0


def test_failed_idle_local_verification_stays_silent_and_does_not_try_cloud(service):
    value = service
    value.decoder.local_error = True
    value.instance._recognized("", value.pcm, value.rate, english_candidate=True)
    assert len(value.decoder.local_calls) == 1 and not value.decoder.cloud_calls
    assert value.heard == [] and value.instance.gate.deadline == 0
    assert value.instance.diagnostics()["decoder_failures"] == 1


def test_idle_audio_without_candidate_or_verified_wake_is_not_transcribed(service):
    value = service
    value.instance._recognized("ordinary idle conversation", value.pcm, value.rate)
    assert not value.decoder.cloud_calls and not value.decoder.local_calls and not value.heard


def test_verified_wake_command_uses_full_utterance_decoder_once(service):
    value = service
    value.instance._recognized("джарвис расскажи о целях", value.pcm, value.rate)
    assert value.decoder.cloud_calls == [(value.pcm, value.rate)]
    assert value.decoder.local_calls == []
    assert value.heard == ["расскажи о следующем шаге."]


def test_armed_followup_is_allowed_but_consumes_the_verified_window(service):
    value = service
    value.instance.gate.feed("джарвис", 100.0)
    value.instance._recognized("расскажи о целях", value.pcm, value.rate)
    assert len(value.decoder.cloud_calls) == 1 and value.heard == ["расскажи о следующем шаге."]
    value.instance._recognized("another uninvoked phrase", value.pcm, value.rate)
    assert len(value.decoder.cloud_calls) == 1 and value.instance.gate.deadline == 0


def test_stop_cancels_inflight_decoder_and_discards_its_late_result(service):
    value = service
    value.decoder.before_cloud = value.instance.stop
    value.instance._recognized("джарвис расскажи о целях", value.pcm, value.rate)
    assert value.instance._stop.is_set() and value.decoder.canceled == 1
    assert value.heard == [] and value.instance.gate.deadline == 0
    value.instance._recognized("джарвис новая команда", value.pcm, value.rate)
    assert len(value.decoder.cloud_calls) == 1


def test_close_disposes_decoder_once_and_removes_reference(service):
    value = service
    value.instance.close()
    assert value.decoder.canceled == 1 and value.decoder.closed == 1 and value.instance._stt is None
    value.instance.close()
    assert value.decoder.closed == 1 and value.instance._closed.is_set()


@pytest.mark.parametrize("factory", [False, True, 1, "cloud", object()])
def test_constructor_rejects_noncallable_factory_without_initializing_anything(tmp_path, factory):
    with pytest.raises(ValueError, match="factory"):
        voice.VoiceService(lambda *_: None, lambda *_: None, tmp_path, transcriber_factory=factory)


def test_default_and_injected_factories_are_lazy_until_listener_prepare(tmp_path, monkeypatch):
    made = []
    def forbidden_import(_name):
        pytest.fail("Constructor imported a speech SDK")
    monkeypatch.setattr(voice.importlib, "import_module", forbidden_import)
    default = voice.VoiceService(lambda *_: None, lambda *_: None, tmp_path)
    custom = voice.VoiceService(lambda *_: None, lambda *_: None, tmp_path,
                                transcriber_factory=lambda: made.append("custom") or Decoder())
    assert default._transcriber_factory is None and custom._transcriber_factory is not None
    assert default._stt is custom._stt is None and made == []
    default.speech_enabled = custom.speech_enabled = False
    default.close()
    custom.close()


@pytest.mark.parametrize("custom", [False, True])
def test_actual_listener_replaces_disposes_old_decoder_and_uses_selected_factory_without_capture(tmp_path, monkeypatch, custom):
    states, order = [], []
    old, new = Decoder(), Decoder()
    service = voice.VoiceService(lambda *_: pytest.fail("Unexpected command"), states.append, tmp_path)
    service.speech_enabled = False
    service.multilingual_stt_enabled = True
    service._stt = old
    def make():
        assert old.closed == 1
        order.append("custom" if custom else "local_default")
        return new
    if custom:
        service._transcriber_factory = make
    def prepare():
        new.prepared += 1
        service._stop.set()
        return True
    new.prepare = prepare
    vosk = SimpleNamespace(SetLogLevel=lambda *_: None, Model=lambda *_: object(),
                           KaldiRecognizer=lambda *_: SimpleNamespace(SetWords=lambda *_: None))
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 16000},
                            RawInputStream=lambda **_: pytest.fail("Test opened a microphone"))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: vosk if name == "vosk" else sound)
    monkeypatch.setitem(sys.modules, "desktop.multilingual_stt", SimpleNamespace(
        MultilingualSTT=(lambda: pytest.fail("Custom factory fell back to default")) if custom else make))
    service._listen()
    assert order == ["custom" if custom else "local_default"] and new.prepared == 1
    assert old.closed == 1 and service._stt is new
    service.close()
    assert new.closed == 1 and service._stt is None
