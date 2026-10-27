"""Offline speech tests: fake ONNX backend, no microphone/speaker or downloads."""
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from desktop import neural_speech as neural
from desktop import neural_speech_setup as setup


@pytest.fixture
def fake_voice(tmp_path, monkeypatch):
    files = {neural.MODEL_NAME + ".onnx": b"synthetic model",
             neural.MODEL_NAME + ".onnx.json": b"{}", "MODEL_CARD": b"CC0 synthetic card"}
    pins = {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in files.items()}
    monkeypatch.setattr(neural, "MODEL_FILES", pins)
    monkeypatch.setattr(setup, "MODEL_FILES", pins)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    monkeypatch.setattr(neural, "_dependencies_ready", lambda: True)
    records = {"loads": 0, "texts": [], "settings": []}
    class Backend:
        def synthesize(self, text, syn_config):
            records["texts"].append(text)
            yield SimpleNamespace(sample_width=2, sample_channels=1, sample_rate=22050,
                                  audio_int16_bytes=b"\0\0" * 50)
    backend = Backend()
    def config(**kwargs):
        records["settings"].append(kwargs)
        return kwargs
    def load(path):
        assert path == tmp_path
        records["loads"] += 1
        return backend, config
    monkeypatch.setattr(neural, "_load_backend", load)
    return neural.NeuralSpeech(tmp_path), records, backend, files


def test_no_model_has_a_safe_fallback_without_native_loading_or_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(neural, "_dependencies_ready", lambda: True)
    monkeypatch.setattr(neural, "_load_backend", lambda *_: pytest.fail("Missing model loaded"))
    engine = neural.NeuralSpeech(tmp_path / "absent")
    assert not engine.available() and engine.reason == "model_missing"
    assert engine.prepare() is False
    with pytest.raises(neural.NeuralSpeechUnavailable, match="model_missing"):
        engine.synthesize("Да, сэр")
    assert list(tmp_path.iterdir()) == []


def test_missing_dependencies_do_not_import_or_download_them(tmp_path, monkeypatch):
    monkeypatch.setattr(neural, "_dependencies_ready", lambda: False)
    engine = neural.NeuralSpeech(tmp_path)
    assert not engine.prepare() and engine.reason == "dependencies_missing"


def test_modified_model_is_rejected_before_backend_load(fake_voice):
    engine, records, _, _ = fake_voice
    (engine.model_dir / (neural.MODEL_NAME + ".onnx")).write_bytes(b"modified model!")
    assert engine.prepare() is False and engine.reason == "model_invalid"
    assert records["loads"] == 0


def test_prepare_loads_once_and_wake_cache_is_immediate_and_never_rehashes(fake_voice, monkeypatch):
    engine, records, _, _ = fake_voice
    assert records["loads"] == 0
    assert engine.prepare(195) and engine.reason == "ready"
    assert records["loads"] == 1 and records["texts"] == [neural.ACKNOWLEDGMENT]
    monkeypatch.setattr(neural, "verified_model", lambda *_: pytest.fail("Loaded model was rehashed"))
    cached = engine.synthesize("Да, сэр", 195)
    assert cached == (b"\0\0" * 50, 22050)
    assert engine.prepare(195) and engine.synthesize("Да, сэр!", 195) == cached
    assert records["texts"] == [neural.ACKNOWLEDGMENT]
    assert engine.prepare(240) and records["loads"] == 1
    assert records["settings"][-1]["length_scale"] == 165 / 240


def test_only_public_fixed_acknowledgment_is_cached_and_cache_is_bounded(fake_voice):
    engine, records, _, _ = fake_voice
    assert engine.prepare()
    for _ in range(2):
        engine.synthesize("A private note must not be cached", 195)
    assert records["texts"].count("A private note must not be cached") == 2
    for rate in range(165, 185):
        engine.synthesize("Да, сэр", rate)
    assert len(engine._ack_cache) == 8
    assert all(isinstance(rate, int) for rate in engine._ack_cache)


@pytest.mark.parametrize("text", ["", " ", None, "x" * 701, "a\0b", "<speak>run</speak>"])
def test_invalid_or_ssml_input_does_not_reach_the_backend(fake_voice, text):
    engine, records, _, _ = fake_voice
    with pytest.raises(neural.NeuralSpeechUnavailable, match="text_invalid"):
        engine.synthesize(text)
    assert records["loads"] == 0


