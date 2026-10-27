"""Measured final usage and bounded prompt projection; no model/server calls."""
from copy import deepcopy
import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time

import pytest

from desktop import chat


def event(content="Ответ.", **fields):
    return {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": content}, **fields}


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, *, stream=False, after_read=None):
        body = data if isinstance(data, bytes) else (
            b"".join(json.dumps(row).encode() + b"\n" for row in data) if stream else json.dumps(data).encode())
        super().__init__(body)
        self.after_read = after_read

    def geturl(self):
        return chat.ENDPOINT

    def read(self, size=-1):
        body = super().read(size)
        if self.after_read:
            self.after_read()
        return body

    def read1(self, size=-1):
        return self.read(size)


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        assert request.full_url == chat.ENDPOINT and 0 < timeout <= 90
        self.requests.append(json.loads(request.data))
        assert self.responses, "unexpected extra model request"
        return self.responses.pop(0)


def client(data=None):
    opener = Opener(Response(data or event()))
    return chat.LocalChat(opener=opener), opener


def context():
    item = {"title": "Явно выбранное дело " + "я" * 135, "time": None,
            "status": "unknown", "provenance": "unknown"}
    return {"today": {"date": "2026-10-28", "items": [dict(item) for _ in range(4)]},
            "tomorrow": {"date": "2026-10-29", "items": [dict(item) for _ in range(4)]},
            "gym_days": [1, 3, 6], "observations": {"work_hint_minutes": 40, "gaming_minutes": 20, "observed_minutes": 100}}


def history():
    return [message for i in range(3) for message in (
        {"role": "user", "content": f"Question {i}: " + "q" * 1980},
        {"role": "assistant", "content": f"Answer {i}: " + "a" * 1980})]


