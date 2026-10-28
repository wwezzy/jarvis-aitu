"""Synthetic Realtime events; no real API, microphone, player or private profile."""
import base64
from copy import deepcopy
import json
import socket
import ssl
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import cloud_ai
from desktop import cloud_speech as module
from desktop.cloud_ai import CloudBudgetExceeded, CloudCancelled, CloudUnavailable


KEY = "sk-" + "offline_speech_placeholder" * 2
TEXT = "PRIVATE_TEST_TEXT_ONLY"
PCM = b"\x04\x00" * 2400


def events(model=module.DEFAULT_MODEL, voice=module.DEFAULT_VOICE, pcm=PCM, text=TEXT):
    session = {"type": "realtime", "model": model}
    updated = {**session, "tools": [], "tool_choice": "none", "output_modalities": ["audio"],
               "audio": {"input": {"turn_detection": None}, "output": {
                   "format": {"type": "audio/pcm", "rate": 24000}, "voice": voice}}}
    target = {"response_id": "resp_test", "item_id": "item_test", "output_index": 0, "content_index": 0}
    item = {"id": "item_test", "type": "message", "role": "assistant", "content": [{"type": "audio", "transcript": text}]}
    return [
        {"type": "session.created", "session": session},
        {"type": "session.updated", "session": updated},
        {"type": "response.created", "response": {"id": "resp_test", "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "response_id": "resp_test", "output_index": 0,
         "item": {**item, "content": []}},
        {"type": "response.content_part.added", **target, "part": {"type": "audio", "transcript": ""}},
        {"type": "response.output_audio.delta", **target, "delta": base64.b64encode(pcm).decode()},
        {"type": "response.output_audio_transcript.delta", **target, "delta": text},
        {"type": "response.output_audio_transcript.done", **target, "transcript": text},
        {"type": "response.output_audio.done", **target},
        {"type": "response.content_part.done", **target, "part": item["content"][0]},
        {"type": "response.output_item.done", "response_id": "resp_test", "output_index": 0, "item": item},
        {"type": "response.done", "response": {"id": "resp_test", "status": "completed", "output": [item],
                                             "usage": {"input_tokens": 70, "output_tokens": 30}}},
    ]


class Connection:
    def __init__(self, server_events=None):
        self.events = list(events() if server_events is None else server_events)
        self.sent, self.closed = [], threading.Event()
        self.received = 0

    def send(self, text):
        if self.closed.is_set():
            raise OSError("synthetic closed transport")
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if self.closed.is_set():
            raise OSError("synthetic closed transport")
        self.received += 1
        if self.events:
            value = self.events.pop(0)
            return json.dumps(value) if isinstance(value, dict) else value
        self.closed.wait(timeout or .01)
        raise TimeoutError("synthetic silent provider")

    def close_socket(self):
        self.closed.set()


class Connector:
    def __init__(self, connection=None):
        self.connection, self.calls = connection or Connection(), []

    def __call__(self, uri, **options):
        self.calls.append((uri, options))
        return self.connection


class FreshConnector:
    def __init__(self, env):
        self.env, self.calls, self.connections = env, [], []
        self.pcm, self.transcript, self.usage = PCM, "Да, сэр.", {"input_tokens": 70, "output_tokens": 30}
        self.status = "completed"

    def __call__(self, uri, **options):
        self.calls.append((uri, options))
        model = uri.partition("model=")[2]
        body = events(model, self.env.get("JARVIS_CLOUD_SPEECH_VOICE", "cedar"), self.pcm, self.transcript)
        body[-1]["response"]["usage"] = self.usage
        body[-1]["response"]["status"] = self.status
        value = Connection(body)
        self.connections.append(value)
        return value


@pytest.fixture(autouse=True)
def no_shared_circuit_contamination():
    with cloud_ai._CIRCUIT_LOCK:
        cloud_ai._CIRCUITS.clear()
    yield
    with cloud_ai._CIRCUIT_LOCK:
        cloud_ai._CIRCUITS.clear()


@pytest.fixture
def env():
    return {"OPENAI_API_KEY": KEY, "JARVIS_CLOUD_SPEECH_ENABLED": "1"}


def wait_for(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.005)
    raise AssertionError("Synthetic speech operation failed to settle")


def finish(client):
    client.close()
    wait_for(lambda: not client._active)


def threaded(function):
    values, errors = [], []
    def work():
        try:
            values.append(function())
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=work)
    worker.start()
    return SimpleNamespace(worker=worker, values=values, errors=errors)


