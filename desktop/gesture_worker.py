"""Isolated opt-in camera recognition; child output is finite intents only.

The parent checks owner policy; the core must check it again before execution.
The recognizer child never authorizes or executes PC actions. Process isolation
allows a blocked native camera/SDK call to be stopped with its owned process.
"""
from __future__ import annotations

from collections import deque
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
import time


_INTENTS = frozenset({"volume_up", "volume_down", "minimize"})
_STATES = {
    "disabled": "Камера выключена.",
    "starting": "Подготавливаю изолированное распознавание жестов…",
    "ready": "Камера включена. Сначала уберите руки, затем удерживайте жест.",
    "owner_required": "Камера выключена: подтвердите доступ владельца.",
    "components_missing": "Камера выключена: установите компоненты и модель жестов.",
    "camera_unavailable": "Камера или распознавание жестов недоступны. Остальные функции работают.",
    "isolation_runtime_unavailable": "Камера выключена: изолированный Python для распознавания недоступен.",
}
_MAX_LINE = 4096


def _local_path(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or len(raw) > 2048 or "\x00" in raw or "://" in raw
            or raw.startswith(("\\\\", "//"))):
        raise ValueError("local_path_required")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path or path.is_symlink():
        raise ValueError("local_path_required")
    return path


def _locations(executable, worker):
    app = worker.parent.parent
    if app.name == "app" and (app.parent / "appsite").is_dir():
        packages = app.parent / "appsite"
    elif executable.parent.name.casefold() == "scripts":
        packages = executable.parent.parent / "Lib/site-packages"
    elif (executable.parent.parent / "pyvenv.cfg").is_file():
        packages = executable.parent.parent / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    else:
        packages = Path(sysconfig.get_path("platlib"))
    return _local_path(app.resolve()), _local_path(packages.resolve())


def _runtime_spec():
    if getattr(sys, "frozen", False):
        return None
    try:
        executable = Path(sys.executable).absolute()
        if executable.name.casefold() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        if (not executable.is_file() or not executable.name.casefold().startswith("python")
                or str(executable).startswith(("\\\\", "//"))):
            return None
        worker = _local_path(Path(__file__).resolve())
        app, packages = _locations(executable, worker)
        if not packages.is_dir() or not (app / "desktop/gestures.py").is_file():
            return None
        return executable, worker
    except (OSError, ValueError, TypeError):
        return None


