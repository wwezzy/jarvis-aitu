"""Cold-load budgets are explicit and independent of interactive deadlines."""
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import desktop_app
from desktop import chat
from desktop.model_runtime import should_start_local_model


def test_free_mode_is_explicit_local_primary_even_with_preserved_cloud_mode_and_flags():
    class NoCloudInspection:
        def get(self, _key):
            pytest.fail("Explicit free mode inspected a cloud setting")
    assert should_start_local_model({"local_chat_enabled": True, "free_mode": True,
                                    "voice_engine": "openai"}, NoCloudInspection())
    assert not should_start_local_model({"local_chat_enabled": False, "free_mode": True}, {})
    assert not should_start_local_model({"local_chat_enabled": True, "free_mode": 1,
                                        "voice_engine": "openai"}, {})


class Response:
    status = 200

    def __init__(self, content="", *, load=False, **metrics):
        self.body = io.BytesIO(json.dumps({"done": True, "done_reason": "load" if load else "stop",
            "message": {"role": "assistant", "content": content}, **metrics}).encode())
        self.reads = 0

    def geturl(self):
        return chat.ENDPOINT

    def read1(self, amount):
        self.reads += 1
        return self.body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.body.close()


def test_slow_cold_preload_uses_separate_budget_then_answer_keeps_original_deadline(monkeypatch):
    clock, calls = [100.0], []
    monkeypatch.setattr(chat.time, "monotonic", lambda: clock[0])
    class Opener:
        def open(self, request, *, timeout):
            body = json.loads(request.data)
            calls.append((body, timeout))
            if len(calls) == 1:
                clock[0] += 30  # Above old20s prewarm, within explicit90s load.
                return Response(load=True, load_duration=30_000_000_000)
            clock[0] += .25
            return Response("На связи.", prompt_eval_count=40, eval_count=8,
                            load_duration=5_000_000, prompt_eval_duration=50_000_000, eval_duration=100_000_000)
    client = chat.LocalChat(opener=Opener())
    assert client.timeout == 45 and client.prewarm(timeout=90)
    assert client.last_warmup == {"completed": True, "elapsed_ms": 30000, "load_ms": 30000.0}
    assert client.last_usage is client.last_budget is None
    assert calls[0][0]["messages"] == [] and calls[0][1] == 90
    assert calls[0][0]["keep_alive"] == "30m" and "tools" not in calls[0][0]
    assert client.answer("Проверка связи.") == "На связи."
    assert calls[1][1] == client.timeout == 45 and len(calls) == 2
    assert client.last_usage == {"input_tokens": 40, "output_tokens": 8, "elapsed_ms": 250,
                                 "load_ms": 5.0, "prompt_eval_ms": 50.0, "generation_ms": 100.0}


def test_default_prewarm_keeps_explicit_caller_deadline_without_retry(monkeypatch):
    clock, calls = [100.0], []
    monkeypatch.setattr(chat.time, "monotonic", lambda: clock[0])
    class Opener:
        def open(self, request, *, timeout):
            calls.append(timeout)
            clock[0] += 2
            return Response(load=True)
    client = chat.LocalChat(opener=Opener(), timeout=1)
    assert client.prewarm() is False and calls == [1]
    assert client.last_warmup["completed"] is False and client.last_usage is None
    assert client.prewarm(timeout=90) is True and calls == [1, 90]


@pytest.mark.parametrize("timeout,expected", [(9999, 90), (-10, 1), (30.5, 30.5)])
def test_explicit_model_load_budget_remains_finite_and_capped(timeout, expected):
    calls = []
    class Opener:
        def open(self, _request, *, timeout):
            calls.append(timeout)
            return Response(load=True)
    client = chat.LocalChat(opener=Opener())
    assert client.prewarm(timeout=timeout) and calls == [expected] and client.timeout == 45


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), True, "90"])
def test_malformed_prewarm_deadline_never_opens_transport(timeout):
    client = chat.LocalChat(opener=SimpleNamespace(open=lambda *_a, **_kw: pytest.fail("Invalid deadline used HTTP")))
    with pytest.raises(ValueError, match="finite"):
        client.prewarm(timeout=timeout)


