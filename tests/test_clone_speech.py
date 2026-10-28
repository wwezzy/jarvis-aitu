"""Private helper protocol checks; no GPU, audio playback, OS commands or model download."""
import base64
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from desktop import clone_speech as clone


def test_public_warming_status_stops_immediately_on_close(tmp_path):
    speech = clone.CloneSpeech(tmp_path/'absent.json', personal_use=True)
    assert speech.is_warming is False
    with speech._state_lock:
        speech._warming = True
    assert speech.is_warming is True
    speech.close()
    assert speech.is_warming is False


def wav(path, *, channels=1, rate=24000, frames=2400):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\x01\x00" * frames * channels)


@pytest.fixture
def manifest(tmp_path):
    python = tmp_path / "python.exe"
    python.write_bytes(b"test runtime metadata only; never executed")
    reference, ack = tmp_path / "reference.wav", tmp_path / "ack.wav"
    wav(reference, frames=24000 * 6)
    wav(ack)
    module = Path(clone.__file__).resolve()
    value = {"version": 1, "kind": "private-local-clone", "personal_use_only": True,
             "model_revision": clone.MODEL_REVISION, "python": str(python), "python_sha256": clone._sha(python),
             "worker": str(module.with_name("clone_speech_worker.py")), "worker_sha256": clone._sha(module.with_name("clone_speech_worker.py")),
             "module_sha256": clone._sha(module), "reference_wav": str(reference), "reference_sha256": clone._sha(reference),
             "ack_ru_wav": str(ack), "ack_ru_sha256": clone._sha(ack), "model_dir": str(tmp_path / "model"), "device": "cuda"}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class FakeOutput:
    def __init__(self):
        self.items = queue.Queue()

    def push(self, value):
        self.items.put(json.dumps(value).encode() + b"\n")

    def readline(self, maximum):
        return self.items.get(timeout=5)[:maximum]

    def close(self):
        self.items.put(b"")


class FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, body):
        request = json.loads(body)
        self.process.requests.append(request)
        if request["type"] == "cancel":
            self.process.stdout.push({"type": "done", "id": request["id"]})
        else:
            for value in self.process.responses(request):
                self.process.stdout.push(value)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    def __init__(self, responses):
        self.stdout, self.requests, self.responses = FakeOutput(), [], responses
        self.stdin, self.returncode, self.terminated = FakeInput(self), None, 0
        self.stdout.push({"type": "ready", "rate": 24000})

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -1
        self.stdout.close()

    def wait(self, timeout):
        return self.returncode

    def kill(self):
        self.terminate()


def valid_response(request):
    yield {"type": "audio", "id": request["id"], "rate": 24000, "pcm": base64.b64encode(b"\x01\x00" * 100).decode()}
    yield {"type": "done", "id": request["id"]}


def adapter(manifest, responses=valid_response, **options):
    process = FakeProcess(responses)
    launches = []

    def factory(args, **kwargs):
        launches.append((args, kwargs))
        return process

    return clone.CloneSpeech(manifest, personal_use=True, _process_factory=factory, **options), process, launches


def test_default_off_never_reads_or_starts_runtime(manifest, monkeypatch):
    monkeypatch.setattr(clone, "load_manifest", lambda *_: pytest.fail("Disabled clone read assets"))
    voice = clone.CloneSpeech(manifest)
    assert not voice.available() and voice.reason == "personal_use_required"
    assert not voice.prepare()
    with pytest.raises(clone.CloneSpeechUnavailable, match="personal_use_required"):
        voice.synthesize("Привет, сэр")


