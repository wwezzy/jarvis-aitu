"""Opt-in, offline microphone/wake-word recognition and Windows speech.

Audio is kept in a bounded memory queue only.  This module neither authorizes
OS actions nor sends audio to a server.  Callbacks run on worker threads: GUI
clients must marshal them onto their UI event loop.
"""
from __future__ import annotations

import importlib
import importlib.util
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
        self._speech: queue.Queue[str] = queue.Queue(maxsize=5)
        self._thread: threading.Thread | None = None
        self._speaker: threading.Thread | None = None
        self.speech_enabled = True
        self._halt_speech = threading.Event()
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
                "speaking": self._speaking.is_set()}

    def start(self) -> bool:
        with self._lock:
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
        self._halt_speech.set()
        while True:
            try:
                self._speech.get_nowait()
            except queue.Empty:
                break
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
        if self._stop.is_set() or self._speaking.is_set() or time.monotonic() - self._last_spoken < 0.6:
            return
        try:
            self._audio.put_nowait(bytes(data))
        except queue.Full:
            # Drop old audio rather than execute a stale delayed utterance.
            self._clear_audio()
            self.gate.reset()

    def _recognized(self, text: str):
        if (self._stop.is_set() or self._speaking.is_set()
                or time.monotonic() - self._last_spoken < 0.6):
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
            with sound.RawInputStream(samplerate=sample_rate, blocksize=4000,
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

    def say(self, text: str):
        """Queue bounded plain text for an installed Windows voice, never SSML."""
        text = re.sub(r"\s+", " ", str(text)).strip()[:700]
        if not text or not self.speech_enabled:
            return
        if os.name != "nt":
            return
        with self._lock:
            if not self._speaker or not self._speaker.is_alive():
                self._speaker = threading.Thread(target=self._speak_loop, name="JarvisSpeech", daemon=True)
                self._speaker.start()
            try:
                self._speech.put_nowait(text)
            except queue.Full:
                pass

    def _speak_loop(self):
        initialized = False
        try:
            pythoncom = importlib.import_module("pythoncom")
            client = importlib.import_module("win32com.client")
            pythoncom.CoInitialize()
            initialized = True
            speaker = client.Dispatch("SAPI.SpVoice")
            speaker.Rate = 0
            # Prefer an installed Russian voice; no voice copying/download.
            voices = list(speaker.GetVoices())
            russian = [v for v in voices if "419" in v.GetAttribute("Language").lower().split(";")]
            if russian:
                speaker.Voice = russian[0]
            while True:
                text = self._speech.get()
                if not self.speech_enabled:
                    continue
                self._halt_speech.clear()
                self._speaking.set()
                self._clear_audio()
                self._emit(self.on_state, "Говорю…")
                try:
                    # SVSFlagsAsync=1 + SVSFIsNotXML=16: plain text only.
                    speaker.Speak(text, 17)
                    while not speaker.WaitUntilDone(100):
                        if self._halt_speech.is_set() or not self.speech_enabled:
                            # Purge on the same COM thread; no cross-thread COM.
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
            self._speaking.clear()
            self._emit(self.on_state, "Голос Windows недоступен. Ответ показан текстом.")
        finally:
            if initialized:
                # Release COM interfaces before uninitializing their apartment.
                speaker = None
                voices = []
                russian = []
                pythoncom.CoUninitialize()
