from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from services import llm
from services.schemas import JarvisActions


@pytest.fixture
def client(monkeypatch):
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text="reply")),
                                  parse=AsyncMock(return_value=SimpleNamespace(output_parsed=JarvisActions()))),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="reply"))])))),
    )
    context = AsyncMock()
    context.__aenter__.return_value = client
    constructor = Mock(return_value=context)
    monkeypatch.setattr(llm, "AsyncOpenAI", constructor)
    monkeypatch.setattr(llm, "settings", replace(llm.settings, openai_api_key="offline", nvidia_api_key="offline"))
    return client, constructor, context


async def test_openai_uses_async_response_and_closes_client(client):
    fake, constructor, context = client
    assert (await llm._openai_reply(history=[], user_text="hi", system_instruction="test", file_bytes=None, mime_type=None))[0] == "reply"
    assert constructor.call_args.kwargs["max_retries"] == 0
    fake.responses.create.assert_awaited_once()
    context.__aexit__.assert_awaited_once()


async def test_nvidia_uses_compatible_chat_and_closes_client(client):
    fake, constructor, context = client
    assert (await llm._nvidia_reply(history=[], user_text="hi", system_instruction="test"))[0] == "reply"
    assert constructor.call_args.kwargs["base_url"] == llm.settings.nvidia_base_url
    assert constructor.call_args.kwargs["max_retries"] == 0
    fake.chat.completions.create.assert_awaited_once()
    context.__aexit__.assert_awaited_once()


async def test_structured_extraction_failure_is_empty_and_redacted(client, monkeypatch, caplog):
    fake, _, context = client
    monkeypatch.setattr(llm, "settings", replace(llm.settings, gemini_api_keys=()))
    monkeypatch.setattr(llm, "_provider_cooldown_until", {})
    fake.responses.parse.side_effect = RuntimeError("PRIVATE_SCHEMA_PAYLOAD")
    actions = await llm.extract_actions(text="hi", reply="reply", user_name="test", memory_context="")
    assert actions == JarvisActions().model_dump()
    assert "PRIVATE_SCHEMA_PAYLOAD" not in caplog.text
    context.__aexit__.assert_awaited_once()


async def test_gemini_pool_rotates_keys_and_closes_clients(monkeypatch):
    calls = []
    contexts = []

    def constructor(api_key):
        calls.append(api_key)
        generate = AsyncMock(return_value=SimpleNamespace(text="reply"))
        if api_key == "first":
            generate.side_effect = RuntimeError("HTTP 429")
        context = AsyncMock()
        context.__aenter__.return_value = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        contexts.append(context)
        return SimpleNamespace(aio=context)

    monkeypatch.setattr(llm.genai, "Client", constructor)
    monkeypatch.setattr(llm, "settings", replace(llm.settings, gemini_api_keys=("first", "second")))
    result = await llm._gemini_reply(history=[], user_text="hi", system_instruction="test", file_bytes=None, mime_type=None)
    assert result[0] == "reply" and calls == ["first", "second"]
    assert all(context.__aexit__.await_count == 1 for context in contexts)
