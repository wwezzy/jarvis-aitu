"""ASR handoff and preferred-voice policy using synthetic PCM/fake backends.

No model is loaded and no input/output device, network or PC action is used.
"""
from array import array
import json
import math
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import voice
from desktop.voice import UtteranceBuffer, VoiceService, WakeGate, pcm_active


def tone(seconds=0.6, rate=16000):
    values = array("h", (round(5000 * math.sin(2 * math.pi * 440 * i / rate))
                         for i in range(round(seconds * rate))))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


@pytest.fixture
def recognition(tmp_path, monkeypatch):
    heard, states, spoken, calls = [], [], [], []
    clock = [100.0]
    service = VoiceService(heard.append, states.append, tmp_path)
    service.multilingual_stt_enabled = True
    service.say = spoken.append
    result = {"text": "Open YouTube"}
    def decode(pcm, rate):
        calls.append((pcm, rate))
        if result.get("error"):
            raise RuntimeError("synthetic rejection")
        return result["text"]
    service._stt = SimpleNamespace(transcribe=decode, prepare=lambda: True)
    monkeypatch.setattr(voice.time, "monotonic", lambda: clock[0])
    return service, heard, states, spoken, calls, clock, result


def test_name_only_russian_result_recovers_name_and_english_command_in_same_pcm(recognition):
    service, heard, _states, spoken, calls, _clock, _result = recognition
    pcm = tone()
    service._recognized("джарвис", pcm, 16000)
    assert heard == ["Open YouTube"] and calls == [(pcm, 16000)]
    assert spoken == ["Да, сэр"]


def test_empty_russian_followup_uses_original_pcm_only_after_verified_wake(recognition):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    pcm = tone()
    service._recognized("", pcm, 16000)
    assert not heard and not calls
    service.gate.feed("джарвис", 100)
    service._recognized("", pcm, 16000)
    assert heard == ["Open YouTube"] and calls == [(pcm, 16000)]
    assert service.gate.deadline == 0


@pytest.mark.parametrize("decoded", ["Джарвис", "Jarvis.", '«Джарвис!»'])
def test_whisper_name_only_is_acknowledgment_never_a_model_question(recognition, decoded):
    service, heard, _states, spoken, calls, _clock, result = recognition
    result["text"] = decoded
    service._recognized("джарвис какой план", tone(), 16000)
    assert not heard and len(calls) == 1 and spoken == ["Да, сэр"]
    assert service.gate.deadline == 110


def test_decode_failure_keeps_original_retry_window_without_indefinite_renewal(recognition):
    service, heard, _states, _spoken, calls, clock, result = recognition
    result["error"] = True
    service.gate.feed("джарвис", 100)
    clock[0] = 101
    service._recognized("какой план", tone(), 16000)
    assert service.gate.deadline == 110
    clock[0] = 106
    service._recognized("какой план", tone(), 16000)
    assert service.gate.deadline == 110
    result["error"] = False
    clock[0] = 109
    service._recognized("какой план", tone(), 16000)
    assert heard == ["Open YouTube"] and len(calls) == 3
    clock[0] = 120
    service._recognized("какой план", tone(), 16000)
    assert len(calls) == 3


@pytest.mark.parametrize("confidence", [None, 0.2, 0.89, True, math.nan])
def test_unscored_or_low_confidence_alias_does_not_override_bilingual_result(recognition, confidence):
    service, heard, _states, _spoken, calls, _clock, result = recognition
    result["text"] = "Расскажи о калькуляторе"
    phrase = "джарвис открой калькулятор"
    words = None if confidence is None else [{"word": word, "conf": confidence} for word in phrase.split()]
    service._recognized(phrase, tone(), 16000, words=words)
    assert heard == ["Расскажи о калькуляторе"] and len(calls) == 1


def test_final_confident_complete_word_metadata_keeps_exact_alias_fast_path(recognition):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    phrase = "джарвис открой калькулятор"
    service._recognized(phrase, tone(), 16000,
                        words=[{"word": word, "conf": 0.95} for word in phrase.split()])
    assert heard == ["открой калькулятор"] and not calls


def test_incomplete_word_metadata_cannot_claim_confidence_for_full_command(recognition):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    service._recognized("джарвис открой калькулятор", tone(), 16000,
                        words=[{"word": "калькулятор", "conf": 1}])
    assert heard == ["Open YouTube"] and len(calls) == 1


def test_missing_audio_never_executes_an_unscored_bilingual_alias(recognition):
    service, heard, states, _spoken, calls, _clock, _result = recognition
    service._recognized("джарвис открой калькулятор", None, 16000)
    assert not heard and not calls
    assert "Недостаточно аудио" in states[-1] and service.gate.deadline == 110


@pytest.mark.parametrize("text", ["отмена", "джарвис отмена", "jarvis cancel", "Cancel.", "джарвис, отмена!"])
def test_cancel_does_not_become_an_action_or_model_question(recognition, text):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    service.gate.feed("джарвис", 100)
    service._recognized(text, tone(), 16000)
    assert not heard and not calls and service.gate.deadline == 0