@pytest.mark.parametrize("rate", [True, 89, 241, "195", 195.5])
def test_invalid_speed_does_not_reach_the_backend(fake_voice, rate):
    engine, records, _, _ = fake_voice
    with pytest.raises(neural.NeuralSpeechUnavailable, match="rate_invalid"):
        engine.synthesize("Да, сэр", rate)
    assert records["loads"] == 0


@pytest.mark.parametrize("rate", [90, 165, 240])
def test_full_ui_speed_range_is_supported_without_reloading(fake_voice, rate):
    engine, records, _, _ = fake_voice
    assert engine.prepare(rate)
    assert records["settings"][-1]["length_scale"] == 165 / rate
    assert engine.prepare(rate) and records["loads"] == 1


def test_invalid_prepare_speed_never_drops_a_loaded_backend(fake_voice):
    engine, records, _, _ = fake_voice
    assert engine.prepare(185)
    assert engine.prepare(89) is False
    assert engine.prepare(185) and records["loads"] == 1


def test_backend_errors_are_fixed_labels_without_private_diagnostics(fake_voice, monkeypatch):
    engine, _, backend, _ = fake_voice
    assert engine.prepare()
    def failed(*args, **kwargs):
        raise RuntimeError("private text, user directory and credential must not be exposed")
    monkeypatch.setattr(backend, "synthesize", failed)
    with pytest.raises(neural.NeuralSpeechUnavailable) as result:
        engine.synthesize("A new sentence")
    assert str(result.value) == "synthesis_failed" and engine.error_class == "RuntimeError"


@pytest.mark.parametrize("changes", [{"sample_width": 4}, {"sample_channels": 2},
    {"sample_rate": 8000}, {"audio_int16_bytes": b"x"}, {"audio_int16_bytes": "not PCM"}])
def test_invalid_audio_is_not_returned_to_the_speaker(fake_voice, monkeypatch, changes):
    engine, _, backend, _ = fake_voice
    assert engine.prepare()
    chunk = dict(sample_width=2, sample_channels=1, sample_rate=22050, audio_int16_bytes=b"\0\0")
    chunk.update(changes)
    monkeypatch.setattr(backend, "synthesize", lambda *a, **k: iter([SimpleNamespace(**chunk)]))
    with pytest.raises(neural.NeuralSpeechUnavailable, match="audio_invalid"):
        engine.synthesize("Different sentence")


def test_empty_and_oversized_audio_fail_closed(fake_voice, monkeypatch):
    engine, _, backend, _ = fake_voice
    assert engine.prepare()
    monkeypatch.setattr(backend, "synthesize", lambda *a, **k: iter([]))
    with pytest.raises(neural.NeuralSpeechUnavailable, match="audio_empty"):
        engine.synthesize("Different sentence")
    huge = SimpleNamespace(sample_width=2, sample_channels=1, sample_rate=22050,
                           audio_int16_bytes=b"\0\0" * (48000 * 60 + 1))
    monkeypatch.setattr(backend, "synthesize", lambda *a, **k: iter([huge]))
    with pytest.raises(neural.NeuralSpeechUnavailable, match="audio_limit"):
        engine.synthesize("Different sentence")


def test_setup_downloads_only_revision_pinned_assets_and_writes_public_manifest(fake_voice, tmp_path, monkeypatch):
    _, _, _, files = fake_voice
    target = tmp_path / "installed"
    requests = []
    def download(request, timeout):
        requests.append(request.full_url)
        assert timeout == 30 and request.full_url.startswith(neural.MODEL_BASE_URL)
        return io.BytesIO(files[request.full_url.rsplit("/", 1)[1]])
    monkeypatch.setattr(setup.urllib.request, "urlopen", download)
    assert setup.install_model(target) == target
    assert neural.verified_model(target) and len(requests) == 3
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["revision"] == neural.MODEL_REVISION and "GPL-3.0" in manifest["engine"]
    assert not list(target.glob("*.partial"))


def test_setup_checksum_failure_preserves_existing_files(fake_voice, tmp_path, monkeypatch):
    _, _, _, _ = fake_voice
    target = tmp_path / "broken-download"
    target.mkdir()
    existing = target / (neural.MODEL_NAME + ".onnx")
    existing.write_bytes(b"existing but incomplete")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"wrong"))
    with pytest.raises(ValueError, match="pinned checksum"):
        setup.install_model(target)
    assert existing.read_bytes() == b"existing but incomplete"
    assert not list(target.glob("*.partial"))
