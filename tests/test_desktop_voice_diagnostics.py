"""Literal utility recognition and fixed diagnostic metadata; no devices."""
import json
import math
from array import array
import sys
from types import SimpleNamespace

import pytest

from desktop import voice
from desktop.ai_routing import FallbackTranscription
from desktop.multilingual_stt import MultilingualSTTUnavailable


@pytest.fixture
def service(tmp_path, monkeypatch):
    heard, states, queued = [], [], []
    instance = voice.VoiceService(heard.append, states.append, tmp_path / "not-a-real-model")
    instance.multilingual_stt_enabled = True
    instance.speech_enabled = False
    instance._enqueue_speech = lambda item: queued.append(item) or True
    instance.say = lambda _text: pytest.fail("No spoken output in diagnostic tests")
    monkeypatch.setattr(voice.time, "monotonic", lambda: 100.0)
    yield SimpleNamespace(instance=instance, heard=heard, states=states, queued=queued)
    instance.close()


def _words(text, confidence=1.0):
    return [{"word": word.casefold(), "conf": confidence} for word in text.split()]


@pytest.mark.parametrize("reason, expected", [("credit_balance_exhausted", "credit_balance_exhausted"),
    ("project_spend_limit_exceeded", "project_spend_limit_exceeded"), ("PRIVATE_PROVIDER_MESSAGE", "unavailable"),
    ({"private": "message"}, "unavailable"), (None, "unavailable")])
def test_speech_availability_projects_only_fixed_account_error_labels(service, monkeypatch, reason, expected):
    instance = service.instance
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda *_: None)
    instance.speech_engine, instance.speech_backend = "openai", "openai_unavailable"
    instance._active_backend = SimpleNamespace(reason=reason)
    state = instance.available()
    assert state["speech_error"] == expected and "PRIVATE_PROVIDER_MESSAGE" not in repr(state)
    instance.speech_backend = "openai"
    assert instance.available()["speech_error"] is None


@pytest.mark.parametrize("phrase", ["доброе утро", "good morning", "ты меня слышишь", "can you hear me", "что ты умеешь"])
@pytest.mark.parametrize("confidence", [.90, 1.0])
def test_clear_literal_utility_after_wake_avoids_redundant_decoder_without_weakening_confidence(service, phrase, confidence):
    instance = service.instance
    instance._stt = SimpleNamespace(transcribe=lambda *_: pytest.fail("A clear finite utility was decoded twice"))
    text = "джарвис " + phrase
    instance._recognized(text, b"\x01\x00" * 8000, 16000, words=_words(text, confidence))
    assert service.heard == [phrase]
    assert instance.diagnostics()["decoder_requests"] == 0 and instance.gate.deadline == 0
    assert instance.recognition_status() == {"reason": "ready", "stage": "vosk_shortcut", "provider": "vosk",
                                             "factory": "not_created"}


@pytest.mark.parametrize("phrase", ["доброе утро", "good morning", "ты меня слышишь"])
def test_utility_still_requires_verified_wake_or_live_original_followup(service, phrase):
    instance = service.instance
    instance._stt = SimpleNamespace(transcribe=lambda *_: pytest.fail("Uninvoked audio reached a decoder"))
    instance._recognized(phrase, b"\x01\x00" * 8000, 16000, words=_words(phrase))
    assert service.heard == []
    instance.gate.feed("джарвис", 100.0)
    instance._recognized(phrase, b"\x01\x00" * 8000, 16000, words=_words(phrase))
    assert service.heard == [phrase] and instance.gate.deadline == 0


@pytest.mark.parametrize("text,command", [
    ("джарвис не доброе утро", "не доброе утро"),
    ("джарвис доброе утро и выключи ноутбук", "доброе утро и выключи ноутбук"),
    ("джарвис «доброе утро»", "доброе утро"),
    ("он сказал джарвис доброе утро", "доброе утро"),
])
def test_utility_shortcut_never_extracts_quoted_negated_buried_or_compound_requests(text, command):
    words = _words(text.replace("«", "").replace("»", ""))
    assert voice.VoiceService._confident_alias(text, command, words) is False


@pytest.mark.parametrize("confidence", [.899, None, True, float("nan")])
def test_weak_or_invalid_acoustic_word_scores_do_not_take_morning_shortcut(confidence):
    text = "джарвис доброе утро"
    assert voice.VoiceService._confident_alias(text, "доброе утро", _words(text, confidence)) is False


def test_default_facade_preserves_local_rejection_stage_in_voice_metadata_without_exception_text(service, tmp_path):
    class Local:
        reason = "ready"

        def prepare(self):
            return True

        def transcribe(self, *_args):
            self.reason = "low_confidence"
            raise MultilingualSTTUnavailable("PRIVATE_EXCEPTION contains personal transcript")

    decoder = FallbackTranscription(tmp_path / "unused-state", environ={}, local_factory=Local)
    assert decoder.prepare()
    service.instance._stt = decoder
    service.instance._recognized("джарвис объясни привычки", b"\x01\x00" * 8000, 16000)
    assert not service.heard
    status = service.instance.recognition_status()
    assert status["reason"] == "low_confidence" and status["stage"] == "local_decode" and status["provider"] == "local"
    assert service.instance.diagnostics()["decoder_failures"] == 1
    public = json.dumps([status, service.instance.diagnostics(), service.states])
    assert "PRIVATE_EXCEPTION" not in public and "personal transcript" not in public


