"""Opt-in, offline microphone/wake-word recognition and Windows speech.

Audio is kept in a bounded memory queue only.  This module neither authorizes
OS actions nor sends audio to a server.  Callbacks run on worker threads: GUI
clients must marshal them onto their UI event loop.
"""
from __future__ import annotations

import importlib
import importlib.util
import itertools
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from desktop.model_setup import default_model_dir, model_ready

_PREPARE_SPEECH = object()
SPEECH_ECHO_TAIL = 0.25


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
        if self.deadline:
            self.deadline = now + self.timeout

    def feed(self, text: str, now: float) -> WakeDecision:
        text = re.sub(r"\s+", " ", text.strip().lower().replace("ё", "е"))
        words = re.findall(r"[\w]+", text, flags=re.UNICODE)
        if not words:
            return WakeDecision("ignore")
        # A name buried inside a sentence is not a command invocation.
        if words[0] in self.WAKE:
            rest = re.sub(r"^[\w]+[\s,:.!?—-]*", "", text).strip()
            if not rest:
                self.deadline = now + self.timeout
                return WakeDecision("wake")
            self.reset()
            return WakeDecision("command", rest)
        if not self.deadline or now > self.deadline:
            self.reset()
            return WakeDecision("ignore")
        self.reset()
        if text in self.CANCEL:
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
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=24)
        self._speech: queue.Queue = queue.Queue(maxsize=5)
        self._thread: threading.Thread | None = None
        self._speaker: threading.Thread | None = None
        self.speech_enabled = True
        self.speech_rate = 185
        self.speech_engine = "auto"
        self.speech_backend = "not_prepared"
        self._halt_speech = threading.Event()
        self._closed = threading.Event()
        self._speech_generation = 0
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
        if self._stop.is_set() or self._speaking.is_set() or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL:
            return
        try:
            self._audio.put_nowait(bytes(data))
        except queue.Full:
            # Drop old audio rather than execute a stale delayed utterance.
            self._clear_audio()
            self.gate.reset()

    def _recognized(self, text: str):
        if (self._stop.is_set() or self._speaking.is_set()
                or time.monotonic() - self._last_spoken < SPEECH_ECHO_TAIL):
            return
        result = self.gate.feed(text, time.monotonic())
        if result.kind == "wake":
            self._emit(self.on_state, "Слушаю команду…")
            self.say("Да, сэр")
        elif result.kind == "command":
            self._emit(self.on_state, "Обрабатываю команду…")
            self._emit(self.on_text, result.text)
        elif result.kind == "cancel":
            self._emit(self.on_state, "Ожидаю слово «Джарвис»")

    def _listen(self):
        try:
            vosk = importlib.import_module("vosk")
            sound = importlib.import_module("sounddevice")
            self._emit(self.on_state, "Загружаю локальную модель…")
            vosk.SetLogLevel(-1)
            model = vosk.Model(str(self.model_dir))
            device = sound.query_devices(kind="input")
            sample_rate = int(device["default_samplerate"])
            recognizer = vosk.KaldiRecognizer(model, sample_rate)
            if self._stop.is_set():
                return
            with sound.RawInputStream(samplerate=sample_rate, blocksize=max(400, int(sample_rate * 0.05)),
                                      dtype="int16", channels=1, callback=self._audio_callback):
                self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                reset_after_speech = self._last_spoken
                while not self._stop.is_set():
                    if self._speaking.is_set():
                        self._clear_audio()
                        self._stop.wait(0.05)
                        continue
                    if reset_after_speech != self._last_spoken:
                        recognizer.Reset()
                        self._clear_audio()
                        reset_after_speech = self._last_spoken
                    try:
                        data = self._audio.get(timeout=0.2)
                    except queue.Empty:
                        if self.gate.deadline and time.monotonic() > self.gate.deadline:
                            self.gate.reset()
                            self._emit(self.on_state, "Ожидаю слово «Джарвис»")
                        continue
                    if recognizer.AcceptWaveform(data):
                        result = json.loads(recognizer.Result())
                        self._recognized(result.get("text", ""))
        except Exception as error:
            self._emit(self.on_state, f"Голос недоступен ({type(error).__name__}). Текстовый ввод работает.")
        finally:
            self._clear_audio()
            self.gate.reset()

    def apply_preferences(self, preferences: dict):
        """Apply bounded local settings; no model download or recording."""
        engine = preferences.get("voice_engine", self.speech_engine)
        rate = preferences.get("voice_rate", self.speech_rate)
        if not isinstance(engine, str) or engine not in {"auto", "windows"}:
            raise ValueError("Unsupported speech engine")
        if type(rate) is not int or not 90 <= rate <= 240:
            raise ValueError("Voice rate must be 90..240")
        if engine != self.speech_engine or rate != self.speech_rate:
            self.interrupt()
        self.speech_engine, self.speech_rate = engine, rate
        self.speech_enabled = bool(preferences.get("speech_enabled", self.speech_enabled))
        if not self.speech_enabled:
            self.interrupt()

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

    def say(self, text: str):
        """Speak bounded plain text. UI labels/Markdown are not read as dialogue."""
        text = re.sub(r"^Локальная модель:\s*", "", str(text).strip())
        text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+)", "", text)
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()[:700]
        if text:
            self._enqueue_speech(text)

    def close(self):
        self._closed.set()
        self.stop()
        try:
            self._speech.put_nowait(None)
        except queue.Full:
            pass
        if self._speaker and self._speaker is not threading.current_thread():
            self._speaker.join(timeout=3)

    def _play_pcm(self, pcm: bytes, sample_rate: int):
        """Use short output blocks for prompt stop/mute; never store audio."""
        if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
                or type(sample_rate) is not int or not 8000 <= sample_rate <= 48000
                or len(pcm) > sample_rate * 2 * 120):
            raise ValueError("Invalid synthesized audio")
        if self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set():
            return
        sound = importlib.import_module("sounddevice")
        with sound.RawOutputStream(samplerate=sample_rate, dtype="int16", channels=1,
                                   blocksize=0, latency="low") as stream:
            for offset in range(0, len(pcm), 2048):
                if self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set():
                    stream.abort()
                    break
                stream.write(pcm[offset:offset + 2048])

    def _speak_loop(self):
        # Neural synthesis and COM remain on this one worker. Fallback COM is
        # lazy: an unavailable Windows voice cannot prevent neural speech.
        initialized, speaker, pythoncom, neural = False, None, None, None
        try:
            while True:
                item = self._speech.get()
                if item is None:
                    break
                generation, text = item if isinstance(item, tuple) else (self._speech_generation, item)
                if not self.speech_enabled or self._closed.is_set():
                    continue
                engine, speech_rate = self.speech_engine, self.speech_rate
                use_neural = False
                if engine == "auto":
                    try:
                        if neural is None:
                            from desktop.neural_speech import NeuralSpeech
                            neural = NeuralSpeech()
                        use_neural = neural.prepare(rate=speech_rate)
                    except Exception:
                        use_neural = False
                with self._lock:
                    if generation != self._speech_generation or self._closed.is_set() or not self.speech_enabled:
                        continue
                    if text is _PREPARE_SPEECH:
                        self.speech_backend = "neural" if use_neural else "windows"
                        continue
                    self._halt_speech.clear()
                    self._speaking.set()
                self._clear_audio()
                self._emit(self.on_state, "Говорю…")
                try:
                    if use_neural:
                        try:
                            pcm, rate = neural.synthesize(text, rate=speech_rate)
                            self.speech_backend = "neural"
                            self._play_pcm(pcm, rate)
                            continue
                        except Exception:
                            self._emit(self.on_state, "Нейроголос недоступен. Использую голос Windows.")
                    if speaker is None:
                        pythoncom = importlib.import_module("pythoncom")
                        client = importlib.import_module("win32com.client")
                        pythoncom.CoInitialize()
                        initialized = True
                        speaker = client.Dispatch("SAPI.SpVoice")
                        select_windows_voice(speaker, client)
                    if self._halt_speech.is_set() or self._closed.is_set() or not self.speech_enabled:
                        continue
                    self.speech_backend = "windows"
                    speaker.Rate = max(-4, min(4, round((speech_rate - 165) / 18)))
                    # SVSFlagsAsync=1 + SVSFIsNotXML=16: plain text only.
                    speaker.Speak(text, 17)
                    while not speaker.WaitUntilDone(100):
                        if self._halt_speech.is_set() or not self.speech_enabled or self._closed.is_set():
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
            if initialized:
                speaker = None
                pythoncom.CoUninitialize()
