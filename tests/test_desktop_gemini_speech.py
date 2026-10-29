"""Verified-free TTS using fake byte HTTPS only; no Google, audio or real keys."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import sqlite3
import threading
import time
import urllib.error

import pytest

from desktop import gemini_ai as shared
from desktop.gemini_speech import GeminiSpeech, MAX_PCM_BYTES, SAMPLE_RATE

KEY = "AQ." + "synthetic_never_real_base64url_key_0123456789"
PCM = b"\x01\x02" * 6000


def payload(pcm=PCM):
    return {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
        {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}],
        "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 8}}


class Response:
    status = 200
    headers = {}

    def __init__(self, value):
        self.content = io.BytesIO(json.dumps(value).encode())

    def geturl(self):
        return f"https://generativelanguage.googleapis.com/v1beta/models/{shared.SPEECH_MODEL}:generateContent"

    def read1(self, size):
        return self.content.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.content.close()


class Opener:
    def __init__(self, value=None):
        self.value = payload() if value is None else value
        self.calls = []

    def open(self, request, **options):
        self.calls.append((request, options))
        return Response(self.value)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    marker = {"version": 1, "project_id": "synthetic-free-project", "key_sha256": hashlib.sha256(KEY.encode()).hexdigest(),
              "billing_enabled": False, "verified_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat()}
    (tmp_path / shared.MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(shared, "_INFLIGHT", threading.BoundedSemaphore(2))
    environment = {"JARVIS_GEMINI_ENABLED": "1", "JARVIS_GEMINI_SPEECH_ENABLED": "1", "GEMINI_API_KEY": KEY}
    return tmp_path, environment


def test_constructor_and_prepare_have_no_network_or_ledger_writes(setup):
    state, env = setup
    opener = Opener()
    before = sorted(state.iterdir())
    client = GeminiSpeech(state, environ=env, opener=opener)
    assert sorted(state.iterdir()) == before
    assert client.prepare(rate=185) and client.reason == "ready"
    assert sorted(state.iterdir()) == before and not opener.calls
    client.close()


def test_real_shared_transport_receives_exact_fixed_audio_request_and_yields_original_pcm(setup):
    state, env = setup
    opener = Opener()
    client = GeminiSpeech(state, environ=env, opener=opener)
    try:
        chunks = list(client.stream("Сэр, выберем один шаг.", rate=195, language="ru"))
        assert b"".join(chunk for chunk, _ in chunks) == PCM
        assert all(rate == SAMPLE_RATE and len(chunk) <= 4096 for chunk, rate in chunks)
        assert len(opener.calls) == 1
        request, options = opener.calls[0]
        assert request.full_url.endswith("/gemini-3.1-flash-tts-preview:generateContent")
        assert request.get_header("X-goog-api-key") == KEY and options["timeout"] <= shared.TEXT_TIMEOUT
        body = json.loads(request.data)
        assert body["generationConfig"] == {"candidateCount": 1, "maxOutputTokens": 1500, "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}}}
        assert set(body) == {"contents", "generationConfig"} and "195" in body["contents"][0]["parts"][0]["text"]
        assert client.last_usage["input_tokens"] == 30 and client.last_usage["output_tokens"] == 8
        with sqlite3.connect(state / "gemini-usage.sqlite3") as db:
            row = db.execute("SELECT requests,input_chars,audio_ms FROM daily").fetchone()
        assert row[0] == 1 and row[1] > 0 and row[2] == 60000
    finally:
        client.close()


@pytest.mark.parametrize("change", ["speech_flag", "main_flag", "key", "marker", "paid_marker", "voice"])
def test_missing_or_changed_opt_in_free_marker_and_voice_refuse_without_any_request(setup, change):
    state, env = setup
    if change == "speech_flag":
        env["JARVIS_GEMINI_SPEECH_ENABLED"] = "0"
    elif change == "main_flag":
        env["JARVIS_GEMINI_ENABLED"] = "0"
    elif change == "key":
        env["GEMINI_API_KEY"] = "AQ." + "different_synthetic_key_0123456789"
    elif change == "marker":
        (state / shared.MARKER_NAME).unlink()
    elif change == "paid_marker":
        marker = json.loads((state / shared.MARKER_NAME).read_text())
        marker["billing_enabled"] = True
        (state / shared.MARKER_NAME).write_text(json.dumps(marker))
    else:
        env["JARVIS_GEMINI_SPEECH_VOICE"] = "UNAPPROVED"
    opener = Opener()
    client = GeminiSpeech(state, environ=env, opener=opener)
    assert not client.prepare()
    with pytest.raises(shared.GeminiUnavailable):
        list(client.stream("Public synthetic text"))
    assert not opener.calls and not (state / "gemini-usage.sqlite3").exists()
    client.close()


@pytest.mark.parametrize("fault", ["b64", "mime", "odd", "silent", "limit", "function", "text", "partial", "blocked"])
def test_all_audio_validates_before_first_pcm_and_invalid_output_is_never_played(setup, fault):
    state, env = setup
    value = payload()
    part = value["candidates"][0]["content"]["parts"][0]
    if fault == "b64":
        part["inlineData"]["data"] = "not base64!"
    elif fault == "mime":
        part["inlineData"]["mimeType"] = "audio/wav"
    elif fault in {"odd", "silent", "limit"}:
        value = payload(b"1" if fault == "odd" else b"\0\0" * 100 if fault == "silent" else b"\1\0" * (MAX_PCM_BYTES // 2 + 1))
    elif fault == "function":
        value["candidates"][0]["content"]["parts"].append({"functionCall": {"name": "shutdown"}})
    elif fault == "text":
        value["candidates"][0]["content"]["parts"].append({"text": "claimed done"})
    elif fault == "partial":
        value["candidates"][0]["finishReason"] = "MAX_TOKENS"
    else:
        value["promptFeedback"] = {"blockReason": "SAFETY"}
    client = GeminiSpeech(state, environ=env, opener=Opener(value))
    output = client.stream("Public phrase")
    with pytest.raises(shared.GeminiUnavailable):
        next(output)
    assert client.last_usage is None
    client.close()


def test_fixed_ack_ram_cache_replays_without_request_or_additional_shared_reservation(setup):
    state, env = setup
    opener = Opener()
    client = GeminiSpeech(state, environ=env, opener=opener)
    try:
        assert list(client.stream("Да, сэр", rate=185))
        assert list(client.stream("Да, сэр", rate=185))
        assert len(opener.calls) == 1 and client.last_usage["cache_hit"] is True
        assert client.last_usage["input_tokens"] == client.last_usage["output_tokens"] == 0
        with sqlite3.connect(state / "gemini-usage.sqlite3") as db:
            assert db.execute("SELECT requests FROM daily").fetchone()[0] == 1
        list(client.stream("Public dynamic phrase"))
        list(client.stream("Public dynamic phrase"))
        assert len(opener.calls) == 3  # Dynamic/private text is never cached.
        assert not list(state.glob("*.wav")) and not list(state.glob("*.pcm"))
    finally:
        client.close()


@pytest.mark.parametrize("change", ["cancel", "close", "flag", "voice", "marker"])
def test_inflight_output_rechecks_every_chunk_and_never_replays_after_revocation(setup, change):
    state, env = setup
    opener = Opener()
    client = GeminiSpeech(state, environ=env, opener=opener)
    canceled = threading.Event()
    output = client.stream("Public long phrase", cancel=canceled.is_set)
    try:
        assert next(output)[1] == SAMPLE_RATE
        if change == "cancel":
            canceled.set()
        elif change == "close":
            client.close()
        elif change == "flag":
            env["JARVIS_GEMINI_SPEECH_ENABLED"] = "0"
        elif change == "voice":
            env["JARVIS_GEMINI_SPEECH_VOICE"] = "Orus"
        else:
            (state / shared.MARKER_NAME).unlink()
        with pytest.raises(shared.GeminiCancelled):
            next(output)
        assert client.last_usage is None and len(opener.calls) == 1
    finally:
        output.close()
        client.close()


def test_pre_cancellation_and_invalid_arguments_touch_no_files(setup, monkeypatch):
    state, env = setup
    client = GeminiSpeech(state, environ=env, opener=Opener())
    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid/canceled speech reached configuration/files")
    monkeypatch.setattr(shared, "_configuration", forbidden)
    for options in ({"rate": True}, {"rate": 241}, {"language": "fr"}, {"cancel": "not-callable"}):
        with pytest.raises(ValueError):
            client.stream("Public phrase", **options)
    with pytest.raises(shared.GeminiCancelled):
        list(client.stream("Public phrase", cancel=lambda: True))
    assert not client.opener.calls
    client.close()


def test_shared_text_reservation_limits_speech_and_quota_error_is_not_retried(setup):
    state, env = setup
    env["JARVIS_GEMINI_DAILY_REQUEST_LIMIT"] = "1"
    shared._Budget(state, env).reserve(input_chars=20)
    opener = Opener()
    client = GeminiSpeech(state, environ=env, opener=opener)
    assert not client.prepare() and client.reason == "budget_exceeded"
    with pytest.raises(shared.GeminiUnavailable, match="budget_exceeded"):
        list(client.stream("Public phrase"))
    assert not opener.calls
    client.close()


def test_http_quota_failure_sets_shared_cooldown_without_audio_or_retry(setup):
    state, env = setup
    calls = []
    class Limited:
        def open(self, request, **_kwargs):
            calls.append(True)
            raise urllib.error.HTTPError(request.full_url, 429, "PRIVATE_PROVIDER_ERROR", {}, io.BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'))
    client = GeminiSpeech(state, environ=env, opener=Limited())
    with pytest.raises(shared.GeminiUnavailable, match="quota_exhausted"):
        list(client.stream("Public phrase"))
    assert len(calls) == 1 and not shared.GeminiChat(state, environ=env).prepare()
    client.close()


def test_blocked_response_cancel_returns_promptly_without_late_output(setup):
    state, env = setup
    entered, release, canceled = threading.Event(), threading.Event(), threading.Event()
    errors, chunks = [], []
    class Blocked(Response):
        def read1(self, size):
            entered.set()
            assert release.wait(2)
            return super().read1(size)
    class Slow:
        def open(self, *_args, **_kwargs):
            return Blocked(payload())
    client = GeminiSpeech(state, environ=env, opener=Slow())
    def consume():
        try:
            chunks.extend(client.stream("Public phrase", cancel=canceled.is_set))
        except shared.GeminiUnavailable as error:
            errors.append(error)
    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        canceled.set()
        worker.join(.5)
        assert not worker.is_alive() and time.monotonic() - started < .5
        assert not chunks and len(errors) == 1 and isinstance(errors[0], shared.GeminiCancelled)
    finally:
        release.set()
        worker.join(1)
        client.close()