def test_background_speech_cannot_revive_an_expired_or_empty_followup_window():
    gate = WakeGate()
    gate.feed("джарвис", 100)
    gate.extend(120)
    assert gate.deadline == 0 and gate.feed("открой блокнот", 121).kind == "ignore"
    gate.feed("джарвис", 200)
    assert gate.feed("", 220).kind == "ignore" and gate.deadline == 0


def test_punctuated_whisper_cancellation_does_not_reach_core(recognition):
    service, heard, _states, _spoken, calls, _clock, result = recognition
    result["text"] = "Отмена."
    service._recognized("джарвис какой план", tone(), 16000)
    assert not heard and len(calls) == 1 and service.gate.deadline == 0


def test_quoted_first_wake_strips_name_and_keeps_whole_token_boundary():
    assert WakeGate().feed('«Джарвис, открой блокнот»', 100).text == "открой блокнот"
    assert WakeGate().feed('он сказал «Джарвис, открой блокнот»', 100).kind == "ignore"


def test_acoustic_buffer_onset_and_silence_endpoint_do_not_require_russian_partial():
    value = UtteranceBuffer(16000)
    quiet = b"\0\0" * 800
    for _ in range(72000):
        value.append(quiet, active=pcm_active(quiet))
    assert not value.started and not value.overflow
    pcm = tone()
    for start in range(0, len(pcm), 1600):
        frame = pcm[start:start + 1600]
        value.append(frame, active=pcm_active(frame))
    for _ in range(12):
        value.append(quiet, active=pcm_active(quiet))
    assert value.started and value.finished and not value.overflow
    assert pcm in value.pcm() and len(value.pcm()) <= 16000 * 2 * 12
    assert not pcm_active(array("h", [3000] * 800).tobytes())


class EnglishWake:
    """A candidate-only fake; its positive result never stands in for Whisper."""

    def __init__(self, *, ready=True, candidate=True):
        self.ready, self.candidate = ready, candidate
        self.events, self.utterances = [], []
        self.frames = []

    def prepare(self, rate, *, vosk_module):
        self.events.append(("prepare", rate, vosk_module))
        return self.ready

    def accept(self, pcm):
        self.events.append("accept")
        self.frames.append(pcm)

    def finish(self):
        self.events.append("finish")
        self.utterances.append(b"".join(self.frames))
        return self.candidate

    def reset(self):
        self.events.append("reset")
        self.frames.clear()

    def close(self):
        self.events.append("close")
        self.frames.clear()


def listen_with_fake_devices(service, monkeypatch, final_text, pcm, *, frames=None,
                             english=None, words=None, on_get=None, on_capture=None):
    """Exercise the actual listener loop with final/partial text always absent."""
    calls = []
    if frames is None:
        frames = [pcm[i:i + 1600] for i in range(0, len(pcm), 1600)] + [b"\0\0" * 800] * 12
    english = EnglishWake(ready=False) if english is None else english
    class InputQueue:
        def __init__(self):
            self.frames = iter(frames)
            self.index = 0
        def get_nowait(self):
            raise queue.Empty
        def get(self, timeout):
            try:
                data = next(self.frames)
                if on_get is not None:
                    on_get(self.index)
                self.index += 1
                return data
            except StopIteration:
                service._stop.set()
                raise queue.Empty from None
    class Recognizer:
        def SetWords(self, value):
            calls.append(("word_scores", value))
        def AcceptWaveform(self, data):
            return False
        def FinalResult(self):
            calls.append("final")
            return json.dumps({"text": final_text, "result": words})
        def Reset(self):
            calls.append("reset")
    class Input:
        def __enter__(self):
            calls.append("capture")
            if on_capture is not None:
                on_capture()
            return self
        def __exit__(self, *_args):
            calls.append("capture_closed")
    vosk = SimpleNamespace(SetLogLevel=lambda _level: None, Model=lambda _path: object(),
                           KaldiRecognizer=lambda *_args: Recognizer())
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 16000},
                            RawInputStream=lambda **_: Input())
    stub = service._stt
    monkeypatch.setitem(sys.modules, "desktop.multilingual_stt", SimpleNamespace(MultilingualSTT=lambda: stub))
    monkeypatch.setitem(sys.modules, "desktop.english_wake", SimpleNamespace(EnglishWakeDetector=lambda: english))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: vosk if name == "vosk" else sound)
    service._audio = InputQueue()
    service._listen()
    return calls


@pytest.mark.parametrize("final_text,armed", [("джарвис", False), ("", True)])
def test_actual_listener_recovers_full_pcm_without_vosk_partial(recognition, monkeypatch, final_text, armed):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    if armed:
        service.gate.feed("джарвис", 100)
    pcm = tone()
    calls = listen_with_fake_devices(service, monkeypatch, final_text, pcm)
    assert heard == ["Open YouTube"] and len(decode_calls) == 1
    assert pcm in decode_calls[0][0] and len(decode_calls[0][0]) <= 16000 * 2 * 12
    assert ("word_scores", True) in calls and "final" in calls and "reset" in calls


