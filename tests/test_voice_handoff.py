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


def listen_with_fake_devices(service, monkeypatch, final_text, pcm):
    """Exercise the actual listener loop with final/partial text always absent."""
    calls = []
    frames = [pcm[i:i + 1600] for i in range(0, len(pcm), 1600)] + [b"\0\0" * 800] * 12
    class InputQueue:
        def __init__(self):
            self.frames = iter(frames)
        def get_nowait(self):
            raise queue.Empty
        def get(self, timeout):
            try:
                return next(self.frames)
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
            return json.dumps({"text": final_text})
        def Reset(self):
            calls.append("reset")
    class Input:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
    vosk = SimpleNamespace(SetLogLevel=lambda _level: None, Model=lambda _path: object(),
                           KaldiRecognizer=lambda *_args: Recognizer())
    sound = SimpleNamespace(query_devices=lambda **_: {"default_samplerate": 16000},
                            RawInputStream=lambda **_: Input())
    stub = service._stt
    monkeypatch.setitem(sys.modules, "desktop.multilingual_stt", SimpleNamespace(MultilingualSTT=lambda: stub))
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


def test_fixed_ack_preempts_old_dynamic_warmup_without_a_second_output_worker(clone_service, monkeypatch):
    service, clone, _states, played = clone_service
    monkeypatch.setattr(service, "_enqueue_speech", lambda text: service._speech.put((service._speech_generation, text)))
    service._speech.put("Старый ответ")
    thread = worker(service)
    try:
        await_condition(service._waiting_clone.is_set)
        service.say("Да, сэр")
        service._speech.put(None)
        thread.join(1)
        assert not thread.is_alive() and played == [(b"\1\0" * 80, 24000)]
        assert not any(isinstance(call, tuple) for call in clone.calls)
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