def test_stream_uses_owned_runtime_keeps_warm_and_never_passes_secret_env(manifest, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-inherit")
    monkeypatch.setenv("PATH", "UNTRUSTED_FOREIGN_DLL_DIRECTORY")
    voice, process, launches = adapter(manifest)
    first = voice.synthesize("Привет, сэр")
    second = voice.synthesize("Good evening, sir.")
    assert first == second == (b"\x01\x00" * 100, 24000)
    assert len(launches) == 1
    args, kwargs = launches[0]
    assert args[1:3] == ["-I", "-u"] and kwargs["shell"] is False
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "UNTRUSTED_FOREIGN_DLL_DIRECTORY" not in kwargs["env"]["PATH"]
    assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert [request["language"] for request in process.requests] == ["ru", "en"]
    assert all(set(request) == {"type", "id", "text", "rate", "language"} for request in process.requests)
    voice.close()
    assert process.terminated == 1


def test_original_ru_ack_and_warmed_en_ack_cached_without_repeated_synthesis(manifest):
    voice, process, _ = adapter(manifest)
    assert voice.synthesize("Да, сэр")[0] == b"\x01\x00" * 2400
    assert process.requests == []
    assert voice.prepare()
    assert len(process.requests) == 1 and process.requests[0]["text"] == "Yes, sir."
    assert voice.synthesize("Yes, sir!")[0] == b"\x01\x00" * 100
    assert len(process.requests) == 1
    voice.close()


def test_background_preparation_does_not_block_exact_cue_or_duplicate_warmers(manifest, monkeypatch):
    voice, _, launches = adapter(manifest)
    started, release = threading.Event(), threading.Event()
    prepare = voice.prepare

    def slow_prepare(rate=185):
        with voice._request_lock:
            started.set()
            release.wait(2)
        return prepare(rate)
    monkeypatch.setattr(voice, "prepare", slow_prepare)
    try:
        assert voice.prepare_background()
        assert started.wait(1) and not voice.is_ready
        assert not voice.prepare_background()
        moment = time.monotonic()
        assert voice.fixed_ack("ru") == (b"\x01\x00" * 2400, 24000)
        assert time.monotonic() - moment < 0.5
        assert not launches and voice.fixed_ack("en") is None
    finally:
        release.set()
    voice._background_thread.join(1)
    assert voice.is_ready and not voice.prepare_background()
    assert voice.fixed_ack("en") == (b"\x01\x00" * 100, 24000)
    voice.close()
    assert not voice.is_ready and not voice.prepare_background()


def test_close_during_cached_cue_read_never_yields_late_audio(manifest, monkeypatch):
    voice, _, launches = adapter(manifest)
    original = clone.read_pcm

    def read_then_close(*args, **kwargs):
        data = original(*args, **kwargs)
        voice.close()
        return data
    monkeypatch.setattr(clone, "read_pcm", read_then_close)
    with pytest.raises(clone.CloneSpeechUnavailable, match="cancelled|closed"):
        voice.synthesize("Да, сэр")
    assert not launches


@pytest.mark.parametrize("wrong_id", [True, 1.0, "1"])
def test_response_identity_requires_strict_integer(manifest, wrong_id):
    def response(_):
        yield {"type": "audio", "id": wrong_id, "rate": 24000, "pcm": base64.b64encode(b"xx").decode()}
    voice, process, _ = adapter(manifest, response)
    with pytest.raises(clone.CloneSpeechUnavailable, match="helper_protocol"):
        voice.synthesize("Обычная фраза")
    assert process.terminated == 1


@pytest.mark.parametrize("field,value", [("worker_sha256", "0" * 64), ("python_sha256", "0" * 64), ("reference_sha256", "0" * 64), ("module_sha256", "0" * 64), ("worker", "https://example.org/helper.py")])
def test_invalid_asset_pin_never_creates_child(manifest, field, value):
    data = json.loads(manifest.read_text())
    data[field] = value
    manifest.write_text(json.dumps(data))
    voice, _, launches = adapter(manifest)
    assert not voice.prepare() and launches == []


def test_venv_launcher_also_requires_pinned_base_interpreter_and_config(manifest, tmp_path):
    data = json.loads(manifest.read_text())
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "Lib" / "site-packages").mkdir(parents=True)
    launcher = scripts / "python.exe"
    launcher.write_bytes(b"synthetic launcher metadata only")
    base = tmp_path / "base" / "python.exe"
    base.parent.mkdir()
    base.write_bytes(b"synthetic base interpreter metadata only")
    config = scripts.parent / "pyvenv.cfg"
    config.write_text(f"executable = {base}\ninclude-system-site-packages = false\n")
    data.update(python=str(launcher), python_sha256=clone._sha(launcher), base_python=str(base),
                base_python_sha256=clone._sha(base), venv_config_sha256=clone._sha(config))
    manifest.write_text(json.dumps(data))
    assert clone.load_manifest(manifest)["base_python"] == str(base.resolve())
    config.write_text("executable = C:/untrusted/python.exe\ninclude-system-site-packages = true\n")
    voice, _, launches = adapter(manifest)
    assert not voice.available() and not launches