def test_acoustic_activity_without_verified_wake_never_calls_whisper(recognition, monkeypatch):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    listen_with_fake_devices(service, monkeypatch, "", tone())
    assert not heard and not decode_calls


def test_english_candidate_requires_one_full_audio_confirmation_before_dispatch(recognition):
    service, heard, _states, spoken, calls, _clock, result = recognition
    result["text"] = "Jarvis, wake up, daddy's home."
    pcm = tone()
    service._recognized("чем весь вы как диареи своём", pcm, 16000, english_candidate=True)
    assert heard == ["wake up, daddy's home."] and calls == [(pcm, 16000)]
    assert not spoken and not service.gate.deadline and not service._transcribing.is_set()


@pytest.mark.parametrize("decoded", ["Jarvis.", "«Jarvis!»", "Джарвис"])
def test_english_name_only_keeps_original_candidate_window_and_ack_once(recognition, decoded):
    service, heard, _states, spoken, calls, clock, _result = recognition
    def decode(_pcm, _rate):
        calls.append("decode")
        clock[0] += 2
        return decoded
    service._stt.transcribe = decode
    service._recognized("", tone(), 16000, english_candidate=True)
    assert spoken == ["Да, сэр"] and not heard and calls == ["decode"]
    assert service.gate.deadline == 110  # ASR latency cannot renew the original window.


@pytest.mark.parametrize("decoded", ["Open YouTube", "say Jarvis open YouTube", "Джорвис открой браузер",
                                     "Jarvislike open YouTube", "[unk] Jarvis open YouTube", "", None, 42,
                                     "Jarvis " + "x" * 500])
def test_false_buried_or_malformed_english_confirmation_stays_silent_unarmed(recognition, decoded):
    service, heard, states, spoken, calls, _clock, result = recognition
    service.gate.deadline = 90  # Even an old window is not mutated by an uncertain candidate.
    result["text"] = decoded
    service._recognized("", tone(), 16000, english_candidate=True)
    assert len(calls) == 1 and not heard and not spoken and not states
    assert service.gate.deadline == 90 and service._wake_serial == 0


@pytest.mark.parametrize("pcm,rate", [(None, 16000), (b"", 16000), (b"odd", 16000),
                                      (bytearray(3200), 16000), (b"\0\0", True),
                                      (b"\0\0", 7999), (b"\0\0", 48001),
                                      (b"\0\0" * (16000 * 12 + 1), 16000)],
                         ids=["missing", "empty", "odd", "mutable", "boolean-rate",
                              "low-rate", "high-rate", "overlong"])
def test_english_candidate_never_decodes_missing_invalid_or_overlong_audio(recognition, pcm, rate):
    service, heard, states, spoken, calls, _clock, _result = recognition
    service._recognized("", pcm, rate, english_candidate=True)
    assert not calls and not heard and not spoken and not states and not service.gate.deadline


@pytest.mark.parametrize("change", ["expired", "stop", "closed", "disabled", "epoch", "interrupt",
                                    "speaking", "echo", "output-finished", "decoder", "gate", "serial"])
def test_english_confirmation_cannot_outlive_capture_output_or_original_deadline(recognition, change):
    service, heard, _states, spoken, calls, clock, _result = recognition
    def decode(pcm, rate):
        calls.append((pcm, rate))
        if change == "expired":
            clock[0] += service.gate.timeout + 0.01
        elif change == "stop":
            service._stop.set()
        elif change == "closed":
            service._closed.set()
        elif change == "disabled":
            service.apply_preferences({"multilingual_stt_enabled": False})
        elif change == "epoch":
            service._audio_epoch += 1
        elif change == "interrupt":
            service.interrupt()
        elif change == "speaking":
            service._speaking.set()
        elif change == "echo":
            service._last_spoken = clock[0]
        elif change == "output-finished":
            service._last_spoken = clock[0] - 1
        elif change == "decoder":
            service._stt = object()
        elif change == "gate":
            service.gate.feed("джарвис", clock[0])
        elif change == "serial":
            service._wake_serial += 1
        return "Jarvis open YouTube"
    service._stt.transcribe = decode
    service._recognized("", tone(), 16000, english_candidate=True)
    assert len(calls) == 1 and not heard and not spoken and not service._transcribing.is_set()
    assert service.gate.deadline == (110 if change == "gate" else 0)


def test_russian_verified_wake_and_armed_followup_win_without_second_english_decode(recognition):
    service, heard, _states, _spoken, calls, _clock, result = recognition
    result["text"] = "Джорвис, что ты умеешь?"
    service._recognized("джарвис что ты умеешь", tone(), 16000, english_candidate=True)
    assert heard == ["что ты умеешь?"] and len(calls) == 1
    service.gate.feed("джарвис", 100)
    result["text"] = "Open YouTube"
    service._recognized("", tone(), 16000, english_candidate=True)
    assert heard == ["что ты умеешь?", "Open YouTube"] and len(calls) == 2


