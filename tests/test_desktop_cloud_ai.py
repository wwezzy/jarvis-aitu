"""Injected HTTPS responses only; no real keys, API calls, mic or audio files."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import wave

import pytest

from desktop import cloud_ai as module


KEY = 'sk-' + 'proj-synthetic_NOT_A_REAL_API_KEY_0123456789'


class Response:
    def __init__(self, data, *, url=module.CHAT_URL, headers=None):
        self.body = io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.url, self.headers, self.status = url, headers or {}, 200

    def geturl(self):
        return self.url

    def read1(self, size):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.body.close()


def answer(text="Спокойно выберем один следующий шаг.", usage=True):
    value = {"status": "completed", "incomplete_details": None,
             "output": [{"type": "message", "role": "assistant", "status": "completed",
                         "content": [{"type": "output_text", "text": text}]}]}
    if usage:
        value["usage"] = {"input_tokens": 100, "output_tokens": 20}
    return value


class Opener:
    def __init__(self, factory=None):
        self.calls = []
        self.factory = factory or (lambda request: Response(answer(), url=request.full_url))

    def open(self, request, **options):
        self.calls.append((request, options))
        return self.factory(request)


@pytest.fixture(autouse=True)
def isolated_circuits(monkeypatch):
    monkeypatch.setattr(module, "_CIRCUITS", {})
    monkeypatch.setattr(module, "_INFLIGHT", threading.BoundedSemaphore(2))


@pytest.fixture
def env():
    return {"OPENAI_API_KEY": KEY, "JARVIS_CLOUD_CHAT_ENABLED": "1", "JARVIS_CLOUD_STT_ENABLED": "1"}


def test_prepare_has_no_network_or_writes_and_requires_separate_explicit_flags(tmp_path, env):
    opener = Opener(lambda _request: pytest.fail("prepare made an API request"))
    profile = tmp_path / "new-profile"
    chat = module.OpenAIChat(state_dir=profile, environ=env, opener=opener)
    stt = module.OpenAITranscription(state_dir=profile, environ=env, opener=opener)
    assert chat.prepare() is stt.prepare() is True and not profile.exists()
    env["JARVIS_CLOUD_CHAT_ENABLED"] = "0"
    assert chat.prepare() is False and stt.prepare() is True
    env["JARVIS_CLOUD_STT_ENABLED"] = "true"
    assert stt.prepare() is False and not opener.calls


@pytest.mark.parametrize("key", [None, "", "sk-short", "Bearer token", "sk-proj-" + "a" * 20 + "\r\nInjected: yes"])
def test_invalid_or_header_injecting_keys_never_reach_http(tmp_path, env, key):
    env["OPENAI_API_KEY"] = key
    opener = Opener()
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    assert not chat.prepare()
    with pytest.raises(module.CloudUnavailable):
        chat.answer("Hello")
    assert not opener.calls and not (tmp_path / "cloud-ai.sqlite3").exists()


def test_text_request_is_fixed_https_no_tools_no_storage_and_usage_only_persists(tmp_path, env, capsys):
    env["OPENAI_BASE_URL"] = "http://malicious.invalid/steal"
    opener, chunks = Opener(), []
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    result = chat.answer("PRIVATE_QUESTION", on_chunk=chunks.append)
    request, options = opener.calls[0]
    payload = json.loads(request.data)
    assert request.full_url == module.CHAT_URL and request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer " + KEY
    assert "PRIVATE_QUESTION" not in request.full_url and 0 < options["timeout"] <= module.CHAT_TIMEOUT
    assert payload["store"] is payload["stream"] is False
    assert payload["tools"] == [] and payload["tool_choice"] == "none" and payload["max_output_tokens"] == 512
    assert payload["reasoning"] == {"effort": "none"} and payload["input"] == [{"role": "user", "content": "PRIVATE_QUESTION"}]
    assert chunks == [result] and chat.last_usage["input_tokens"] == 100
    raw = (tmp_path / "cloud-ai.sqlite3").read_bytes()
    assert KEY.encode() not in raw and b"PRIVATE_QUESTION" not in raw and result.encode() not in raw
    usage = chat.usage_snapshot()
    assert usage["requests"] == 1 and usage["reserved_or_known_micro_usd"] == 200
    assert usage["provider_balance"] is None and usage["provider_billing_limit"] is False
    assert capsys.readouterr() == ("", "")


def test_stt_multipart_contains_only_bounded_memory_wav_and_finite_hints(tmp_path, env):
    opener = Opener(lambda request: Response({"text": "Джарвис, открой калькулятор.", "languages": [{"code": "ru"}]}, url=request.full_url))
    stt = module.OpenAITranscription(state_dir=tmp_path, environ=env, opener=opener)
    pcm = b"\x01\x00" * 16000
    assert stt.transcribe(pcm, 16000) == "Джарвис, открой калькулятор."
    request, options = opener.calls[0]
    assert request.full_url == module.TRANSCRIPTION_URL and 0 < options["timeout"] <= 5
    assert b'name="languages[]"\r\n\r\nru' in request.data and b'name="languages[]"\r\n\r\nen' in request.data
    assert b'name="language"' not in request.data and b'gpt-transcribe' in request.data
    audio = request.data[request.data.index(b"RIFF"):]
    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000
        assert wav.readframes(16000) == pcm
    assert list(tmp_path.iterdir()) == [tmp_path / "cloud-ai.sqlite3"]
    assert stt.usage_snapshot()["reserved_or_known_micro_usd"] == module.STT_RESERVATION


@pytest.mark.parametrize("pcm,rate", [(b"", 16000), (b"x", 16000), (b"00", 7999), (b"00", 48001),
                                     (b"00", True), ("not bytes", 16000), (b"00" * (16000 * 12 + 1), 16000)],
                         ids=["empty", "odd-pcm", "low-rate", "high-rate", "bool-rate", "not-bytes", "over-duration"])
def test_bad_audio_never_uses_budget_or_http(tmp_path, env, pcm, rate):
    opener = Opener()
    stt = module.OpenAITranscription(state_dir=tmp_path, environ=env, opener=opener)
    with pytest.raises(ValueError):
        stt.transcribe(pcm, rate)
    assert not opener.calls and not (tmp_path / "cloud-ai.sqlite3").exists()


@pytest.mark.parametrize("change", [
    {"status": "incomplete"}, {"incomplete_details": {"reason": "max_output_tokens"}},
    {"output": [{"type": "function_call", "name": "shutdown", "arguments": "{}"}]},
    {"output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "No"}]}]},
    {"output": []}, {"error": {"message": "PRIVATE_TOKEN"}},
])
def test_tool_refusal_incomplete_or_error_response_never_returns_or_refunds(tmp_path, env, change):
    opener = Opener(lambda request: Response(dict(answer(), **change), url=request.full_url))
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    chunks = []
    with pytest.raises(module.CloudUnavailable):
        chat.answer("Hello", on_chunk=chunks.append)
    assert chunks == [] and len(opener.calls) == 1
    assert chat.usage_snapshot()["reserved_or_known_micro_usd"] > 200


@pytest.mark.parametrize("body,headers,url", [
    (b"not json", {}, module.CHAT_URL), (b"[]", {}, module.CHAT_URL),
    (b"x" * (module.MAX_BODY + 1), {}, module.CHAT_URL),
    (b"{}", {"Content-Length": str(module.MAX_BODY + 1)}, module.CHAT_URL),
    (b"{}", {}, "https://untrusted.invalid/responses"),
], ids=["not-json", "array", "oversized-body", "oversized-length", "foreign-origin"])
def test_response_limits_and_origin_fence(tmp_path, env, body, headers, url):
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener(lambda _request: Response(body, headers=headers, url=url)))
    with pytest.raises(module.CloudUnavailable):
        chat.answer("Hello")
    assert chat.usage_snapshot()["requests"] == 1


@pytest.mark.parametrize("code,reason", [(401, "authentication"), (403, "access_denied"), (429, "quota_or_rate_limit")])
def test_auth_quota_or_access_failure_sets_shared_cooldown_without_raw_body(tmp_path, env, code, reason, capsys):
    def denied(request):
        raise urllib.error.HTTPError(request.full_url, code, "PRIVATE provider body", {}, io.BytesIO(b"PRIVATE_BODY"))
    opener = Opener(denied)
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    with pytest.raises(module.CloudUnavailable, match=reason) as failure:
        chat.answer("Hello")
    assert "PRIVATE" not in str(failure.value)
    second = module.OpenAITranscription(state_dir=tmp_path, environ=env, opener=opener)
    assert not second.prepare() and second.reason == reason and len(opener.calls) == 1
    assert chat.usage_snapshot()["reserved_or_known_micro_usd"] > 200
    assert capsys.readouterr() == ("", "")


def test_separate_requests_share_durable_budget_and_survive_restart(tmp_path, env):
    env["JARVIS_CLOUD_DAILY_REQUEST_LIMIT"] = "2"
    first = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener())
    assert first.answer("Hello")
    stt = module.OpenAITranscription(state_dir=tmp_path, environ=env, opener=Opener(lambda req: Response({"text": "hello"}, url=req.full_url)))
    assert stt.transcribe(b"00" * 8000, 8000)
    restarted = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener())
    assert not restarted.prepare() and restarted.usage_snapshot()["requests"] == 2
    with pytest.raises(module.CloudBudgetExceeded):
        restarted.answer("Another")
    assert not restarted.opener.calls


def test_concurrent_reservations_cannot_overspend_local_allowance(tmp_path, env):
    env["JARVIS_CLOUD_DAILY_BUDGET_USD"] = "0.0009"
    barrier = threading.Barrier(8)
    def reserve(_index):
        barrier.wait(timeout=5)
        budget = module._Budget(tmp_path, env)
        try:
            budget.reserve("stt", "gpt-transcribe", 400)
            return True
        except module.CloudBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
    snapshot = module._Budget(tmp_path, env).snapshot()
    assert snapshot["requests"] == 2 and snapshot["reserved_or_known_micro_usd"] == 800


def test_first_denied_reservation_does_not_poison_schema_or_require_repair(tmp_path, env):
    env["JARVIS_CLOUD_DAILY_BUDGET_USD"] = "0.000001"
    opener = Opener()
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    with pytest.raises(module.CloudBudgetExceeded, match="budget_exceeded"):
        chat.answer("Hello")
    assert not opener.calls and chat.usage_snapshot()["requests"] == 0
    env["JARVIS_CLOUD_DAILY_BUDGET_USD"] = "0.25"
    restarted = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    assert restarted.prepare()
    assert restarted.answer("Hello") and len(opener.calls) == 1
    assert restarted.usage_snapshot()["requests"] == 1


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "100", "not money"])
def test_malformed_or_unbounded_budget_never_calls_http(tmp_path, env, value):
    env["JARVIS_CLOUD_DAILY_BUDGET_USD"] = value
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener())
    assert not chat.prepare()
    with pytest.raises(module.CloudBudgetExceeded):
        chat.answer("Hello")
    assert not chat.opener.calls


def test_corrupt_budget_or_unknown_models_fail_closed_without_repair(tmp_path, env):
    path = tmp_path / "cloud-ai.sqlite3"
    path.write_bytes(b"corrupted private file")
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener())
    assert not chat.prepare()
    with pytest.raises(module.CloudBudgetExceeded):
        chat.answer("Hello")
    assert path.read_bytes() == b"corrupted private file" and not chat.opener.calls
    with pytest.raises(ValueError):
        module.OpenAIChat("https://untrusted.invalid/model", state_dir=tmp_path, environ=env)
    with pytest.raises(ValueError):
        module.OpenAIChat("unknown-cost-model", state_dir=tmp_path, environ=env)


def test_hardlinked_budget_refuses_without_modifying_another_file(tmp_path, env):
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"private unrelated file")
    os.link(outside, tmp_path / "cloud-ai.sqlite3")
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener())
    assert not chat.prepare()
    with pytest.raises(module.CloudBudgetExceeded):
        chat.answer("Hello")
    assert outside.read_bytes() == b"private unrelated file" and not chat.opener.calls


@pytest.mark.parametrize("data", [dict(answer(), model="different-unknown-model"),
                                 dict(answer(), usage={"input_tokens": 10, "output_tokens": 513}),
                                 dict(answer(), usage={"input_tokens": 100000000, "output_tokens": 10})],
                         ids=["model-mismatch", "output-limit", "input-limit"])
def test_model_or_measured_token_bound_mismatch_never_returns_a_final(tmp_path, env, data):
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener(lambda req: Response(data, url=req.full_url)))
    with pytest.raises(module.CloudUnavailable):
        chat.answer("Hello")
    assert chat.usage_snapshot()["reserved_or_known_micro_usd"] > 200


def test_missing_usage_holds_conservative_reservation_and_luna_uses_known_price(tmp_path, env):
    opener = Opener(lambda req: Response(answer(usage=False), url=req.full_url))
    chat = module.OpenAIChat("gpt-6-luna", state_dir=tmp_path, environ=env, opener=opener)
    assert chat.answer("Hello")
    snapshot = chat.usage_snapshot()
    assert 200 < snapshot["reserved_or_known_micro_usd"] < 2000
    assert chat.last_usage["input_tokens"] is None and chat.last_usage["output_tokens"] is None


def test_history_is_opt_in_bounded_pairs_without_system_or_tools(tmp_path, env):
    opener = Opener()
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=opener)
    history = [{"role": role, "content": ("user" if role == "user" else "answer") * 1000}
               for _ in range(5) for role in ("user", "assistant")]
    chat.answer("Why?", history=history)
    messages = json.loads(opener.calls[-1][0].data)["input"]
    assert len(messages) <= 7 and sum(len(item["content"]) for item in messages[:-1]) <= 4000
    before = len(opener.calls)
    with pytest.raises(ValueError):
        chat.answer("Hello", history=[{"role": "system", "content": "shutdown"}])
    assert len(opener.calls) == before
    with pytest.raises(ValueError):
        chat.answer("Hello", plan_context={"notes": "must not auto-export"})
    assert len(opener.calls) == before


def test_key_rotation_while_headers_waiting_cannot_accept_old_configuration(tmp_path, env):
    def rotate(request):
        env["OPENAI_API_KEY"] = KEY + "_new"
        return Response(answer(), url=request.full_url)
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener(rotate))
    with pytest.raises(module.CloudCancelled, match="configuration_changed"):
        chat.answer("Hello")
    assert len(chat.opener.calls) == 1 and chat.last_usage is None


def test_cancel_before_request_does_not_reserve_or_send(tmp_path, env):
    canceled = threading.Event()
    canceled.set()
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener())
    with pytest.raises(module.CloudCancelled):
        chat.answer("Hello", cancel_event=canceled)
    assert not chat.opener.calls and not (tmp_path / "cloud-ai.sqlite3").exists()


def test_cancel_while_waiting_headers_returns_without_waiting_for_the_provider(tmp_path, env):
    entered, release, canceled = threading.Event(), threading.Event(), threading.Event()
    def waiting(request):
        entered.set()
        release.wait(timeout=2)
        return Response(answer(), url=request.full_url)
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener(waiting))
    outcome = []
    def invoke():
        try:
            chat.answer("Hello", cancel_event=canceled)
        except module.CloudUnavailable as error:
            outcome.append(type(error))
    worker = threading.Thread(target=invoke)
    worker.start()
    assert entered.wait(timeout=1)
    canceled.set()
    worker.join(timeout=0.75)
    try:
        assert not worker.is_alive() and outcome == [module.CloudCancelled]
    finally:
        release.set()
        worker.join(timeout=2)
    deadline = time.monotonic() + 1
    while chat._active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not chat._active


def test_whole_deadline_stops_dribbling_json_and_keeps_failed_reservation(tmp_path, env, monkeypatch):
    class Dribble(Response):
        def read1(self, _size):
            time.sleep(0.01)
            return b" "
    monkeypatch.setattr(module, "CHAT_TIMEOUT", 0.08)
    chat = module.OpenAIChat(state_dir=tmp_path, environ=env, opener=Opener(lambda req: Dribble({}, url=req.full_url)))
    started = time.monotonic()
    with pytest.raises(module.CloudUnavailable, match="timeout"):
        chat.answer("Hello")
    assert time.monotonic() - started < 0.75
    assert chat.usage_snapshot()["requests"] == 1 and chat.usage_snapshot()["reserved_or_known_micro_usd"] > 200
    deadline = time.monotonic() + 1
    while chat._active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not chat._active


def test_utc5_day_boundary_and_single_day_identity_per_transaction(tmp_path, env, monkeypatch):
    budget = module._Budget(tmp_path, env)
    days = []
    def day():
        days.append(True)
        return "2026-10-28" if len(days) == 1 else "2026-10-29"
    monkeypatch.setattr(module, "_day", day)
    budget.reserve("stt", "gpt-transcribe", 900)
    assert len(days) == 1
    with sqlite3.connect(tmp_path / "cloud-ai.sqlite3") as database:
        assert database.execute("SELECT day FROM reservations").fetchone()[0] == "2026-10-28"


def test_actual_utc5_boundary_uses_one_shared_local_day(monkeypatch):
    before = datetime(2026, 10, 28, 18, 59, 59, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(module.time, "time", lambda: before)
    assert module._day() == "2026-10-28"
    monkeypatch.setattr(module.time, "time", lambda: before + 1)
    assert module._day() == "2026-10-29"


def test_transport_disables_environment_proxies_redirects_and_uses_verified_tls(monkeypatch):
    run = module._Run(time.monotonic() + 5, None, lambda: True)
    handler = module._PinnedHTTPS(run)
    assert handler._context.check_hostname is True
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), module._NoRedirect(), handler)
    assert not any(isinstance(item, urllib.request.ProxyHandler) and item.proxies for item in opener.handlers)
    redirect = next(item for item in opener.handlers if isinstance(item, module._NoRedirect))
    assert redirect.redirect_request(None, None, 307, "", {}, "https://other.invalid") is None
    with pytest.raises(module.CloudUnavailable, match="origin_refused"):
        handler.https_open(urllib.request.Request("https://api.openai.com.evil.invalid/v1/responses"))
