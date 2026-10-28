"""Explicit RU/EN context preserves acoustic uncertainty and raw probabilities."""
from array import array
from dataclasses import replace
import math
import sys
from types import SimpleNamespace

import pytest

from desktop import multilingual_stt as stt


def pcm():
    values = array("h", (round(5000 * math.sin(index / 9)) for index in range(8000)))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


@pytest.fixture
def decoding(monkeypatch, tmp_path):
    probabilities = [["<|ru|>", .43], ["<|de|>", .32], ["<|en|>", .25]]
    output = SimpleNamespace(sequences_ids=[[10, 11]], scores=[-.3], no_speech_prob=.01)
    calls, detected = [], []
    tokens = {"<|startoftranscript|>": 1, "<|ru|>": 2, "<|en|>": 3,
              "<|transcribe|>": 4, "<|notimestamps|>": 5}

    def detect(encoded):
        detected.append(encoded)
        return [probabilities]

    def generate(encoded, prompts, **options):
        calls.append((encoded, prompts, options))
        return [output]

    model = SimpleNamespace(encode=lambda _: "encoded-original-audio", detect_language=detect, generate=generate)
    tokenizer = SimpleNamespace(token_to_id=tokens.__getitem__, decode=lambda *_args, **_kwargs: "Открой калькулятор.")
    backend = stt._Backend(model, tokenizer, None, None)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(StorageView=SimpleNamespace(from_array=lambda value: value)))
    monkeypatch.setattr(stt, "_audio_16k", lambda values, rate: values)
    monkeypatch.setattr(stt, "_speech_window", lambda _vad, waveform: (waveform, .4))
    monkeypatch.setattr(stt, "_features", lambda waveform, _: waveform)

    def engine(preferred=None):
        instance = stt.MultilingualSTT(tmp_path, preferred_language=preferred)
        instance._backend = backend
        return instance

    return SimpleNamespace(probabilities=probabilities, calls=calls, detected=detected, output=output,
                           engine=engine, backend=backend)


def test_explicit_russian_context_resolves_language_only_uncertainty_without_fabricating_global_confidence(decoding):
    automatic = decoding.engine()
    with pytest.raises(stt.MultilingualSTTUnavailable, match="^low_confidence$"):
        automatic.transcribe(pcm(), 16000)
    assert not decoding.calls
    preferred = decoding.engine("ru")
    assert preferred.transcribe(pcm(), 16000) == "Открой калькулятор."
    assert preferred.last_language == "ru" and preferred.last_confidence == .43
    assert preferred.last_supported_confidence == pytest.approx(.68)
    assert preferred.last_conditional_confidence == pytest.approx(.43 / .68)
    assert preferred.last_language_selection == "ru_en_preference"
    assert len(decoding.calls) == 1 and decoding.calls[0][1] == [[1, 2, 4, 5]]
    assert not any(key in vars(preferred) for key in ("audio", "pcm", "text", "transcript", "history"))


def test_explicit_english_context_uses_the_same_bounded_rule_and_one_generation(decoding):
    decoding.probabilities[:] = [["<|en|>", .43], ["<|de|>", .32], ["<|ru|>", .25]]
    preferred = decoding.engine("en")
    preferred.transcribe(pcm(), 16000)
    assert preferred.last_language == "en" and preferred.last_confidence == .43
    assert len(decoding.calls) == 1 and decoding.calls[0][1] == [[1, 3, 4, 5]]


def test_confident_english_stays_english_despite_russian_preference(decoding):
    decoding.probabilities[:] = [["<|en|>", .80], ["<|ru|>", .12], ["<|de|>", .08]]
    preferred = decoding.engine("ru")
    preferred.transcribe(pcm(), 16000)
    assert preferred.last_language == "en" and preferred.last_confidence == .80
    assert preferred.last_language_selection == "auto" and preferred.last_supported_confidence is None
    assert decoding.calls[0][1] == [[1, 3, 4, 5]]


