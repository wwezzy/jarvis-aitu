"""Deterministic speech/model safety checks; no microphone, playback or download."""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from desktop import expressive_speech as speech
from desktop import expressive_speech_setup as setup


@pytest.fixture
def engine(monkeypatch, tmp_path):
    calls, loads, checks = [], [], []
    backend = object()
    monkeypatch.setattr(speech, "_dependencies_ready", lambda: True)
    monkeypatch.setattr(speech, "verified_model", lambda path: checks.append(path) or True)
    monkeypatch.setattr(speech, "_load_backend", lambda path: loads.append(path) or backend)
    monkeypatch.setattr(speech, "_render", lambda model, text, voice, tier:
                        calls.append((model, text, voice, tier)) or (b"\x01\x00" * 20, 24000))
    instance = speech.ExpressiveSpeech(tmp_path, personal_use=True)
    return instance, calls, loads, checks


def test_default_is_personal_opt_in_and_does_not_load_or_download(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "_dependencies_ready", lambda: pytest.fail("no dependency inspection before consent"))
    instance = speech.ExpressiveSpeech(tmp_path)
    assert not instance.available() and not instance.prepare()
    assert instance.reason == "personal_use_required" and instance.error_class is None
    assert not list(tmp_path.iterdir())
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="personal_use_required"):
        instance.synthesize("Проверка")


def test_lazy_prepare_warms_one_ack_and_reuses_model_and_hash(engine, monkeypatch):
    instance, calls, loads, checks = engine
    assert not loads and not calls and not checks
    assert instance.prepare(185)
    assert instance.reason == "ready" and instance.error_class is None
    assert calls[0][1:] == ("Да, сэр", "aidar", "fast")
    monkeypatch.setattr(speech, "verified_model", lambda _: pytest.fail("warm preparation must not hash again"))
    for _ in range(4):
        assert instance.prepare(195)
        assert instance.synthesize("да сэр!", 185) == (b"\x01\x00" * 20, 24000)
    assert len(loads) == len(checks) == len(calls) == 1


def test_speaker_cache_is_distinct_and_personal_responses_never_cached(engine):
    instance, calls, _, _ = engine
    instance.synthesize("Да, сэр", voice="aidar")
    instance.synthesize("Да, сэр", voice="eugene")
    instance.synthesize("Да, сэр", voice="eugene")
    instance.synthesize("Моя личная заметка")
    instance.synthesize("Моя личная заметка")
    assert len(calls) == 4 and len(instance._ack_cache) == 2
    assert not any("заметка" in str(key) for key in instance._ack_cache)
    for voice in speech.VOICES:
        for rate in range(90, 241):
            instance.synthesize("Да, сэр", rate, voice)
    assert len(instance._ack_cache) == 10


def test_personal_gate_cannot_be_bypassed_by_cached_ack(engine):
    instance, calls, _, _ = engine
    instance.synthesize("Да, сэр")
    instance.personal_use = False
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="personal_use_required"):
        instance.synthesize("Да, сэр")
    assert len(calls) == 1


@pytest.mark.parametrize("rate,tier", [(90, "x-slow"), (119, "x-slow"), (120, "slow"),
                                      (159, "slow"), (160, "medium"), (179, "medium"),
                                      (180, "fast"), (185, "fast"), (195, "fast"),
                                      (225, "x-fast"), (240, "x-fast")])
def test_full_slider_range_uses_native_fixed_speed_tiers(engine, rate, tier):
    instance, calls, _, _ = engine
    assert instance.prepare(rate)
    assert calls[-1][-1] == tier


@pytest.mark.parametrize("rate", [True, False, 89, 241, 185.0, "185", None])
def test_invalid_rate_refused_without_losing_warm_backend(engine, rate):
    instance, calls, loads, _ = engine
    assert instance.prepare()
    original = instance._model
    assert not instance.prepare(rate) and instance.reason == "rate_invalid"
    assert instance._model is original
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="rate_invalid"):
        instance.synthesize("Текст", rate)
    assert len(loads) == len(calls) == 1


@pytest.mark.parametrize("text", [None, "", "  ", "x" * 701, "bad\x00text", '<voice name="another">Текст</voice>'],
                         ids=["none", "empty", "blank", "long", "nul", "ssml"])
def test_invalid_text_does_not_load_backend(engine, text):
    instance, calls, loads, _ = engine
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="text_invalid"):
        instance.synthesize(text)
    assert not loads and not calls


@pytest.mark.parametrize("voice", ["dmitri", "random", "http://example.com", "Aidar", ""])
def test_only_two_fixed_voices_accepted(engine, voice):
    instance, calls, loads, _ = engine
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="voice_invalid"):
        instance.synthesize("Текст", voice=voice)
    assert not loads and not calls


def test_missing_dependencies_missing_model_and_bad_checksum_never_unpickle(monkeypatch, tmp_path):
    instance = speech.ExpressiveSpeech(tmp_path / "absent", personal_use=True)
    monkeypatch.setattr(speech, "_dependencies_ready", lambda: False)
    monkeypatch.setattr(speech, "_load_backend", lambda _: pytest.fail("untrusted model was loaded"))
    assert not instance.prepare() and instance.reason == "dependencies_missing"
    monkeypatch.setattr(speech, "_dependencies_ready", lambda: True)
    assert not instance.prepare() and instance.reason == "model_missing"
    instance.model_dir.mkdir()
    (instance.model_dir / speech.MODEL_NAME).write_bytes(b"untrusted pickle")
    assert not instance.prepare() and instance.reason == "model_invalid"