def _environment(executable):
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
               "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system = Path(os.environ.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join(map(str, (executable.parent, Path(sys.base_prefix),
        Path(sys.base_prefix) / "DLLs", system / "System32", system)))
    environment.update(PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", DO_NOT_TRACK="1", OMP_NUM_THREADS="2")
    return environment


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _decode(raw):
    if not isinstance(raw, bytes) or len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
        raise ValueError("frame_invalid")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    if not isinstance(value, dict):
        raise ValueError("frame_invalid")
    return value


class IsolatedGestureService:
    def __init__(self, on_intent, on_state, *, owner_gate, model_dir=None, camera_index=0,
                 _process_factory=None, _job_factory=None):
        if type(camera_index) is not int or not 0 <= camera_index <= 9:
            raise ValueError("A local camera index is required")
        gestures = importlib.import_module("desktop.gestures")  # stdlib-only module import
        self.on_intent, self.on_state, self.owner_gate = on_intent, on_state, owner_gate
        self.model_dir = Path(model_dir) if model_dir is not None else gestures.default_model_dir()
        self.camera_index = camera_index
        self._factory, self._job_factory = _process_factory or subprocess.Popen, _job_factory
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._process = self._job = None
        self._generation, self._stopping, self._active = 0, False, False
        self._shutdown_done = threading.Event()
        self._shutdown_done.set()
        self._shutdown_reason, self._shutdown_generation, self._stop_waiters = "disabled", 0, 0
        self._last_message = self._ready_at = self._last_intent = None
        self.reason = "disabled"

    def _allowed(self, intent=None):
        try:
            return callable(self.owner_gate) and self.owner_gate(intent) is True
        except Exception:
            return False

    @staticmethod
    def _emit(callback, value):
        try:
            callback(value)
        except Exception:
            pass

    def available(self):
        gestures = importlib.import_module("desktop.gestures")
        dependencies = gestures._dependencies_ready()
        try:
            model = gestures.model_ready(_local_path(self.model_dir))
        except (OSError, ValueError, TypeError):
            model = False
        runtime = _runtime_spec() is not None
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {"ready": dependencies and model and runtime and callable(self.owner_gate),
                    "dependencies": dependencies, "model": model, "running": running,
                    "active": self._active and not self._stop.is_set(), "reason": self.reason,
                    "metadata_policy": gestures.METADATA_POLICY, "isolated": True}

    def start(self):
        if not self._allowed():
            self._halt("owner_required")
            return False
        with self._lock:
            if self._stopping or self._stop_waiters:
                return False
            if self._process is not None and self._process.poll() is None:
                return not self._stop.is_set()
            report, runtime = self.available(), _runtime_spec()
            if not report["ready"] or runtime is None:
                reason = "isolation_runtime_unavailable" if runtime is None else "components_missing"
                self.reason = reason
                self._emit(self.on_state, _STATES[reason])
                return False
            executable, worker = runtime
            config = json.dumps({"version": 1, "model_dir": str(_local_path(self.model_dir)),
                                 "camera_index": self.camera_index}, separators=(",", ":")).encode() + b"\n"
            if len(config) > _MAX_LINE:
                self.reason = "components_missing"
                return False
            process = job = None
            try:
                process = self._factory([str(executable), "-I", "-S", str(worker), "--worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    shell=False, bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=_environment(executable), cwd=str(worker.parent.parent))
                from desktop.clone_speech import _OwnedJob
                job = (self._job_factory or _OwnedJob)(process)
                if os.name == "nt" and not getattr(job, "handle", None):
                    raise RuntimeError("owned_job_unavailable")
                self._generation += 1
                generation = self._generation
                self._process, self._job = process, job
                self._stop.clear()
                self._active, self.reason, self._stopping = False, "starting", False
                self._last_message, self._ready_at, self._last_intent = time.monotonic(), None, None
                threading.Thread(target=self._write_config, args=(process, generation, config), daemon=True).start()
                threading.Thread(target=self._read, args=(process, generation), name="jarvis-gesture-output", daemon=True).start()
                threading.Thread(target=self._watch, args=(process, generation), name="jarvis-gesture-owner", daemon=True).start()
            except Exception:
                if process is not None:
                    self._dispose(process, job, graceful=False)
                self._process = self._job = None
                self.reason = "isolation_runtime_unavailable"
                self._emit(self.on_state, _STATES[self.reason])
                return False
        if self._current(process, generation) and self.reason == "starting":
            self._emit(self.on_state, _STATES["starting"])
        return True

    def _current(self, process, generation):
        return self._process is process and self._generation == generation and not self._stop.is_set()

    def _write_config(self, process, generation, config):
        try:
            if not self._current(process, generation) or not self._allowed():
                self._fail(process, generation, "owner_required")
                return
            process.stdin.write(config)
            process.stdin.flush()
        except Exception:
            self._fail(process, generation, "camera_unavailable")

    def _event(self, process, generation, event):
        callback = failure = None
        with self._lock:
            if not self._current(process, generation):
                return
            now = time.monotonic()
            kind = event.get("type")
            if kind == "heartbeat" and set(event) == {"type", "progress"} and type(event["progress"]) is bool:
                if self._ready_at is not None and not event["progress"] and now - self._ready_at > 3:
                    raise ValueError("recognizer_stalled")
            elif kind == "state" and set(event) == {"type", "value"} and event.get("value") in _STATES:
                state = event["value"]
                if state not in {"ready", "starting"}:
                    failure = state
                else:
                    self.reason, self._active = state, state == "ready"
                    if state == "ready" and self._ready_at is None:
                        self._ready_at = now
                    callback = self.on_state, _STATES[state]
            elif kind == "intent" and set(event) == {"type", "value"} and event.get("value") in _INTENTS:
                if not self._active or self._last_intent is not None and now - self._last_intent < 1.5:
                    raise ValueError("intent_rate_refused")
                if not self._allowed(event["value"]):
                    failure = "owner_required"
                else:
                    self._last_intent = now
                    callback = self.on_intent, event["value"]
            else:
                raise ValueError("event_refused")
            self._last_message = now
        if failure:
            self._fail(process, generation, failure)
        elif callback is not None and self._current(process, generation):
            self._emit(*callback)

    def _read(self, process, generation):
        events = deque()
        try:
            while self._current(process, generation):
                raw = process.stdout.readline(_MAX_LINE + 1)
                if not raw:
                    break
                now = time.monotonic()
                while events and events[0] < now - 1:
                    events.popleft()
                events.append(now)
                if len(events) > 20:
                    raise ValueError("event_rate_refused")
                self._event(process, generation, _decode(raw))
        except Exception:
            pass
        finally:
            self._fail(process, generation, "camera_unavailable")

    def _watch(self, process, generation):
        started = time.monotonic()
        while self._current(process, generation):
            if not self._allowed():
                self._fail(process, generation, "owner_required")
                return
            now = time.monotonic()
            if (process.poll() is not None or now - started > 30 and self._ready_at is None
                    or self._ready_at is not None and now - self._last_message > 3):
                self._fail(process, generation, "camera_unavailable")
                return
            self._stop.wait(0.1)

    def _fail(self, process, generation, reason):
        self._halt(reason, expected=(process, generation))

    @staticmethod
    def _dispose(process, job, *, graceful):
        if graceful:
            def request_stop():
                try:
                    process.stdin.write(b'{"type":"stop"}\n')
                    process.stdin.flush()
                    process.stdin.close()
                except Exception:
                    pass
            threading.Thread(target=request_stop, daemon=True).start()
        try:
            process.wait(timeout=0.35 if graceful else 0)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=0.35)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=0.35)
                except Exception:
                    pass
        finally:
            if job is not None:
                try:
                    job.close()
                except Exception:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=0.35)
                except Exception:
                    pass

    def _halt(self, reason, *, expected=None):
        with self._lock:
            if expected is not None and not self._current(*expected):
                return False
            if self._stopping:
                # An explicit UI stop joins the already-owned shutdown. It
                # overrides the watchdog's final label without a second kill.
                if reason != "disabled" or expected is not None:
                    return False
                self._generation += 1
                self._shutdown_reason, self._shutdown_generation = reason, self._generation
                completed, process = self._shutdown_done, self._process
                joining = True
            else:
                self._generation += 1
                self._stop.set()
                self._active, self._stopping = False, True
                process, job = self._process, self._job
                completed = self._shutdown_done = threading.Event()
                self._shutdown_reason, self._shutdown_generation = reason, self._generation
                joining = False
        if joining:
            completed.wait(timeout=2)
            stopped = process is None or process.poll() is not None
            with self._lock:
                # start() cannot race a public stop waiter. If the bounded
                # wait expired, the first shutdown still emits its final state.
                if completed.is_set() and self._shutdown_done is completed:
                    self.reason = "disabled" if stopped else "camera_unavailable"
            if completed.is_set():
                self._emit(self.on_state, _STATES["disabled" if stopped else "camera_unavailable"])
            return stopped and completed.is_set()
        if process is not None:
            self._dispose(process, job, graceful=True)
        with self._lock:
            stopped = process is None or process.poll() is not None
            if self._process is process and stopped:
                self._process = self._job = None
            reason = self._shutdown_reason if stopped else "camera_unavailable"
            generation = self._shutdown_generation
            self.reason, self._stopping = reason, False
            completed.set()
        if generation == self._generation:
            self._emit(self.on_state, _STATES[reason])
        return stopped

    def stop(self):
        with self._lock:
            self._stop_waiters += 1
        try:
            return self._halt("disabled")
        finally:
            with self._lock:
                self._stop_waiters -= 1

    close = stop


