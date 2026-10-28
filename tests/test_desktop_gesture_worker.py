"""Owned fake child processes only; no live capture, SDK import or model run."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import gesture_worker as worker
from desktop import gestures


def wait_for(predicate, seconds=2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        threading.Event().wait(0.005)
    assert predicate()


class Pipe:
    def __init__(self):
        self.queue = queue.Queue()

    def send(self, value):
        self.queue.put(json.dumps(value).encode() + b"\n" if isinstance(value, dict) else value)

    def readline(self, limit):
        return self.queue.get()[:limit]


class Process:
    def __init__(self, *, graceful=True, terminate=True):
        self.stdout, self.returncode = Pipe(), None
        self.config = threading.Event()
        self.writes, self.waits = [], []
        self.graceful, self.terminate_works = graceful, terminate
        self.terminated = self.killed = False
        self.stdin = SimpleNamespace(write=self.write, flush=lambda: None, close=lambda: None)

    def write(self, value):
        self.writes.append(value)
        if value == b'{"type":"stop"}\n':
            if self.graceful:
                self.exit(0)
        else:
            self.config.set()
        return len(value)

    def exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.send(b"")

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("owned-fake-gesture", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.terminate_works:
            self.exit(-15)

    def kill(self):
        self.killed = True
        self.exit(-9)


@pytest.fixture
def service(tmp_path, monkeypatch):
    intents, states, owner, processes, arguments, jobs = [], [], [True], [], [], []
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(gestures, "_dependencies_ready", lambda: True)
    monkeypatch.setattr(gestures, "model_ready", lambda _: True)
    monkeypatch.setattr(gestures, "_open_camera", lambda _: pytest.fail("Parent must not open a camera"))
    monkeypatch.setattr(gestures, "_Recognizer", lambda _: pytest.fail("Parent must not import an SDK recognizer"))
    runtime = (tmp_path / "python.exe", Path(worker.__file__).resolve())
    monkeypatch.setattr(worker, "_runtime_spec", lambda: runtime)

    def factory(argv, **kwargs):
        arguments.append((argv, kwargs))
        process = Process()
        processes.append(process)
        return process

    def job_factory(process):
        job = SimpleNamespace(handle=True, closed=False)
        job.close = lambda: setattr(job, "closed", True)
        jobs.append(job)
        return job

    instance = worker.IsolatedGestureService(intents.append, states.append, owner_gate=lambda _: owner[0],
        model_dir=model, _process_factory=factory, _job_factory=job_factory)
    yield instance, intents, states, owner, processes, arguments, jobs
    instance.stop()


def test_constructor_and_available_never_start_child_or_import_camera_sdk(service):
    instance, intents, _, _, processes, _, _ = service
    assert instance.reason == "disabled" and not processes and not intents
    report = instance.available()
    assert report["ready"] and report["isolated"] and not report["running"] and not report["active"]
    assert report["metadata_policy"] == "sdk_metrics_possible" and not processes


@pytest.mark.parametrize("camera", [-1, 10, True, "0", 1.0])
def test_only_small_local_integer_camera_indices_are_accepted(camera):
    with pytest.raises(ValueError):
        worker.IsolatedGestureService(lambda _: None, lambda _: None, owner_gate=lambda _: True, camera_index=camera)


def test_explicit_start_checks_owner_before_spawning_and_reports_starting_not_active(service):
    instance, _, _, owner, processes, arguments, _ = service
    owner[0] = False
    assert not instance.start() and not processes and instance.reason == "owner_required"
    owner[0] = True
    assert instance.start()
    process = processes[0]
    wait_for(process.config.is_set)
    assert instance.available()["running"] and not instance.available()["active"]
    argv, options = arguments[0]
    assert argv[1:] == ["-I", "-S", str(Path(worker.__file__).resolve()), "--worker"]
    assert options["shell"] is False and options["stderr"] == subprocess.DEVNULL
    assert "PYTHONPATH" not in options["env"] and options["env"]["PYTHONNOUSERSITE"] == "1"
    config = json.loads(process.writes[0])
    assert config == {"version": 1, "model_dir": str(instance.model_dir), "camera_index": 0}
    assert instance.start() and len(processes) == 1


@pytest.mark.parametrize("missing", ["runtime", "dependencies", "model"])
def test_missing_runtime_components_or_model_never_falls_back_to_parent_camera(service, monkeypatch, missing):
    instance, _, _, _, processes, _, _ = service
    if missing == "runtime":
        monkeypatch.setattr(worker, "_runtime_spec", lambda: None)
    elif missing == "dependencies":
        monkeypatch.setattr(gestures, "_dependencies_ready", lambda: False)
    else:
        monkeypatch.setattr(gestures, "model_ready", lambda _: False)
    assert not instance.start() and not processes
    assert instance.reason == ("isolation_runtime_unavailable" if missing == "runtime" else "components_missing")


def test_ready_and_finite_intent_require_parent_owner_gate_for_that_intent(service):
    instance, intents, _, _, processes, _, _ = service
    gates = []
    instance.owner_gate = lambda intent: gates.append(intent) or True
    assert instance.start()
    process = processes[0]
    process.stdout.send({"type": "state", "value": "ready"})
    wait_for(lambda: instance.available()["active"])
    process.stdout.send({"type": "intent", "value": "volume_up"})
    wait_for(lambda: intents == ["volume_up"])
    assert None in gates and "volume_up" in gates


def test_owner_revocation_stops_even_if_capture_does_not_emit_frames_or_intents(service):
    instance, intents, _, owner, processes, _, jobs = service
    assert instance.start()
    processes[0].graceful = False
    owner[0] = False
    wait_for(lambda: processes[0].terminated)
    wait_for(lambda: instance.reason == "owner_required")
    assert not intents and jobs[0].closed and not instance.available()["running"]


def test_revoked_per_intent_gate_rejects_child_intent_without_dispatch(service):
    instance, intents, _, _, processes, _, _ = service
    instance.owner_gate = lambda intent: intent is None
    assert instance.start()
    processes[0].stdout.send({"type": "state", "value": "ready"})
    processes[0].stdout.send({"type": "intent", "value": "minimize"})
    wait_for(lambda: instance.reason == "owner_required")
    assert not intents and not instance.available()["running"]


def test_blocked_capture_shutdown_terminates_only_owned_child_within_bounded_wait(service):
    instance, intents, _, _, processes, _, jobs = service
    assert instance.start()
    process = processes[0]
    process.graceful = process.terminate_works = False
    generation = instance._generation
    started = time.monotonic()
    assert instance.stop()
    assert time.monotonic() - started < 2
    assert process.terminated and process.killed and jobs[0].closed
    assert all(value <= 0.35 for value in process.waits)
    instance._event(process, generation, {"type": "intent", "value": "volume_up"})
    assert not intents and not instance.available()["active"]


def test_blocked_stdin_stop_write_does_not_block_parent_shutdown(service):
    instance, _, _, _, processes, _, _ = service
    assert instance.start()
    process = processes[0]
    wait_for(process.config.is_set)
    release = threading.Event()
    process.stdin.write = lambda _: release.wait(5)
    process.graceful = False
    try:
        started = time.monotonic()
        assert instance.stop() and time.monotonic() - started < 2
    finally:
        release.set()


def test_old_reader_eof_or_late_intents_cannot_stop_or_dispatch_after_restart(service):
    instance, intents, _, _, processes, _, _ = service
    assert instance.start()
    previous, generation = processes[0], instance._generation
    assert instance.stop() and instance.start()
    current = processes[-1]
    instance._event(previous, generation, {"type": "intent", "value": "volume_up"})
    instance._fail(previous, generation, "camera_unavailable")
    assert instance._process is current and current.poll() is None and not intents


def test_explicit_stop_joins_watchdog_shutdown_without_duplicate_kill_or_stale_restart(service, monkeypatch):
    instance, intents, states, _, processes, _, jobs = service
    assert instance.start()
    process, generation = processes[0], instance._generation
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    disposals, results = [], []
    original = instance._dispose

    def blocked_dispose(owned, job, *, graceful):
        disposals.append(owned)
        entered.set()
        assert release.wait(2)
        original(owned, job, graceful=graceful)

    monkeypatch.setattr(instance, "_dispose", blocked_dispose)
    watchdog = threading.Thread(target=instance._fail, args=(process, generation, "owner_required"), daemon=True)
    watchdog.start()
    assert entered.wait(1)

    def stop():
        results.append(instance.stop())
        stopped.set()

    operator = threading.Thread(target=stop, daemon=True)
    operator.start()
    try:
        wait_for(lambda: instance._stop_waiters == 1)
        assert not stopped.is_set() and not instance.start()
        assert not instance.available()["active"]
    finally:
        release.set()
    operator.join(timeout=2)
    watchdog.join(timeout=2)
    assert not operator.is_alive() and not watchdog.is_alive()
    assert results == [True] and disposals == [process] and jobs[0].closed
    assert instance.reason == "disabled" and states[-1] == "Камера выключена."
    assert not instance.available()["running"] and not intents
    assert instance.start()
    current = processes[-1]
    instance._fail(process, generation, "owner_required")
    assert instance._process is current and current.poll() is None


def test_join_timeout_is_truthful_but_eventual_shutdown_still_emits_exact_camera_off(service, monkeypatch):
    instance, _, states, _, processes, _, _ = service
    assert instance.start()
    process, generation = processes[0], instance._generation
    entered, release = threading.Event(), threading.Event()
    original = instance._dispose

    def blocked_dispose(owned, job, *, graceful):
        entered.set()
        release.wait(2)
        original(owned, job, graceful=graceful)

    monkeypatch.setattr(instance, "_dispose", blocked_dispose)
    watchdog = threading.Thread(target=instance._fail, args=(process, generation, "owner_required"), daemon=True)
    watchdog.start()
    assert entered.wait(1)
    waits = []
    monkeypatch.setattr(instance._shutdown_done, "wait", lambda timeout: waits.append(timeout) or False)
    try:
        assert not instance.stop() and waits == [2]
        assert process.poll() is None and "Камера выключена." not in states
        assert not instance.start()
    finally:
        release.set()
    watchdog.join(timeout=2)
    assert not watchdog.is_alive() and process.poll() is not None
    assert states[-1] == "Камера выключена." and instance.reason == "disabled"


@pytest.mark.parametrize("raw", [b"{invalid}\n", b'{"type":"intent","value":"shutdown"}\n',
    b'{"type":"state","value":"private camera path"}\n', b'{"type":"heartbeat","progress":1}\n',
    b'{"type":"intent","value":"volume_up","image":"bytes"}\n',
    b'{"type":"state","type":"intent","value":"volume_up"}\n', b"x" * 4097],
    ids=["invalid-json", "power-intent", "private-state", "invalid-health", "extra-frame", "duplicate", "oversize"])
def test_malformed_or_unapproved_child_output_is_closed_without_leaking_content(service, raw):
    instance, intents, states, _, processes, _, _ = service
    assert instance.start()
    processes[0].stdout.send(raw)
    wait_for(lambda: instance.reason == "camera_unavailable")
    assert not intents and not instance.available()["running"]
    assert all(state in worker._STATES.values() for state in states)


def test_child_event_flood_or_impossible_gesture_rate_stops_camera(service):
    instance, intents, _, _, processes, _, _ = service
    assert instance.start()
    process = processes[0]
    process.stdout.send({"type": "state", "value": "ready"})
    process.stdout.send({"type": "intent", "value": "volume_up"})
    process.stdout.send({"type": "intent", "value": "volume_up"})
    wait_for(lambda: instance.reason == "camera_unavailable")
    assert intents == ["volume_up"]


def test_stalled_recognition_health_stops_owned_camera_even_when_child_can_send_heartbeats(service):
    instance, _, _, _, processes, _, _ = service
    assert instance.start()
    instance._ready_at = time.monotonic() - 4
    processes[0].stdout.send({"type": "heartbeat", "progress": False})
    wait_for(lambda: instance.reason == "camera_unavailable")
    assert not instance.available()["running"]


def test_failed_own_job_assignment_never_sends_camera_configuration(service, monkeypatch):
    instance, _, _, _, processes, _, _ = service
    monkeypatch.setattr(worker.os, "name", "nt")
    instance._job_factory = lambda _: SimpleNamespace(handle=None, close=lambda: None)
    assert not instance.start()
    assert processes[0].terminated and not processes[0].writes


def test_callback_exception_cannot_prevent_owned_process_cleanup(service):
    instance, _, _, _, processes, _, jobs = service
    instance.on_state = lambda _: (_ for _ in ()).throw(RuntimeError("private"))
    assert instance.start() and instance.stop()
    assert processes[0].poll() is not None and jobs[0].closed


def test_frozen_build_reports_isolation_unavailable_instead_of_starting_an_unchecked_camera(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert worker._runtime_spec() is None


def test_pythonw_uses_only_its_sibling_console_runtime_and_fixed_worker(tmp_path, monkeypatch):
    executable = tmp_path / "pythonw.exe"
    executable.write_bytes(b"not executed")
    console = tmp_path / "python.exe"
    console.write_bytes(b"not executed")
    packages = tmp_path / "site-packages"
    packages.mkdir()
    app = Path(worker.__file__).resolve().parent.parent
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(worker, "_locations", lambda *_: (app, packages))
    specification = worker._runtime_spec()
    assert specification == (console, Path(worker.__file__).resolve())
    console.unlink()
    assert worker._runtime_spec() is None


@pytest.mark.parametrize("changes", [{"camera_index": True}, {"camera_index": 10}, {"model_dir": "https://evil/model"},
    {"model_dir": "../private"}, {"version": True}, {"extra": "code"}])
def test_worker_config_is_closed_local_pinned_model_input_only(tmp_path, monkeypatch, changes):
    monkeypatch.setattr(gestures, "model_ready", lambda _: True)
    value = {"version": 1, "model_dir": str(tmp_path), "camera_index": 0, **changes}
    with pytest.raises(ValueError):
        worker._worker_config(json.dumps(value).encode() + b"\n")


def test_worker_requires_original_checksum_validation_before_sdk_can_start(tmp_path, monkeypatch):
    checked = []
    monkeypatch.setattr(gestures, "model_ready", lambda path: checked.append(path) or False)
    value = json.dumps({"version": 1, "model_dir": str(tmp_path), "camera_index": 0}).encode() + b"\n"
    with pytest.raises(ValueError, match="model_invalid"):
        worker._worker_config(value)
    assert checked == [tmp_path]


@pytest.mark.parametrize("valid_config", [True, False], ids=["validated-model", "rejected-config"])
def test_child_initializes_native_modules_and_model_on_main_thread_before_any_threads(tmp_path, monkeypatch, valid_config):
    order, events = [], []
    model = tmp_path / "model"

    class Prepared:
        def __init__(self, path):
            assert threading.current_thread() is threading.main_thread()
            assert path == model / "gesture_recognizer.task"
            order.append("model-main")

        def close(self):
            order.append("model-close")

    class Service:
        def __init__(self, *_args, **kwargs):
            assert kwargs["owner_gate"](None) is True
            order.append("service-created")

        def start(self):
            order.append("service-thread-start")
            return False

        def stop(self):
            order.append("service-stop")

    class Reader:
        def __init__(self, **_kwargs):
            order.append("stdin-thread-created")

        def start(self):
            order.append("stdin-thread-start")

    def validate(_):
        order.append("config-validated")
        if not valid_config:
            raise ValueError("invalid")
        return {"model_dir": model, "camera_index": 0}

    fake_gestures = SimpleNamespace(_Recognizer=Prepared, GestureService=Service, MODEL_NAME="gesture_recognizer.task")

    def module(name):
        if name == "desktop.gestures":
            return fake_gestures
        order.append("native-" + name)
        return SimpleNamespace()

    monkeypatch.setattr(worker, "_bootstrap", lambda: [])
    monkeypatch.setattr(worker, "_worker_config", validate)
    monkeypatch.setattr(worker.importlib, "import_module", module)
    monkeypatch.setattr(worker.threading, "Thread", Reader)
    assert worker._run_worker(events.append, SimpleNamespace(readline=lambda _: b"{}\n")) == 1
    if valid_config:
        assert order == ["config-validated", "native-numpy", "native-cv2", "native-mediapipe", "model-main",
                         "service-created", "stdin-thread-created", "stdin-thread-start", "service-thread-start",
                         "service-stop", "model-close"]
    else:
        assert order == ["config-validated"]
        assert events == [{"type": "state", "value": "camera_unavailable"}]