def test_constructor_prepare_and_disabled_flags_make_no_network_or_database(tmp_path, env):
    connector = Connector()
    state = tmp_path / "not-created"
    client = module.OpenAIRealtimeSpeech(state, environ=env, connector=connector)
    assert not state.exists() and not connector.calls
    assert client.prepare(rate=185) and not state.exists() and not connector.calls
    for values in ({}, {"OPENAI_API_KEY": KEY}, {"JARVIS_CLOUD_SPEECH_ENABLED": "1"}):
        disabled = module.OpenAIRealtimeSpeech(state, environ=values, connector=connector)
        assert not disabled.prepare()
        with pytest.raises(CloudUnavailable, match="not_configured"):
            list(disabled.stream(TEXT))
    assert not state.exists() and not connector.calls
    client.close()


def test_default_constructor_uses_the_client_state_directory_without_loading_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    client = module.OpenAIRealtimeSpeech(environ={})
    assert client.state_dir == tmp_path / "JarvisDesktop"
    assert not client.prepare() and not client.state_dir.exists()
    client.close()


@pytest.mark.parametrize("model", list(module._RATES))
@pytest.mark.parametrize("voice", ["cedar", "ash", "marin"])
def test_stream_is_exact_finite_audio_protocol_and_records_only_numeric_budget(tmp_path, env, model, voice, capsys):
    env.update(JARVIS_CLOUD_SPEECH_MODEL=model, JARVIS_CLOUD_SPEECH_VOICE=voice)
    connection = Connection(events(model, voice))
    connector = Connector(connection)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        chunks = list(client.stream(TEXT, rate=195))
        assert b"".join(pcm for pcm, rate in chunks) == PCM and all(rate == 24000 for _pcm, rate in chunks)
        assert all(0 < len(pcm) <= module.PCM_CHUNK_BYTES and len(pcm) % 2 == 0 for pcm, _rate in chunks)
        assert len(connector.calls) == 1
        uri, options = connector.calls[0]
        assert uri == "wss://api.openai.com/v1/realtime?model=" + model
        assert options["proxy"] is options["compression"] is None
        assert options["max_size"] == 256 * 1024 and options["max_queue"] == 4
        assert options["logger"].disabled and options["additional_headers"] == {"Authorization": "Bearer " + KEY}
        assert [event["type"] for event in connection.sent] == ["session.update", "conversation.item.create", "response.create"]
        session = connection.sent[0]["session"]
        assert session["tools"] == [] and session["tool_choice"] == "none" and session["output_modalities"] == ["audio"]
        assert session["audio"]["input"]["turn_detection"] is None
        assert session["audio"]["output"] == {"format": {"type": "audio/pcm", "rate": 24000}, "voice": voice}
        assert "195" in session["instructions"] and TEXT not in session["instructions"]
        assert connection.sent[1]["item"]["content"] == [{"type": "input_text", "text": TEXT}]
        assert connection.sent[2]["response"]["max_output_tokens"] == 1024
        assert client.last_usage["input_tokens"] == 70 and client.last_usage["output_tokens"] == 30
        assert client.reason == "ready" and client.is_ready
        with client.budget.database() as db:
            row = db.execute("SELECT kind,model,reserved,settled FROM reservations").fetchone()
        incoming, outgoing = module._RATES[model]
        measured = (70 * incoming + 30 * outgoing + 999999) // 1000000
        assert row[0:2] == ("speech", model) and row[2] > measured == row[3]
        blob = (tmp_path / "cloud-ai.sqlite3").read_bytes()
        assert TEXT.encode() not in blob and KEY.encode() not in blob
        assert capsys.readouterr().out == ""
    finally:
        finish(client)
    assert connection.closed.is_set()


@pytest.mark.parametrize("setting,value", [("JARVIS_CLOUD_SPEECH_MODEL", "https://other.invalid"),
                                          ("JARVIS_CLOUD_SPEECH_MODEL", "unknown-model"),
                                          ("JARVIS_CLOUD_SPEECH_VOICE", "unknown"),
                                          ("OPENAI_API_KEY", "invalid\r\nheader")])