@pytest.mark.parametrize("probabilities", [
    [("<|de|>", .85), ("<|ru|>", .10), ("<|en|>", .05)],
    [("<|de|>", .46), ("<|ru|>", .40), ("<|en|>", .14)],
    [("<|de|>", .4), ("<|ru|>", .3), ("<|en|>", .3)],
    [("<|en|>", .5), ("<|ru|>", .2), ("<|de|>", .3)],
    [("<|de|>", .6), ("<|en|>", .4)],
])
def test_context_never_renormalizes_foreign_dominance_or_ambiguous_preference_into_a_command(decoding, probabilities):
    decoding.probabilities[:] = probabilities
    preferred = decoding.engine("ru")
    with pytest.raises(stt.MultilingualSTTUnavailable):
        preferred.transcribe(pcm(), 16000)
    assert not decoding.calls and preferred.last_language is None and preferred.last_confidence is None
    assert preferred.last_supported_confidence is None and preferred.last_language_selection is None


@pytest.mark.parametrize("probabilities", [[], [("<|ru|>", True)], [("<|ru|>", math.nan)],
    [("<|ru|>", math.inf)], [("<|ru|>", -.1)], [("<|ru|>", 1.1)],
    [("<|ru|>", .4), ("<|ru|>", .3)], [("<|ru|>", .7), ("<|en|>", .7)], [("<|ru|>",)],
    [["<|ru|>", "PRIVATE invalid probability"]], [("PRIVATE invalid token", .9)], [("<|RU|>", .9)]])
def test_malformed_language_metadata_fails_closed_before_generation(decoding, probabilities):
    decoding.probabilities[:] = probabilities
    with pytest.raises(stt.MultilingualSTTUnavailable, match="^low_confidence$"):
        decoding.engine("ru").transcribe(pcm(), 16000)
    assert not decoding.calls


@pytest.mark.parametrize("preference", [True, 1, "fr", "RU", "Russian", "ru; open calculator", ["ru"]])
def test_language_preference_is_an_exact_structural_option_only(preference):
    with pytest.raises(ValueError, match="^Preferred language must be ru, en or None$"):
        stt.MultilingualSTT(preferred_language=preference)


@pytest.mark.parametrize("field,value", [("scores", [-1.3]), ("no_speech_prob", .41), ("no_speech_prob", math.nan)])
def test_language_context_does_not_weaken_acoustic_hypothesis_or_no_speech_gates(decoding, field, value):
    setattr(decoding.output, field, value)
    preferred = decoding.engine("ru")
    with pytest.raises(stt.MultilingualSTTUnavailable, match="^low_confidence$"):
        preferred.transcribe(pcm(), 16000)
    assert len(decoding.calls) == 1
    assert preferred.last_language is None and preferred.last_supported_confidence is None


def test_silence_is_rejected_before_language_context_or_native_model_work(decoding):
    preferred = decoding.engine("ru")
    with pytest.raises(stt.MultilingualSTTUnavailable, match="^silence$"):
        preferred.transcribe(b"\0\0" * 8000, 16000)
    assert not decoding.detected and not decoding.calls


@pytest.mark.parametrize("changes", [
    {"supported_language_probability": .2}, {"supported_language_probability": math.nan},
    {"supported_language_probability": True}, {"supported_language_probability": 1.1},
    {"language": "en"}, {"language_selection": "UNTRUSTED"},
])
def test_second_boundary_validates_context_metadata_instead_of_trusting_decoder_claims(decoding, monkeypatch, changes):
    result = stt._Decoded("Открой калькулятор.", "ru", .43, -.2, .01, .4, .68, "ru_en_preference")
    monkeypatch.setattr(stt, "_decode", lambda *_args, **_kwargs: replace(result, **changes))
    with pytest.raises(stt.MultilingualSTTUnavailable, match="^low_confidence$"):
        decoding.engine("ru").transcribe(pcm(), 16000)


def test_success_metadata_clears_on_the_next_failed_audio_request(decoding):
    preferred = decoding.engine("ru")
    preferred.transcribe(pcm(), 16000)
    assert preferred.last_supported_confidence is not None
    with pytest.raises(stt.MultilingualSTTUnavailable):
        preferred.transcribe(b"", 16000)
    assert preferred.last_language is preferred.last_confidence is None
    assert preferred.last_language_selection is preferred.last_supported_confidence is None
