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
CLONE_READY_TIMEOUT = 30.0


def pcm_active(data):
    """An acoustic buffer trigger, never a wake/intent decision.

    Use zero-mean energy so silence and a DC offset do not start an utterance.
    Only an exact recognized wake can send these bounded samples to Whisper.
    """
    if not isinstance(data, bytes) or len(data) < 4 or len(data) % 2:
        return False
    values = array("h")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    mean = sum(values) / len(values)
    variance = max(0, sum(value * value for value in values) / len(values) - mean * mean)
    return math.sqrt(variance) / 32768 >= 0.002


def strip_wake_prefix(text):
    text = text.strip()
    first = re.search(r"\w+", text, flags=re.UNICODE)
    if first and first.group().casefold() in WakeGate.WAKE:
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
        self.silence_limit = int(sample_rate * 2 * 0.6)
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


class WakeGate:
    """Whole-token wake word, one command, bounded follow-up; no fuzzy match."""

    WAKE = {"джарвис", "джарвиз", "jarvis"}
    CANCEL = {"отмена", "не надо", "хватит", "cancel", "stop listening"}

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
                 model_dir: Path | None = None):
        self.on_text, self.on_state = on_text, on_state
        self.model_dir = Path(model_dir) if model_dir else default_model_dir()
        self.gate = WakeGate()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._transcribing = threading.Event()
        self._audio_epoch = 0
        self.multilingual_stt_enabled = False
        self._stt = None
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
        self._waiting_clone = threading.Event()
        self._last_spoken = 0.0
        self._lock = threading.Lock()

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
        return {"ready": dependencies and model and microphone, "model": model,
                "dependencies": dependencies, "microphone": microphone, "reason": reason,
                "running": bool(self._thread and self._thread.is_alive()),
                "speaking": self._speaking.is_set(), "speech_backend": self.speech_backend}

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
        if (self._stop.is_set() or self._speaking.is_set() or self._transcribing.is_set()
                or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL):
            return
        try:
            self._audio.put_nowait(bytes(data))
        except queue.Full:
            # Drop old audio rather than execute a stale delayed utterance.
            self._clear_audio()
            self.gate.reset()
            self._audio_epoch += 1

    @staticmethod
    def _confident_alias(full_text, command, words):
        from desktop.core import ALIASES, normalize
        if normalize(command) not in ALIASES or not isinstance(words, list) or not words:
            return False
        recognized = []
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("word"), str):
                return False
            confidence = word.get("conf")
            if (type(confidence) not in {int, float} or not math.isfinite(confidence)
                    or not VOSK_ALIAS_CONFIDENCE <= confidence <= 1):
                return False
            recognized.append(word["word"])
        return normalize(" ".join(recognized)) == normalize(full_text)

    def _decode_command(self, text, pcm=None, sample_rate=None, *, confident=False):
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
        try:
            decoded = self._stt.transcribe(pcm, sample_rate)
            if self._stop.is_set() or self._closed.is_set():
                return None
            return strip_wake_prefix(decoded)
        except Exception:
            self._emit(self.on_state, "Не удалось уверенно распознать команду. Повтори её или напиши текстом.")
            return None
        finally:
            self._clear_audio()
            self._transcribing.clear()

    def _recognized(self, text: str, pcm=None, sample_rate=None, *, words=None):
        if (self._stop.is_set() or self._speaking.is_set()
                or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL):
            return
        now = time.monotonic()
        previous_deadline = self.gate.deadline
        armed = bool(previous_deadline and now <= previous_deadline)
        result = self.gate.feed(text, now)
        first = re.search(r"\w+", text, flags=re.UNICODE)
        verified_wake = bool(first and first.group().casefold() in WakeGate.WAKE)
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
        acknowledged = result.kind == "wake"
        if result.kind == "wake":
            self._emit(self.on_state, "Слушаю команду…")
            self.say("Да, сэр")
            if not bilingual_audio:
                return
            # The same utterance can contain a command that Vosk omitted after
            # the recognized name. Keep the original fixed ACK immediate.
        if result.kind in {"wake", "command"}:
            self._emit(self.on_state, "Обрабатываю команду…")
            confident = result.kind == "command" and self._confident_alias(text, result.text, words)
            decoded = self._decode_command(result.text if result.kind == "command" else text,
                                           pcm, sample_rate, confident=confident)
            if self._stop.is_set() or self._closed.is_set():
                return
            if decoded is None or not decoded.strip():
                # A failed decoder cannot keep extending authorization. Retry
                # uses the original finite window, never a fresh ten seconds.
                self.gate.deadline = retry_deadline if time.monotonic() <= retry_deadline else 0
                if decoded == "" and not acknowledged:
                    self._emit(self.on_state, "Слушаю команду…")
                    self.say("Да, сэр")
                return
            self.gate.reset()
            if WakeGate.is_cancel(decoded):
                self._emit(self.on_state, "Ожидаю слово «Джарвис»")
            else:
                self._emit(self.on_text, decoded)
        elif result.kind == "cancel":
            self._emit(self.on_state, "Ожидаю слово «Джарвис»")

    def _listen(self):
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
                    self._stt = MultilingualSTT()
                    if not self._stt.prepare():
                        self._stt = None
                        self._emit(self.on_state, "Два языка пока недоступны. Русское распознавание продолжает работать.")
                except Exception:
                    self._stt = None
                    self._emit(self.on_state, "Два языка пока недоступны. Русское распознавание продолжает работать.")
            device = sound.query_devices(kind="input")
            sample_rate = int(device["default_samplerate"])
            recognizer = vosk.KaldiRecognizer(model, sample_rate)
            set_words = getattr(recognizer, "SetWords", None)
            if callable(set_words):
                set_words(True)
            if self._stop.is_set():
                return
            with sound.RawInputStream(samplerate=sample_rate, blocksize=max(400, int(sample_rate * 0.05)),
                                      dtype="int16", channels=1, callback=self._audio_callback):
                self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                reset_after_speech = self._last_spoken
                audio_epoch, utterance = self._audio_epoch, UtteranceBuffer(sample_rate)
                while not self._stop.is_set():
                    if self._speaking.is_set():
                        self._clear_audio()
                        self._stop.wait(0.05)
                        continue
                    if reset_after_speech != self._last_spoken or audio_epoch != self._audio_epoch:
                        recognizer.Reset()
                        self._clear_audio()
                        reset_after_speech = self._last_spoken
                        audio_epoch = self._audio_epoch
                        utterance.reset()
                    try:
                        data = self._audio.get(timeout=0.2)
                    except queue.Empty:
                        if self.gate.deadline and time.monotonic() > self.gate.deadline:
                            self.gate.reset()
                            self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                        continue
                    complete = recognizer.AcceptWaveform(data)
                    result = json.loads(recognizer.Result()) if complete else None
                    if self.multilingual_stt_enabled:
                        utterance.append(data, active=pcm_active(data))
                        if not complete and utterance.finished:
                            result = json.loads(recognizer.FinalResult())
                            complete = True
                    if complete or utterance.overflow:
                        result = result or {"text": ""}
                        if utterance.overflow:
                            first = re.search(r"\w+", result.get("text", ""), flags=re.UNICODE)
                            invoked = bool(first and first.group().casefold() in WakeGate.WAKE)
                            was_armed = bool(self.gate.deadline and time.monotonic() <= self.gate.deadline)
                            self.gate.reset()
                            if invoked or was_armed:
                                self._emit(self.on_state, "Фраза слишком длинная. Повтори короткую команду после «Джарвис».")
                        else:
                            self._recognized(result.get("text", ""), utterance.pcm(), sample_rate,
                                             words=result.get("result"))
                        recognizer.Reset()
                        utterance.reset()
        except Exception as error:
            self._emit(self.on_state, f"Голос недоступен ({type(error).__name__}). Текстовый ввод работает.")
        finally:
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
        self.multilingual_stt_enabled = multilingual
        self.speech_enabled = bool(preferences.get("speech_enabled", self.speech_enabled))
        if not self.speech_enabled:
            self.interrupt()
            self._close_backend()

    def interrupt(self):
        """Stop old speech without stopping the microphone or authorizing actions."""
        with self._lock:
            self._speech_generation += 1
            self._halt_speech.set()
            while True:
                try:
                    self._speech.get_nowait()
                except queue.Empty:
                    break

    def _enqueue_speech(self, item):
        if not self.speech_enabled or self._closed.is_set() or os.name != "nt":
            return
        with self._lock:
            if not self._speaker or not self._speaker.is_alive():
                self._speaker = threading.Thread(target=self._speak_loop, name="JarvisSpeech", daemon=True)
                self._speaker.start()
            try:
                self._speech.put_nowait((self._speech_generation, item))
            except queue.Full:
                pass

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
        if (self.speech_engine == "clone" and self._waiting_clone.is_set()
                and text.casefold().rstrip(".!?") in {"да сэр", "да, сэр"}):
            # A wake acknowledgment must not sit behind an old dynamic reply
            # waiting for its preferred model. Keep one output worker only.
            self.interrupt()
        if text:
            self._enqueue_speech(text)

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

    def _wait_for_clone(self, backend, generation, character, validator):
        """Wait off-GUI without holding a lifecycle/request lock."""
        deadline = time.monotonic() + CLONE_READY_TIMEOUT
        self._waiting_clone.set()
        try:
            while self._backend_current(generation, "clone", character) and _event_current(validator):
                if backend.is_ready:
                    return True
                if not getattr(backend, "is_warming", False) or time.monotonic() >= deadline:
                    return False
                self._closed.wait(0.05)
            return False
        finally:
            self._waiting_clone.clear()

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

    def _play_pcm(self, pcm: bytes, sample_rate: int, *, is_current=None):
        """Use short output blocks for prompt stop/mute; never store audio."""
        if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
                or type(sample_rate) is not int or not 8000 <= sample_rate <= 48000
                or len(pcm) > sample_rate * 2 * 120):
            raise ValueError("Invalid synthesized audio")
        if self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set() or not _event_current(is_current):
            return
        sound = importlib.import_module("sounddevice")
        with sound.RawOutputStream(samplerate=sample_rate, dtype="int16", channels=1,
                                   blocksize=0, latency="low") as stream:
            for offset in range(0, len(pcm), 2048):
                if (self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set()
                        or not _event_current(is_current)):
                    stream.abort()
                    break
                stream.write(pcm[offset:offset + 2048])
                self._utterance_audio_started = True

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
                validator = text.is_current if isinstance(text, _EventSpeech) else None
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
                            if not use_neural and text is not _PREPARE_SPEECH:
                                self.speech_backend = "clone_warming"
                                self._emit(self.on_state, "Голос по образцу готовится. Ответ доступен текстом.")
                                use_neural = self._wait_for_clone(neural, generation, character, validator)
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
                    self._halt_speech.clear()
                    self._speaking.set()
                    self._utterance_audio_started = False
                self._clear_audio()
                self._emit(self.on_state, "Говорю…")
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
                    self._last_spoken = time.monotonic()
                    self.gate.extend(self._last_spoken)
                    self._clear_audio()
                    self._speaking.clear()
                    self._emit(self.on_state, "Ожидаю слово «Джарвис»" if self._thread and self._thread.is_alive()
                               and not self._stop.is_set() else "Микрофон выключен")
        except Exception:
            self.speech_backend = "unavailable"
            self._speaking.clear()
            self._emit(self.on_state, "Голос недоступен. Ответ показан текстом.")
        finally:
            self._close_backend()
            if initialized:
                speaker = None
                pythoncom.CoUninitialize()
