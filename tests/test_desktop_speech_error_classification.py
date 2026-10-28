"""Realtime quota/rate errors use fixed labels without real API or audio."""
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import cloud_ai, cloud_speech


KEY = "sk-" + "synthetic_NEVER_REAL_speech_error_key_0123456789"
PRIVATE = "PRIVATE raw provider text " + KEY
CODES = ("credit_balance_exhausted", "insufficient_quota", "organization_spend_limit_exceeded",
         "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "rate_limit_exceeded", "slow_down")


@pytest.fixture(autouse=True)
def circuits():
    with cloud_ai._CIRCUIT_LOCK:
        cloud_ai._CIRCUITS.clear()
    yield
    with cloud_ai._CIRCUIT_LOCK:
        cloud_ai._CIRCUITS.clear()


def environment():
    return {"OPENAI_API_KEY": KEY, "JARVIS_CLOUD_SPEECH_ENABLED": "1"}


class Connection:
    def __init__(self, details):
        self.details, self.closed = details, threading.Event()

    def recv(self, **_options):
        return json.dumps({"type": "error", "error": self.details})

    def send(self, _text):
        pytest.fail("Error-only session must not send a user utterance")

    def close_socket(self):
        self.closed.set()


def inactive(client):
    deadline = time.monotonic() + 2
    while client._active and time.monotonic() < deadline:
        time.sleep(.005)
    assert not client._active


@pytest.mark.parametrize("channel", ["websocket_event", "sdk_invalid_status"])
@pytest.mark.parametrize("code", CODES)
def test_speech_fixed_codes_and_cooldowns_match_http_without_retry_or_private_data(tmp_path, channel, code, capsys, caplog):
    calls = []
    details = {"code": code, "type": "requests", "message": PRIVATE}
    connection = Connection(details)
    if channel == "sdk_invalid_status":
        sdk = pytest.importorskip("websockets.exceptions")
        from websockets.http11 import Response
        from websockets.datastructures import Headers
    def connector(_uri, **_options):
        calls.append(True)
        if channel == "sdk_invalid_status":
            raise sdk.InvalidStatus(Response(429, "Too Many Requests", Headers(),
                                            body=json.dumps({"error": details}).encode()))
        return connection
    client = cloud_speech.OpenAIRealtimeSpeech(tmp_path, environ=environment(), connector=connector)
    started = time.monotonic()
    try:
        with pytest.raises(cloud_ai.CloudUnavailable) as failure:
            next(client.stream("Синтетическая проверка."))
        assert failure.value.reason == client.reason == str(failure.value) == code
        assert client.last_usage is None and len(calls) == 1
        until, saved_reason = cloud_ai._CIRCUITS[hashlib.sha256(KEY.encode()).hexdigest()]
        expected_delay = 10 if code in {"rate_limit_exceeded", "slow_down"} else 120
        assert saved_reason == code and expected_delay - 1 <= until - started <= expected_delay + 1
        assert client.prepare() is False
        with pytest.raises(cloud_ai.CloudUnavailable) as second:
            next(client.stream("Следующая явная проверка."))
        assert second.value.reason == code and len(calls) == 1
        assert client.budget.snapshot()["requests"] == 1
        raw = (tmp_path / "cloud-ai.sqlite3").read_bytes()
        assert KEY.encode() not in raw and PRIVATE.encode() not in raw
        assert capsys.readouterr() == ("", "") and PRIVATE not in caplog.text
    finally:
        client.close()
        inactive(client)


@pytest.mark.parametrize("details,expected", [
    ({"code": "unknown_future", "type": "insufficient_quota"}, "insufficient_quota"),
    ({"type": "insufficient_quota"}, "insufficient_quota"),
    ({"code": "slow_down", "type": "insufficient_quota"}, "slow_down"),
    ({"code": {"private": PRIVATE}, "type": [PRIVATE]}, "provider_error"),
    ({"message": "credit_balance_exhausted insufficient_quota"}, "provider_error"),
    ({"code": "invalid_api_key", "type": "insufficient_quota"}, "authentication"),
])
def test_event_uses_only_exact_code_or_known_type_and_keeps_authentication_priority(tmp_path, details, expected):
    client = cloud_speech.OpenAIRealtimeSpeech(tmp_path, environ=environment(),
                                              connector=lambda *_args, **_kwargs: Connection(details))
    try:
        with pytest.raises(cloud_ai.CloudUnavailable) as error:
            list(client.stream("Проверка."))
        assert error.value.reason == expected
    finally:
        client.close()
        inactive(client)


@pytest.mark.parametrize("body", [None, b"not-json", b"[]", b"{}", b"\xff", b" " * (cloud_ai.MAX_ERROR_BODY + 1),
                                  json.dumps({"error": {"message": "credit_balance_exhausted"}}).encode()])
def test_sdk_unknown_429_body_keeps_legacy_label_without_reading_or_inference(body):
    error = RuntimeError(PRIVATE)
    error.response = SimpleNamespace(status_code=429, body=body)
    assert cloud_speech._reason(error) == "quota_or_rate_limit"


def test_available_sdk_body_inspection_never_calls_a_stream_reader():
    class Body:
        def read(self, *_args):
            pytest.fail("SDK error classification started I/O")
    error = RuntimeError(PRIVATE)
    error.response = SimpleNamespace(status_code=429, body=Body())
    assert cloud_speech._reason(error) == "quota_or_rate_limit"


def test_late_credit_error_after_cancel_cannot_become_a_quota_failure_or_cache_entry(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Blocked(Connection):
        def recv(self, **options):
            entered.set()
            assert release.wait(2)
            return super().recv(**options)
    connection = Blocked({"code": "credit_balance_exhausted", "message": PRIVATE})
    calls = []
    def connector(*_args, **_kwargs):
        calls.append(True)
        return connection
    client = cloud_speech.OpenAIRealtimeSpeech(tmp_path, environ=environment(), connector=connector)
    outcomes = []
    def invoke():
        try:
            list(client.stream("Да, сэр."))
        except cloud_ai.CloudUnavailable as error:
            outcomes.append((type(error), error.reason))
    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert entered.wait(1)
        client.cancel()
        worker.join(.6)
        assert not worker.is_alive() and outcomes == [(cloud_ai.CloudCancelled, "canceled")]
        release.set()
        inactive(client)
        assert len(calls) == 1 and not cloud_ai._CIRCUITS and not client._ack_cache
        assert client.last_usage is None and client.budget.snapshot()["requests"] == 1
    finally:
        release.set()
        worker.join(2)
        client.close()
        inactive(client)
