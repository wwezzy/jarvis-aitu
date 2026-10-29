"""Optional Gemini/accepted-clone handoff, never real helpers/audio/providers."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from desktop.gemini_ai import GeminiCancelled, GeminiUnavailable
from desktop.speech_routing import GeminiLocalSpeech
from desktop.voice import VoiceService

GOOGLE_PCM = (b"\1\0" * 80, 24000)
CLONE_PCM = (b"\2\0" * 80, 24000)
ACK_PCM = (b"\3\0" * 80, 24000)


class Google:
    last_usage = {"input_tokens": 20, "output_tokens": 5}

    def __init__(self):
        self.ready, self.reason = True, "ready"
        self.calls, self.closed, self.canceled, self.prepared = [], 0, 0, 0
        self.failure, self.partial = None, False

    def prepare(self, *, rate):
        self.prepared += 1
        return self.ready

    def stream(self, text, **options):
        self.calls.append((text, options))
        if self.partial:
            yield GOOGLE_PCM
        if self.failure:
            raise self.failure
        if not self.partial:
            yield GOOGLE_PCM

    def cancel(self):
        self.canceled += 1

    def close(self):
        self.closed += 1


class Clone:
    def __init__(self, **_kwargs):
        self.is_ready, self.is_warming = True, False
        self.warmed, self.closed, self.calls = [], 0, []
        self.ack, self.failure, self.empty = ACK_PCM, None, False

    def prepare_background(self, *, rate):
        assert not self.closed
        self.warmed.append(rate)

    def fixed_ack(self, language):
        assert language in {"ru", "en"}
        return self.ack

    def stream(self, text, **options):
        self.calls.append((text, options))
        if self.failure:
            raise self.failure
        if not self.empty:
            yield CLONE_PCM

    def close(self):
        self.closed += 1


@pytest.fixture
def route():
    google, clone = Google(), Clone()
    router = GeminiLocalSpeech(gemini_factory=lambda: google, clone_factory=lambda: clone)
    yield SimpleNamespace(router=router, google=google, clone=clone)
    router.close()


def test_constructor_is_lazy_and_prepare_only_warms_owned_clone_without_speech_requests(route):
    assert route.router._clone is route.router._gemini is None
    assert route.router.prepare(rate=195)
    assert route.clone.warmed == [195] and route.google.prepared == 1
    assert not route.clone.calls and not route.google.calls


def test_google_success_does_not_run_clone_synthesis(route):
    assert list(route.router.stream("Public phrase", rate=200)) == [GOOGLE_PCM]
    assert len(route.google.calls) == 1 and not route.clone.calls
    assert route.router.backend == "gemini" and route.router.last_usage == route.google.last_usage


@pytest.mark.parametrize("reason", ["quota_exhausted", "network_unavailable", "authentication", "not_configured"])
def test_one_pre_pcm_provider_failure_falls_back_to_accepted_clone_only(route, reason):
    route.google.failure = GeminiUnavailable(reason)
    assert list(route.router.stream("Public phrase", rate=195)) == [CLONE_PCM]
    assert len(route.google.calls) == len(route.clone.calls) == 1
    assert route.router.backend == "clone_fallback" and route.router.last_usage is None


def test_failed_prepare_uses_clone_without_making_a_google_request(route):
    route.google.ready, route.google.reason = False, "quota_exhausted"
    assert route.router.prepare()
    assert list(route.router.stream("Public phrase")) == [CLONE_PCM]
    assert not route.google.calls and route.router.backend == "clone_fallback"


def test_unavailable_google_factory_still_allows_local_pre_pcm_fallback():
    clone = Clone()
    def unavailable():
        raise GeminiUnavailable("not_configured")
    router = GeminiLocalSpeech(gemini_factory=unavailable, clone_factory=lambda: clone)
    try:
        assert router.prepare()
        assert list(router.stream("Public phrase")) == [CLONE_PCM]
    finally:
        router.close()


@pytest.mark.parametrize("partial", [False, True])
def test_cancel_or_live_key_flag_revocation_is_terminal_and_never_falls_back(route, partial):
    route.google.failure, route.google.partial = GeminiCancelled("configuration_changed"), partial
    output = route.router.stream("Public phrase")
    if partial:
        assert next(output) == GOOGLE_PCM
    with pytest.raises(GeminiCancelled):
        next(output)
    assert not route.clone.calls and route.clone.closed == 1


def test_google_failure_after_partial_pcm_never_repeats_text_using_another_voice(route):
    route.google.partial, route.google.failure = True, GeminiUnavailable("network_unavailable")
    output = route.router.stream("Public phrase")
    assert next(output) == GOOGLE_PCM
    with pytest.raises(GeminiUnavailable):
        next(output)
    assert not route.clone.calls and route.router.backend == "gemini"


def test_fixed_verified_ack_uses_local_cue_without_google_configuration_or_quota(route):
    route.google.prepare = lambda **_: pytest.fail("Local ACK unnecessarily prepared Google")
    assert list(route.router.stream("Да, сэр", rate=240)) == [ACK_PCM]
    assert not route.google.calls and not route.clone.calls and route.router.backend == "clone_fallback"
    assert route.router.last_usage == {"input_tokens": 0, "output_tokens": 0, "cache_hit": True}


def test_unavailable_fixed_ack_uses_the_regular_finite_provider_path(route):
    route.clone.ack = None
    assert list(route.router.stream("Да, сэр")) == [GOOGLE_PCM]
    assert len(route.google.calls) == 1


def test_clone_warmup_has_a_real_forty_five_second_bound_without_retrying_provider(route, monkeypatch):
    now = [100.0]
    route.router.clock = lambda: now[0]
    monkeypatch.setattr(route.router._closed, "wait", lambda _: now.__setitem__(0, now[0] + 1))
    route.google.failure = GeminiUnavailable("quota_exhausted")
    route.clone.is_ready, route.clone.is_warming = False, True
    with pytest.raises(GeminiUnavailable, match="local_unavailable"):
        list(route.router.stream("Public phrase"))
    assert now[0] == 145 and len(route.google.calls) == 1 and not route.clone.calls


def test_cancel_while_fallback_warms_disposes_owned_helper_promptly(route, monkeypatch):
    canceled = threading.Event()
    route.google.failure = GeminiUnavailable("quota_exhausted")
    route.clone.is_ready, route.clone.is_warming = False, True
    monkeypatch.setattr(route.router._closed, "wait", lambda _: canceled.set())
    with pytest.raises(GeminiCancelled):
        list(route.router.stream("Public phrase", cancel=canceled.is_set))
    assert route.clone.closed == 1 and not route.clone.calls and route.google.canceled == 1


def test_empty_or_failed_clone_never_claims_success_or_uses_another_renderer(route):
    route.google.failure = GeminiUnavailable("quota_exhausted")
    route.clone.empty = True
    with pytest.raises(GeminiUnavailable):
        list(route.router.stream("Public phrase"))
    assert route.router.backend == "gemini_unavailable"


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_constructor_finishing_after_cancel_cannot_start_late_helper(action):
    entered, release, results = threading.Event(), threading.Event(), []
    clone, google = Clone(), Google()
    def blocked():
        entered.set()
        assert release.wait(2)
        return clone
    router = GeminiLocalSpeech(gemini_factory=lambda: google, clone_factory=blocked)
    def prepare():
        try:
            router.prepare()
        except GeminiCancelled:
            results.append("canceled")
    worker = threading.Thread(target=prepare)
    worker.start()
    try:
        assert entered.wait(1)
        getattr(router, action)()
        release.set()
        worker.join(1)
        assert not worker.is_alive() and results == ["canceled"]
        assert not clone.warmed and clone.closed == 1 and google.prepared == 0
    finally:
        release.set()
        worker.join(1)
        router.close()


def test_close_disposes_both_owned_components_and_future_requests_are_terminal(route):
    route.router.prepare()
    route.router.close()
    assert route.clone.closed == route.google.closed == 1
    with pytest.raises(GeminiCancelled):
        list(route.router.stream("Public phrase"))


@pytest.mark.parametrize("outcome", ["gemini", "fallback", "partial_failure", "unavailable"])
def test_actual_voice_worker_integrates_optional_factory_without_windows_timbre_or_microphone(monkeypatch, outcome):
    google, clone = Google(), Clone()
    if outcome != "gemini":
        google.failure = GeminiUnavailable("network_unavailable")
    if outcome == "partial_failure":
        google.partial = True
    if outcome == "unavailable":
        clone.is_ready, clone.is_warming = False, False
    monkeypatch.setitem(sys.modules, "desktop.clone_speech", SimpleNamespace(CloneSpeech=lambda **_: clone))
    states, played, backends = [], [], []
    service = VoiceService(lambda _: pytest.fail("Speech emitted a command"), states.append,
        speech_factory=lambda: pytest.fail("Gemini used OpenAI factory"), gemini_speech_factory=lambda: google)
    def consume(chunks, **_options):
        for chunk in chunks:
            played.append(chunk)
            service._utterance_audio_started = True
            backends.append(service._active_backend.backend)
    service._play_pcm_stream = consume
    try:
        service.apply_preferences({"voice_engine": "gemini", "free_mode": True, "multilingual_stt_enabled": False})
        assert not service.multilingual_stt_enabled
        service._speech.put((service._speech_generation, "Public phrase"))
        service._speech.put(None)
        service._speak_loop()
        assert played == ([GOOGLE_PCM] if outcome in {"gemini", "partial_failure"} else [CLONE_PCM] if outcome == "fallback" else [])
        assert "windows" != service.speech_backend
        if outcome == "fallback":
            assert backends == ["clone_fallback"] and service.speech_backend == "clone_fallback"
        if outcome == "partial_failure":
            assert not clone.calls and any("прервалось" in state for state in states)
    finally:
        service.close()
    assert google.closed == clone.closed == 1


def test_gemini_requires_an_explicit_factory_and_keeps_default_and_openai_contracts(monkeypatch):
    states = []
    service = VoiceService(lambda _: None, states.append)
    assert service.speech_engine == "auto"
    service.apply_preferences({"voice_engine": "gemini"})
    service._speech.put((service._speech_generation, "Public phrase"))
    service._speech.put(None)
    service._speak_loop()
    assert service.speech_backend == "gemini_unavailable" and any("остается" in state.replace("ё", "е") for state in states)
    service.close()


@pytest.mark.parametrize("revoked", ["key", "speech_flag"])
def test_real_gemini_adapter_live_revocation_is_terminal_inside_wrapper(tmp_path, revoked):
    from desktop.gemini_speech import GeminiSpeech
    from desktop import gemini_ai

    key = "AQ." + "synthetic_wrapper_key_not_real_0123456789"
    now = datetime.now(timezone.utc)
    marker = {"version": 1, "project_id": "synthetic-project", "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
              "billing_enabled": False, "verified_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat()}
    (tmp_path / gemini_ai.MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    env = {"GEMINI_API_KEY": key, "JARVIS_GEMINI_ENABLED": "1", "JARVIS_GEMINI_SPEECH_ENABLED": "1"}
    raw = b"\1\0" * 6000
    payload = {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
        {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(raw).decode()}}]}}]}
    calls = []
    class Response:
        status, headers = 200, {}
        def __init__(self):
            self.raw = io.BytesIO(json.dumps(payload).encode())
        def read1(self, size):
            return self.raw.read(size)
        def geturl(self):
            return f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_ai.SPEECH_MODEL}:generateContent"
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.raw.close()
    class Opener:
        def open(self, *_args, **_kwargs):
            calls.append(True)
            return Response()
    clone = Clone()
    router = GeminiLocalSpeech(gemini_factory=lambda: GeminiSpeech(tmp_path, environ=env, opener=Opener()),
                              clone_factory=lambda: clone)
    output = router.stream("Public test phrase")
    try:
        assert next(output)[1] == 24000
        if revoked == "key":
            env["GEMINI_API_KEY"] = "AQ." + "different_synthetic_key_not_real_12345"
        else:
            env["JARVIS_GEMINI_SPEECH_ENABLED"] = "0"
        with pytest.raises(GeminiCancelled):
            next(output)
        assert calls == [True] and not clone.calls and clone.closed == 1
    finally:
        output.close()
        router.close()