def test_confident_russian_wake_route_needs_no_whisper_even_with_english_candidate(recognition):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    phrase = "джарвис что ты умеешь"
    service._recognized(phrase, tone(), 16000, english_candidate=True,
                        words=[{"word": word, "conf": 1} for word in phrase.split()])
    assert heard == ["что ты умеешь"] and not calls


def test_uncertain_english_keyword_or_disabled_feature_cannot_start_transcription(recognition):
    service, heard, _states, spoken, calls, _clock, result = recognition
    result["text"] = "Jarvis open YouTube"
    service._recognized("", tone(), 16000, english_candidate=False)
    service._recognized("", tone(), 16000, english_candidate=1)  # Strict candidate boolean.
    service.multilingual_stt_enabled = False
    service._recognized("", tone(), 16000, english_candidate=True)
    service.multilingual_stt_enabled = True
    service._stt = None
    service._recognized("", tone(), 16000, english_candidate=True)
    assert not heard and not calls and not spoken and not service.gate.deadline


def test_english_cancel_consumes_its_own_verified_utterance_without_dispatch(recognition):
    service, heard, _states, spoken, calls, _clock, result = recognition
    result["text"] = "Jarvis, cancel."
    service._recognized("", tone(), 16000, english_candidate=True)
    assert len(calls) == 1 and not heard and not spoken and not service.gate.deadline


def test_optional_english_listener_uses_preroll_once_and_ignores_long_idle_time(recognition, monkeypatch):
    service, heard, _states, _spoken, decode_calls, _clock, result = recognition
    english, quiet, pcm = EnglishWake(), b"\0\0" * 800, tone()
    active = [pcm[i:i + 1600] for i in range(0, len(pcm), 1600)]
    result["text"] = "Jarvis, open YouTube."
    frames = [quiet] * 400 + active + [quiet] * 12
    def before_capture():
        assert len(english.events) == 1 and english.events[0][:2] == ("prepare", 16000)
    calls = listen_with_fake_devices(service, monkeypatch, "", pcm, frames=frames,
                                    english=english, on_capture=before_capture)
    expected = quiet * 19 + pcm + quiet * 12
    assert heard == ["open YouTube."] and decode_calls == [(expected, 16000)]
    assert english.utterances == [expected]
    assert english.events.count("accept") == len(active) + 12
    assert english.events.count("finish") == 1 and english.events[-1] == "close"
    assert english.events.index("finish") < english.events.index("reset")
    assert "capture_closed" in calls and service._english_wake is None


@pytest.mark.parametrize("ready,candidate", [(False, True), (True, False)])
def test_optional_english_absent_or_negative_keeps_idle_audio_private(recognition, monkeypatch, ready, candidate):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    english = EnglishWake(ready=ready, candidate=candidate)
    listen_with_fake_devices(service, monkeypatch, "", tone(), english=english)
    assert not heard and not calls and english.events[-1] == "close"
    assert english.events.count("finish") == (1 if ready else 0)


def test_missing_optional_english_model_does_not_disable_fast_russian_commands(recognition, monkeypatch):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    phrase, english = "джарвис как дела", EnglishWake(ready=False)
    listen_with_fake_devices(service, monkeypatch, phrase, tone(), english=english,
                             words=[{"word": word, "conf": 1} for word in phrase.split()])
    assert heard == ["как дела"] and not decode_calls
    assert english.events[-1] == "close" and "accept" not in english.events


@pytest.mark.parametrize("failure", ["prepare", "accept", "finish"])
def test_optional_english_failure_never_breaks_russian_capture(recognition, monkeypatch, failure):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    english, phrase = EnglishWake(), "джарвис как дела"
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic optional engine failure")
    setattr(english, failure, fail)
    calls = listen_with_fake_devices(service, monkeypatch, phrase, tone(), english=english,
                                    words=[{"word": word, "conf": 1} for word in phrase.split()])
    assert heard == ["как дела"] and not decode_calls and english.events[-1] == "close"
    assert "capture_closed" in calls


@pytest.mark.parametrize("mode", ["disabled", "decoder_unavailable"])
def test_english_sdk_is_not_prepared_when_bilingual_capture_is_unavailable(recognition, monkeypatch, mode):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    english = EnglishWake()
    if mode == "disabled":
        service.multilingual_stt_enabled = False
    else:
        service._stt.prepare = lambda: False
    listen_with_fake_devices(service, monkeypatch, "", tone(), english=english)
    assert not english.events and not calls and not heard and service._english_wake is None


def test_stop_during_optional_english_preparation_never_opens_camera_or_microphone(recognition, monkeypatch):
    service, _heard, _states, _spoken, _calls, _clock, _result = recognition
    english = EnglishWake()
    def prepare(*_args, **_kwargs):
        service._stop.set()
        return True
    english.prepare = prepare
    calls = listen_with_fake_devices(service, monkeypatch, "", tone(), english=english)
    assert "capture" not in calls and english.events == ["close"] and service._english_wake is None