@pytest.mark.parametrize("reason,stage,provider", [
    ("silence", "local_decode", "local"), ("no_speech", "local_decode", "local"),
    ("language_unsupported", "local_decode", "local"), ("audio_clipped", "local_decode", "local"),
    ("quota_or_rate_limit", "cloud_decode", "openai"),
    ("PRIVATE_REASON secret", "PRIVATE_STAGE token", "PRIVATE_PROVIDER token"),
])
def test_diagnostics_project_only_exact_allowlisted_decoder_metadata(service, reason, stage, provider):
    def reject(*_args):
        raise RuntimeError("NEVER_LOG_EXCEPTION_BODY")

    decoder = SimpleNamespace(reason=reason, last_error_stage=stage, last_error_provider=provider, transcribe=reject)
    service.instance._stt = decoder
    service.instance._recognized("джарвис объясни привычки", b"\x01\x00" * 8000, 16000)
    status = service.instance.recognition_status()
    if reason.startswith("PRIVATE"):
        assert status == {"reason": "transcription_failed", "stage": "decode", "provider": None, "factory": "not_created"}
    else:
        assert (status["reason"], status["stage"], status["provider"]) == (reason, stage, provider)
    serialized = json.dumps([status, service.instance.diagnostics(), service.states])
    assert "PRIVATE" not in serialized and "NEVER_LOG" not in serialized
    assert all(type(value) in {int, float, type(None)} for value in service.instance.diagnostics().values())
    assert all(not isinstance(value, float) or math.isfinite(value) for value in service.instance.diagnostics().values())


@pytest.mark.parametrize("mode", ["ready", "missing", "factory_error"])
def test_listener_reports_factory_prepare_and_same_device_rate_without_opening_capture(service, monkeypatch, mode):
    instance = service.instance

    class Decoder:
        reason = "model_missing" if mode == "missing" else "ready"

        def prepare(self):
            instance._stop.set()
            return mode == "ready"

    def create():
        if mode == "factory_error":
            instance._stop.set()
            raise RuntimeError("PRIVATE_CONSTRUCTOR_ERROR")
        return Decoder()

    instance._transcriber_factory = create
    vosk = SimpleNamespace(SetLogLevel=lambda *_: None, Model=lambda *_: object(),
                           KaldiRecognizer=lambda *_: SimpleNamespace(SetWords=lambda *_: None))
    probes = []
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 44100, "max_input_channels": 4},
                            default=SimpleNamespace(device=[7, 8]),
                            check_input_settings=lambda **options: probes.append(options),
                            RawInputStream=lambda **_: pytest.fail("Diagnostic opened a microphone"))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: vosk if name == "vosk" else sound)
    instance._listen()
    report, status = instance.diagnostics(), instance.recognition_status()
    assert report["capture_sample_rate"] == 16000 and report["capture_default_sample_rate"] == 44100
    assert report["capture_input_channels"] == 4 and report["capture_16k_probe"] == 1
    assert report["decoder_prepare_ms"] == 0 and status["factory"] == "custom"
    assert probes == [{"device": 7, "channels": 1, "dtype": "int16", "samplerate": 16000}]
    assert status["reason"] == {"ready": "ready", "missing": "model_missing", "factory_error": "backend_unavailable"}[mode]
    assert status["stage"] == ("factory" if mode == "factory_error" else "prepare")
    assert "PRIVATE_CONSTRUCTOR_ERROR" not in json.dumps([report, status, service.states])


def test_status_projection_is_a_copy_and_never_imports_microphone_or_models(service, monkeypatch):
    monkeypatch.setattr(voice.importlib, "import_module", lambda _name: pytest.fail("Status imported a native SDK"))
    status = service.instance.recognition_status()
    status["reason"] = "PRIVATE_MUTATION"
    assert service.instance.recognition_status()["reason"] == "not_prepared"


def test_actual_listener_retains_utterance_level_after_silent_tail_with_synthetic_capture_only(service, monkeypatch):
    instance = service.instance
    text = "джарвис доброе утро"
    voiced = array("h", (int(2000 * math.sin(index * 2 * math.pi * 220 / 16000)) for index in range(800))).tobytes()

    class Recognizer:
        def SetWords(self, _enabled):
            pass

        def AcceptWaveform(self, _data):
            return False

        def FinalResult(self):
            return json.dumps({"text": text, "result": _words(text)})

        def Reset(self):
            pass

    class Stream:
        def __init__(self, **options):
            self.callback = options["callback"]

        def __enter__(self):
            for frame in [voiced] * 6 + [bytes(1600)] * 16:
                self.callback(frame, 800, None, None)
            return self

        def __exit__(self, *_args):
            pass

    instance.on_text = lambda phrase: (service.heard.append(phrase), instance._stop.set())
    instance._transcriber_factory = lambda: SimpleNamespace(prepare=lambda: True,
        transcribe=lambda *_: pytest.fail("Exact morning utility unnecessarily invoked ASR"))
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 16000, "max_input_channels": 1},
                            default=SimpleNamespace(device=[7, 8]), check_input_settings=lambda **_: None,
                            RawInputStream=Stream)
    vosk = SimpleNamespace(SetLogLevel=lambda *_: None, Model=lambda *_: object(), KaldiRecognizer=lambda *_: Recognizer())
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: vosk if name == "vosk" else sound)
    monkeypatch.setitem(sys.modules, "desktop.english_wake", SimpleNamespace(EnglishWakeDetector=lambda: SimpleNamespace(
        prepare=lambda *_a, **_kw: False, close=lambda: None)))
    instance._listen()
    report = instance.diagnostics()
    assert service.heard == ["доброе утро"] and report["decoder_requests"] == 0
    assert report["last_level_dbfs"] == -120 and -28 <= report["last_utterance_level_dbfs"] <= -26
    assert report["last_utterance_ms"] == 1100 and report["queue_overflows"] == report["capture_overflows"] == 0
    assert report["audio_callbacks"] == 22 and report["commands"] == 1