def test_unknown_models_voices_or_injected_key_never_connect(tmp_path, env, setting, value):
    env[setting] = value
    connector = Connector()
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    assert not client.prepare()
    with pytest.raises(CloudUnavailable):
        list(client.stream(TEXT))
    assert not connector.calls and not (tmp_path / "cloud-ai.sqlite3").exists()


@pytest.mark.parametrize("text,rate", [("", 185), ("x" * 1001, 185), ("bad\x00text", 185),
                                      ("\ud800", 185), (TEXT, True), (TEXT, 89), (TEXT, 241)])
def test_invalid_text_or_rate_never_reserves_or_connects(tmp_path, env, text, rate):
    connector = Connector()
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    with pytest.raises(ValueError):
        list(client.stream(text, rate=rate))
    assert not connector.calls and not (tmp_path / "cloud-ai.sqlite3").exists()


def test_conservative_speech_reservation_shares_chat_stt_and_prevents_network_before_budget(tmp_path, env):
    budget = cloud_ai._Budget(tmp_path, env)
    budget.reserve("chat", "gpt-6-luna", 230000)
    budget.reserve("stt", "gpt-transcribe", 1000)
    connector = Connector()
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    with pytest.raises(CloudBudgetExceeded):
        list(client.stream(TEXT))
    assert not connector.calls and budget.snapshot()["requests"] == 2
    env["JARVIS_CLOUD_DAILY_BUDGET_USD"] = "1"
    assert list(client.stream(TEXT))
    finish(client)
    assert budget.snapshot()["requests"] == 3


@pytest.mark.parametrize("failure", ["model", "tools", "format", "voice", "vad", "response_model", "tool_item", "base64", "odd_pcm", "binary", "json"])
def test_malformed_or_tool_output_is_rejected_before_any_pcm(tmp_path, env, failure):
    body = events()
    if failure == "model":
        body[0]["session"]["model"] = "unapproved-model"
    elif failure == "tools":
        body[1]["session"]["tools"] = [{"type": "function", "name": "shutdown"}]
    elif failure == "format":
        body[1]["session"]["audio"]["output"]["format"]["rate"] = 16000
    elif failure == "voice":
        body[1]["session"]["audio"]["output"]["voice"] = "unapproved"
    elif failure == "vad":
        body[1]["session"]["audio"]["input"]["turn_detection"] = {"type": "server_vad"}
    elif failure == "response_model":
        body[2]["response"]["model"] = "unapproved-model"
    elif failure == "tool_item":
        body[3]["item"] = {"type": "function_call", "name": "shutdown"}
    elif failure == "base64":
        body[5]["delta"] = "%%%%not-audio"
    elif failure == "odd_pcm":
        body[5]["delta"] = base64.b64encode(b"x").decode()
    elif failure == "binary":
        body[0] = b"binary websocket frame"
    else:
        body[0] = "{broken-json"
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=Connector(Connection(body)))
    try:
        with pytest.raises(CloudUnavailable):
            next(client.stream(TEXT))
        assert client.last_usage is None
        assert client.budget.snapshot()["requests"] == 1
    finally:
        finish(client)


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete"])
def test_error_after_partial_pcm_has_no_second_voice_or_automatic_request(tmp_path, env, status):
    body = events()
    body[-1]["response"]["status"] = status
    connector = Connector(Connection(body))
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    stream = client.stream(TEXT)
    try:
        assert next(stream) == (PCM, 24000)
        with pytest.raises(CloudUnavailable, match="incomplete_response"):
            list(stream)
        assert len(connector.calls) == 1 and client.last_usage is None
        assert client.budget.snapshot()["requests"] == 1
    finally:
        finish(client)


def test_missing_usage_is_unknown_and_keeps_full_reservation(tmp_path, env):
    body = events()
    body[-1]["response"].pop("usage")
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=Connector(Connection(body)))
    try:
        assert list(client.stream(TEXT))
        assert client.last_usage["input_tokens"] is client.last_usage["output_tokens"] is None
        with client.budget.database() as db:
            assert db.execute("SELECT settled FROM reservations").fetchone()[0] is None
    finally:
        finish(client)