@pytest.mark.parametrize("change", ["epoch", "interrupt", "output", "disabled"])
def test_listener_drops_partial_english_utterance_across_capture_boundary(recognition, monkeypatch, change):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    english, quiet, pcm = EnglishWake(), b"\0\0" * 800, tone(seconds=0.3)
    active = [pcm[i:i + 1600] for i in range(0, len(pcm), 1600)]
    def revoke(index):
        if index != len(active):
            return
        if change == "epoch":
            service._audio_epoch += 1
        elif change == "interrupt":
            service.interrupt()
        elif change == "output":
            service._last_spoken = 99  # Output finished, but this capture still predates it.
        else:
            service.apply_preferences({"multilingual_stt_enabled": False})
    listen_with_fake_devices(service, monkeypatch, "", pcm, english=english,
                             frames=active + [quiet] * 20, on_get=revoke)
    assert not heard and not decode_calls and not english.utterances
    assert english.events[-1] == "close" and service._english_wake is None
    assert "reset" in english.events or change == "disabled"


def test_overlong_english_audio_is_discarded_before_keyword_or_full_transcription(recognition, monkeypatch):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    english, active, quiet = EnglishWake(), tone(seconds=0.05), b"\0\0" * 800
    listen_with_fake_devices(service, monkeypatch, "", active, english=english,
                             frames=[active] * 241 + [quiet] * 12)
    assert not heard and not decode_calls and not english.utterances
    assert english.events.count("accept") == 240 and "reset" in english.events


def test_english_candidate_authority_is_not_replayed_into_next_idle_utterance(recognition, monkeypatch):
    service, heard, _states, _spoken, decode_calls, _clock, result = recognition
    english, pcm = EnglishWake(), tone()
    candidates = iter([True, False])
    finish = english.finish
    def finish_once():
        finish()
        return next(candidates)
    english.finish = finish_once
    result["text"] = "Jarvis open YouTube"
    frames = [pcm[i:i + 1600] for i in range(0, len(pcm), 1600)] + [b"\0\0" * 800] * 12
    listen_with_fake_devices(service, monkeypatch, "", pcm, english=english, frames=frames * 2)
    assert heard == ["open YouTube"] and len(decode_calls) == 1
    assert len(english.utterances) == 2 and english.events.count("finish") == 2
    assert not service.gate.deadline


@pytest.mark.parametrize("change", ["epoch", "interrupt", "output", "disabled", "stop"])
def test_english_endpoint_rechecks_capture_before_any_whisper_call(recognition, monkeypatch, change):
    service, heard, _states, _spoken, decode_calls, _clock, _result = recognition
    english, finish = EnglishWake(), None
    finish = english.finish
    def revoke_and_finish():
        if change == "epoch":
            service._audio_epoch += 1
        elif change == "interrupt":
            service.interrupt()
        elif change == "output":
            service._last_spoken = 99
        elif change == "disabled":
            service.apply_preferences({"multilingual_stt_enabled": False})
        else:
            service._stop.set()
        return finish()
    english.finish = revoke_and_finish
    listen_with_fake_devices(service, monkeypatch, "", tone(), english=english)
    assert not heard and not decode_calls and english.events[-1] == "close"


def test_capture_failure_closes_optional_english_model_without_frame_or_audio_leak(recognition, monkeypatch):
    service, heard, states, _spoken, decode_calls, _clock, _result = recognition
    english = EnglishWake()
    def fail():
        raise RuntimeError("synthetic capture failure")
    listen_with_fake_devices(service, monkeypatch, "", tone(), english=english, on_capture=fail)
    assert english.events[-1] == "close" and not english.utterances
    assert service._english_wake is None and not heard and not decode_calls
    assert states[-1] == "Голос недоступен (RuntimeError). Текстовый ввод работает."


class Clone:
    def __init__(self):
        self.ready = threading.Event()
        self.warming = True
        self.closed = False
        self.calls = []

    @property
    def is_ready(self):
        return self.ready.is_set() and not self.closed

    @property
    def is_warming(self):
        return self.warming and not self.closed

    def prepare_background(self, **_kwargs):
        self.calls.append("prepare")

    def fixed_ack(self, _language):
        return b"\1\0" * 80, 24000

    def stream(self, text, **_kwargs):
        self.calls.append(("stream", text))
        yield b"\2\0" * 100, 24000

    def close(self):
        self.closed = True


@pytest.fixture
def clone_service(tmp_path, monkeypatch):
    states, played, clone = [], [], Clone()
    service = VoiceService(lambda _text: None, states.append, tmp_path)
    service.speech_engine = "clone"
    monkeypatch.setitem(sys.modules, "desktop.clone_speech", SimpleNamespace(CloneSpeech=lambda **_: clone))
    monkeypatch.setattr(voice.importlib, "import_module", lambda _name: pytest.fail("No alternative timbre or native output"))
    monkeypatch.setattr(service, "_play_pcm", lambda pcm, rate, **_: played.append((pcm, rate)))
    monkeypatch.setattr(service, "_play_pcm_stream", lambda chunks, **_: played.extend(list(chunks)))
    return service, clone, states, played


