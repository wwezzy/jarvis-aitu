"""The installed STT facade must preserve safe failure labels, not private errors."""
from types import SimpleNamespace

import pytest

from desktop.ai_routing import FallbackTranscription
from desktop.cloud_ai import CloudUnavailable


PCM, RATE = b"00" * 8000, 16000


class LocalDecoder:
    def __init__(self, reason="low_confidence", ready=True):
        self.reason, self.ready = reason, ready
        self.result = None
        self.last_language, self.last_confidence = "ru", 0.98
        self.prepared = self.calls = 0

    def prepare(self):
        self.prepared += 1
        return self.ready

    def transcribe(self, _pcm, _rate):
        self.calls += 1
        if self.result is None:
            raise RuntimeError("PRIVATE transcript /private/recording.wav")
        return self.result


def facade(tmp_path, local):
    def forbidden(**_kwargs):
        pytest.fail("Local ASR diagnostics must not construct a cloud client")
    return FallbackTranscription(tmp_path, environ={}, local_factory=lambda: local, cloud_factory=forbidden)


@pytest.mark.parametrize("reason", [
    "audio_invalid", "silence", "no_speech", "audio_clipped", "vad_invalid",
    "language_unsupported", "low_confidence", "transcript_limit", "transcript_invalid",
    "repetition_rejected", "transcription_failed",
])
@pytest.mark.parametrize("method", ["transcribe", "transcribe_local"])
def test_decode_rejection_retains_only_exact_safe_label(tmp_path, reason, method):
    value = facade(tmp_path, LocalDecoder(reason))
    assert value.prepare()
    with pytest.raises(CloudUnavailable) as raised:
        getattr(value, method)(PCM, RATE)
    assert raised.value.reason == value.reason == reason
    assert value.last_error_stage == value.error_stage == "local_decode"
    assert value.last_error_provider == "local"
    assert value.last_provider is value.last_language is value.last_confidence is None
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("reason", ["dependencies_missing", "model_missing", "model_invalid", "backend_unavailable"])
def test_failed_prepare_preserves_safe_local_reason_and_stage(tmp_path, reason):
    local = LocalDecoder(reason, ready=False)
    value = facade(tmp_path, local)
    assert value.prepare() is False
    assert value.reason == reason
    assert value.error_stage == "local_prepare" and value.last_error_provider == "local"
    with pytest.raises(CloudUnavailable) as raised:
        value.transcribe(PCM, RATE)
    assert raised.value.reason == reason
    assert value.error_stage == "local_prepare" and local.calls == 0


@pytest.mark.parametrize("reason", [None, "PRIVATE transcript /secret/file", {}, ["silence"], "ready"])
def test_arbitrary_local_metadata_cannot_escape_diagnostics(tmp_path, reason):
    value = facade(tmp_path, LocalDecoder(reason))
    with pytest.raises(CloudUnavailable) as raised:
        value.transcribe_local(PCM, RATE)
    assert value.reason == raised.value.reason == "local_unavailable"
    assert value.error_stage == "local_decode" and value.last_error_provider == "local"


def test_later_success_clears_failure_without_marking_previous_failure_successful(tmp_path):
    local = LocalDecoder("silence")
    value = facade(tmp_path, local)
    with pytest.raises(CloudUnavailable):
        value.transcribe(PCM, RATE)
    assert value.last_provider is None
    local.result = "Доброе утро."
    assert value.transcribe(PCM, RATE) == "Доброе утро."
    assert value.reason == "ready" and value.last_provider == "local"
    assert value.last_error_stage is value.error_stage is value.last_error_provider is None
    assert value.last_language == "ru" and value.last_confidence == 0.98
    with pytest.raises(AttributeError):
        value.error_stage = "arbitrary"


def test_prepare_factory_exception_exposes_no_private_text(tmp_path):
    def unavailable():
        raise RuntimeError("PRIVATE model directory")
    value = FallbackTranscription(tmp_path, environ={}, local_factory=unavailable)
    assert value.prepare() is False
    assert value.reason == "local_unavailable"
    assert value.last_error_stage == "local_prepare" and value.last_error_provider == "local"


def test_real_decoder_prepare_is_idempotent_through_facade(tmp_path, monkeypatch):
    from desktop import multilingual_stt

    loads = []
    backend = object()
    local = multilingual_stt.MultilingualSTT(tmp_path)
    monkeypatch.setattr(local, "available", lambda: True)
    monkeypatch.setattr(multilingual_stt, "_load_backend", lambda path: loads.append(path) or backend)
    monkeypatch.setattr(multilingual_stt, "_pcm_samples", lambda _pcm, _rate: [])
    monkeypatch.setattr(multilingual_stt, "_decode", lambda actual, _samples, _rate: (
        SimpleNamespace(text="Проверка связи.", language="ru", language_probability=0.98,
                        avg_logprob=-0.1, no_speech_probability=0.01, speech_seconds=1.0)
        if actual is backend else pytest.fail("The cached CPU backend changed")))
    value = facade(tmp_path, local)
    assert value.prepare()
    for _ in range(3):
        assert value.transcribe(PCM, RATE) == "Проверка связи."
    assert loads == [tmp_path]


@pytest.mark.parametrize("bad_result, reason", [(None, "local_unavailable"), ("", "transcript_invalid"),
                                               ("a" * 501, "transcript_limit"), ("<|special|>", "transcript_limit")])
def test_wrapper_validation_reports_decode_stage_without_success(tmp_path, bad_result, reason):
    local = LocalDecoder("ready")
    local.result = bad_result
    value = facade(tmp_path, local)
    with pytest.raises(CloudUnavailable) as raised:
        value.transcribe_local(PCM, RATE)
    assert raised.value.reason == value.reason == reason
    assert value.error_stage == "local_decode" and value.last_error_provider == "local"
    assert value.last_provider is None


def test_cloud_failure_then_local_success_has_no_stale_error_metadata(tmp_path):
    class CloudDecoder:
        def prepare(self):
            return True

        def transcribe(self, *_args):
            raise CloudUnavailable("timeout")

    local = LocalDecoder()
    local.result = "Я на связи."
    value = FallbackTranscription(tmp_path, environ={
        "JARVIS_CLOUD_STT_ENABLED": "1", "OPENAI_API_KEY": "sk-" + "x" * 30},
        local_factory=lambda: local, cloud_factory=lambda **_kwargs: CloudDecoder())
    assert value.transcribe(PCM, RATE) == "Я на связи."
    assert value.last_provider == "local" and value.reason == "ready"
    assert value.last_error_stage is value.last_error_provider is None