def test_total_audio_and_event_size_limits_fail_without_unbounded_buffer(tmp_path, env, monkeypatch):
    monkeypatch.setattr(module, "MAX_SECONDS", .25)
    body = events(pcm=b"\x01\x00" * 4000)
    body.insert(6, deepcopy(body[5]))  # 16,000 PCM bytes exceed the 12,000-byte test bound.
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=Connector(Connection(body)))
    try:
        with pytest.raises(CloudUnavailable, match="response_limit"):
            list(client.stream(TEXT))
    finally:
        finish(client)


def test_cancel_before_iteration_does_not_touch_budget_or_transport(tmp_path, env):
    connector = Connector()
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    stream = client.stream(TEXT)
    client.cancel()
    with pytest.raises(CloudCancelled):
        next(stream)
    assert not connector.calls and not (tmp_path / "cloud-ai.sqlite3").exists()


def test_cancel_and_close_return_promptly_while_provider_is_silent(tmp_path, env):
    connection, entered = Connection([]), threading.Event()
    connector = Connector(connection)
    original = connection.recv
    def receive(**kwargs):
        entered.set()
        return original(**kwargs)
    connection.recv = receive
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    task = threaded(lambda: list(client.stream(TEXT)))
    try:
        assert entered.wait(1)
        started = time.monotonic()
        client.close()
        task.worker.join(.5)
        assert not task.worker.is_alive() and time.monotonic() - started < .5
        assert isinstance(task.errors[0], CloudCancelled) and not task.values
        assert connection.closed.is_set() and client.last_usage is None
    finally:
        finish(client)


def test_whole_deadline_stops_silent_provider_and_retains_reservation(tmp_path, env, monkeypatch):
    monkeypatch.setattr(module, "MAX_SECONDS", .12)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=Connector(Connection([])))
    try:
        started = time.monotonic()
        with pytest.raises(CloudUnavailable, match="timeout"):
            list(client.stream(TEXT))
        assert time.monotonic() - started < .6 and client.budget.snapshot()["requests"] == 1
    finally:
        finish(client)


def test_key_rotation_or_opt_out_drops_late_audio_before_it_can_be_yielded(tmp_path, env):
    connection, connector = Connection(), Connector()
    connector.connection = connection
    original = connection.recv
    def receive(**kwargs):
        value = original(**kwargs)
        if connection.received == 6:
            env["JARVIS_CLOUD_SPEECH_ENABLED"] = "0"
        return value
    connection.recv = receive
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        with pytest.raises(CloudCancelled, match="configuration_changed"):
            next(client.stream(TEXT))
        assert client.last_usage is None and len(connector.calls) == 1
    finally:
        finish(client)


def test_late_dns_after_cancel_never_creates_a_socket_or_sends_auth(tmp_path, env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def resolve(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.socket, "socket", lambda *_a, **_kw: calls.append("socket") or pytest.fail("Late socket"))
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env)
    task = threaded(lambda: list(client.stream(TEXT)))
    try:
        assert entered.wait(1)
        client.cancel()
        task.worker.join(.5)
        assert not task.worker.is_alive() and isinstance(task.errors[0], CloudCancelled)
        release.set()
        wait_for(lambda: not client._active)
        assert calls == []
    finally:
        release.set()
        task.worker.join(2)
        finish(client)


def test_cancel_immediately_before_sdk_handshake_never_sends_authorization(tmp_path, env, monkeypatch):
    from websockets.sync import client as sdk
    entered, release = threading.Event(), threading.Event()
    authorized, transports = [], []
    class RawSocket:
        def __init__(self, *_args):
            self.closed = False
            transports.append(self)
        def settimeout(self, _timeout):
            pass
        def connect(self, _address):
            pass
        def shutdown(self, _how):
            self.closed = True
        def close(self):
            self.closed = True
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))])
    monkeypatch.setattr(module.socket, "socket", RawSocket)
    monkeypatch.setattr(sdk.ClientConnection, "handshake", lambda *_a, **_kw: authorized.append(True))
    def connect(_uri, **options):
        assert options["proxy"] is options["compression"] is None
        assert options["ssl"].context.check_hostname and options["ssl"].context.verify_mode == ssl.CERT_REQUIRED
        value = object.__new__(options["create_connection"])
        entered.set()
        assert release.wait(2)
        value.handshake(options["additional_headers"])
        pytest.fail("Canceled handshake returned")
    monkeypatch.setattr(sdk, "connect", connect)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env)
    task = threaded(lambda: list(client.stream(TEXT)))
    try:
        assert entered.wait(1)
        client.close()
        task.worker.join(.5)
        assert not task.worker.is_alive() and isinstance(task.errors[0], CloudCancelled)
        release.set()
        wait_for(lambda: not client._active)
        assert authorized == [] and transports and all(value.closed for value in transports)
    finally:
        release.set()
        task.worker.join(2)
        finish(client)