def worker(service):
    thread = threading.Thread(target=service._speak_loop, daemon=True)
    service._speaker = thread
    thread.start()
    return thread


def await_condition(check):
    deadline = time.monotonic() + 2
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError("Fake speech worker did not progress")
        time.sleep(0.005)


def test_dynamic_clone_waits_without_lifecycle_lock_or_mic_suppression(clone_service):
    service, clone, _states, played = clone_service
    service._speech.put("Новый ответ")
    service._speech.put(None)
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        assert not service._speaking.is_set() and service._lock.acquire(timeout=0.2)
        service._lock.release()
        clone.ready.set()
        clone.warming = False
        thread.join(1)
        assert not thread.is_alive() and played == [(b"\2\0" * 100, 24000)]
        assert ("stream", "Новый ответ") in clone.calls
    finally:
        service.close()


@pytest.mark.parametrize("warming", [False, True])
def test_clone_unavailable_or_timeout_stays_text_only_without_other_voice(clone_service, monkeypatch, warming):
    service, clone, states, played = clone_service
    clone.warming = warming
    monkeypatch.setattr(voice, "CLONE_READY_TIMEOUT", 0.05)
    service._speech.put("Ответ доступен на экране")
    service._speech.put(None)
    service._speak_loop()
    assert not played and not any(isinstance(call, tuple) for call in clone.calls)
    assert service.speech_backend == ("clone_warming" if warming else "clone_unavailable")
    assert any("текстом" in state for state in states)


def test_plan_becoming_stale_during_clone_warmup_cannot_play(clone_service):
    service, clone, _states, played = clone_service
    current = [True]
    service._speech.put(voice._EventSpeech("Старый план", lambda: current[0]))
    service._speech.put(None)
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        current[0] = False
        clone.ready.set()
        thread.join(1)
        assert not thread.is_alive() and not played
    finally:
        service.close()


def test_fixed_ack_preempts_without_losing_pending_and_followup_cancels_old_answer(clone_service, monkeypatch):
    service, clone, _states, played = clone_service
    monkeypatch.setattr(service, "_enqueue_speech", lambda text: service._speech.put((service._speech_generation, text)))
    service._speech.put("Старый ответ")
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        generation = service._speech_generation
        service.gate.feed("джарвис", time.monotonic())
        service.say("Да, сэр")
        await_condition(lambda: bool(played))
        assert service._speech_generation == generation
        clone.ready.set()
        clone.warming = False
        time.sleep(0.08)
        assert not any(isinstance(call, tuple) for call in clone.calls)
        # A new original command consumes wake permission; the UI interrupts
        # its old reply before queuing this command's fresh response.
        service.gate.reset()
        service.interrupt()
        service.say("Новый ответ")
        service._speech.put(None)
        thread.join(1)
        assert not thread.is_alive() and played == [(b"\1\0" * 80, 24000), (b"\2\0" * 100, 24000)]
        assert ("stream", "Старый ответ") not in clone.calls and ("stream", "Новый ответ") in clone.calls
    finally:
        service.close()


