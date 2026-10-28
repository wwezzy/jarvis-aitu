"""Opt-in, offline microphone/wake-word recognition and Windows speech.

Audio is kept in a bounded memory queue only.  This module neither authorizes
OS actions nor sends audio to a server.  Callbacks run on worker threads: GUI
clients must marshal them onto their UI event loop.
"""
from __future__ import annotations

import importlib
import importlib.util
import itertools
import math
from collections import deque
from contextlib import ExitStack
from array import array
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from desktop.model_setup import default_model_dir, model_ready

_PREPARE_SPEECH = object()
SPEECH_ECHO_TAIL = 0.25
VOSK_ALIAS_CONFIDENCE = 0.90
CLONE_READY_TIMEOUT = 120.0
UTTERANCE_SILENCE_SECONDS = 0.8
_CONFIDENT_READ_ONLY = frozenset({"как дела", "что ты умеешь", "помощь", "команды", "привет", "ты здесь",
                                "how are you", "what can you do", "help", "hello", "are you there"})


def _pcm_rms(data):
    """An acoustic buffer trigger, never a wake/intent decision.

    Use zero-mean energy so silence and a DC offset do not start an utterance.
    A verified wake or a finite keyword candidate can hand off these samples.
    Candidates still require an exact leading wake before any command window.
    """
    if not isinstance(data, bytes) or len(data) < 4 or len(data) % 2:
        return 0.0
    values = array("h")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    mean = sum(values) / len(values)
    variance = max(0, sum(value * value for value in values) / len(values) - mean * mean)
    return math.sqrt(variance) / 32768


def pcm_active(data):
    return _pcm_rms(data) >= 0.002


def strip_wake_prefix(text, *, verified=False):
    text = text.strip()
    first = re.search(r"\w+", text, flags=re.UNICODE)
    # The CPU decoder can spell an already-verified Russian wake "Джорвис".
    # This normalization must never itself open a new invocation window.
    names = WakeGate.WAKE | ({"джорвис"} if verified is True else set())
    if first and first.group().casefold() in names:
        return text[first.end():].lstrip(" \t\r\n,.!?;:—-«»“”\"'()[]").rstrip(" \t\r\n«»“”\"'()[]")
    return text


def select_windows_voice(speaker, client):
    """Prefer an existing Russian male voice, without creating registry tokens.

    Windows exposes some installed voices through OneCore rather than the
    legacy collection. SetId's final False forbids creating a category. A
    rejected engine falls back to the next installed Russian voice.
    """
    candidates = []
    try:
        candidates.extend(itertools.islice(iter(speaker.GetVoices()), 128))
    except Exception:
        pass
    try:
        category = client.Dispatch("SAPI.SpObjectTokenCategory")
        category.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)
        candidates.extend(itertools.islice(iter(category.EnumerateTokens()), 128))
    except Exception:
        pass
    russian = []
    for token in candidates:
        try:
            if "419" not in str(token.GetAttribute("Language")).lower().split(";"):
                continue
            try:
                male = str(token.GetAttribute("Gender")).casefold() == "male"
            except Exception:
                male = False
            russian.append((not male, token))
        except Exception:
            continue
    for _, token in sorted(russian, key=lambda item: item[0]):
        try:
            speaker.Voice = token
            return True
        except Exception:
            continue
    return False


@dataclass(frozen=True)
class WakeDecision:
    kind: str
    text: str = ""


@dataclass(frozen=True)
class _EventSpeech:
    text: str
    is_current: Callable[[], bool]
    preserve_wake_deadline: bool = False


@dataclass(frozen=True)
class _CueSpeech:
    pcm: bytes
    sample_rate: int
    channels: int
    completion: object = None


class _CueCompletion:
    """Trusted callback receives one result, never a media path or command.

    A UI caller must only bridge this worker notification onto its Qt thread.
    """
    def __init__(self, callback):
        self._callback, self._lock = callback, threading.Lock()

    def finish(self, played):
        with self._lock:
            callback, self._callback = self._callback, None
        if callback is not None:
            try:
                callback(played is True)
            except Exception:
                pass


def _event_current(validator=None) -> bool:
    if validator is None:
        return True
    try:
        return validator() is True
    except Exception:
        return False


class UtteranceBuffer:
    """One second of preroll, then at most twelve seconds after speech starts.

    Continuous idle microphone samples never grow the command buffer or make
    the next real command look like an overlong utterance.
    """
    def __init__(self, sample_rate):
        self.maximum = sample_rate * 2 * 12
        self.silence_limit = int(sample_rate * 2 * UTTERANCE_SILENCE_SECONDS)
        self.reset()

    def reset(self):
        self._preroll = deque(maxlen=20)
        self._frames = []
        self._bytes = 0
        self._silence = 0
        self.started = self.overflow = False

    def append(self, data, *, active):
        if self.overflow:
            return
        if not self.started:
            self._preroll.append(data)
            if not active:
                return
            self.started = True
            self._frames = list(self._preroll)
            self._preroll.clear()
            self._bytes = sum(map(len, self._frames))
        else:
            self._frames.append(data)
            self._bytes += len(data)
        if self._bytes > self.maximum:
            self._frames.clear()
            self.overflow = True
        self._silence = 0 if active else self._silence + len(data)

    @property
    def finished(self):
        return self.started and self._silence >= self.silence_limit

    def pcm(self):
        return b"".join(self._frames) if self.started and not self.overflow else None