def _bootstrap():
    executable, worker = Path(sys.executable).absolute(), Path(__file__).resolve()
    app, packages = _locations(executable, worker)
    if not packages.is_dir():
        raise ValueError("runtime_unavailable")
    sys.path[:] = [str(app), str(packages)] + [entry for entry in sys.path if "site-packages" not in Path(entry).parts]
    handles = []
    if os.name == "nt":
        for name in ("cv2", "numpy.libs", "mediapipe"):
            directory = packages / name
            if directory.is_dir():
                handles.append(os.add_dll_directory(str(directory)))
    return handles


def _worker_config(raw):
    value = _decode(raw)
    if (set(value) != {"version", "model_dir", "camera_index"} or type(value["version"]) is not int or value["version"] != 1
            or type(value["camera_index"]) is not int or not 0 <= value["camera_index"] <= 9):
        raise ValueError("config_invalid")
    gestures = importlib.import_module("desktop.gestures")
    value["model_dir"] = _local_path(value["model_dir"])
    if not gestures.model_ready(value["model_dir"]):
        raise ValueError("model_invalid")
    return value


def _run_worker(emit, source):
    stopped, progress = threading.Event(), [None]
    service = None
    prepared = None
    recognizer_used = threading.Event()
    try:
        handles = _bootstrap()
        config = _worker_config(source.readline(_MAX_LINE + 1))
        gestures = importlib.import_module("desktop.gestures")
        # Windows native extensions can deadlock when their first DLL/model
        # initialization races another Python thread. Warm them on this main
        # thread before even starting the stdin reader. No camera opens here.
        for name in ("numpy", "cv2", "mediapipe"):
            importlib.import_module(name)
        prepared = gestures._Recognizer(config["model_dir"] / gestures.MODEL_NAME)

        def recognizer(model):
            if recognizer_used.is_set() or model != config["model_dir"] / gestures.MODEL_NAME:
                raise ValueError("recognizer_refused")
            recognizer_used.set()

            class Progress:
                def recognize(self, frame, timestamp):
                    result = prepared.recognize(frame, timestamp)
                    progress[0] = time.monotonic()
                    return result

                def close(self):
                    prepared.close()
            return Progress()

        state_codes = {_STATES[key]: key for key in ("ready", "disabled", "camera_unavailable")}
        state_codes["Камера выключена: установите компоненты и модель жестов."] = "components_missing"
        service = gestures.GestureService(
            lambda intent: emit({"type": "intent", "value": intent}) if intent in _INTENTS else None,
            lambda state: emit({"type": "state", "value": state_codes.get(state, "camera_unavailable")}),
            owner_gate=lambda _: True, model_dir=config["model_dir"], camera_index=config["camera_index"],
            recognizer_factory=recognizer)

        def commands():
            try:
                value = _decode(source.readline(_MAX_LINE + 1))
                if value != {"type": "stop"}:
                    raise ValueError("command_refused")
            except Exception:
                pass
            stopped.set()
            service._stop.set()

        threading.Thread(target=commands, daemon=True).start()
        if stopped.is_set() or not service.start():
            return 1
        while not stopped.wait(0.4):
            if service._thread is None or not service._thread.is_alive():
                break
            emit({"type": "heartbeat", "progress": progress[0] is not None and time.monotonic() - progress[0] < 2})
        return 0
    except Exception:
        try:
            emit({"type": "state", "value": "camera_unavailable"})
        except Exception:
            pass
        return 1
    finally:
        if service is not None:
            service.stop()
        if prepared is not None and not recognizer_used.is_set():
            prepared.close()
        _ = locals().get("handles")  # Retain native DLL-directory handles.


def _worker_main():
    # Keep a private protocol descriptor, suppress Python AND native SDK logs.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    with open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
    lock = threading.Lock()

    def emit(value):
        body = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        with lock:
            output.write(body)

    try:
        return _run_worker(emit, sys.stdin.buffer)
    finally:
        output.close()


if __name__ == "__main__":
    raise SystemExit(_worker_main() if sys.argv[1:] == ["--worker"] else 2)