def test_clone_stream_failure_never_falls_back_to_windows_or_eugene(clone_service):
    service, clone, states, played = clone_service
    clone.ready.set()
    clone.warming = False
    def failure(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")
        yield  # make failure happen during stream iteration
    clone.stream = failure
    service._speech.put("Ответ")
    service._speech.put(None)
    service._speak_loop()
    assert not played and service.speech_backend == "clone_unavailable"
    assert any("Выбранный голос недоступен" in state for state in states)


def test_mute_or_engine_switch_cancels_clone_warmup_promptly(clone_service):
    service, _clone, _states, played = clone_service
    service._speech.put("Старый ответ")
    service._speech.put(None)
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        service.apply_preferences({"speech_enabled": False})
        await_condition(lambda: not service._waiting_clone.is_set())
        service._speech.put(None)  # interrupt() correctly purges queued items.
        thread.join(1)
        assert not thread.is_alive() and not played and not service._waiting_clone.is_set()
    finally:
        service.close()


@pytest.mark.parametrize("command", ["как дела", "что ты умеешь", "помощь", "привет", "how are you", "what can you do"])
def test_confident_complete_read_only_smalltalk_and_help_do_not_need_second_decoder(recognition, command):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    phrase = "джарвис " + command
    service._recognized(phrase, tone(), 16000,
        words=[{"word": word, "conf": 0.95} for word in phrase.split()])
    assert heard == [command] and not calls


@pytest.mark.parametrize("confidence", [0.89, True, math.nan])
def test_uncertain_read_only_phrase_does_not_skip_full_pcm_decode(recognition, confidence):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    phrase = "джарвис как дела"
    service._recognized(phrase, tone(), 16000,
        words=[{"word": word, "conf": confidence} for word in phrase.split()])
    assert heard == ["Open YouTube"] and len(calls) == 1


def test_high_confidence_without_verified_wake_still_cannot_dispatch_read_only_phrase(recognition):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    service._recognized("как дела", tone(), 16000,
        words=[{"word": word, "conf": 1} for word in "как дела".split()])
    assert not heard and not calls


def test_metadata_must_contain_real_single_complete_words_not_one_injected_sentence(recognition):
    service, heard, _states, _spoken, calls, _clock, _result = recognition
    phrase = "джарвис как дела"
    service._recognized(phrase, tone(), 16000, words=[{"word": phrase, "conf": 1}])
    assert heard == ["Open YouTube"] and len(calls) == 1


def test_recognition_failure_queues_one_selected_voice_retry_per_original_wake(recognition, monkeypatch):
    service, heard, _states, _spoken, _calls, clock, result = recognition
    queued = []
    monkeypatch.setattr(service, "_enqueue_speech", lambda item: queued.append(item) or True)
    result["error"] = True
    service.gate.feed("джарвис", 100)
    service._recognized("какой план", tone(), 16000)
    clock[0] = 105
    service._recognized("какой план", tone(), 16000)
    assert len(queued) == 1 and not heard and service.gate.deadline == 110
    assert queued[0].preserve_wake_deadline and queued[0].is_current()
    clock[0] = 111
    assert not queued[0].is_current()
    service._recognized("какой план", tone(), 16000)
    assert len(queued) == 1 and service.gate.deadline == 0
    clock[0] = 120
    service._recognized("джарвис какой план", tone(), 16000)
    assert len(queued) == 2 and queued[-1].is_current() and not queued[0].is_current()


def test_failed_name_only_whisper_waits_for_followup_without_spamming_retry(recognition, monkeypatch):
    service, heard, _states, spoken, _calls, _clock, result = recognition
    queued = []
    monkeypatch.setattr(service, "_enqueue_speech", lambda item: queued.append(item) or True)
    result["error"] = True
    service._recognized("джарвис", tone(), 16000)
    assert spoken == ["Да, сэр"] and not queued and not heard and service.gate.deadline == 110


def test_cold_selected_voice_remains_pending_beyond_old_thirty_second_boundary(clone_service, monkeypatch):
    service, _clone, _states, _played = clone_service
    now = [100.0]

    class SlowColdClone:
        is_warming = True

        @property
        def is_ready(self):
            return now[0] >= 135

    monkeypatch.setattr(voice.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(service._closed, "wait", lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert service._wait_for_clone(SlowColdClone(), service._speech_generation, service.speech_character, None)
    assert 135 <= now[0] < 136


def test_queued_typed_responses_are_not_lost_while_selected_clone_warms(clone_service):
    service, clone, _states, played = clone_service
    service._speech.put("Первый ответ")
    service._speech.put("Второй ответ")
    service._speech.put(None)
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        clone.ready.set()
        clone.warming = False
        thread.join(1)
        assert not thread.is_alive() and len(played) == 2
        assert [call[1] for call in clone.calls if isinstance(call, tuple)] == ["Первый ответ", "Второй ответ"]
    finally:
        service.close()


def test_audible_retry_plays_inside_original_window_without_renewing_it(clone_service):
    service, clone, _states, played = clone_service
    clone.ready.set()
    clone.warming = False
    service.gate.feed("джарвис", time.monotonic())
    deadline = service.gate.deadline
    service._speech.put(voice._EventSpeech("Повтори команду, пожалуйста.", lambda: True, preserve_wake_deadline=True))
    service._speech.put(None)
    service._speak_loop()
    assert played and service.gate.deadline == deadline


def test_ack_preserves_pending_answer_until_followup_window_expires(clone_service):
    service, clone, _states, played = clone_service
    service.gate.timeout = 0.12
    service._speech.put("Ожидающий ответ")
    service._speech.put(None)
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        service.gate.feed("джарвис", time.monotonic())
        service.say("Да, сэр")
        await_condition(lambda: len(played) == 1)
        clone.ready.set()
        clone.warming = False
        assert not any(isinstance(call, tuple) for call in clone.calls)
        thread.join(1)
        assert not thread.is_alive() and len(played) == 2
        assert ("stream", "Ожидающий ответ") in clone.calls
    finally:
        service.close()


def test_verified_private_stereo_cue_plays_before_clone_without_tts_or_pcm_changes(clone_service, monkeypatch):
    service, clone, _states, played = clone_service
    pcm = b"\x01\x02\x03\x04" * 400
    reads = []
    monkeypatch.setitem(sys.modules, "desktop.audio_cues", SimpleNamespace(CueLibrary=lambda: SimpleNamespace(
        read=lambda name: reads.append(name) or SimpleNamespace(pcm=pcm, sample_rate=44100, channels=2))))
    monkeypatch.setattr(service, "_enqueue_speech", lambda item: service._speech.put((service._speech_generation, item)) or True)
    monkeypatch.setattr(service, "_play_pcm", lambda data, rate, **kwargs: played.append((data, rate, kwargs["channels"], service._speaking.is_set())))
    assert service.play_cue("welcome_home")
    service._speech.put(None)
    service._speak_loop()
    assert reads == ["welcome_home"] and played == [(pcm, 44100, 2, True)]
    assert not clone.calls and service.speech_backend == "personal_cue" and not service._speaking.is_set()


@pytest.mark.parametrize("name", ["../secret.wav", "https://example.test/audio.wav", "shutdown", None])
def test_arbitrary_cue_names_are_never_files_or_commands(clone_service, monkeypatch, name):
    service, _clone, _states, played = clone_service
    monkeypatch.setitem(sys.modules, "desktop.audio_cues", SimpleNamespace(CueLibrary=lambda: pytest.fail("No file read")))
    assert not service.play_cue(name) and not played


def test_missing_private_cue_returns_false_for_ui_selected_voice_fallback(clone_service, monkeypatch):
    service, _clone, _states, played = clone_service
    monkeypatch.setitem(sys.modules, "desktop.audio_cues", SimpleNamespace(CueLibrary=lambda: SimpleNamespace(read=lambda _: None)))
    assert not service.play_cue("welcome_home") and not played and service._speech_generation == 0


def test_muted_or_closed_cue_does_not_read_private_file(clone_service, monkeypatch):
    service, _clone, _states, _played = clone_service
    monkeypatch.setitem(sys.modules, "desktop.audio_cues", SimpleNamespace(CueLibrary=lambda: pytest.fail("No private file read")))
    service.speech_enabled = False
    assert not service.play_cue("welcome_home")
    service.speech_enabled = True
    service._closed.set()
    assert not service.play_cue("welcome_home")


def test_post_output_ui_does_not_claim_listening_during_echo_tail(recognition):
    service, heard, states, _spoken, _calls, clock, _result = recognition
    service.multilingual_stt_enabled = False
    service._thread = SimpleNamespace(is_alive=lambda: True)
    service.gate.feed("джарвис", clock[0])
    service._end_audio()
    assert states[-1] == "Закончил говорить. Готовлю микрофон…"
    clock[0] += 0.1
    service._recognized("как дела")
    assert not heard
    clock[0] += 0.2
    service._recognized("как дела")
    assert heard == ["как дела"]


def test_observed_whisper_wake_misspelling_is_stripped_only_after_real_wake(recognition):
    service, heard, _states, _spoken, calls, _clock, result = recognition
    result["text"] = "Джорвис, что ты умеешь?"
    service._recognized("джорвис что ты умеешь", tone(), 16000)
    assert not heard and not calls
    service._recognized("джарвис какой план", tone(), 16000)
    assert heard == ["что ты умеешь?"] and len(calls) == 1 and service.gate.deadline == 0
    assert "джорвис" not in WakeGate.WAKE
    assert voice.strip_wake_prefix("Джорвис, открой калькулятор") == "Джорвис, открой калькулятор"


def test_ack_arriving_during_stale_generation_wait_is_queued_instead_of_orphaned(clone_service, monkeypatch):
    service, _clone, _states, _played = clone_service
    queued = []
    monkeypatch.setattr(service, "_enqueue_speech", lambda item: queued.append(item))
    service._waiting_clone.set()
    service._waiting_generation = 0
    service.interrupt()
    service.say("Да, сэр")
    assert queued == ["Да, сэр"] and service._urgent_ack is None


def test_old_wait_cannot_remove_new_generation_urgent_ack(clone_service):
    service, clone, _states, _played = clone_service
    service._urgent_ack = 1
    service._play_urgent_ack(clone, 0, service.speech_character)
    assert service._urgent_ack == 1


def test_ready_warm_backend_is_projected_without_model_rehash_or_audio_initialization(clone_service, monkeypatch):
    service, clone, _states, _played = clone_service
    service._active_backend = clone
    service.speech_backend = "clone_warming"
    clone.ready.set()
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(voice, "model_ready", lambda _: False)
    assert service.available()["speech_backend"] == "clone"
    assert not clone.calls


def test_stereo_cue_output_keeps_exact_pcm_and_channel_layout(monkeypatch):
    service = VoiceService(lambda _: None, lambda _: None)
    outputs, options = [], []

    class Stream:
        def __init__(self, **kwargs):
            options.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def write(self, data):
            outputs.append(data)

    monkeypatch.setattr(voice.importlib, "import_module", lambda _: SimpleNamespace(RawOutputStream=Stream))
    pcm = b"\x01\x02\x03\x04" * 4000
    service._play_pcm(pcm, 44100, channels=2)
    assert options[0]["channels"] == 2 and options[0]["samplerate"] == 44100
    assert b"".join(outputs) == pcm and all(len(block) % 4 == 0 for block in outputs)
