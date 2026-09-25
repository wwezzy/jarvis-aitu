import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from services import llm


@pytest.fixture(autouse=True)
def router_state(monkeypatch):
    monkeypatch.setattr(llm, "settings", replace(llm.settings, openai_api_key="offline",
        nvidia_api_key="offline", gemini_api_keys=("offline",), llm_timeout_seconds=0.15))
    monkeypatch.setattr(llm, "_provider_cooldown_until", {})
    monkeypatch.setattr(llm, "_llm_state", {})
    monkeypatch.setattr(llm, "redis", None)


async def generate(**kwargs):
    return await llm.generate_reply(text="Hi", user_id=42, user_name="Test",
        preferences=None, memory_context="", **kwargs)


@pytest.mark.parametrize("failures,expected", [
    (0, ["openai"]), (1, ["openai", "nvidia"]),
    (2, ["openai", "nvidia", "gemini"]), (3, ["openai", "nvidia", "gemini"]),
])
@pytest.mark.parametrize("failure", ["quota", "timeout", "empty", "malformed"])
async def test_provider_failure_chain(monkeypatch, failures, expected, failure):
    calls = []

    def adapter(name):
        async def fake(**kwargs):
            calls.append(name)
            if len(calls) <= failures:
                if failure == "timeout":
                    await asyncio.sleep(1)
                if failure == "empty":
                    return " ", "test"
                if failure == "malformed":
                    raise KeyError("missing choices")
                raise RuntimeError("HTTP 429")
            return "Helpful answer", "test"
        return fake

    for name in ("openai", "nvidia", "gemini"):
        monkeypatch.setattr(llm, f"_{name}_reply", adapter(name))
    answer = await generate()
    assert calls == expected
    if failures == 3:
        assert "Базовые функции" in answer
    else:
        assert answer == "Helpful answer"


async def test_circuit_skips_failed_provider_then_recovers(monkeypatch):
    primary = AsyncMock(side_effect=RuntimeError("HTTP 429"))
    fallback = AsyncMock(return_value=("ok", "test"))
    monkeypatch.setattr(llm, "_openai_reply", primary)
    monkeypatch.setattr(llm, "_nvidia_reply", fallback)
    await generate()
    await generate()
    assert primary.await_count == 1 and fallback.await_count == 2
    llm._provider_cooldown_until["openai"] = 0
    primary.side_effect = None
    primary.return_value = ("recovered", "test")
    assert await generate() == "recovered"


async def test_cancellation_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(llm, "_openai_reply", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await generate()


async def test_provider_errors_never_log_payloads(monkeypatch, caplog):
    marker = "PRIVATE_PAYLOAD_DO_NOT_LOG"
    for name in ("openai", "nvidia", "gemini"):
        monkeypatch.setattr(llm, f"_{name}_reply", AsyncMock(side_effect=RuntimeError(marker)))
    await generate()
    assert marker not in caplog.text + json.dumps(llm.get_llm_diagnostics())


async def test_audio_is_not_silently_dropped_by_text_only_adapters(monkeypatch):
    primary, fallback = AsyncMock(), AsyncMock()
    gemini = AsyncMock(return_value=("audio answer", "test"))
    monkeypatch.setattr(llm, "_openai_reply", primary)
    monkeypatch.setattr(llm, "_nvidia_reply", fallback)
    monkeypatch.setattr(llm, "_gemini_reply", gemini)
    assert await generate(file_bytes=b"fake", mime_type="audio/ogg") == "audio answer"
    primary.assert_not_awaited()
    fallback.assert_not_awaited()


async def test_history_is_bounded_and_never_uses_global_legacy_key(monkeypatch):
    redis = SimpleNamespace(get=AsyncMock(return_value=None))
    monkeypatch.setattr(llm, "redis", redis)
    assert await llm.get_chat_history(42) == []
    redis.get.assert_awaited_once_with("jarvis:chat_history:42")
    redis.get.return_value = json.dumps([{"role": "model", "text": "x"}] * 50 + [None])
    history = await llm.get_chat_history(42)
    assert len(history) <= llm.RECENT_HISTORY_MESSAGES
    assert all(row["role"] == "assistant" for row in history)


@pytest.mark.parametrize("query,field", [
    ("привет", "openai_default_model"), ("спланируй неделю", "openai_planner_model"),
    ("глубокий анализ", "openai_premium_model"),
])
def test_existing_model_tiers_are_preserved(query, field):
    assert llm._choose_openai_model(query)[0] == getattr(llm.settings, field)