class _RecognitionSegments:
    """Keep early Vosk endpoints until the bounded acoustic phrase finishes."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.parts, self.words = [], []

    def add(self, result):
        text = result.get("text", "")
        if not isinstance(text, str) or len(text) > 2000 or len(self.parts) >= 32:
            raise ValueError("Invalid final speech segment")
        if not text.strip():
            return
        self.parts.append(text.strip())
        words = result.get("result")
        if self.words is not None:
            if not isinstance(words, list) or len(words) != len(text.split()) or len(self.words) + len(words) > 256:
                self.words = None
            else:
                self.words.extend(words)

    def result(self):
        text = " ".join(self.parts)
        if len(text) > 2000:
            raise ValueError("Final speech result too large")
        return {"text": text, "result": self.words}


class WakeGate:
    """Whole-token wake word, one command, bounded follow-up; no fuzzy match."""

    WAKE = {"джарвис", "джарвиз", "jarvis"}
    CANCEL = {"отмена", "не надо", "хватит", "стоп", "stop", "cancel", "stop listening"}

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.deadline = 0.0

    def reset(self):
        self.deadline = 0.0

    def extend(self, now: float):
        if self.deadline and now <= self.deadline:
            self.deadline = now + self.timeout
        elif self.deadline:
            self.reset()

    @classmethod
    def is_cancel(cls, text):
        return text.casefold().replace("ё", "е").strip(" \t\r\n,.!?;:«»“”\"'()[]") in cls.CANCEL

    def feed(self, text: str, now: float) -> WakeDecision:
        text = re.sub(r"\s+", " ", text.strip().lower().replace("ё", "е"))
        if self.deadline and now > self.deadline:
            self.reset()
        words = re.findall(r"[\w]+", text, flags=re.UNICODE)
        if not words:
            return WakeDecision("ignore")
        # A name buried inside a sentence is not a command invocation.
        if words[0] in self.WAKE:
            first = re.search(r"\w+", text, flags=re.UNICODE)
            rest = text[first.end():].lstrip(" \t\r\n,.!?;:—-«»“”\"'()[]").rstrip(" \t\r\n«»“”\"'()[]")
            if not rest:
                self.deadline = now + self.timeout
                return WakeDecision("wake")
            self.reset()
            return WakeDecision("command", rest)
        if not self.deadline or now > self.deadline:
            self.reset()
            return WakeDecision("ignore")
        self.reset()
        if self.is_cancel(text):
            return WakeDecision("cancel")
        return WakeDecision("command", text)


class VoiceService:
    """Explicit start/stop capture; missing optional dependencies never break text.

    ``available`` returns ready/model/dependencies/microphone/running/speaking
    booleans and a fixed reason label.  ``start`` returns immediately; the
    callback reports model loading, listening, recognized command or failure.
    """

    def __init__(self, on_text: Callable[[str], None], on_state: Callable[[str], None],
                 model_dir: Path | None = None, *, transcriber_factory=None, on_command=None):
        if transcriber_factory is not None and not callable(transcriber_factory):
            raise ValueError("A transcriber factory is required")
        if on_command is not None and not callable(on_command):
            raise ValueError("A command receiver is required")
        self._on_command = on_command
        self._transcriber_factory = transcriber_factory
        self.on_text, self.on_state = on_text, on_state
        self.model_dir = Path(model_dir) if model_dir else default_model_dir()
        self.gate = WakeGate()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._transcribing = threading.Event()
        self._audio_epoch = 0
        self.multilingual_stt_enabled = False
        self._stt = None
        self._english_wake = None
        self._active_backend = None
        self._utterance_audio_started = False
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=24)
        self._speech: queue.Queue = queue.Queue(maxsize=5)
        self._thread: threading.Thread | None = None
        self._speaker: threading.Thread | None = None
        self.speech_enabled = True
        self.speech_rate = 185
        self.speech_engine = "auto"
        self.speech_character = "aidar"
        self.speech_backend = "not_prepared"
        self._halt_speech = threading.Event()
        self._closed = threading.Event()
        self._speech_generation = 0
        self._command_cancel = threading.Event()
        self._waiting_clone = threading.Event()
        self._waiting_generation = None
        self._urgent_ack = None
        self._wake_serial, self._retry_serial = 0, None
        self._last_spoken = 0.0
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._reset_diagnostics()

    def _reset_diagnostics(self):
        with self._stats_lock:
            self._stats = {key: 0 for key in ("audio_callbacks", "suppressed_speech", "suppressed_decoder", "suppressed_echo",
                "queue_overflows", "capture_overflows", "maximum_queue", "utterances", "commands", "wake_shortcuts",
                "decoder_requests", "decoder_failures", "capture_sample_rate")}
            self._stats.update(last_decoder_ms=None, last_level_dbfs=None)

    def _count(self, key):
        with self._stats_lock:
            self._stats[key] += 1

    def diagnostics(self):
        """Session-only numeric counters: no audio, transcripts, files or IDs."""
        with self._stats_lock:
            return dict(self._stats)

    def _emit(self, callback, value):
        try:
            callback(value)
        except Exception:
            # A disposed window must not strand a recording thread.
            pass

    def available(self) -> dict:
        dependencies = all(importlib.util.find_spec(name) is not None
                           for name in ("vosk", "sounddevice"))
        model = model_ready(self.model_dir)
        microphone = False
        if dependencies:
            try:
                device = importlib.import_module("sounddevice").query_devices(kind="input")
                microphone = bool(device and device.get("max_input_channels", 0) > 0)
            except Exception:
                pass
        reason = ("dependencies_missing" if not dependencies else "model_missing" if not model
                  else "microphone_missing" if not microphone else "ready")
        if self.speech_engine == "clone" and self.speech_backend in {"not_prepared", "clone_warming", "clone_unavailable"}:
            try:
                if self._active_backend is not None and self._active_backend.is_ready:
                    self.speech_backend = "clone"
            except Exception:
                pass
        return {"ready": dependencies and model and microphone, "model": model,
                "dependencies": dependencies, "microphone": microphone, "reason": reason,
                "running": bool(self._thread and self._thread.is_alive()),
                "speaking": self._speaking.is_set(), "speech_backend": self.speech_backend,
                "diagnostics": self.diagnostics()}

    def start(self) -> bool:
        with self._lock:
            if self._closed.is_set():
                return False
            if self._thread and self._thread.is_alive():
                return not self._stop.is_set()
            report = self.available()
            if not report["ready"]:
                labels = {"dependencies_missing": "Голосовые компоненты не установлены. Текстовый ввод работает.",
                          "model_missing": "Сначала загрузите локальную модель речи в настройках.",
                          "microphone_missing": "Микрофон не найден. Проверьте устройство и разрешения Windows."}
                self._emit(self.on_state, labels[report["reason"]])
                return False
            self._stop.clear()
            self._reset_diagnostics()
            self.gate.reset()
            self._clear_audio()
            self._thread = threading.Thread(target=self._listen, name="JarvisMicrophone", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        self._stop.set()
        self.interrupt()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.gate.reset()
        self._clear_audio()
        self._emit(self.on_state, "Микрофон выключен")

    def _clear_audio(self):
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                break

    def _audio_callback(self, data, frames, timing, status):
        self._count("audio_callbacks")
        if self._stop.is_set():
            return
        if self._speaking.is_set():
            self._count("suppressed_speech")
            return
        if self._transcribing.is_set():
            self._count("suppressed_decoder")
            return
        if time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL:
            self._count("suppressed_echo")
            return
        if getattr(status, "input_overflow", False):
            self._count("capture_overflows")
            self._clear_audio()
            self.gate.reset()
            self._audio_epoch += 1
            return
        try:
            self._audio.put_nowait(bytes(data))
            with self._stats_lock:
                self._stats["maximum_queue"] = max(self._stats["maximum_queue"], self._audio.qsize())
        except queue.Full:
            # Drop old audio rather than execute a stale delayed utterance.
            self._clear_audio()
            self.gate.reset()
            self._audio_epoch += 1
            self._count("queue_overflows")

    @staticmethod
    def _complete_name_only(text, words, pcm, sample_rate, pcm_start):
        """A high-confidence final wake plus silent aligned tail needs no ASR.

        Vosk 0.3.45 word times are cumulative across Reset(), so the listener
        supplies the accepted-sample offset of this bounded original buffer.
        Missing timing or additional audible speech cannot take the shortcut.
        """
        if (not isinstance(words, list) or len(words) != 1 or not isinstance(words[0], dict)
                or not isinstance(pcm, bytes) or len(pcm) % 2 or type(sample_rate) is not int or not 8000 <= sample_rate <= 48000
                or type(pcm_start) not in {int, float} or not math.isfinite(pcm_start) or pcm_start < 0):
            return False
        word = words[0]
        if (not isinstance(word.get("word"), str) or word["word"] not in WakeGate.WAKE
                or re.findall(r"\w+", text.casefold()) != [word["word"]]
                or any(type(word.get(key)) not in {int, float} or not math.isfinite(word[key]) for key in ("start", "end", "conf"))
                or not 0.95 <= word["conf"] <= 1 or not 0 <= word["start"] < word["end"]
                or not pcm_start - 0.1 <= word["start"] < word["end"] <= pcm_start + len(pcm) / (2 * sample_rate)):
            return False
        start = max(0, round((word["end"] - pcm_start + 0.12) * sample_rate)) * 2
        tail, frame, consecutive = pcm[start:], max(2, sample_rate // 50 * 2), 0
        if len(tail) < sample_rate // 4 * 2:
            return False
        for offset in range(0, len(tail) - frame + 1, frame):
            consecutive = consecutive + 1 if pcm_active(tail[offset:offset + frame]) else 0
            if consecutive >= 3:
                return False
        return True

    @staticmethod
    def _capture_configuration(sound):
        """Pin the same default input; use 16k only when its SDK probe accepts."""
        info = sound.query_devices(kind="input")
        rate, device = int(info["default_samplerate"]), None
        try:
            candidate = sound.default.device[0]
            if type(candidate) is int and candidate >= 0:
                device = candidate
        except (AttributeError, IndexError, TypeError):
            pass
        check = getattr(sound, "check_input_settings", None)
        if device is not None and callable(check):
            try:
                if check(device=device, channels=1, dtype="int16", samplerate=16000) is None:
                    rate = 16000
            except Exception:
                pass  # Preserve the original rate and device; no OS setting changes.
        return rate, device

    @staticmethod
    def _confident_alias(full_text, command, words):
        from desktop.core import ALIASES, normalize
        if normalize(command) not in {*ALIASES, *_CONFIDENT_READ_ONLY} or not isinstance(words, list) or not words:
            return False
        recognized = []
        for word in words:
            if (not isinstance(word, dict) or not isinstance(word.get("word"), str)
                    or not re.fullmatch(r"\w{1,80}", word["word"], re.UNICODE)):
                return False
            confidence = word.get("conf")
            if (type(confidence) not in {int, float} or not math.isfinite(confidence)
                    or not VOSK_ALIAS_CONFIDENCE <= confidence <= 1):
                return False
            recognized.append(word["word"])
        return normalize(" ".join(recognized)) == normalize(full_text)

    def _decode_command(self, text, pcm=None, sample_rate=None, *, confident=False, request_context=None):
        with self._lock:
            context = request_context or (self._speech_generation, self._audio_epoch, self._stt, self._command_cancel)
        generation, epoch, decoder, canceled = context

        def current():
            return (not canceled.is_set() and not self._stop.is_set() and not self._closed.is_set()
                    and self._speech_generation == generation and self._audio_epoch == epoch
                    and self._stt is decoder and self.multilingual_stt_enabled)

        if canceled.is_set() or generation != self._speech_generation:
            return None
        if not self.multilingual_stt_enabled or self._stt is None:
            return text
        # Text equality alone is not acoustic confidence. Only the caller's
        # validated final Vosk word scores permit the finite utility shortcut.
        if confident:
            return text
        if pcm is None:
            self._emit(self.on_state, "Недостаточно аудио для уверенного распознавания. Повтори команду.")
            return None
        self._transcribing.set()
        self._clear_audio()
        started = time.monotonic()
        self._count("decoder_requests")
        try:
            bounded = getattr(decoder, "transcribe_cancelable", None)
            decoded = (bounded(pcm, sample_rate, cancel_event=canceled) if callable(bounded)
                       else decoder.transcribe(pcm, sample_rate))
            if not current():
                return None
            return strip_wake_prefix(decoded, verified=True)
        except Exception:
            self._count("decoder_failures")
            if current():
                self._emit(self.on_state, "Не удалось уверенно распознать команду. Повтори её или напиши текстом.")
            return None
        finally:
            with self._stats_lock:
                self._stats["last_decoder_ms"] = round(max(0, time.monotonic() - started) * 1000)
            self._clear_audio()
            self._transcribing.clear()

    def _decode_english_wake(self, pcm, sample_rate, started):
        """A keyword candidate needs a fresh exact leading wake in full audio.

        The optional English keyword model cannot itself open a command window.
        Keep the original candidate deadline and invalidate work on any capture,
        output or preference change while the second decoder is running.
        """
        if (not self.multilingual_stt_enabled or self._stt is None
                or not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
                or type(sample_rate) is not int or not 8000 <= sample_rate <= 48000
                or len(pcm) > sample_rate * 2 * 12):
            return None
        decoder = self._stt
        epoch, generation = self._audio_epoch, self._speech_generation
        serial, deadline = self._wake_serial, self.gate.deadline
        last_spoken = self._last_spoken
        self._transcribing.set()
        self._clear_audio()
        decoding_started = time.monotonic()
        self._count("decoder_requests")
        try:
            # An idle English candidate is not permission to upload audio.
            # Optional cloud decoders expose a strictly local verification path.
            local_decode = getattr(decoder, "transcribe_local", None)
            decoded = local_decode(pcm, sample_rate) if callable(local_decode) else decoder.transcribe(pcm, sample_rate)
            if (self._stop.is_set() or self._closed.is_set() or self._speaking.is_set()
                    or not self.multilingual_stt_enabled or self._stt is not decoder
                    or self._audio_epoch != epoch or self._speech_generation != generation
                    or self._wake_serial != serial or self.gate.deadline != deadline
                    or self._last_spoken != last_spoken
                    or time.monotonic() > started + self.gate.timeout
                    or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL
                    or not isinstance(decoded, str) or not 1 <= len(decoded) <= 500):
                return None
            first = re.search(r"\w+", decoded, flags=re.UNICODE)
            return decoded if first and first.group().casefold() in WakeGate.WAKE else None
        except Exception:
            self._count("decoder_failures")
            # A false/uncertain idle candidate must stay silent and unarmed.
            return None
        finally:
            with self._stats_lock:
                self._stats["last_decoder_ms"] = round(max(0, time.monotonic() - decoding_started) * 1000)
            self._clear_audio()
            self._transcribing.clear()

    @property
    def command_generation(self):
        return self._speech_generation

    def set_command_receiver(self, receiver):
        if receiver is not None and not callable(receiver):
            raise ValueError("A command receiver is required")
        with self._lock:
            self._on_command = receiver

    def _emit_command(self, text, generation):
        if generation != self._speech_generation or self._stop.is_set() or self._closed.is_set():
            return
        if self._on_command is None:
            self._emit(self.on_text, text)
        else:
            try:
                self._on_command(text, generation)
            except Exception:
                pass  # A closed GUI cannot revive or execute this command.

    def _recognized(self, text: str, pcm=None, sample_rate=None, *, words=None, english_candidate=False,
                    pcm_start=None, expected_generation=None):
        with self._lock:
            context = (self._speech_generation, self._audio_epoch, self._stt, self._command_cancel)
        generation, epoch, decoder, canceled = context

        def current():
            return (not canceled.is_set() and not self._stop.is_set() and not self._closed.is_set()
                    and generation == self._speech_generation and epoch == self._audio_epoch
                    and decoder is self._stt)

        if expected_generation is not None and expected_generation != generation:
            return
        if (self._stop.is_set() or self._closed.is_set() or self._speaking.is_set()
                or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL):
            return
        now = time.monotonic()
        previous_deadline = self.gate.deadline
        armed = bool(previous_deadline and now <= previous_deadline)
        first = re.search(r"\w+", text, flags=re.UNICODE)
        verified_wake = bool(first and first.group().casefold() in WakeGate.WAKE)
        english_decoded = None
        if english_candidate is True and not verified_wake and not armed:
            english_decoded = self._decode_english_wake(pcm, sample_rate, now)
            if english_decoded is None or not current():
                return
            text, verified_wake = english_decoded, True
        result = self.gate.feed(text, now)
        if verified_wake:
            self._wake_serial += 1
        retry_deadline = now + self.gate.timeout if verified_wake else previous_deadline if armed else 0
        bilingual_audio = self.multilingual_stt_enabled and self._stt is not None and pcm is not None
        # A Russian decoder can produce an empty string for an English phrase.
        # Only a still-armed, previously verified wake allows its audio through.
        if result.kind == "ignore" and armed and bilingual_audio and not text.strip():
            self.gate.reset()
            result = WakeDecision("command", "")
        if result.kind == "command" and WakeGate.is_cancel(result.text):
            self.gate.reset()
            result = WakeDecision("cancel")
        if result.kind == "wake":
            if not bilingual_audio or self._complete_name_only(text, words, pcm, sample_rate, pcm_start):
                if not current():
                    return
                self._count("wake_shortcuts")
                self._emit(self.on_state, "Слушаю команду…")
                self.say("Да, сэр")
                return
            # Vosk may omit an English tail in this same utterance. Decode it
            # before inviting a follow-up: saying ACK during synchronous ASR
            # otherwise causes the user's next command to be discarded.
        if result.kind in {"wake", "command"}:
            self._emit(self.on_state, "Обрабатываю команду…")
            confident = result.kind == "command" and self._confident_alias(text, result.text, words)
            decoded = (strip_wake_prefix(english_decoded) if english_decoded is not None else
                       self._decode_command(result.text if result.kind == "command" else text,
                                            pcm, sample_rate, confident=confident, request_context=context))
            if not current():
                return
            if decoded is None or not decoded.strip():
                # A failed decoder cannot keep extending authorization. Retry
                # uses the original finite window, never a fresh ten seconds.
                self.gate.deadline = retry_deadline if time.monotonic() <= retry_deadline else 0
                if decoded is None:
                    self._recognition_retry(retry_deadline)
                if decoded == "" and self.gate.deadline:
                    self._emit(self.on_state, "Слушаю команду…")
                    self.say("Да, сэр")
                return
            self.gate.reset()
            if WakeGate.is_cancel(decoded):
                self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                self._emit_command("стоп", generation)
            else:
                self._count("commands")
                self._emit_command(decoded, generation)
        elif result.kind == "cancel":
            self._emit(self.on_state, "Ожидаю слово «Джарвис»")
            self._emit_command("стоп", generation)

    def _recognition_retry(self, deadline):
        """One audible clarification per real wake; it never renews permission."""
        serial = self._wake_serial
        if (not deadline or time.monotonic() > deadline or self._retry_serial == serial
                or self._stop.is_set() or self._closed.is_set()):
            return
        self._retry_serial = serial

        def current():
            return (self._wake_serial == serial and bool(self.gate.deadline)
                    and time.monotonic() <= min(deadline, self.gate.deadline)
                    and not self._stop.is_set() and not self._closed.is_set())

        self._enqueue_speech(_EventSpeech("Не расслышал команду. Повтори её, пожалуйста, или напиши текстом.",
                                         current, preserve_wake_deadline=True))

    def _listen(self):
        english = None

        def close_english():
            nonlocal english
            if english is not None:
                try:
                    english.close()
                except Exception:
                    pass
            english = self._english_wake = None

        def reset_english():
            if english is not None:
                try:
                    english.reset()
                except Exception:
                    close_english()

        try:
            vosk = importlib.import_module("vosk")
            sound = importlib.import_module("sounddevice")
            self._emit(self.on_state, "Загружаю локальную модель…")
            vosk.SetLogLevel(-1)
            model = vosk.Model(str(self.model_dir))
            if self.multilingual_stt_enabled:
                try:
                    from desktop.multilingual_stt import MultilingualSTT
                    self._emit(self.on_state, "Готовлю русское и английское распознавание…")
                    self._dispose_backend(self._stt)
                    self._stt = self._transcriber_factory() if self._transcriber_factory is not None else MultilingualSTT()
                    if not self._stt.prepare():
                        self._stt = None
                        self._emit(self.on_state, "Два языка пока недоступны. Русское распознавание продолжает работать.")
                except Exception:
                    self._stt = None
                    self._emit(self.on_state, "Два языка пока недоступны. Русское распознавание продолжает работать.")
            sample_rate, device = self._capture_configuration(sound)
            with self._stats_lock:
                self._stats["capture_sample_rate"] = sample_rate
            recognizer = vosk.KaldiRecognizer(model, sample_rate)
            set_words = getattr(recognizer, "SetWords", None)
            if callable(set_words):
                set_words(True)
            if self.multilingual_stt_enabled and self._stt is not None and not self._stop.is_set():
                try:
                    from desktop.english_wake import EnglishWakeDetector
                    english = EnglishWakeDetector()
                    if not english.prepare(sample_rate, vosk_module=vosk):
                        close_english()
                    else:
                        self._english_wake = english
                except Exception:
                    close_english()
            if self._stop.is_set():
                return
            with sound.RawInputStream(device=device, samplerate=sample_rate, blocksize=max(400, int(sample_rate * 0.05)),
                                      dtype="int16", channels=1, callback=self._audio_callback):
                self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                reset_after_speech = self._last_spoken
                audio_epoch, utterance = self._audio_epoch, UtteranceBuffer(sample_rate)
                generation, bilingual = self._speech_generation, self.multilingual_stt_enabled
                segments, accepted_samples = _RecognitionSegments(), 0
                while not self._stop.is_set():
                    if english is not None and not self.multilingual_stt_enabled:
                        close_english()
                        recognizer.Reset()
                        utterance.reset()
                        segments.reset()
                    if self._speaking.is_set():
                        reset_english()
                        utterance.reset()
                        segments.reset()
                        self._clear_audio()
                        self._stop.wait(0.05)
                        continue
                    echo_remaining = SPEECH_ECHO_TAIL - (time.monotonic() - self._last_spoken)
                    if echo_remaining > 0:
                        reset_english()
                        utterance.reset()
                        segments.reset()
                        self._clear_audio()
                        self._stop.wait(min(0.05, echo_remaining))
                        continue
                    if (reset_after_speech != self._last_spoken or audio_epoch != self._audio_epoch
                            or generation != self._speech_generation or bilingual != self.multilingual_stt_enabled):
                        recognizer.Reset()
                        self._clear_audio()
                        reset_after_speech = self._last_spoken
                        audio_epoch = self._audio_epoch
                        generation, bilingual = self._speech_generation, self.multilingual_stt_enabled
                        utterance.reset()
                        reset_english()
                        segments.reset()
                        self._emit(self.on_state, "Слушаю команду…" if self._followup_armed() else "Ожидаю слово «Джарвис»")
                    try:
                        data = self._audio.get(timeout=0.2)
                    except queue.Empty:
                        if self.gate.deadline and time.monotonic() > self.gate.deadline:
                            self.gate.reset()
                            self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                        continue
                    if self._stop.is_set() or self._closed.is_set():
                        break
                    if (audio_epoch != self._audio_epoch or generation != self._speech_generation
                            or bilingual != self.multilingual_stt_enabled or self._speaking.is_set()
                            or reset_after_speech != self._last_spoken
                            or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL):
                        # A callback may revoke the capture while get() waits.
                        # This frame cannot finish an utterance from that epoch.
                        continue
                    complete = recognizer.AcceptWaveform(data)
                    accepted_samples += len(data) // 2
                    result = json.loads(recognizer.Result()) if complete else None
                    if self.multilingual_stt_enabled:
                        was_started = utterance.started
                        level = _pcm_rms(data)
                        with self._stats_lock:
                            self._stats["last_level_dbfs"] = round(20 * math.log10(max(level, 0.000001)))
                        utterance.append(data, active=level >= 0.002)
                        if english is not None and utterance.started and not utterance.overflow:
                            try:
                                # First active frame is already in pcm(), together
                                # with the complete bounded preroll. Never feed
                                # idle time or duplicate this onset frame.
                                english.accept(data if was_started else utterance.pcm())
                            except Exception:
                                close_english()
                        if complete:
                            segments.add(result)
                        # Vosk may end a segment at a short mid-sentence pause.
                        # Keep that literal result and the whole original PCM
                        # until the longer acoustic endpoint is reached.
                        if utterance.finished:
                            segments.add(json.loads(recognizer.FinalResult()))
                            result, complete = segments.result(), True
                        elif utterance.started:
                            complete = False
                    if complete or utterance.overflow:
                        self._count("utterances")
                        result = result or {"text": ""}
                        if utterance.overflow:
                            first = re.search(r"\w+", result.get("text", ""), flags=re.UNICODE)
                            invoked = bool(first and first.group().casefold() in WakeGate.WAKE)
                            was_armed = bool(self.gate.deadline and time.monotonic() <= self.gate.deadline)
                            self.gate.reset()
                            if invoked or was_armed:
                                self._emit(self.on_state, "Фраза слишком длинная. Повтори короткую команду после «Джарвис».")
                        else:
                            candidate = False
                            if english is not None and self.multilingual_stt_enabled:
                                try:
                                    candidate = english.finish() is True
                                except Exception:
                                    close_english()
                            if (audio_epoch == self._audio_epoch and generation == self._speech_generation
                                    and bilingual == self.multilingual_stt_enabled
                                    and reset_after_speech == self._last_spoken):
                                pcm = utterance.pcm()
                                pcm_start = max(0, (accepted_samples - len(pcm) // 2) / sample_rate) if pcm is not None else None
                                self._recognized(result.get("text", ""), pcm, sample_rate,
                                                 words=result.get("result"), english_candidate=candidate, pcm_start=pcm_start,
                                                 expected_generation=generation)
                        recognizer.Reset()
                        utterance.reset()
                        reset_english()
                        segments.reset()
        except Exception as error:
            self._emit(self.on_state, f"Голос недоступен ({type(error).__name__}). Текстовый ввод работает.")
        finally:
            close_english()
            self._clear_audio()
            self.gate.reset()

    def apply_preferences(self, preferences: dict):
        """Apply bounded local settings; no model download or recording."""
        engine = preferences.get("voice_engine", self.speech_engine)
        rate = preferences.get("voice_rate", self.speech_rate)
        character = preferences.get("voice_character", self.speech_character)
        multilingual = preferences.get("multilingual_stt_enabled", self.multilingual_stt_enabled)
        if type(multilingual) is not bool:
            raise ValueError("Boolean recognition preference required")
        if not isinstance(engine, str) or engine not in {"auto", "windows", "expressive", "clone"}:
            raise ValueError("Unsupported speech engine")
        if not isinstance(character, str) or character not in {"aidar", "eugene"}:
            raise ValueError("Unsupported speech character")
        if type(rate) is not int or not 90 <= rate <= 240:
            raise ValueError("Voice rate must be 90..240")
        if engine != self.speech_engine or rate != self.speech_rate or character != self.speech_character:
            self.interrupt()
        if engine != self.speech_engine or character != self.speech_character:
            self._close_backend()
        self.speech_engine, self.speech_rate = engine, rate
        self.speech_character = character
        if multilingual != self.multilingual_stt_enabled:
            self.gate.reset()
            self._audio_epoch += 1
            self._clear_audio()
        self.multilingual_stt_enabled = multilingual
        self.speech_enabled = bool(preferences.get("speech_enabled", self.speech_enabled))
        if not self.speech_enabled:
            self.interrupt()
            self._close_backend()

    def interrupt(self):
        """Stop old speech without stopping the microphone or authorizing actions."""
        canceled = []
        with self._lock:
            self._speech_generation += 1
            self._command_cancel.set()
            self._command_cancel = threading.Event()
            self._halt_speech.set()
            self._urgent_ack = None
            while True:
                try:
                    item = self._speech.get_nowait()
                    item = item[1] if isinstance(item, tuple) else item
                    if isinstance(item, _CueSpeech) and item.completion is not None:
                        canceled.append(item.completion)
                except queue.Empty:
                    break
        cancel_decoder = getattr(self._stt, "cancel", None)
        if callable(cancel_decoder):
            try:
                cancel_decoder()
            except Exception:
                pass
        for completion in canceled:
            completion.finish(False)

    def _enqueue_speech(self, item):
        if not self.speech_enabled or self._closed.is_set() or os.name != "nt":
            return False
        with self._lock:
            if not self._speaker or not self._speaker.is_alive():
                self._speaker = threading.Thread(target=self._speak_loop, name="JarvisSpeech", daemon=True)
                self._speaker.start()
            try:
                self._speech.put_nowait((self._speech_generation, item))
            except queue.Full:
                if item is not _PREPARE_SPEECH:
                    self._emit(self.on_state, "Очередь озвучивания заполнена. Ответ остаётся на экране.")
                return False
            return True

    def prepare(self):
        """Warm CPU speech and cache the wake reply, asynchronously and silently."""
        self._enqueue_speech(_PREPARE_SPEECH)

    @staticmethod
    def _speech_text(text: str) -> str:
        text = re.sub(r"^Локальная модель:\s*", "", str(text).strip())
        text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+)", "", text)
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 700:
            short = text[:650]
            sentence_end = max(short.rfind("."), short.rfind("!"), short.rfind("?"))
            short = short[:sentence_end + 1] if sentence_end >= 200 else short.rsplit(" ", 1)[0].rstrip(",;:") + "."
            text = short + " Подробности на экране."
        return text

    def say(self, text: str):
        """Speak bounded plain text. UI labels/Markdown are not read as dialogue."""
        text = self._speech_text(text)
        if text.casefold().rstrip(".!?") in {"да сэр", "да, сэр"}:
            with self._lock:
                if (self.speech_engine == "clone" and self._waiting_clone.is_set()
                        and self._waiting_generation == self._speech_generation
                        and self.speech_enabled and not self._closed.is_set()):
                    # One output worker plays the cached ACK without deleting
                    # the reply that is still waiting for the selected voice.
                    self._urgent_ack = self._speech_generation
                    return
        if text:
            self._enqueue_speech(text)

    def play_cue(self, name, *, on_complete=None):
        """Queue a verified cue; notify exactly once after drain or cancellation.

        The trusted UI callback must only emit its own thread-safe signal. This
        worker never follows the notification with a Core/OS action itself.
        """
        completion = _CueCompletion(on_complete) if callable(on_complete) else None
        def refused():
            if completion is not None:
                completion.finish(False)
            return False
        if on_complete is not None and not callable(on_complete):
            return refused()
        if name != "welcome_home" or not self.speech_enabled or self._closed.is_set():
            return refused()
        try:
            from desktop.audio_cues import CueLibrary
            cue = CueLibrary().read(name)
            if (cue is None or not isinstance(cue.pcm, bytes) or not cue.pcm
                    or type(cue.channels) is not int or cue.channels not in {1, 2}
                    or type(cue.sample_rate) is not int or not 16000 <= cue.sample_rate <= 48000
                    or len(cue.pcm) % (2 * cue.channels) or len(cue.pcm) > cue.sample_rate * 2 * cue.channels * 20):
                return refused()
            self.interrupt()
            if self._enqueue_speech(_CueSpeech(cue.pcm, cue.sample_rate, cue.channels, completion)) is True:
                return True
            return refused()
        except Exception:
            return refused()

    def say_event(self, text: str, *, is_current):
        """Recheck a versioned plan event in the audio worker, never dispatch it."""
        if not callable(is_current):
            raise ValueError("A current-event validator is required")
        text = self._speech_text(text)
        if text and _event_current(is_current):
            self._enqueue_speech(_EventSpeech(text, is_current))

    def close(self):
        self._closed.set()
        self.stop()
        self._dispose_backend(self._stt)
        self._stt = None
        self._close_backend()
        try:
            self._speech.put_nowait(None)
        except queue.Full:
            pass
        if self._speaker and self._speaker is not threading.current_thread():
            self._speaker.join(timeout=3)

    def _close_backend(self):
        with self._lock:
            backend, self._active_backend = self._active_backend, None
        self._dispose_backend(backend)

    @staticmethod
    def _dispose_backend(backend):
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _backend_current(self, generation, engine, character):
        return (generation == self._speech_generation and not self._closed.is_set()
                and self.speech_enabled and self.speech_engine == engine
                and self.speech_character == character)

    def _wait_for_clone(self, backend, generation, character, validator, *, deadline=None, hold_followup=True):
        """Keep the answer bounded in RAM while warmup/verified follow-up runs."""
        deadline = time.monotonic() + CLONE_READY_TIMEOUT if deadline is None else deadline
        with self._lock:
            self._waiting_generation = generation
            self._waiting_clone.set()
        try:
            while self._backend_current(generation, "clone", character) and _event_current(validator):
                self._play_urgent_ack(backend, generation, character)
                if backend.is_ready:
                    # Do not speak an old answer over the command the owner is
                    # about to give after the cached wake acknowledgment.
                    if not hold_followup or not self._followup_armed():
                        return True
                if not getattr(backend, "is_warming", False) or time.monotonic() >= deadline:
                    if not backend.is_ready or time.monotonic() >= deadline:
                        return False
                self._closed.wait(0.05)
            return False
        finally:
            with self._lock:
                if self._waiting_generation == generation:
                    self._waiting_clone.clear()
                    self._waiting_generation = None

    def _followup_armed(self):
        return bool(self.gate.deadline and time.monotonic() <= self.gate.deadline)

    def _begin_audio(self, generation):
        with self._lock:
            if generation != self._speech_generation or self._closed.is_set() or not self.speech_enabled:
                return False
            self._halt_speech.clear()
            self._speaking.set()
            self._utterance_audio_started = False
        self._clear_audio()
        self._emit(self.on_state, "Говорю…")
        return True

    def _end_audio(self, *, preserve_wake_deadline=False):
        self._last_spoken = time.monotonic()
        if not preserve_wake_deadline:
            self.gate.extend(self._last_spoken)
        self._clear_audio()
        self._speaking.clear()
        self._emit(self.on_state, "Закончил говорить. Готовлю микрофон…" if self._thread and self._thread.is_alive()
                   and not self._stop.is_set() else "Микрофон выключен")

    def _play_urgent_ack(self, backend, generation, character):
        with self._lock:
            urgent = self._urgent_ack
            if urgent == generation:
                self._urgent_ack = None
        if urgent != generation or not self._backend_current(generation, "clone", character):
            return
        try:
            fixed = backend.fixed_ack("ru")
        except Exception:
            fixed = None
        if fixed is None or not self._begin_audio(generation):
            return
        try:
            self._play_pcm(*fixed, is_current=lambda: self._backend_current(generation, "clone", character))
        finally:
            self._end_audio()

    def _play_pcm_stream(self, chunks, *, is_current=None):
        """Keep one audio stream open while the isolated voice yields PCM."""
        sound, stream, rate, total = None, None, None, 0
        try:
            with ExitStack() as stack:
                for pcm, chunk_rate in chunks:
                    if (self._halt_speech.is_set() or self._closed.is_set() or not self.speech_enabled
                            or not _event_current(is_current)):
                        if stream is not None:
                            stream.abort()
                        return
                    if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
                            or type(chunk_rate) is not int or not 8000 <= chunk_rate <= 48000
                            or rate is not None and chunk_rate != rate):
                        raise ValueError("Invalid streamed audio")
                    rate, total = chunk_rate, total + len(pcm)
                    if total > rate * 2 * 60:
                        raise ValueError("Streamed audio exceeded its limit")
                    if stream is None:
                        sound = importlib.import_module("sounddevice")
                        stream = stack.enter_context(sound.RawOutputStream(samplerate=rate, dtype="int16", channels=1,
                                                                          blocksize=0, latency="low"))
                    for offset in range(0, len(pcm), 2048):
                        if (self._halt_speech.is_set() or self._closed.is_set() or not self.speech_enabled
                                or not _event_current(is_current)):
                            stream.abort()
                            return
                        stream.write(pcm[offset:offset + 2048])
                        self._utterance_audio_started = True
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()

    def _play_pcm(self, pcm: bytes, sample_rate: int, *, is_current=None, channels=1):
        """Use short output blocks for prompt stop/mute; never store audio."""
        if (type(channels) is not int or channels not in {1, 2}
                or not isinstance(pcm, bytes) or not pcm or len(pcm) % (2 * channels)
                or type(sample_rate) is not int or not 8000 <= sample_rate <= 48000
                or len(pcm) > sample_rate * 2 * channels * 120):
            raise ValueError("Invalid synthesized audio")
        if self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set() or not _event_current(is_current):
            return False
        sound = importlib.import_module("sounddevice")
        with sound.RawOutputStream(samplerate=sample_rate, dtype="int16", channels=channels,
                                   blocksize=0, latency="low") as stream:
            for offset in range(0, len(pcm), 2048):
                if (self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set()
                        or not _event_current(is_current)):
                    stream.abort()
                    return False
                stream.write(pcm[offset:offset + 2048])
                self._utterance_audio_started = True
        # RawOutputStream.__exit__ stops and drains the device before this point.
        return (not self._halt_speech.is_set() and self.speech_enabled and not self._closed.is_set()
                and _event_current(is_current))

    def _speak_loop(self):
        # Neural synthesis and COM remain on this one worker. Fallback COM is
        # lazy: an unavailable Windows voice cannot prevent neural speech.
        initialized, speaker, pythoncom, neural, neural_key = False, None, None, None, None
        try:
            while True:
                item = self._speech.get()
                if item is None:
                    break
                generation, text = item if isinstance(item, tuple) else (self._speech_generation, item)
                if isinstance(text, _CueSpeech):
                    if not self._begin_audio(generation):
                        if text.completion is not None:
                            text.completion.finish(False)
                        continue
                    played = False
                    try:
                        self.speech_backend = "personal_cue"
                        played = self._play_pcm(text.pcm, text.sample_rate, channels=text.channels,
                            is_current=lambda: generation == self._speech_generation) is True
                    except Exception:
                        self._emit(self.on_state, "Личный аудиосигнал недоступен. Ответ остаётся на экране.")
                    finally:
                        self._end_audio()
                        if text.completion is not None:
                            text.completion.finish(played and generation == self._speech_generation
                                and self.speech_enabled and not self._closed.is_set())
                    continue
                validator = text.is_current if isinstance(text, _EventSpeech) else None
                preserve_wake_deadline = isinstance(text, _EventSpeech) and text.preserve_wake_deadline
                voice_deadline = time.monotonic() + CLONE_READY_TIMEOUT
                if validator is not None:
                    text = text.text
                if not _event_current(validator):
                    continue
                if not self.speech_enabled or self._closed.is_set():
                    continue
                engine, speech_rate, character = self.speech_engine, self.speech_rate, self.speech_character
                use_neural = False
                renderer, fixed_audio = None, None
                if engine in {"auto", "expressive", "clone"}:
                    try:
                        key = (engine, character if engine == "expressive" else None)
                        if neural is None or neural_key != key or self._active_backend is not neural:
                            self._close_backend()
                            if engine == "clone":
                                from desktop.clone_speech import CloneSpeech
                                candidate = CloneSpeech(personal_use=True)
                            elif engine == "expressive":
                                from desktop.expressive_speech import ExpressiveSpeech
                                candidate = ExpressiveSpeech(voice=character, personal_use=True)
                            else:
                                from desktop.neural_speech import NeuralSpeech
                                candidate = NeuralSpeech()
                            with self._lock:
                                current = self._backend_current(generation, engine, character)
                                if current:
                                    neural, neural_key, self._active_backend = candidate, key, candidate
                            if not current:
                                self._dispose_backend(candidate)
                                continue
                        if engine == "clone":
                            # Starting an owned helper is a lifecycle mutation:
                            # serialize its lightweight start with opt-out/close.
                            with self._lock:
                                if not self._backend_current(generation, engine, character):
                                    continue
                                neural.prepare_background(rate=speech_rate)
                            if isinstance(text, str) and text.casefold().rstrip(".!?") in {"да сэр", "да, сэр"}:
                                fixed_audio = neural.fixed_ack("ru")
                            use_neural = fixed_audio is not None or neural.is_ready
                            renderer = neural
                            if fixed_audio is None and text is not _PREPARE_SPEECH and not use_neural:
                                if not use_neural:
                                    self.speech_backend = "clone_warming"
                                    self._emit(self.on_state, "Голос по образцу готовится. Озвучу ответ, когда он будет готов.")
                            while (fixed_audio is None and text is not _PREPARE_SPEECH
                                   and (not use_neural or self._followup_armed() and not preserve_wake_deadline)):
                                use_neural = self._wait_for_clone(neural, generation, character, validator,
                                    deadline=voice_deadline, hold_followup=not preserve_wake_deadline)
                                self._play_urgent_ack(neural, generation, character)
                                if not use_neural:
                                    break
                        else:
                            use_neural = neural.prepare(rate=speech_rate)
                            renderer = neural
                    except Exception:
                        use_neural = False
                if not _event_current(validator):
                    continue
                if engine == "clone" and text is not _PREPARE_SPEECH and not use_neural:
                    if self._backend_current(generation, engine, character):
                        warming = bool(neural is not None and getattr(neural, "is_warming", False))
                        self.speech_backend = "clone_warming" if warming else "clone_unavailable"
                        self._emit(self.on_state, "Голос по образцу ещё готовится. Ответ показан текстом." if warming else
                                   "Выбранный голос недоступен. Ответ показан текстом.")
                    continue
                with self._lock:
                    if generation != self._speech_generation or self._closed.is_set() or not self.speech_enabled:
                        continue
                    if text is _PREPARE_SPEECH:
                        if engine == "clone":
                            self.speech_backend = ("clone" if use_neural else "clone_warming"
                                                   if neural is not None and getattr(neural, "is_warming", False) else "clone_unavailable")
                        else:
                            self.speech_backend = ("expressive" if engine == "expressive" else "neural") if use_neural else "windows"
                        continue
                if not self._begin_audio(generation):
                    continue
                try:
                    if use_neural:
                        try:
                            if fixed_audio is not None:
                                self.speech_backend = "clone"
                                pcm, rate = fixed_audio
                                self._play_pcm(pcm, rate, is_current=validator)
                            elif engine == "clone" and renderer is neural:
                                self.speech_backend = "clone"
                                chunks = neural.stream(text, rate=speech_rate,
                                    cancel=lambda: self._closed.is_set() or self._halt_speech.is_set()
                                    or not self.speech_enabled or not _event_current(validator))
                                self._play_pcm_stream(chunks, is_current=validator)
                            else:
                                pcm, rate = renderer.synthesize(text, rate=speech_rate)
                                self.speech_backend = "expressive" if engine in {"expressive", "clone"} else "neural"
                                if validator is None:
                                    self._play_pcm(pcm, rate)
                                else:
                                    self._play_pcm(pcm, rate, is_current=validator)
                            continue
                        except Exception:
                            if (self._closed.is_set() or self._halt_speech.is_set() or not self.speech_enabled
                                    or not _event_current(validator)):
                                continue
                            if self._utterance_audio_started:
                                self._emit(self.on_state, "Озвучивание прервалось. Полный ответ остаётся на экране.")
                                continue
                            if engine == "clone":
                                self.speech_backend = "clone_unavailable"
                                self._emit(self.on_state, "Выбранный голос недоступен. Ответ показан текстом.")
                                continue
                            self._emit(self.on_state, "Нейроголос недоступен. Использую голос Windows.")
                    if speaker is None:
                        pythoncom = importlib.import_module("pythoncom")
                        client = importlib.import_module("win32com.client")
                        pythoncom.CoInitialize()
                        initialized = True
                        speaker = client.Dispatch("SAPI.SpVoice")
                        select_windows_voice(speaker, client)
                    if (self._halt_speech.is_set() or self._closed.is_set() or not self.speech_enabled
                            or not _event_current(validator)):
                        continue
                    self.speech_backend = "windows"
                    speaker.Rate = max(-4, min(4, round((speech_rate - 165) / 18)))
                    # SVSFlagsAsync=1 + SVSFIsNotXML=16: plain text only.
                    speaker.Speak(text, 17)
                    while not speaker.WaitUntilDone(100):
                        if (self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set()
                                or not _event_current(validator)):
                            speaker.Speak("", 18)
                            break
                finally:
                    self._end_audio(preserve_wake_deadline=preserve_wake_deadline)
        except Exception:
            self.speech_backend = "unavailable"
            self._speaking.clear()
            self._emit(self.on_state, "Голос недоступен. Ответ показан текстом.")
        finally:
            self._close_backend()
            if initialized:
                speaker = None
                pythoncom.CoUninitialize()
