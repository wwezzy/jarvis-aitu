"""Opt-in personal XTTS voice over a private, checksum-verified local helper.

The model and its outputs have the noncommercial CPML license. Nothing is
downloaded at runtime. Reference recordings are user-provided data, never code.
Only bounded text/rate/language crosses stdin; the helper has no tools, URLs,
file selectors or command execution interface. Ordinary app dependencies stay
independent of the optional CUDA environment.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import wave


SAMPLE_RATE = 24000
MAX_PCM_BYTES = SAMPLE_RATE * 2 * 60
MAX_FRAME_BYTES = 800000
MAX_CHUNK_BYTES = SAMPLE_RATE * 2 * 12
PREPARATION_RETRY_DELAY = 60.0
STARTUP_REASONS = {"model_invalid", "manifest_invalid", "asset_invalid", "worker_invalid",
                   "runtime_invalid", "cuda_unavailable", "reference_invalid", "manifest_missing"}
MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"
MODEL_FILES = {
    "model.pth": (1867929118, "c7ea20001c6a0a841c77e252d8409f6a74fb423e79b3206a0771ba5989776187"),
    "speakers_xtts.pth": (7754818, "f0f6137c19a4eab0cbbe4c99b5babacf68b1746e50da90807708c10e645b943b"),
    "config.json": (4368, "ef262b1454dd2a77e1461b0b2cd53e19b8a7624cc131b837d36df67356bc75e8"),
    "vocab.json": (361219, "928260878a59da8a72a2a5b7687fea29d5106137669d90945430fe17e415304a"),
    "LICENSE.txt": (4014, "190f6d7c19b8984f91b97712b94ce92d2b2e640fc677dacab966e955ece9d043"),
}


class CloneSpeechUnavailable(RuntimeError):
    """Only fixed, nonprivate reason labels leave the adapter."""


def default_manifest_path():
    return Path.home() / "JarvisData" / "VoiceLab" / "clone-runtime.json"


def _sha(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _path(value):
    if (not isinstance(value, str) or not value or "\x00" in value
            or value.startswith(("//", "\\\\")) or "://" in value):
        raise CloneSpeechUnavailable("manifest_invalid")
    result = Path(value)
    if not result.is_absolute():
        raise CloneSpeechUnavailable("manifest_invalid")
    return result.resolve()


def _verified_file(value, expected, maximum):
    path = _path(value)
    if (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or not path.is_file() or not 0 < path.stat().st_size <= maximum or _sha(path) != expected):
        raise CloneSpeechUnavailable("asset_invalid")
    return path


def verified_model(path):
    try:
        return all((Path(path) / name).is_file() and (Path(path) / name).stat().st_size == size
                   and _sha(Path(path) / name) == expected for name, (size, expected) in MODEL_FILES.items())
    except OSError:
        return False


def load_manifest(path):
    """Verify executable/source pins before creating any child process."""
    try:
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= 32768:
            raise CloneSpeechUnavailable("manifest_missing")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
                or value.get("kind") != "private-local-clone" or value.get("personal_use_only") is not True
                or value.get("model_revision") != MODEL_REVISION or value.get("device") not in {"cuda", "cpu"}):
            raise CloneSpeechUnavailable("manifest_invalid")
        worker = _verified_file(value["worker"], value["worker_sha256"], 200000)
        if worker != Path(__file__).resolve().with_name("clone_speech_worker.py"):
            raise CloneSpeechUnavailable("worker_invalid")
        _verified_file(str(Path(__file__).resolve()), value["module_sha256"], 200000)
        python = _verified_file(value["python"], value["python_sha256"], 100000000)
        if python.name.casefold() not in {"python.exe", "python", "python3", "python3.12"}:
            raise CloneSpeechUnavailable("runtime_invalid")
        # A Windows venv executable is a launcher. Pin its actual interpreter
        # and config as well, rather than trusting a modified pyvenv.cfg.
        base = _verified_file(value.get("base_python", value["python"]),
                              value.get("base_python_sha256", value["python_sha256"]), 100000000)
        venv_config = python.parent.parent / "pyvenv.cfg"
        if python.parent.name.casefold() == "scripts" and venv_config.is_file():
            _verified_file(str(venv_config), value["venv_config_sha256"], 20000)
            settings = dict(line.split(" = ", 1) for line in venv_config.read_text(encoding="utf-8").splitlines() if " = " in line)
            if _path(settings.get("executable")) != base or settings.get("include-system-site-packages") != "false":
                raise CloneSpeechUnavailable("runtime_invalid")
            site_packages = python.parent.parent / "Lib" / "site-packages"
            if not site_packages.is_dir():
                raise CloneSpeechUnavailable("runtime_invalid")
            value["site_packages"] = str(site_packages)
        value["base_python"] = str(base)
        _verified_file(value["reference_wav"], value["reference_sha256"], 6000000)
        _path(value["model_dir"])
        for lang in ("ru", "en"):
            if value.get(f"ack_{lang}_wav"):
                _verified_file(value[f"ack_{lang}_wav"], value[f"ack_{lang}_sha256"], 500000)
        return value
    except CloneSpeechUnavailable:
        raise
    except (KeyError, ValueError, TypeError, OSError):
        raise CloneSpeechUnavailable("manifest_invalid") from None


def read_pcm(path, *, minimum=0.05, maximum=30):
    """Only bounded PCM16 mono WAV; compressed/executable inputs are rejected."""
    try:
        with wave.open(str(path), "rb") as source:
            seconds = source.getnframes() / source.getframerate()
            if (source.getnchannels() != 1 or source.getsampwidth() != 2
                    or source.getframerate() != SAMPLE_RATE or source.getcomptype() != "NONE"
                    or not minimum <= seconds <= maximum):
                raise CloneSpeechUnavailable("reference_invalid")
            expected = source.getnframes() * 2
            data = source.readframes(source.getnframes())
            if len(data) != expected:
                raise CloneSpeechUnavailable("reference_invalid")
            return data
    except CloneSpeechUnavailable:
        raise
    except (OSError, EOFError, wave.Error, ValueError, ZeroDivisionError):
        raise CloneSpeechUnavailable("reference_invalid") from None


def validate_request(text, rate=185, language=None):
    if (not isinstance(text, str) or not text.strip() or len(text) > 700
            or "\x00" in text or re.search(r"<[^>]*>", text)):
        raise CloneSpeechUnavailable("text_invalid")
    if type(rate) is not int or not 90 <= rate <= 240:
        raise CloneSpeechUnavailable("rate_invalid")
    if language is None:
        cyrillic = len(re.findall(r"[а-яё]", text, re.I))
        latin = len(re.findall(r"[a-z]", text, re.I))
        language = "en" if latin > cyrillic else "ru"
    if not isinstance(language, str) or language not in {"ru", "en"}:
        raise CloneSpeechUnavailable("language_invalid")
    return " ".join(text.split()), rate, language


def _environment(manifest):
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA",
               "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    base = Path(manifest["base_python"]).parent
    result["PATH"] = os.pathsep.join(map(str, (Path(manifest["python"]).parent, base, base / "DLLs", system / "System32", system)))
    result.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                  DO_NOT_TRACK="1", CUDA_MODULE_LOADING="LAZY", OMP_NUM_THREADS="4", MKL_NUM_THREADS="4")
    return result


class _OwnedJob:
    """Windows closes only this helper's job if the app exits unexpectedly."""

    def __init__(self, process):
        self.handle = None
        if os.name != "nt" or not hasattr(process, "_handle"):
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6), ("process_limit", ctypes.c_size_t),
                        ("job_limit", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if handle:
            information = Extended()
            information.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (kernel.SetInformationJobObject(handle, 9, ctypes.byref(information), ctypes.sizeof(information))
                    and kernel.AssignProcessToJobObject(handle, int(process._handle))):
                self.handle, self._kernel = handle, kernel
            else:
                kernel.CloseHandle(handle)

    def close(self):
        if self.handle is not None:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


class CloneSpeech:
    """Private helper with warm latents and bounded PCM streaming.

    ``cancel()`` returning True stops a stream. ``close()`` interrupts an owned
    helper immediately, without waiting for the request lock. Only fixed wake
    acknowledgments are cached; arbitrary speech is not written to disk.
    """

    def __init__(self, manifest=None, *, personal_use=False, _process_factory=None,
                 startup_timeout=120, request_timeout=60):
        self.manifest_path = Path(manifest) if manifest is not None else default_manifest_path()
        self.personal_use = personal_use is True
        self.reason, self.error_class = "not_prepared", None
        self._factory = _process_factory or subprocess.Popen
        self._startup_timeout, self._request_timeout = max(0.1, min(float(startup_timeout), 120)), max(0.1, min(float(request_timeout), 90))
        self._request_lock, self._state_lock, self._write_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self._process = self._job = self._frames = self._reader_stop = None
        self._closed = threading.Event()
        self._sequence = 0
        self._manifest = None
        self._ack_cache = OrderedDict()
        self._cue_lock, self._fixed_acks = threading.RLock(), {}
        self._ready, self._warming, self._background_thread = False, False, None
        self._prepare_retry_after = 0.0

    @property
    def is_ready(self):
        with self._state_lock:
            return bool(self._ready and not self._closed.is_set()
                        and self._process is not None and self._process.poll() is None)

    @property
    def is_warming(self):
        """Report preparation without waiting for the synthesis request lock."""
        with self._state_lock:
            return bool(self._warming and not self._closed.is_set())

    def fixed_ack(self, language="ru"):
        """Small local cue only; never waits on neural preparation/request lock."""
        if not self.personal_use or self._closed.is_set() or not isinstance(language, str) or language not in {"ru", "en"}:
            return None
        with self._cue_lock:
            if self._closed.is_set():
                return None
            if language in self._fixed_acks:
                return self._fixed_acks[language], SAMPLE_RATE
            try:
                manifest = load_manifest(self.manifest_path)
                if not manifest.get(f"ack_{language}_wav"):
                    return None
                pcm = read_pcm(manifest[f"ack_{language}_wav"], maximum=5)
                if self._closed.is_set():
                    return None
                self._fixed_acks[language] = pcm
                return pcm, SAMPLE_RATE
            except CloneSpeechUnavailable:
                return None

    def prepare_background(self, rate=185):
        """Start at most one preparation thread; True means started, not ready."""
        try:
            validate_request("Да, сэр", rate)
        except CloneSpeechUnavailable:
            return False
        if not self.personal_use or self._closed.is_set():
            return False
        with self._state_lock:
            if (self._warming or time.monotonic() < self._prepare_retry_after
                    or self._ready and self._process is not None and self._process.poll() is None):
                return False
            self._warming = True

        def warm():
            prepared = False
            try:
                prepared = self.prepare(rate)
            finally:
                with self._state_lock:
                    self._warming = False
                    self._prepare_retry_after = (0.0 if prepared else
                                                 time.monotonic() + PREPARATION_RETRY_DELAY)
        thread = threading.Thread(target=warm, name="jarvis-clone-prepare", daemon=True)
        self._background_thread = thread
        try:
            thread.start()
        except RuntimeError:
            with self._state_lock:
                self._warming = False
                self._prepare_retry_after = time.monotonic() + PREPARATION_RETRY_DELAY
            return False
        return True

    @contextmanager
    def _request_slot(self, cancel=None):
        """Bound admission to the owned helper, including cancellation while busy."""
        deadline = time.monotonic() + self._request_timeout
        acquired = False
        try:
            while True:
                if self._closed.is_set():
                    raise CloneSpeechUnavailable("closed")
                try:
                    cancelled = cancel is not None and cancel()
                except Exception:
                    cancelled = True
                if cancelled:
                    raise CloneSpeechUnavailable("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CloneSpeechUnavailable("helper_timeout")
                if acquired:
                    break
                acquired = self._request_lock.acquire(timeout=min(0.05, remaining))
            yield
        finally:
            if acquired:
                self._request_lock.release()

    def _publish_ready(self, process):
        """Publish preparation only after releasing the request admission lock."""
        with self._state_lock:
            if (self._process is process and process is not None
                    and process.poll() is None and not self._closed.is_set()):
                self._ready = True
                return True
        return False

    def available(self):
        if not self.personal_use:
            self.reason = "personal_use_required"
            return False
        if self._closed.is_set():
            self.reason = "closed"
            return False
        try:
            self._manifest = load_manifest(self.manifest_path)
            return True
        except CloneSpeechUnavailable as error:
            self.reason = str(error)
            return False

    def _read_frames(self, process, frames, stopped):
        def put(value):
            while not stopped.is_set():
                try:
                    frames.put(value, timeout=0.1)
                    return
                except queue.Full:
                    pass
        try:
            while not stopped.is_set():
                raw = process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    put({"type": "eof"})
                    return
                if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                    break
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    break
                put(frame)
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        put({"type": "protocol_error"})

    def _receive(self, deadline, cancel=None):
        while not self._closed.is_set():
            if cancel is not None and cancel():
                raise CloneSpeechUnavailable("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloneSpeechUnavailable("helper_timeout")
            with self._state_lock:
                frames = self._frames
            if frames is None:
                raise CloneSpeechUnavailable("helper_closed")
            try:
                frame = frames.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if frame.get("type") in {"eof", "protocol_error"}:
                raise CloneSpeechUnavailable("helper_protocol")
            return frame
        raise CloneSpeechUnavailable("closed")

    def _send(self, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(body) > 8192:
            raise CloneSpeechUnavailable("request_limit")
        with self._write_lock:
            with self._state_lock:
                process = self._process
            if process is None or process.poll() is not None:
                raise CloneSpeechUnavailable("helper_closed")
            try:
                process.stdin.write(body)
                process.stdin.flush()
            except (OSError, ValueError):
                raise CloneSpeechUnavailable("helper_closed") from None

    def _stop_child(self):
        with self._state_lock:
            process, job, stopped = self._process, self._job, self._reader_stop
            self._process = self._job = self._frames = self._reader_stop = None
            self._ready = False
        if stopped:
            stopped.set()
        if job:
            try:
                job.close()
            except OSError:
                pass  # Still terminate/wait for our owned process below.
        if process is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    # Closing a kill-on-close Windows job is asynchronous.
                    # TerminateProcess can report AccessDenied while that
                    # exact child is already exiting; wait before escalating.
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired) as error:
                        self.error_class = type(error).__name__
                        # A reader may still hold BufferedReader's lock while
                        # the OS completes termination. Never block UI close
                        # indefinitely trying to close that live pipe.
                        return
                except OSError as error:
                    self.error_class = type(error).__name__
                    return
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

    def _ensure_child(self):
        with self._state_lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
        self._stop_child()
        if not self.available():
            raise CloneSpeechUnavailable(self.reason)
        manifest = self._manifest
        # Launch the pinned actual interpreter, not the Windows venv launcher
        # which creates a grandchild that could race job assignment.
        process = self._factory([manifest["base_python"], "-I", "-u", manifest["worker"], "--manifest", str(self.manifest_path.resolve())],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=_environment(manifest), bufsize=65536)
        frames, stopped = queue.Queue(maxsize=8), threading.Event()
        with self._state_lock:
            self._process, self._frames, self._reader_stop, self._job = process, frames, stopped, _OwnedJob(process)
        if self._closed.is_set():
            self._stop_child()
            raise CloneSpeechUnavailable("closed")
        threading.Thread(target=self._read_frames, args=(process, frames, stopped), daemon=True, name="jarvis-clone-output").start()
        frame = self._receive(time.monotonic() + self._startup_timeout)
        if frame.get("type") != "ready" or type(frame.get("rate")) is not int or frame["rate"] != SAMPLE_RATE:
            backend_error = frame.get("error_class")
            if isinstance(backend_error, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", backend_error):
                self.error_class = backend_error
            reason = frame.get("reason")
            raise CloneSpeechUnavailable(reason if isinstance(reason, str) and reason in STARTUP_REASONS else "helper_unavailable")
        return process

    def prepare(self, rate=185):
        self.error_class = None
        try:
            validate_request("Да, сэр", rate)
            with self._request_slot():
                try:
                    process = self._ensure_child()
                    # Fixed cues are read lazily. No unrelated English synthesis
                    # can delay the first ordinary response in the selected voice.
                except Exception:
                    self._stop_child()
                    raise
            if not self._publish_ready(process):
                raise CloneSpeechUnavailable("closed" if self._closed.is_set() else "helper_closed")
            self.reason, self.error_class = "ready", None
            return True
        except Exception as error:
            self.reason = str(error) if isinstance(error, CloneSpeechUnavailable) else "helper_unavailable"
            self.error_class = self.error_class or type(error).__name__
            return False

    def _ack(self, text, language):
        return ((language == "ru" and text.casefold().rstrip(".!?") in {"да сэр", "да, сэр"})
                or (language == "en" and text.casefold().rstrip(".!?") in {"yes sir", "yes, sir"}))

    def stream(self, text, rate=185, *, language=None, cancel=None):
        text, rate, language = validate_request(text, rate, language)
        if not self.personal_use or self._closed.is_set():
            raise CloneSpeechUnavailable("personal_use_required" if not self.personal_use else "closed")
        if self._ack(text, language):
            fixed = self.fixed_ack(language)
            if fixed is not None:
                if self._closed.is_set() or cancel is not None and cancel():
                    raise CloneSpeechUnavailable("cancelled")
                yield fixed
                return
        process = None
        with self._request_slot(cancel):
            if cancel is not None and cancel():
                raise CloneSpeechUnavailable("cancelled")
            key = (language, rate)
            acknowledgment = self._ack(text, language)
            if acknowledgment and key in self._ack_cache:
                if self._closed.is_set() or cancel is not None and cancel():
                    raise CloneSpeechUnavailable("cancelled")
                yield self._ack_cache[key], SAMPLE_RATE
                return
            if acknowledgment and self.available() and self._manifest.get(f"ack_{language}_wav"):
                data = read_pcm(self._manifest[f"ack_{language}_wav"], maximum=5)
                self._ack_cache[key] = data
                while len(self._ack_cache) > 8:
                    self._ack_cache.popitem(last=False)
                if self._closed.is_set() or cancel is not None and cancel():
                    raise CloneSpeechUnavailable("cancelled")
                yield data, SAMPLE_RATE
                return
            request_id, total, count, collected = None, 0, 0, []
            try:
                process = self._ensure_child()
                self._sequence += 1
                request_id = self._sequence
                self._send({"type": "synthesize", "id": request_id, "text": text, "rate": rate, "language": language})
                deadline = time.monotonic() + self._request_timeout
                while True:
                    frame = self._receive(deadline, cancel=cancel)
                    if type(frame.get("id")) is not int or frame["id"] != request_id:
                        raise CloneSpeechUnavailable("helper_protocol")
                    if frame.get("type") == "done":
                        if not total:
                            raise CloneSpeechUnavailable("audio_empty")
                        break
                    if frame.get("type") == "error":
                        raise CloneSpeechUnavailable("synthesis_failed")
                    if frame.get("type") != "audio" or type(frame.get("rate")) is not int or frame["rate"] != SAMPLE_RATE:
                        raise CloneSpeechUnavailable("helper_protocol")
                    encoded = frame.get("pcm")
                    if not isinstance(encoded, str) or len(encoded) > MAX_CHUNK_BYTES * 4 // 3 + 4:
                        raise CloneSpeechUnavailable("audio_invalid")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError):
                        raise CloneSpeechUnavailable("audio_invalid") from None
                    total, count = total + len(data), count + 1
                    if not data or len(data) % 2 or len(data) > MAX_CHUNK_BYTES or total > MAX_PCM_BYTES or count > 512:
                        raise CloneSpeechUnavailable("audio_limit")
                    if self._closed.is_set() or cancel is not None and cancel():
                        raise CloneSpeechUnavailable("cancelled")
                    if acknowledgment:
                        collected.append(data)
                    yield data, SAMPLE_RATE
                if acknowledgment:
                    self._ack_cache[key] = b"".join(collected)
                    while len(self._ack_cache) > 8:
                        self._ack_cache.popitem(last=False)
                    if len(self._ack_cache[key]) <= SAMPLE_RATE * 2 * 5:
                        with self._cue_lock:
                            self._fixed_acks[language] = self._ack_cache[key]
                self.reason, self.error_class = "ready", None
            except GeneratorExit:
                self._cancel_request(request_id)
                raise
            except Exception as error:
                self.error_class = type(error).__name__
                self.reason = str(error) if isinstance(error, CloneSpeechUnavailable) else "synthesis_failed"
                if self.reason == "cancelled":
                    self._cancel_request(request_id)
                else:
                    self._stop_child()
                raise CloneSpeechUnavailable(self.reason) from None
        if process is not None:
            self._publish_ready(process)

    def _cancel_request(self, request_id):
        if request_id is None:
            return
        try:
            self._send({"type": "cancel", "id": request_id})
            deadline = time.monotonic() + 2
            while True:
                frame = self._receive(deadline)
                if frame.get("id") == request_id and frame.get("type") in {"done", "error"}:
                    return
        except Exception:
            self._stop_child()

    def synthesize(self, text, rate=185, *, language=None):
        return b"".join(data for data, _ in self.stream(text, rate, language=language)), SAMPLE_RATE

    def close(self):
        self._closed.set()
        self._stop_child()
        self._ack_cache.clear()
        with self._cue_lock:
            self._fixed_acks.clear()