@pytest.mark.parametrize("text,rate,language", [("", 185, None), ("x\0y", 185, None), ("a" * 701, 185, None), ("<speak>hello</speak>", 185, None), ("hello", True, None), ("hello", 241, None), ("hello", 185, "ru; cmd")])
def test_invalid_request_cannot_reach_helper(manifest, text, rate, language):
    voice, _, launches = adapter(manifest)
    with pytest.raises(clone.CloneSpeechUnavailable):
        voice.synthesize(text, rate, language=language)
    assert not launches


@pytest.mark.parametrize("pcm,rate,kind", [("!!!", 24000, "audio"), (base64.b64encode(b"x").decode(), 24000, "audio"), (base64.b64encode(b"xx").decode(), 16000, "audio"), ("", 24000, "tool_call")])
def test_malformed_output_rejected_and_only_owned_child_closed(manifest, pcm, rate, kind):
    def response(request):
        yield {"type": kind, "id": request["id"], "rate": rate, "pcm": pcm}
    voice, process, _ = adapter(manifest, response)
    with pytest.raises(clone.CloneSpeechUnavailable):
        voice.synthesize("Обычная фраза")
    assert process.terminated == 1


def test_timeout_is_bounded_and_closes_only_owned_helper(manifest):
    voice, process, _ = adapter(manifest, lambda _: [], request_timeout=0.1)
    started = time.monotonic()
    with pytest.raises(clone.CloneSpeechUnavailable, match="helper_timeout"):
        voice.synthesize("Обычная фраза")
    assert time.monotonic() - started < 1
    assert process.terminated == 1


def test_close_interrupts_wait_without_request_lock_deadlock(manifest):
    requested = threading.Event()

    def response(_):
        requested.set()
        return []
    voice, process, _ = adapter(manifest, response)
    errors = []

    def render():
        try:
            voice.synthesize("Длинная фраза")
        except clone.CloneSpeechUnavailable as error:
            errors.append(str(error))
    worker = threading.Thread(target=render)
    worker.start()
    assert requested.wait(1)
    voice.close()
    worker.join(1)
    assert not worker.is_alive() and errors
    assert process.terminated == 1


def test_close_tolerates_async_job_termination_access_denied_race(manifest, monkeypatch):
    voice, process, _ = adapter(manifest)
    process.exiting, process.waited = False, 0

    class AsyncJob:
        def __init__(self, owned):
            self.owned = owned

        def close(self):
            self.owned.exiting = True

    def terminate():
        assert process.exiting
        process.terminated += 1
        raise PermissionError("Synthetic already-terminating child")

    def wait(timeout):
        process.waited += 1
        process.returncode = -1
        process.stdout.close()
        return -1

    monkeypatch.setattr(clone, "_OwnedJob", AsyncJob)
    monkeypatch.setattr(process, "terminate", terminate)
    monkeypatch.setattr(process, "wait", wait)
    assert voice.prepare() and voice.is_ready
    voice.close()
    assert not voice.is_ready and voice._process is None
    assert process.terminated == 1 and process.waited == 1
    assert voice.error_class is None


