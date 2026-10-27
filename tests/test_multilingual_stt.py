"""Bounded synthetic PCM tests: no capture, real models, downloads or PC calls."""
from array import array
from dataclasses import replace
import hashlib
import io
import json
import math
import sys
from types import SimpleNamespace

import pytest

from desktop import multilingual_stt as stt
from desktop import multilingual_stt_setup as setup


def tone(rate=16000, seconds=0.5, frequency=440, amplitude=5000):
    samples = array("h", (round(amplitude * math.sin(2 * math.pi * frequency * i / rate)) for i in range(round(rate * seconds))))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


@pytest.fixture
def engine(monkeypatch, tmp_path):
    calls, loads, checks = [], [], []
    result = stt._Decoded("Open the calculator.", "en", 0.99, -0.2, 0.01, 0.4)
    monkeypatch.setattr(stt, "_dependencies_ready", lambda: True)
    monkeypatch.setattr(stt, "verified_model", lambda path: checks.append(path) or True)
    monkeypatch.setattr(stt, "_load_backend", lambda path: loads.append(path) or object())
    monkeypatch.setattr(stt, "_decode", lambda backend, values, rate: calls.append((len(values), rate)) or result)
    return stt.MultilingualSTT(tmp_path), calls, loads, checks, result


def test_lazy_local_preparation_once_and_warm_calls_do_not_rehash(engine, monkeypatch):
    instance, calls, loads, checks, _ = engine
    assert not loads and not checks
    assert instance.prepare() and instance.reason == "ready" and instance.error_class is None
    monkeypatch.setattr(stt, "verified_model", lambda _: pytest.fail("warm model rehashed"))
    assert instance.prepare()
    assert instance.transcribe(tone(), 16000) == "Open the calculator."
    assert instance.last_language == "en" and instance.last_confidence == 0.99
    assert calls == [(8000, 16000)] and len(loads) == len(checks) == 1
    assert not any(name in vars(instance) for name in ("pcm", "audio", "text", "transcript", "prompt"))


@pytest.mark.parametrize("pcm,rate", [(None, 16000), (b"", 16000), (b"x", 16000),
    (bytearray(b"xx"), 16000), (tone(), True), (tone(), 7999), (tone(), 48001),
    (tone(), "16000"), (b"\x00\x00" * 192001, 16000), (b"\x00\x00" * 100, 16000)],
    ids=["none", "empty", "odd", "mutable", "bool", "low-rate", "high-rate", "string-rate", "long", "short"])
def test_input_contract_refuses_invalid_or_over_12_second_audio_before_model_load(engine, pcm, rate):
    instance, calls, loads, _, _ = engine
    with pytest.raises(stt.MultilingualSTTUnavailable, match="audio_invalid"):
        instance.transcribe(pcm, rate)
    assert not calls and not loads


@pytest.mark.parametrize("value", [0, 1, 1000])
def test_silence_and_dc_signal_never_become_hallucinated_text(engine, value):
    instance, calls, loads, _, _ = engine
    pcm = array("h", [value] * 16000).tobytes()
    with pytest.raises(stt.MultilingualSTTUnavailable, match="silence"):
        instance.transcribe(pcm, 16000)
    assert not calls and not loads


def test_one_loud_click_and_extreme_clipping_are_not_commands(engine):
    instance, calls, loads, _, _ = engine
    click = array("h", [0] * 8000)
    click[1000] = 32000
    with pytest.raises(stt.MultilingualSTTUnavailable, match="no_speech"):
        instance.transcribe(click.tobytes(), 16000)
    with pytest.raises(stt.MultilingualSTTUnavailable, match="audio_clipped"):
        instance.transcribe(tone(amplitude=32767, frequency=4000), 16000)
    assert not calls and not loads


@pytest.mark.parametrize("field,value", [("language", "fr"), ("language_probability", 0.54),
    ("language_probability", float("nan")), ("avg_logprob", -0.81), ("no_speech_probability", 0.41),
    ("no_speech_probability", -0.1), ("speech_seconds", 0.1), ("speech_seconds", 13)],
    ids=["other-language", "uncertain-language", "nan", "weak-hypothesis", "no-speech", "invalid-probability", "short-vad", "long-vad"])
def test_language_vad_and_probability_checks_refuse_uncertain_hypotheses(engine, monkeypatch, field, value):
    instance, _, _, _, result = engine
    monkeypatch.setattr(stt, "_decode", lambda *_: replace(result, **{field: value}))
    with pytest.raises(stt.MultilingualSTTUnavailable):
        instance.transcribe(tone(), 16000)
    assert instance.last_language is None and instance.last_confidence is None


@pytest.mark.parametrize("text", ["", " ", "x" * 501, "hidden\x00text", "<|startoftranscript|>", "hello " * 50],
                         ids=["empty", "blank", "long", "nul", "special-token", "repeated"])
def test_blank_special_control_or_repeated_output_not_returned(engine, monkeypatch, text):
    instance, _, _, _, result = engine
    monkeypatch.setattr(stt, "_decode", lambda *_: replace(result, text=text))
    with pytest.raises(stt.MultilingualSTTUnavailable):
        instance.transcribe(tone(), 16000)


def test_recognized_text_is_literal_and_grants_no_pc_authority(engine, monkeypatch):
    instance, _, _, _, result = engine
    monkeypatch.setattr(stt, "_decode", lambda *_: replace(result, text="  Открой   калькулятор. ", language="ru"))
    assert instance.transcribe(tone(), 16000) == "Открой калькулятор."
    assert instance.last_language == "ru"
    assert not hasattr(instance, "execute") and not hasattr(instance, "handle")


