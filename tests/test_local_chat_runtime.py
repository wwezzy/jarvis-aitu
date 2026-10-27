"""Regression coverage from an actual local Qwen3/Ollama integration probe."""
import json

import pytest

from desktop.chat import ENDPOINT, LocalChat, LocalChatUnavailable


class FakeResponse:
    status = 200

    def __init__(self, content, done_reason="stop"):
        self.content = content
        self.done_reason = done_reason

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def geturl(self):
        return ENDPOINT

    def read(self, _limit):
        return json.dumps({"done": True, "done_reason": self.done_reason,
                           "message": {"content": self.content}}).encode()


class FakeOpener:
    def __init__(self, content, done_reason="stop"):
        self.content = content
        self.done_reason = done_reason
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(json.loads(request.data))
        return FakeResponse(self.content, self.done_reason)


@pytest.mark.parametrize("content", [
    "<think>Internal draft, not a user answer.</think>\n1. Open your textbook.",
    "Internal draft without an opening tag.\n</think>\n1. Open your textbook.",
    "<think></think>\n1. Open your textbook.",
    "1. Open your textbook.",
])
def test_only_completed_final_text_reaches_the_ui_and_tts(content):
    assert LocalChat(opener=FakeOpener(content)).answer("How to begin?") == "1. Open your textbook."


@pytest.mark.parametrize("content", ["<think>unfinished", "<think>unfinished</think>",
                                    "Internal draft</think>  ", "Final<think>unclosed"])
def test_incomplete_reasoning_is_not_a_displayable_final_answer(content):
    with pytest.raises(LocalChatUnavailable, match="final answer"):
        LocalChat(opener=FakeOpener(content)).answer("How to begin?")


def test_non_thinking_default_preserves_original_request_and_has_no_tools():
    opener = FakeOpener("A short answer")
    assert LocalChat(opener=opener).answer("Original question") == "A short answer"
    payload = opener.requests[0]
    assert payload["model"] == "qwen3:4b-instruct-2507-q4_K_M"
    assert payload["messages"][1] == {"role": "user", "content": "Original question"}
    assert len(payload["messages"]) == 2
    assert payload["think"] is False and "tools" not in payload


@pytest.mark.parametrize("model", ["qwen3:4b-instruct", "gemma3:4b"])
def test_models_do_not_receive_unsupported_prompt_workarounds(model):
    opener = FakeOpener("A short answer")
    LocalChat(model, opener=opener).answer("Original question")
    assert "/no_think" not in opener.requests[0]["messages"][0]["content"]


@pytest.mark.parametrize("reason", ["length", "cancelled", "unknown"])
def test_truncated_or_cancelled_model_text_never_reaches_ui_or_tts(reason):
    with pytest.raises(LocalChatUnavailable, match="complete its answer"):
        LocalChat(opener=FakeOpener("Truncated draft without reasoning tags", reason)).answer("Question")