def test_cancellation_drains_final_marker_and_retains_warm_child(manifest):
    cancelled = threading.Event()

    def response(request):
        yield {"type": "audio", "id": request["id"], "rate": 24000, "pcm": base64.b64encode(b"xx").decode()}
    voice, process, launches = adapter(manifest, response)
    stream = voice.stream("Обычная фраза", cancel=cancelled.is_set)
    assert next(stream) == (b"xx", 24000)
    cancelled.set()
    with pytest.raises(clone.CloneSpeechUnavailable, match="cancelled"):
        next(stream)
    assert process.terminated == 0
    assert process.requests[-1]["type"] == "cancel"
    assert len(launches) == 1
    voice.close()


def test_only_pinned_model_bytes_are_accepted_before_deserialization(tmp_path, monkeypatch):
    path = tmp_path / "model.pth"
    path.write_bytes(b"known safe fixture, not a pickle")
    monkeypatch.setattr(clone, "MODEL_FILES", {"model.pth": (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())})
    assert clone.verified_model(tmp_path)
    path.write_bytes(b"untrusted replaced checkpoint")
    assert not clone.verified_model(tmp_path)


def test_reference_is_pcm_only_and_bounded(tmp_path):
    path = tmp_path / "data.wav"
    wav(path, channels=2)
    with pytest.raises(clone.CloneSpeechUnavailable):
        clone.read_pcm(path)
    wav(path, rate=16000)
    with pytest.raises(clone.CloneSpeechUnavailable):
        clone.read_pcm(path)
    wav(path, frames=24000 * 31)
    with pytest.raises(clone.CloneSpeechUnavailable):
        clone.read_pcm(path)


@pytest.mark.parametrize("text,explicit,expected", [
    ("Открываю YouTube", None, "ru"), ("Today: задача Algorithms", None, "en"),
    ("Ready, sir", None, "en"), ("План на сегодня", None, "ru"),
    ("2026-10-28", None, "ru"), ("English with задача", "ru", "ru"),
])
def test_language_uses_letter_majority_with_explicit_override(text, explicit, expected):
    assert clone.validate_request(text, language=explicit)[2] == expected


def test_native_adapter_retains_model_when_coqui_eval_returns_none(tmp_path, monkeypatch):
    from desktop.clone_speech_worker import ModelEngine

    class Samples:
        def astype(self, _):
            return self

        def __truediv__(self, _):
            return self

        def unsqueeze(self, _):
            return self

        def to(self, _):
            return self

    class Configuration:
        def load_json(self, _):
            pass

    class Backend:
        def __init__(self, _):
            pass

        def load_checkpoint(self, _config, *, checkpoint_dir, **_kw):
            self._load_checkpoint(Path(checkpoint_dir) / "model.pth")

        def _load_checkpoint(self, _):
            pytest.fail("The upstream rewriting checkpoint loader ran")

        def to(self, _):
            return self

        def eval(self):
            return None  # The actual Coqui contract differs from nn.Module.

        def get_speaker_embedding(self, *_):
            return "speaker"

        def get_gpt_cond_latents(self, *_a, **_kw):
            return "conditioning"

    fake_numpy = SimpleNamespace(frombuffer=lambda *_a, **_kw: Samples())
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True),
                                set_num_threads=lambda _: None, from_numpy=lambda _: Samples(),
                                inference_mode=nullcontext,
                                load=lambda *_a, **kw: {"model": {}} if kw.get("weights_only") is True else pytest.fail("Unsafe pickle load"),
                                save=lambda *_a, **_kw: pytest.fail("Pinned model was overwritten"))
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "TTS.tts.configs.xtts_config", SimpleNamespace(XttsConfig=Configuration))
    monkeypatch.setitem(sys.modules, "TTS.tts.models.xtts", SimpleNamespace(Xtts=Backend, _legacy_safe_globals=nullcontext))
    monkeypatch.setattr(clone, "verified_model", lambda _: True)
    monkeypatch.setattr(clone, "read_pcm", lambda *_a, **_kw: b"xx")
    engine = ModelEngine({"model_dir": str(tmp_path), "reference_wav": str(tmp_path / "reference.wav"), "device": "cuda"}, clone)
    assert isinstance(engine.model, Backend)
    assert engine.embedding == "speaker" and engine.conditioning == "conditioning"