def test_precanceled_model_load_is_inert_and_reports_only_safe_numeric_metadata():
    canceled = threading.Event()
    canceled.set()
    client = chat.LocalChat(opener=SimpleNamespace(open=lambda *_a, **_kw: pytest.fail("Canceled warmup used HTTP")))
    assert client.prewarm(timeout=90, cancel_event=canceled) is False
    assert client.last_warmup["completed"] is False and client.last_warmup["canceled"] is True
    assert client.last_usage is client.last_budget is None


def test_cancel_during_cold_load_rejects_late_ready_result_without_reading_it():
    canceled = threading.Event()
    response = Response(load=True)
    class Opener:
        def open(self, _request, *, timeout):
            canceled.set()
            return response
    client = chat.LocalChat(opener=Opener())
    assert client.prewarm(timeout=90, cancel_event=canceled) is False
    assert response.reads == 0 and client.last_warmup["canceled"] is True
    assert client.last_usage is None


@pytest.mark.parametrize("metrics", [
    {"load_duration": -1, "prompt_eval_duration": True, "eval_duration": "private-data"},
    {"load_duration": float("inf"), "prompt_eval_duration": None, "eval_duration": 2**63},
])
def test_invalid_optional_server_timings_do_not_pollute_usage(metrics):
    result = chat.LocalChat._usage(metrics, chat.time.monotonic())
    assert set(result) == {"input_tokens", "output_tokens", "elapsed_ms"}


def test_actual_startup_warm_helper_requests_only_empty_preload_with_90s_budget(monkeypatch):
    calls = []
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass
    class Local:
        def __init__(self, model, *, timeout):
            calls.append((model, timeout))

        def prewarm(self, **options):
            calls.append(options)
            return True
    monkeypatch.setattr(chat, "LocalChat", Local)
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: Connection()
                        if address == ("127.0.0.1", 11434) and timeout == .3 else pytest.fail("Unexpected connection"))
    desktop_app._warm_local_chat("synthetic-local-model")
    assert calls == [("synthetic-local-model", 20), {"timeout": 90}]


def test_loopback_connect_timeout_does_not_inherit_long_model_load_budget(monkeypatch):
    captured = []
    class Socket:
        def setsockopt(self, *_args):
            pass
    def connect(address, timeout, source_address=None):
        captured.append((address, timeout))
        return Socket()
    client = chat.LocalChat()
    monkeypatch.setattr(chat.socket, "create_connection", connect)
    def open_connection(connection_class, _request):
        connection = connection_class("127.0.0.1", 11434, timeout=90)
        connection.connect()
        assert connection.timeout == 90
        return object()
    monkeypatch.setattr(client._transport, "do_open", open_connection)
    request = client._request({"messages": []})
    request.timeout = 90
    client._transport.http_open(request)
    assert captured == [(("127.0.0.1", 11434), chat.LOCAL_CONNECT_TIMEOUT)]


def test_real_cold_load_header_wait_remains_cancelable_with_90_second_budget(monkeypatch):
    entered, release, canceled = threading.Event(), threading.Event(), threading.Event()
    requests, outcomes = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            entered.set()
            assert release.wait(3)
            body = json.dumps({"done": True, "done_reason": "load", "message": {"content": ""}}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setattr(chat, "ENDPOINT", "http://127.0.0.1:" + str(server.server_port) + "/api/chat")
    client = chat.LocalChat(timeout=45)
    worker = threading.Thread(target=lambda: outcomes.append(client.prewarm(timeout=90, cancel_event=canceled)))
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        canceled.set()
        worker.join(.8)
        assert not worker.is_alive() and time.monotonic() - started < .8 and outcomes == [False]
        assert client.last_warmup["canceled"] is True and client.last_usage is None
        assert len(requests) == 1 and requests[0]["messages"] == []
        assert not client._transport.sockets
    finally:
        canceled.set()
        release.set()
        worker.join(2)
        server.shutdown()
        server.server_close()
        serving.join(2)
