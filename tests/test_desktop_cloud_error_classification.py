"""Classify synthetic HTTP failures without live keys, API or private files."""
import hashlib
import io
import json
import threading
import time
import urllib.error

import pytest

from desktop import cloud_ai as module


KEY = "sk-" + "synthetic_NOT_REAL_error_fixture_0123456789"
PRIVATE = "PRIVATE provider message /private/credential.env " + KEY


class ErrorBody(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_sizes, self.bytes_returned = [], 0

    def read1(self, amount):
        self.read_sizes.append(amount)
        block = super().read(amount)
        self.bytes_returned += len(block)
        return block


class Opener:
    def __init__(self, body, status=429):
        self.body, self.status, self.calls = body, status, []

    def open(self, request, **_options):
        self.calls.append(request)
        raise urllib.error.HTTPError(request.full_url, self.status, PRIVATE,
                                     {"X-Private": PRIVATE}, self.body)


@pytest.fixture(autouse=True)
def isolate_shared_state(monkeypatch):
    monkeypatch.setattr(module, "_CIRCUITS", {})
    monkeypatch.setattr(module, "_INFLIGHT", threading.BoundedSemaphore(2))


def client(tmp_path, body, *, status=429):
    environment = {"OPENAI_API_KEY": KEY, "JARVIS_CLOUD_CHAT_ENABLED": "1"}
    opener = Opener(body, status)
    return module.OpenAIChat(state_dir=tmp_path, environ=environment, opener=opener)


def payload(code=None, kind="requests", message=PRIVATE):
    return json.dumps({"error": {"code": code, "type": kind, "message": message}}).encode()


@pytest.mark.parametrize("code,cooldown", [
    ("credit_balance_exhausted", 120), ("insufficient_quota", 120),
    ("organization_spend_limit_exceeded", 120), ("project_spend_limit_exceeded", 120),
    ("organization_usage_limit_exceeded", 120), ("rate_limit_exceeded", 10), ("slow_down", 10),
])
def test_fixed_error_codes_distinguish_billing_limits_and_rate_without_logs_or_retry(tmp_path, code, cooldown, capsys, caplog):
    body = ErrorBody(payload(code))
    value = client(tmp_path, body)
    started = time.monotonic()
    with pytest.raises(module.CloudUnavailable) as caught:
        value.answer("synthetic question")
    assert str(caught.value) == caught.value.reason == value.reason == code
    assert value.last_usage is None and len(value.opener.calls) == 1 and body.closed
    fingerprint = hashlib.sha256(KEY.encode()).hexdigest()
    until, reason = module._CIRCUITS[fingerprint]
    assert reason == code and cooldown - 1 <= until - started <= cooldown + 1
    assert value.prepare() is False
    with pytest.raises(module.CloudUnavailable) as second:
        value.answer("another explicit question")
    assert second.value.reason == code and len(value.opener.calls) == 1
    raw = (tmp_path / "cloud-ai.sqlite3").read_bytes()
    assert KEY.encode() not in raw and PRIVATE.encode() not in raw
    assert value.usage_snapshot()["requests"] == 1
    assert capsys.readouterr() == ("", "") and PRIVATE not in caplog.text and KEY not in caplog.text


@pytest.mark.parametrize("code,kind,expected", [
    (None, "insufficient_quota", "insufficient_quota"),
    ("unknown_future_code", "insufficient_quota", "insufficient_quota"),
    ("rate_limit_exceeded", "insufficient_quota", "rate_limit_exceeded"),
    ("unknown_future_code", "rate_limit_exceeded", "quota_or_rate_limit"),
    ({"private": PRIVATE}, [PRIVATE], "quota_or_rate_limit"),
])
def test_only_exact_code_then_known_quota_type_are_used(tmp_path, code, kind, expected):
    value = client(tmp_path, ErrorBody(payload(code, kind)))
    with pytest.raises(module.CloudUnavailable) as caught:
        value.answer("synthetic question")
    assert caught.value.reason == expected


@pytest.mark.parametrize("raw", [b"not JSON", b"[]", b"{}", b'{"error":[]}', b"\xff",
                                  payload(message="credit_balance_exhausted insufficient_quota")])
def test_message_text_or_malformed_json_cannot_invent_a_billing_error(tmp_path, raw):
    value = client(tmp_path, ErrorBody(raw))
    with pytest.raises(module.CloudUnavailable, match="^quota_or_rate_limit$"):
        value.answer("synthetic question")
    assert value.reason == "quota_or_rate_limit" and value.last_usage is None


def test_error_json_has_an_independent_eight_kibibyte_read_cap(tmp_path):
    body = ErrorBody(payload("credit_balance_exhausted") + b" " * module.MAX_ERROR_BODY)
    value = client(tmp_path, body)
    with pytest.raises(module.CloudUnavailable, match="^quota_or_rate_limit$"):
        value.answer("synthetic question")
    assert body.bytes_returned == module.MAX_ERROR_BODY + 1
    assert max(body.read_sizes) <= 2048 and len(value.opener.calls) == 1


@pytest.mark.parametrize("status,reason", [(401, "authentication"), (403, "access_denied"), (500, "provider_error")])
def test_non429_status_does_not_consume_or_interpret_private_body(tmp_path, status, reason):
    body = ErrorBody(payload("credit_balance_exhausted"))
    value = client(tmp_path, body, status=status)
    with pytest.raises(module.CloudUnavailable, match="^" + reason + "$"):
        value.answer("synthetic question")
    assert body.bytes_returned == 0 and body.read_sizes == [] and body.closed


def wait_inactive(value):
    deadline = time.monotonic() + 2
    while value._active and time.monotonic() < deadline:
        time.sleep(.005)
    assert not value._active


@pytest.mark.parametrize("change", ["cancel", "revoke", "close"])
def test_blocked_error_body_cannot_hold_consumer_or_publish_late_quota_failure(tmp_path, change):
    entered, release, canceled = threading.Event(), threading.Event(), threading.Event()
    class Blocked(ErrorBody):
        def read1(self, amount):
            entered.set()
            assert release.wait(2)
            return super().read1(amount)
    body = Blocked(payload("credit_balance_exhausted"))
    value = client(tmp_path, body)
    results = []
    def answer():
        try:
            value.answer("synthetic question", cancel_event=canceled)
        except module.CloudUnavailable as error:
            results.append((type(error), error.reason))
    worker = threading.Thread(target=answer)
    worker.start()
    try:
        assert entered.wait(1)
        if change == "cancel":
            canceled.set()
        elif change == "revoke":
            value.environ["JARVIS_CLOUD_CHAT_ENABLED"] = "0"
        else:
            value.close()
        worker.join(.6)
        assert not worker.is_alive()
        assert results == [(module.CloudCancelled, "configuration_changed" if change == "revoke" else "canceled")]
        release.set()
        wait_inactive(value)
        assert len(value.opener.calls) == 1 and value.last_usage is None and body.closed
        assert not module._CIRCUITS  # Late billing result must not replace cancellation.
        assert value.usage_snapshot()["requests"] == 1
    finally:
        release.set()
        worker.join(2)
        value.close()
        wait_inactive(value)


def test_error_body_dribble_obeys_original_whole_request_deadline(tmp_path, monkeypatch):
    class Dribble(ErrorBody):
        def read1(self, amount):
            time.sleep(.01)
            self.read_sizes.append(amount)
            return b" "
    monkeypatch.setattr(module, "CHAT_TIMEOUT", .08)
    value = client(tmp_path, Dribble(b""))
    started = time.monotonic()
    with pytest.raises(module.CloudUnavailable, match="^timeout$"):
        value.answer("synthetic question")
    assert time.monotonic() - started < .6
    wait_inactive(value)
    assert len(value.opener.calls) == 1 and value.last_usage is None
    assert value.usage_snapshot()["requests"] == 1


def test_error_body_without_read1_still_uses_bounded_read(tmp_path):
    class ReadOnlyBody(ErrorBody):
        read1 = None

        def read(self, amount):
            assert 0 < amount <= 2048
            return super().read1(amount)
    body = ReadOnlyBody(payload("rate_limit_exceeded"))
    value = client(tmp_path, body)
    with pytest.raises(module.CloudUnavailable, match="^rate_limit_exceeded$"):
        value.answer("synthetic question")
    assert body.bytes_returned <= module.MAX_ERROR_BODY and body.closed
