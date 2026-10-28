"""Real loopback cancellation: no model, personal profile or external service."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import chat


def _event(text="", *, done=False):
    return {"message": {"role": "assistant", "content": text}, "done": done,
            **({"done_reason": "stop", "prompt_eval_count": 7, "eval_count": 3} if done else {})}


def _line(text="", *, done=False):
    return json.dumps(_event(text, done=done)).encode() + b"\n"


@contextmanager
def _loopback(monkeypatch, modes):
    """A blocked provider is released in finally, even when an assertion fails."""
    state = SimpleNamespace(entered=threading.Event(), release=threading.Event(),
                            requests=[], handlers=[], errors=[], lock=threading.Lock())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(3)
            with state.lock:
                state.handlers.append(threading.current_thread())
                index = len(state.requests)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                assert self.path == "/api/chat" and 0 < length < 20000
                request = json.loads(self.rfile.read(length))
                with state.lock:
                    state.requests.append(request)
                mode = modes[index] if index < len(modes) else "unexpected_retry"
                if mode == "unexpected_retry":
                    raise AssertionError("The client retried a canceled generation")
                streaming = request["stream"] is True
                content = "" if request["messages"] == [] else "Completed synthetic answer"
                body = (_line(content, done=True) if streaming else
                        json.dumps(_event(content, done=True)).encode())
                if mode == "headers":
                    state.entered.set()
                    assert state.release.wait(8), "Synthetic provider was not released"
                elif mode == "header_dribble":
                    header = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                              + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n")
                    state.entered.set()
                    for byte in header:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if state.release.wait(.04):
                            return
                    self.wfile.write(body)
                    self.wfile.flush()
                    return
                prefix = b""
                if mode == "partial_line":
                    assert streaming
                    prefix = _line("Never publish an incomplete event")[:28]
                elif mode == "after_preview":
                    assert streaming
                    prefix = _line("Visible before cancellation")
                elif mode == "buffered_tokens":
                    assert streaming
                    body = _line("First") + _line("STALE after cancellation") + _line(done=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson" if streaming else "application/json")
                self.send_header("Content-Length", str(len(prefix) + len(body)))
                self.end_headers()
                self.wfile.flush()
                if prefix:
                    self.wfile.write(prefix)
                    self.wfile.flush()
                if mode in {"first_body", "partial_line", "after_preview"}:
                    state.entered.set()
                    assert state.release.wait(8), "Synthetic provider was not released"
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass  # A canceled owned loopback socket is the expected outcome.
            except Exception as error:
                state.errors.append(type(error).__name__)
            finally:
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02),
                               name="SyntheticChatServer", daemon=True)
    monkeypatch.setattr(chat, "ENDPOINT", f"http://127.0.0.1:{server.server_port}/api/chat")
    serving.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        serving.join(2)
        for handler in state.handlers:
            handler.join(2)
        assert not serving.is_alive() and all(not handler.is_alive() for handler in state.handlers)
        assert state.errors == []


def _client_task(client, cancel, *, on_chunk=None):
    outcome = {}

    def answer():
        try:
            outcome["answer"] = client.answer("A synthetic cancellation question", cancel_event=cancel,
                                              on_chunk=on_chunk)
        except Exception as error:
            outcome["error"] = error

    worker = threading.Thread(target=answer, name="SyntheticChatClient", daemon=True)
    worker.start()
    return worker, outcome


def _assert_no_new_watchdog(previous):
    assert not [thread for thread in threading.enumerate()
                if thread.name == "JarvisChatCancel" and thread not in previous]


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "ndjson"])
@pytest.mark.parametrize("phase", ["headers", "first_body"])
def test_cancel_interrupts_real_header_or_first_token_wait_without_waiting_45_seconds(monkeypatch, phase, streaming):
    before = set(threading.enumerate())
    cancel, preview = threading.Event(), []
    with _loopback(monkeypatch, [phase]) as provider:
        client = chat.LocalChat(timeout=45)
        worker, outcome = _client_task(client, cancel, on_chunk=preview.append if streaming else None)
        try:
            assert provider.entered.wait(3), "The real HTTP request never reached the provider"
            started = time.monotonic()
            cancel.set()
            worker.join(2)
            assert not worker.is_alive(), "Cancellation waited for the 45-second model timeout"
            assert time.monotonic() - started < 2
            assert isinstance(outcome.get("error"), chat.LocalChatCancelled)
            assert "answer" not in outcome and preview == []
            assert client.last_usage is None and len(provider.requests) == 1
            _assert_no_new_watchdog(before)
        finally:
            cancel.set()
            provider.release.set()
            worker.join(3)
        assert not worker.is_alive()
        assert preview == [] and "answer" not in outcome


@pytest.mark.parametrize("phase", ["partial_line", "after_preview"])
def test_cancel_discards_incomplete_or_late_stream_text_and_joins_watchdog(monkeypatch, phase):
    before = set(threading.enumerate())
    cancel, seen = threading.Event(), []
    preview_received = threading.Event()

    def receive(text):
        seen.append(text)
        preview_received.set()

    with _loopback(monkeypatch, [phase]) as provider:
        client = chat.LocalChat(timeout=45)
        worker, outcome = _client_task(client, cancel, on_chunk=receive)
        try:
            assert provider.entered.wait(3)
            if phase == "after_preview":
                assert preview_received.wait(2)
            cancel.set()
            worker.join(2)
            assert not worker.is_alive()
            assert isinstance(outcome.get("error"), chat.LocalChatCancelled)
            expected = ["Visible before cancellation"] if phase == "after_preview" else []
            assert seen == expected and "answer" not in outcome and client.last_usage is None
            _assert_no_new_watchdog(before)
            # Even a provider completing afterward cannot publish stale data.
            provider.release.set()
        finally:
            cancel.set()
            provider.release.set()
            worker.join(3)
        assert not worker.is_alive() and seen == expected
        assert len(provider.requests) == 1


def test_cancel_from_first_delta_prevents_later_buffered_deltas_and_final_answer(monkeypatch):
    before = set(threading.enumerate())
    cancel, seen = threading.Event(), []

    def receive(text):
        seen.append(text)
        cancel.set()

    with _loopback(monkeypatch, ["buffered_tokens"]) as provider:
        client = chat.LocalChat(timeout=45)
        with pytest.raises(chat.LocalChatCancelled):
            client.answer("Synthetic buffered cancellation", cancel_event=cancel, on_chunk=receive)
        assert seen == ["First"]
        assert client.last_usage is None and len(provider.requests) == 1
        _assert_no_new_watchdog(before)


def test_canceled_client_can_start_one_fresh_request_without_stale_transport_or_watchdog(monkeypatch):
    before = set(threading.enumerate())
    cancel = threading.Event()
    with _loopback(monkeypatch, ["headers", "complete"]) as provider:
        client = chat.LocalChat(timeout=45)
        worker, outcome = _client_task(client, cancel)
        try:
            assert provider.entered.wait(3)
            cancel.set()
            worker.join(2)
            assert not worker.is_alive() and isinstance(outcome.get("error"), chat.LocalChatCancelled)
            provider.release.set()
            assert client.answer("A different synthetic question", cancel_event=threading.Event()) == "Completed synthetic answer"
            assert len(provider.requests) == 2 and client.last_usage["output_tokens"] == 3
            _assert_no_new_watchdog(before)
        finally:
            provider.release.set()
            cancel.set()
            worker.join(3)
        assert not worker.is_alive()


def test_precanceled_new_answer_clears_previous_usage_without_opening_another_request(monkeypatch):
    before = set(threading.enumerate())
    with _loopback(monkeypatch, ["complete"]) as provider:
        client = chat.LocalChat(timeout=45)
        assert client.answer("The previous successful synthetic question") == "Completed synthetic answer"
        assert client.last_usage["output_tokens"] == 3 and client.last_budget is not None
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(chat.LocalChatCancelled):
            client.answer("This new request was already canceled", cancel_event=cancel)
        assert client.last_usage is None and client.last_budget is None
        assert len(provider.requests) == 1
        _assert_no_new_watchdog(before)


def test_explicit_prewarm_after_cancellation_does_not_inherit_the_old_cancel_event(monkeypatch):
    before = set(threading.enumerate())
    cancel = threading.Event()
    with _loopback(monkeypatch, ["headers", "complete"]) as provider:
        client = chat.LocalChat(timeout=45)
        worker, outcome = _client_task(client, cancel)
        try:
            assert provider.entered.wait(3)
            cancel.set()
            worker.join(2)
            assert not worker.is_alive() and isinstance(outcome.get("error"), chat.LocalChatCancelled)
            provider.release.set()
            assert client.prewarm() is True
            assert len(provider.requests) == 2 and provider.requests[-1]["messages"] == []
            assert client.last_usage is None and client.last_budget is None
            _assert_no_new_watchdog(before)
        finally:
            cancel.set()
            provider.release.set()
            worker.join(3)
        assert not worker.is_alive()


@pytest.mark.parametrize("operation", ["answer", "stream", "prewarm"])
def test_real_dribbling_headers_cannot_restart_the_whole_request_deadline(monkeypatch, operation):
    before = set(threading.enumerate())
    with _loopback(monkeypatch, ["header_dribble"]) as provider:
        client, seen = chat.LocalChat(timeout=1), []
        started = time.monotonic()
        if operation == "prewarm":
            assert client.prewarm() is False
        else:
            with pytest.raises(chat.LocalChatUnavailable) as caught:
                client.answer("Synthetic header deadline", on_chunk=seen.append if operation == "stream" else None)
            assert not isinstance(caught.value, chat.LocalChatCancelled)
        elapsed = time.monotonic() - started
        assert .7 <= elapsed < 2.5
        assert provider.entered.is_set() and len(provider.requests) == 1
        assert seen == [] and client.last_usage is None
        if operation == "prewarm":
            assert provider.requests[0]["messages"] == [] and client.last_budget is None
        _assert_no_new_watchdog(before)