def test_two_stalled_workers_bound_late_connectors_and_reject_a_third_without_reservation(tmp_path, env):
    entered, release = threading.Semaphore(0), threading.Event()
    connections = []
    def connect(_uri, **_options):
        entered.release()
        assert release.wait(2)
        connection = Connection()
        connections.append(connection)
        return connection
    clients = [module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connect) for _ in range(3)]
    tasks = [threaded(lambda value=value: list(value.stream(TEXT))) for value in clients[:2]]
    try:
        assert entered.acquire(timeout=1) and entered.acquire(timeout=1)
        for client in clients[:2]:
            client.cancel()
        for task in tasks:
            task.worker.join(.5)
            assert not task.worker.is_alive() and isinstance(task.errors[0], CloudCancelled)
        with pytest.raises(CloudUnavailable, match="busy"):
            list(clients[2].stream(TEXT))
        assert clients[2].budget.snapshot()["requests"] == 2
        release.set()
        for client in clients:
            wait_for(lambda value=client: not value._active)
        assert len(connections) == 2 and all(not value.sent and value.closed.is_set() for value in connections)
    finally:
        release.set()
        for task in tasks:
            task.worker.join(2)
        for client in clients:
            finish(client)


def test_slow_consumer_applies_four_chunk_backpressure_and_cancel_releases_worker(tmp_path, env):
    body = events()
    body[5:6] = [deepcopy(body[5]) for _ in range(100)]
    connection = Connection(body)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=Connector(connection))
    stream = client.stream(TEXT)
    try:
        assert next(stream) == (PCM, 24000)
        wait_for(lambda: connection.received >= 10)
        # Five metadata events, the yielded chunk, four queued chunks and at
        # most the next chunk being published. No entire response is buffered.
        threading.Event().wait(.06)
        assert connection.received <= 11
        client.cancel()
        with pytest.raises(CloudCancelled):
            next(stream)
        wait_for(lambda: not client._active)
        assert connection.closed.is_set()
    finally:
        stream.close()
        finish(client)


@pytest.mark.parametrize("kind", ["error_event", "connector_exception", "http_authentication"])
def test_provider_failures_expose_fixed_labels_without_text_keys_or_provider_logs(tmp_path, env, capsys, caplog, kind):
    if kind == "error_event":
        connector = Connector(Connection([{"type": "error", "error": {
            "code": "invalid_api_key", "message": KEY + TEXT}}]))
        expected = "authentication"
    elif kind == "connector_exception":
        def connector(*_args, **_kwargs):
            raise RuntimeError(KEY + TEXT)
        expected = "network_unavailable"
    else:
        class AuthenticationError(Exception):
            response = SimpleNamespace(status_code=401)
        def connector(*_args, **_kwargs):
            raise AuthenticationError(KEY + TEXT)
        expected = "authentication"
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        with pytest.raises(CloudUnavailable) as caught:
            list(client.stream(TEXT))
        assert caught.value.reason == client.reason == expected
        assert str(caught.value) == expected and client.last_usage is None
        assert client.budget.snapshot()["requests"] == 1
        assert not client.prepare()  # The shared cooldown avoids a retry storm.
        captured = capsys.readouterr()
        assert not captured.out and not captured.err and KEY not in caplog.text and TEXT not in caplog.text
    finally:
        finish(client)