def test_nonstream_usage_committed_only_after_validation_and_measures_wall_clock(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(chat.time, "monotonic", lambda: clock[0])
    response = Response(event(prompt_eval_count=70, eval_count=16, total_duration="not a duration",
                              private_diagnostic="never expose"), after_read=lambda: clock.__setitem__(0, 10.125))
    opener = Opener(response)
    instance = chat.LocalChat(opener=opener)
    assert instance.last_usage is None
    assert instance.answer("Question") == "Ответ."
    assert instance.last_usage == {"input_tokens": 70, "output_tokens": 16, "elapsed_ms": 125}
    assert len(opener.requests) == 1 and response.closed
    assert "private_diagnostic" not in instance.last_usage


@pytest.mark.parametrize("value", [-1, True, False, "12", 1.5, None, [], {}, float("nan"), float("inf"), 1 << 63],
                         ids=["negative", "true", "false", "string", "float", "null", "array", "object", "nan", "infinity", "overflow"])
def test_malformed_usage_is_unknown_never_coerced_or_a_response_failure(value):
    instance, opener = client(event(prompt_eval_count=value, eval_count=value))
    assert instance.answer("Question") == "Ответ."
    assert instance.last_usage["input_tokens"] is instance.last_usage["output_tokens"] is None
    assert type(instance.last_usage["elapsed_ms"]) is int and instance.last_usage["elapsed_ms"] >= 0
    assert len(opener.requests) == 1


def test_zero_counters_are_measured_zero_and_absent_counters_are_unknown():
    instance, _ = client(event(prompt_eval_count=0, eval_count=0))
    instance.answer("Question")
    assert instance.last_usage["input_tokens"] == instance.last_usage["output_tokens"] == 0
    instance, _ = client()
    instance.answer("Question")
    assert instance.last_usage["input_tokens"] is instance.last_usage["output_tokens"] is None


def test_pathologically_nested_metric_cannot_escape_as_parser_recursion_or_commit_usage(monkeypatch):
    raw = b'{"synthetic_deep_counter":true}'
    opener = Opener(Response(raw))
    instance = chat.LocalChat(opener=opener)
    original = json.loads

    def decode(value, *args, **kwargs):
        if value == raw:
            raise RecursionError("PRIVATE parser details")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(chat.json, "loads", decode)
    with pytest.raises(chat.LocalChatUnavailable, match="unavailable") as error:
        instance.answer("Question")
    assert "PRIVATE" not in str(error.value)
    assert instance.last_usage is None and len(opener.requests) == 1


def test_stream_uses_only_final_event_usage_and_preview_cannot_observe_committed_usage():
    events = [event("Промежуточный ", done=False, done_reason=None, prompt_eval_count=999, eval_count=999),
              event("ответ.", prompt_eval_count=18, eval_count=7)]
    opener = Opener(Response(events, stream=True))
    instance = chat.LocalChat(opener=opener)
    preview = []

    def receive(delta):
        assert instance.last_usage is None
        preview.append(delta)

    assert instance.answer("Question", on_chunk=receive) == "Промежуточный ответ."
    assert "".join(preview) == "Промежуточный ответ."
    assert instance.last_usage["input_tokens"] == 18 and instance.last_usage["output_tokens"] == 7
    assert len(opener.requests) == 1


@pytest.mark.parametrize("response", [event(done_reason="length", prompt_eval_count=12, eval_count=256),
                                     event("<think>unfinished", prompt_eval_count=12, eval_count=10),
                                     event(done=False, prompt_eval_count=12, eval_count=10)])
def test_failed_answer_clears_prior_usage_and_never_retries(response):
    opener = Opener(Response(event(prompt_eval_count=10, eval_count=2)), Response(response))
    instance = chat.LocalChat(opener=opener)
    instance.answer("First")
    assert instance.last_usage is not None
    with pytest.raises(chat.LocalChatUnavailable):
        instance.answer("Second")
    assert instance.last_usage is None and len(opener.requests) == 2


def test_invalid_stream_after_final_does_not_commit_its_usage():
    response = Response([event(prompt_eval_count=20, eval_count=4), event("Unexpected trailing event")], stream=True)
    instance = chat.LocalChat(opener=Opener(response))
    with pytest.raises(chat.LocalChatUnavailable):
        instance.answer("Question", on_chunk=lambda _: None)
    assert instance.last_usage is None


def test_preview_cancellation_does_not_commit_final_or_reissue_a_request():
    response = Response([event("preview", done=False, done_reason=None), event(prompt_eval_count=20, eval_count=4)], stream=True)
    opener = Opener(response)
    instance = chat.LocalChat(opener=opener)
    with pytest.raises(chat.LocalChatUnavailable):
        instance.answer("Question", on_chunk=lambda _: False)
    assert instance.last_usage is None and len(opener.requests) == 1


def test_explicit_prewarm_resets_metadata_but_has_no_eval_prompt_or_automatic_retry():
    opener = Opener(Response(event(prompt_eval_count=10, eval_count=5)), Response(event("", done_reason="load", prompt_eval_count=0, eval_count=0)))
    instance = chat.LocalChat(opener=opener)
    instance.answer("Question")
    assert len(opener.requests) == 1
    assert instance.prewarm()
    assert instance.last_usage is None and instance.last_budget is None
    assert len(opener.requests) == 2 and opener.requests[-1]["messages"] == []
    assert opener.requests[-1]["think"] is False


def test_brief_default_is_single_request_4096_context_and_256_output_no_tools():
    instance, opener = client()
    question = "Как начать учиться сегодня?"
    instance.answer(question)
    payload = opener.requests[0]
    assert len(opener.requests) == 1
    assert payload["options"]["num_ctx"] == 4096 and payload["options"]["num_predict"] == 256
    assert payload["messages"] == [{"role": "system", "content": chat.SYSTEM}, {"role": "user", "content": question}]
    assert payload["think"] is False and "tools" not in payload
    assert instance.last_budget["prompt_characters"] == len(question) + len(chat.SYSTEM)
    assert not any("token" in key for key in instance.last_budget)


@pytest.mark.parametrize("question", ["Объясни подробно, шаг за шагом.", "Give a detailed explanation.",
                                     "Write a Python function that adds two numbers.", "Напиши код сортировки.",
                                     "Why does this fail?\n```python\nprint(1)\n```"])
def test_explicit_detail_and_code_allow_up_to_768_output_without_expanding_short_context(question):
    instance, opener = client()
    instance.answer(question)
    assert opener.requests[0]["options"]["num_predict"] == 768
    assert opener.requests[0]["options"]["num_ctx"] == 4096
    assert opener.requests[0]["messages"][-1]["content"] == question


@pytest.mark.parametrize("question", ["Кратко, без подробностей, объясни программу.", "Explain this code briefly.",
                                     "How long does it take to start?", "Что такое алгоритм?"])
def test_short_or_duration_questions_do_not_trigger_detailed_budget(question):
    instance, opener = client()
    instance.answer(question)
    assert opener.requests[0]["options"]["num_predict"] == 256


def test_plan_priority_prunes_history_without_mutating_or_corrupting_validated_sources():
    plan, records = context(), history()
    original_plan, original_history = deepcopy(plan), deepcopy(records)
    projection = chat.bounded_plan_context(plan)
    instance, opener = client()
    instance.answer("Как вернуться к моему плану?", history=records, plan_context=plan)
    messages = opener.requests[0]["messages"]
    assert messages[0]["content"] == chat.SYSTEM + projection
    assert instance.last_budget["plan_included"] is True
    assert instance.last_budget["history_characters"] < chat.MAX_HISTORY_CHARS
    assert instance.last_budget["prompt_characters"] <= instance.last_budget["prompt_char_budget"]
    assert len(messages[1:-1]) % 2 == 0
    assert messages[-2]["content"].startswith("Answer 2")
    assert plan == original_plan and records == original_history


def test_maximum_original_question_kept_verbatim_context_pruned_before_it():
    plan, records = context(), history()
    original_plan, original_history = deepcopy(plan), deepcopy(records)
    question = "  Exact original question:\n" + "в" * 7955 + "\nEND ORIGINAL   "
    assert len(question) <= 8000
    instance, opener = client()
    instance.answer(question, history=records, plan_context=plan)
    payload = opener.requests[0]
    assert payload["messages"][-1] == {"role": "user", "content": question}
    assert payload["options"]["num_ctx"] == 8192 and payload["options"]["num_predict"] == 768
    assert instance.last_budget["plan_included"] is False
    assert "план не передан из-за лимита" in payload["messages"][0]["content"]
    assert len(payload["messages"][1:-1]) % 2 == 0
    assert instance.last_budget["prompt_characters"] <= instance.last_budget["prompt_char_budget"]
    assert plan == original_plan and records == original_history


def test_invalid_context_is_validated_even_when_question_would_leave_no_context_room():
    instance, opener = client()
    invalid = {**context(), "permission": "execute PC commands"}
    with pytest.raises(ValueError):
        instance.answer("x" * 8000, plan_context=invalid)
    assert not opener.requests and instance.last_usage is None and instance.last_budget is None


@pytest.mark.parametrize("operation", ["answer", "prewarm"])
def test_real_nonstream_body_dribble_cannot_extend_answer_or_prewarm_deadline(monkeypatch, operation):
    """A real socket defeats one-shot read(N), not merely a compliant fake."""
    requests = []
    final = event("synthetic" if operation == "answer" else "", done_reason="stop" if operation == "answer" else "load")
    body = json.dumps(final).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for value in body:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(chat, "ENDPOINT", f"http://127.0.0.1:{server.server_port}/api/chat")
    instance = chat.LocalChat(timeout=1)
    started = time.perf_counter()
    try:
        if operation == "answer":
            with pytest.raises(chat.LocalChatUnavailable):
                instance.answer("Synthetic deadline test")
        else:
            assert instance.prewarm() is False
        assert time.perf_counter() - started < 2.5
        assert instance.last_usage is None and len(requests) == 1
        if operation == "prewarm":
            assert requests[0]["messages"] == []
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