def test_missing_dependencies_or_model_and_bad_hash_never_load_native_backend(monkeypatch, tmp_path):
    instance = stt.MultilingualSTT(tmp_path / "missing")
    monkeypatch.setattr(stt, "_dependencies_ready", lambda: False)
    monkeypatch.setattr(stt, "_load_backend", lambda _: pytest.fail("unsafe load"))
    assert not instance.prepare() and instance.reason == "dependencies_missing"
    monkeypatch.setattr(stt, "_dependencies_ready", lambda: True)
    assert not instance.prepare() and instance.reason == "model_missing"
    instance.model_dir.mkdir()
    (instance.model_dir / "model.bin").write_bytes(b"unverified")
    assert not instance.prepare() and instance.reason == "model_invalid"


def test_failures_expose_fixed_label_and_exception_class_without_private_details(engine, monkeypatch):
    instance, _, _, _, _ = engine

    def failed(*_):
        raise ImportError("blocked native library private path")

    monkeypatch.setattr(stt, "_load_backend", failed)
    assert not instance.prepare()
    assert instance.reason == "backend_unavailable" and instance.error_class == "ImportError"
    monkeypatch.setattr(stt, "_load_backend", lambda _: object())
    monkeypatch.setattr(stt, "_decode", failed)
    with pytest.raises(stt.MultilingualSTTUnavailable, match="^transcription_failed$") as error:
        instance.transcribe(tone(), 16000)
    assert error.value.__cause__ is None and instance.error_class == "ImportError"


def test_native_decoder_uses_only_structural_transcribe_tokens_cpu_features_and_bounded_generation(monkeypatch):
    prompt_tokens = {"<|startoftranscript|>": 1, "<|ru|>": 2, "<|en|>": 3,
                     "<|transcribe|>": 4, "<|notimestamps|>": 5}
    calls = []
    model = SimpleNamespace(
        encode=lambda features: "encoded",
        detect_language=lambda _: [[("<|ru|>", 0.99), ("<|en|>", 0.01)]])

    def generate(features, prompts, **kwargs):
        calls.append((features, prompts, kwargs))
        return [SimpleNamespace(sequences_ids=[[10, 11]], scores=[-0.3], no_speech_prob=0.01)]

    model.generate = generate
    backend = stt._Backend(model, SimpleNamespace(token_to_id=prompt_tokens.__getitem__,
        decode=lambda tokens, skip_special_tokens: "Открой калькулятор."), "vad", "filters")
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(StorageView=SimpleNamespace(from_array=lambda value: value)))
    monkeypatch.setattr(stt, "_audio_16k", lambda *_: "waveform")
    monkeypatch.setattr(stt, "_speech_window", lambda *_: ("window", 0.4))
    monkeypatch.setattr(stt, "_features", lambda *_: "features")
    result = stt._decode(backend, [1], 16000)
    assert result.text == "Открой калькулятор." and result.language == "ru"
    assert calls == [("encoded", [[1, 2, 4, 5]], {"beam_size": 3, "max_length": 128,
        "return_scores": True, "return_no_speech_prob": True, "suppress_blank": True, "sampling_temperature": 0})]
    model.detect_language = lambda _: [[("<|fr|>", 0.99)]]
    with pytest.raises(stt.MultilingualSTTUnavailable, match="language_unsupported"):
        stt._decode(backend, [1], 16000)
    assert len(calls) == 1


def test_resampler_preserves_speech_tone_and_suppresses_downsample_aliases():
    np = pytest.importorskip("numpy", reason="Desktop runtime provides NumPy for the optional speech frontend")
    low = stt._audio_16k(stt._pcm_samples(tone(48000, frequency=1000), 48000), 48000)
    high = stt._audio_16k(stt._pcm_samples(tone(48000, frequency=12000), 48000), 48000)
    assert low.shape == high.shape == (8000,)
    assert np.isfinite(low).all() and np.sqrt(np.mean(low ** 2)) > 0.09
    assert np.sqrt(np.mean(high[100:-100] ** 2)) < np.sqrt(np.mean(low ** 2)) / 20


@pytest.fixture
def tiny_assets(monkeypatch):
    assets = {"config.json": b"{}", "model.bin": b"official fixture weights"}
    files = {name: (len(body), hashlib.sha256(body).hexdigest()) for name, body in assets.items()}
    monkeypatch.setattr(stt, "MODEL_FILES", files)
    monkeypatch.setattr(setup, "MODEL_FILES", files)
    return assets


def test_setup_uses_only_pinned_urls_and_existing_valid_assets_are_not_downloaded(tmp_path, tiny_assets, monkeypatch):
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        assert timeout == 30 and request.full_url in stt.MODEL_URLS.values()
        name = next(name for name, url in stt.MODEL_URLS.items() if url == request.full_url)
        return io.BytesIO(tiny_assets[name])

    monkeypatch.setattr(setup.urllib.request, "urlopen", download)
    assert setup.install_model(tmp_path) == tmp_path.resolve()
    assert len(requests) == 2 and stt.verified_model(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["network_during_transcription"] is False and manifest["pyav_required"] is False
    (tmp_path / "manifest.json").unlink()
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("unnecessary network"))
    setup.install_model(tmp_path)
    assert (tmp_path / "manifest.json").is_file()


def test_setup_bad_checksum_preserves_existing_asset_and_removes_partial(tmp_path, tiny_assets, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b"existing")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"NO"))
    with pytest.raises(ValueError, match="checksum"):
        setup.install_model(tmp_path)
    assert path.read_bytes() == b"existing" and not list(tmp_path.glob("*.partial"))