def test_tls_transport_is_attached_before_its_handshake_and_checked_afterward(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class TLS:
        closed = False
        def do_handshake(self):
            entered.set()
            assert release.wait(2)
        def shutdown(self, _how):
            self.closed = True
        def close(self):
            self.closed = True
    secured, seen = TLS(), []
    class Context:
        def wrap_socket(self, raw, **options):
            seen.append((raw, options))
            return secured
    monkeypatch.setattr(module.ssl, "create_default_context", Context)
    run = cloud_ai._Run(time.monotonic() + 2, None, lambda: True)
    context, raw = module._VerifiedTLS(run), object()
    task = threaded(lambda: context.wrap_socket(raw, server_hostname="api.openai.com"))
    try:
        assert entered.wait(1)
        assert seen == [(raw, {"server_hostname": "api.openai.com", "do_handshake_on_connect": False})]
        assert secured in run.sockets
        run.interrupt()
        release.set()
        task.worker.join(1)
        assert not task.worker.is_alive() and secured.closed
        assert isinstance(task.errors[0], CloudCancelled)
    finally:
        release.set()
        run.interrupt()
        task.worker.join(2)


def test_only_a_fully_completed_public_ack_is_replayed_without_http_or_reservation(tmp_path, env):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        first = list(client.stream("Да, сэр."))
        before = client.budget.snapshot()
        assert len(client._ack_cache) == 1 and client.prepare(rate=185)
        second = list(client.stream("Да, сэр."))
        assert first == second and len(connector.calls) == 1
        assert client.budget.snapshot() == before
        assert client.last_usage["cache_hit"] and client.last_usage["input_tokens"] == client.last_usage["output_tokens"] == 0
        assert all(KEY not in str(key) and key[-1] in module._ACKS for key in client._ack_cache)
        assert sum(len(pcm) for chunks in client._ack_cache.values() for pcm in chunks) <= module._ACK_MAX_BYTES * 2
    finally:
        finish(client)


@pytest.mark.parametrize("change", ["key", "model", "voice", "rate", "punctuation"])
def test_ack_cache_is_scoped_to_key_model_voice_rate_and_text(tmp_path, env, change):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        assert list(client.stream("Да, сэр."))
        rate, text = 185, "Да, сэр."
        if change == "key":
            env["OPENAI_API_KEY"] = "sk-" + "rotated_offline_placeholder" * 2
        elif change == "model":
            env["JARVIS_CLOUD_SPEECH_MODEL"] = "gpt-realtime-2.1"
        elif change == "voice":
            env["JARVIS_CLOUD_SPEECH_VOICE"] = "ash"
        elif change == "rate":
            rate = 195
        else:
            text = "Да, сэр"
        assert list(client.stream(text, rate=rate))
        assert len(connector.calls) == 2 and client.budget.snapshot()["requests"] == 2
        assert len(client._ack_cache) <= 2
    finally:
        finish(client)


def test_cache_eviction_is_bounded_to_two_public_ack_entries(tmp_path, env):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        for rate in (185, 195, 205):
            assert list(client.stream("Да, сэр.", rate=rate))
        assert len(client._ack_cache) == 2 and {key[-2] for key in client._ack_cache} == {195, 205}
        assert list(client.stream("Да, сэр.", rate=185))
        assert len(connector.calls) == 4 and len(client._ack_cache) == 2
    finally:
        finish(client)


@pytest.mark.parametrize("failure", ["private_text", "wrong_transcript", "long_ack", "partial_ack"])
def test_private_wrong_overlong_or_partial_audio_is_never_put_in_ack_cache(tmp_path, env, failure):
    connector = FreshConnector(env)
    text = "Да, сэр."
    if failure == "private_text":
        text = TEXT
    elif failure == "wrong_transcript":
        connector.transcript = "Another phrase"
    elif failure == "long_ack":
        connector.pcm = b"\x04\x00" * (24000 * 4 + 1)
    else:
        connector.status = "incomplete"
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        if failure == "partial_ack":
            with pytest.raises(CloudUnavailable, match="incomplete_response"):
                list(client.stream(text))
        else:
            assert list(client.stream(text))
        assert not client._ack_cache
    finally:
        finish(client)


def test_cached_ack_still_plays_at_the_cap_but_other_speech_cannot_bypass_the_budget(tmp_path, env):
    env["JARVIS_CLOUD_DAILY_REQUEST_LIMIT"] = "1"
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        assert list(client.stream("Да, сэр."))
        assert client.prepare(rate=185) and not client.prepare(rate=195)
        assert list(client.stream("Да, сэр."))
        with pytest.raises(CloudBudgetExceeded, match="budget_exceeded"):
            list(client.stream(TEXT))
        assert len(connector.calls) == 1 and client.budget.snapshot()["requests"] == 1
    finally:
        finish(client)


def test_cached_ack_does_not_authorize_new_network_requests_during_a_cooldown(tmp_path, env):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        assert list(client.stream("Да, сэр."))
        fingerprint = next(iter(client._ack_cache))[0]
        with cloud_ai._CIRCUIT_LOCK:
            cloud_ai._CIRCUITS[fingerprint] = (time.monotonic() + 60, "authentication")
        assert client.prepare(rate=185) and list(client.stream("Да, сэр."))
        with pytest.raises(CloudUnavailable, match="authentication"):
            list(client.stream(TEXT))
        assert len(connector.calls) == 1
    finally:
        finish(client)


@pytest.mark.parametrize("invalidate", ["cancel", "close", "revoke"])
def test_cache_is_cleared_by_explicit_cancel_close_or_configuration_revocation(tmp_path, env, invalidate):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        assert list(client.stream("Да, сэр.")) and client._ack_cache
        if invalidate == "cancel":
            client.cancel()
        elif invalidate == "close":
            client.close()
        else:
            env["JARVIS_CLOUD_SPEECH_ENABLED"] = "0"
            assert not client.prepare()
        assert not client._ack_cache
        if invalidate != "cancel":
            with pytest.raises(CloudUnavailable):
                list(client.stream("Да, сэр."))
        assert len(connector.calls) == 1
    finally:
        finish(client)


def test_canceling_a_partial_ack_never_settles_or_caches_its_audio(tmp_path, env):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    stream = client.stream("Да, сэр.")
    try:
        assert next(stream) == (PCM, 24000)
        client.cancel()
        with pytest.raises(CloudCancelled):
            next(stream)
        assert not client._ack_cache and client.last_usage is None
        with client.budget.database() as database:
            assert database.execute("SELECT settled FROM reservations").fetchone()[0] is None
    finally:
        stream.close()
        finish(client)


def test_unknown_usage_retains_reservation_even_when_public_ack_can_be_cached(tmp_path, env):
    connector = FreshConnector(env)
    connector.usage = None
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    try:
        assert list(client.stream("Да, сэр."))
        assert client.last_usage["input_tokens"] is client.last_usage["output_tokens"] is None
        with client.budget.database() as database:
            assert database.execute("SELECT settled FROM reservations").fetchone()[0] is None
        assert list(client.stream("Да, сэр.")) and client.last_usage["cache_hit"]
        assert len(connector.calls) == 1 and client.budget.snapshot()["requests"] == 1
    finally:
        finish(client)


def test_external_cancellation_stops_cached_audio_and_invalidates_the_ram_copy(tmp_path, env):
    connector = FreshConnector(env)
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=connector)
    canceled = threading.Event()
    try:
        assert list(client.stream("Да, сэр."))
        cached = client.stream("Да, сэр.", cancel=canceled.is_set)
        assert next(cached) == (PCM, 24000)
        canceled.set()
        with pytest.raises(CloudCancelled):
            next(cached)
        assert not client._ack_cache and len(connector.calls) == 1
        assert client.budget.snapshot()["requests"] == 1
    finally:
        finish(client)