def test_failed_load_and_render_expose_only_fixed_labels(engine, monkeypatch):
    instance, _, _, _ = engine

    def failure(*args):
        raise RuntimeError("private utterance / private path")

    monkeypatch.setattr(speech, "_load_backend", failure)
    assert not instance.prepare() and instance.reason == "backend_unavailable"
    assert instance.error_class == "RuntimeError"
    assert instance._model is None and not instance._ack_cache
    monkeypatch.setattr(speech, "_load_backend", lambda _: object())
    monkeypatch.setattr(speech, "_render", failure)
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="^synthesis_failed$") as error:
        instance.synthesize("private utterance")
    assert error.value.__cause__ is None and instance.error_class == "RuntimeError"


@pytest.mark.parametrize("pcm,rate", [(b"", 24000), (b"x", 24000), (bytearray(b"xx"), 24000),
                                    (b"xx", 48000), (b"xx" * (24000 * 60 + 1), 24000)],
                         ids=["empty", "odd", "mutable", "rate", "oversized"])
def test_invalid_pcm_is_rejected_before_playback(engine, monkeypatch, pcm, rate):
    instance, _, _, _ = engine
    monkeypatch.setattr(speech, "_render", lambda *_: (pcm, rate))
    with pytest.raises(speech.ExpressiveSpeechUnavailable, match="^audio_invalid$"):
        instance.synthesize("Проверка")


def test_native_render_escapes_user_text_without_pitch_or_external_ssml(monkeypatch):
    arguments, events = [], []

    class Tensor:
        ndim = 1

        def numel(self):
            return 3

        def __getattr__(self, name):
            if name == "numpy":
                return lambda: SimpleNamespace(astype=lambda encoding: SimpleNamespace(tobytes=lambda: b"\x00\x01" * 3))
            return lambda *_: self

    @contextmanager
    def inference():
        events.append("inference")
        yield

    torch = SimpleNamespace(Tensor=Tensor, int16="int16", inference_mode=inference,
                            isfinite=lambda _: SimpleNamespace(all=lambda: SimpleNamespace(item=lambda: True)))
    monkeypatch.setitem(sys.modules, "torch", torch)
    model = SimpleNamespace(apply_tts=lambda **kwargs: arguments.append(kwargs) or Tensor())
    assert speech._render(model, 'Два < трёх & "текст"', "eugene", "fast") == (b"\x00\x01" * 3, 24000)
    assert arguments == [{"ssml_text": '<speak><prosody rate="fast">Два &lt; трёх &amp; &quot;текст&quot;</prosody></speak>',
                          "speaker": "eugene", "sample_rate": 24000, "put_accent": True, "put_yo": True}]
    assert events == ["inference"]


@pytest.fixture
def tiny_assets(monkeypatch):
    bodies = {"v5_6_ru.pt": b"official model fixture", "LICENSE_SILERO.txt": b"noncommercial fixture"}
    files = {name: (len(body), hashlib.sha256(body).hexdigest()) for name, body in bodies.items()}
    monkeypatch.setattr(speech, "MODEL_FILES", files)
    monkeypatch.setattr(setup, "MODEL_FILES", files)
    return bodies


def test_setup_requires_personal_opt_in_before_creating_anything(tmp_path, monkeypatch):
    destination = tmp_path / "model"
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("unapproved download"))
    with pytest.raises(ValueError, match="personal noncommercial"):
        setup.install_model(destination)
    assert not destination.exists()


def test_setup_downloads_only_pinned_assets_with_notice_and_writes_nc_manifest(tmp_path, monkeypatch, tiny_assets):
    requests, progress = [], []

    def download(request, timeout):
        requests.append((request.full_url, timeout))
        name = next(name for name, url in speech.MODEL_URLS.items() if request.full_url == url)
        return io.BytesIO(tiny_assets[name])

    monkeypatch.setattr(setup.urllib.request, "urlopen", download)
    assert setup.install_model(tmp_path, progress.append, personal_use=True) == tmp_path.resolve()
    assert len(requests) == 2 and all(timeout == 30 for _, timeout in requests)
    assert all("некоммерческого" in message for message in progress)
    assert speech.verified_model(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["personal_noncommercial_only"] is True
    assert manifest["commercial_distribution"] is False and manifest["license"] == "CC-BY-NC-SA-4.0"
    assert "no upstream checksum" in manifest["checksum_provenance"]
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("valid model downloaded again"))
    (tmp_path / "manifest.json").unlink()
    setup.install_model(tmp_path, personal_use=True)
    assert (tmp_path / "manifest.json").is_file()


@pytest.mark.parametrize("body", [b"incorrect same size!!", b"x" * 200])
def test_bad_download_preserves_existing_file_and_cleans_temporary(tmp_path, monkeypatch, tiny_assets, body):
    original = tmp_path / speech.MODEL_NAME
    original.write_bytes(b"existing asset")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(body))
    with pytest.raises(ValueError, match="pinned"):
        setup.install_model(tmp_path, personal_use=True)
    assert original.read_bytes() == b"existing asset"
    assert not list(tmp_path.glob("*.partial")) and not (tmp_path / "manifest.json").exists()


def test_verified_model_rejects_asset_tampering_before_unpickle(tmp_path, tiny_assets):
    for name, body in tiny_assets.items():
        (tmp_path / name).write_bytes(body)
    assert speech.verified_model(tmp_path)
    (tmp_path / speech.MODEL_NAME).write_bytes(b"x" * len(tiny_assets[speech.MODEL_NAME]))
    assert not speech.verified_model(tmp_path)


def test_model_cache_location_is_explicit_public_data_outside_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert speech.default_model_dir() == tmp_path / "JarvisData/publicmodels/silero-v5_6-ru"