def test_settlement_failure_preserves_reservation_without_losing_validated_audio(tmp_path, env, monkeypatch):
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=Connector())
    def fail(*_args, **_kwargs):
        raise OSError("synthetic ledger failure")
    monkeypatch.setattr(client.budget, "settle", fail)
    try:
        assert list(client.stream(TEXT))
        assert client.last_usage["input_tokens"] == 70 and client.reason == "ready"
        with client.budget.database() as database:
            assert database.execute("SELECT settled FROM reservations").fetchone()[0] is None
    finally:
        finish(client)


def test_cancellation_during_known_completion_settlement_never_caches_or_replays_late_audio(tmp_path, env, monkeypatch):
    client = module.OpenAIRealtimeSpeech(tmp_path, environ=env, connector=FreshConnector(env))
    original = client.budget.settle
    def settle(*args):
        client.cancel()
        return original(*args)
    monkeypatch.setattr(client.budget, "settle", settle)
    try:
        with pytest.raises(CloudCancelled):
            list(client.stream("Да, сэр."))
        assert client.last_usage is None and not client._ack_cache
        with client.budget.database() as database:
            # Billing is known once the provider has fully completed; a late
            # local playback cancellation doesn't invent an unknown outcome.
            assert database.execute("SELECT settled FROM reservations").fetchone()[0] == 642
    finally:
        finish(client)
