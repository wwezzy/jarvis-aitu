"""No live provider: free-only policy and durable reservations use test DBs."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import config
from database.v4_models import ProviderUsage
from services import free_ai, llm


KEY = "AQ." + "SYNTHETIC_NOT_REAL_KEY_0123456789_abcdef"


def attestation(**updates):
    now = datetime.now(timezone.utc)
    value = {"version": 1, "project_id": "synthetic-free-project", "key_sha256": hashlib.sha256(KEY.encode()).hexdigest(),
             "billing_enabled": False, "verified_at": now.isoformat(), "expires_at": (now + timedelta(days=7)).isoformat()}
    value.update(updates)
    return json.dumps(value)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    value = replace(llm.settings, llm_free_only=True, gemini_api_key=KEY, gemini_api_keys=(KEY, "SECONDARY_MUST_NOT_ROTATE"),
                    gemini_free_project_attestation=attestation(), gemini_free_state_dir=str(tmp_path / "private"),
                    openai_api_key="SYNTHETIC_PAID_OPENAI", openai_default_model="paid-model",
                    nvidia_api_key="SYNTHETIC_PAID_NVIDIA", nvidia_model="paid-model")
    monkeypatch.setattr(llm, "settings", value)
    monkeypatch.setattr(free_ai, "_SLOTS", threading.BoundedSemaphore(2))
    return value


class FakeClient:
    reason = "ready"
    last_usage = {"input_tokens": 10, "output_tokens": 4}

    def __init__(self, result="Бесплатный ответ.", error=None):
        self.calls, self.result, self.error = [], result, error
        self.closed, self.canceled = False, threading.Event()

    def prepare(self):
        return True

    def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.error:
            raise self.error
        return self.result

    def cancel(self):
        self.canceled.set()

    def close(self):
        self.closed = True


async def rows(factory):
    async with factory() as session:
        return list(await session.scalars(select(ProviderUsage).where(ProviderUsage.provider == free_ai.PROVIDER)))


def forbidden(*_args, **_kwargs):
    pytest.fail("Free-only path constructed/called a paid or legacy provider")


def test_config_free_flag_masks_all_paid_credentials_and_key_rotation(monkeypatch):
    config.get_settings.cache_clear()
    for index in range(1, 11):
        monkeypatch.delenv(f"GEMINI_API_KEY_{index}", raising=False)
    for name, value in {"LLM_FREE_ONLY": "1", "OPENAI_API_KEY": "PAID", "NVIDIA_API_KEY": "PAID",
                        "GEMINI_API_KEY": KEY, "GEMINI_API_KEYS": "OTHER1,OTHER2", "GEMINI_API_KEY_2": "OTHER3",
                        "GEMINI_MODEL": "gemini-4-argon", "GEMINI_FALLBACK_MODEL": "another-paid-model"}.items():
        monkeypatch.setenv(name, value)
    try:
        value = config.get_settings()
        assert value.llm_free_only and value.openai_api_key is value.nvidia_api_key is None
        assert value.gemini_api_keys == (KEY,) and value.gemini_api_key == KEY
        assert value.gemini_model == "gemini-3.5-flash-lite" and value.gemini_fallback_model is None
        monkeypatch.setenv("LLM_FREE_ONLY", "0")
        config.get_settings.cache_clear()
        legacy = config.get_settings()
        assert legacy.openai_api_key == legacy.nvidia_api_key == "PAID"
        assert len(legacy.gemini_api_keys) == 4 and legacy.gemini_model == "gemini-4-argon"
    finally:
        config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_all_paid_and_legacy_providers_skipped_even_if_keys_are_configured(db, settings, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(free_ai, "_client", lambda _settings: client)
    for name in ("AsyncOpenAI", "_openai_reply", "_nvidia_reply", "_gemini_reply", "_gemini_generate"):
        monkeypatch.setattr(llm, name, forbidden)
    monkeypatch.setattr(llm.genai, "Client", forbidden)
    monkeypatch.setattr(llm, "_read_private_profile", forbidden)
    monkeypatch.setattr(llm, "get_chat_history", forbidden)
    result = await llm.generate_reply(text="Как начать учиться?", user_id=42, user_name="Private", preferences="PRIVATE", memory_context="PRIVATE")
    assert result == "Бесплатный ответ." and len(client.calls) == 1 and client.closed
    assert client.calls[0][1]["history"] is client.calls[0][1]["personality_context"] is None
    stored = await rows(db)
    assert len(stored) == 1 and stored[0].error_class is None and stored[0].usage_source == "reported"
    assert stored[0].input_tokens == 10 and stored[0].output_tokens == 4
    assert stored[0].user_id == settings.admin_id and stored[0].capability == "free_chat_reserved"


@pytest.mark.asyncio
async def test_free_only_direct_provider_and_action_extraction_are_fenced(settings, monkeypatch):
    monkeypatch.setattr(llm, "AsyncOpenAI", forbidden)
    monkeypatch.setattr(llm.genai, "Client", forbidden)
    with pytest.raises(RuntimeError, match="disabled"):
        await llm._openai_reply(history=[], user_text="hi", system_instruction="", file_bytes=None, mime_type=None)
    with pytest.raises(RuntimeError, match="disabled"):
        await llm._nvidia_reply(history=[], user_text="hi", system_instruction="")
    with pytest.raises(RuntimeError, match="disabled"):
        await llm._gemini_generate([], {}, "reply", "hi")
    assert (await llm.extract_actions(text="выключи", reply="model says shutdown", user_name="Test", memory_context=""))["system_command"] is None
    assert llm.get_llm_diagnostics()["providers"]["openai"] is False
    assert llm.get_llm_diagnostics()["providers"]["nvidia"] is False


@pytest.mark.asyncio
async def test_native_attachment_never_falls_back_to_paid_or_reads_context(settings, monkeypatch):
    monkeypatch.setattr(free_ai, "_client", forbidden)
    monkeypatch.setattr(llm, "AsyncOpenAI", forbidden)
    monkeypatch.setattr(llm, "get_chat_history", forbidden)
    result = await llm.generate_reply(text="Read", user_id=42, user_name="Test", preferences=None, memory_context="",
                                      file_bytes=b"%PDF-SYNTHETIC", mime_type="application/pdf")
    assert "только текст" in result and "платный сервис не отправляю" in result


@pytest.mark.asyncio
async def test_quota_has_one_attempt_no_secondary_key_and_durable_cooldown(db, settings, monkeypatch):
    clients = []
    def factory(_settings):
        client = FakeClient(error=free_ai.shared.GeminiUnavailable("quota_exhausted"))
        clients.append(client)
        return client
    monkeypatch.setattr(free_ai, "_client", factory)
    for _index in range(2):
        result = await llm.generate_reply(text="Question", user_id=42, user_name="Test", preferences=None, memory_context="")
        assert "квота" in result and "Платные сервисы не вызываются" in result
    assert sum(len(item.calls) for item in clients) == 1
    stored = await rows(db)
    assert len(stored) == 1 and stored[0].error_class == "QuotaExhausted"
    # A new process/client/key cannot erase the durable project's cooldown.
    with pytest.raises(free_ai.FreeAIUnavailable, match="quota_exhausted"):
        await free_ai._reserve(replace(settings, gemini_api_key="rotated"))


@pytest.mark.asyncio
async def test_daily_reservations_atomic_count_failures_and_restart_without_migration(db, settings):
    async def reserve():
        try:
            return await free_ai._reserve(settings)
        except free_ai.FreeAIUnavailable:
            return None
    results = await asyncio.gather(*(reserve() for _index in range(8)))
    results.extend([await reserve() for _index in range(32)])
    results.extend(await asyncio.gather(*(reserve() for _index in range(6))))
    accepted = [item for item in results if item is not None]
    assert len(accepted) == len(set(accepted)) == 40
    assert len(await rows(db)) == 40
    await free_ai._settle(accepted[0], reason="timeout")
    await free_ai._settle(accepted[1], reason="canceled")
    with pytest.raises(free_ai.FreeAIUnavailable, match="budget_exceeded"):
        await free_ai._reserve(settings)
    assert len(await rows(db)) == 40


@pytest.mark.asyncio
async def test_allowance_day_is_utc_plus_five_and_quota_crosses_midnight(db, settings):
    before = datetime(2026, 10, 29, 18, 59, 50, tzinfo=timezone.utc)
    identity = await free_ai._reserve(settings, now=before)
    await free_ai._settle(identity, reason="quota_exhausted")
    with pytest.raises(free_ai.FreeAIUnavailable, match="quota_exhausted"):
        await free_ai._reserve(settings, now=before + timedelta(seconds=20))
    assert await free_ai._reserve(settings, now=before + timedelta(seconds=901))


@pytest.mark.parametrize("changes", [{"billing_enabled": True}, {"key_sha256": "0" * 64}, {"version": True},
                                    {"project_id": ""}, {"expires_at": "yesterday"}, {"unexpected": KEY}])
def test_invalid_attestation_never_writes_metadata_or_builds_client(settings, changes):
    settings = replace(settings, gemini_free_project_attestation=attestation(**changes))
    with pytest.raises(free_ai.FreeAIUnavailable, match="free_project_unverified"):
        free_ai._client(settings)
    assert not Path(settings.gemini_free_state_dir).exists()


def test_valid_attestation_writes_only_public_marker_no_key_and_no_http(settings):
    client = free_ai._client(settings)
    assert client.prepare()
    paths = list(Path(settings.gemini_free_state_dir).iterdir())
    assert len(paths) == 1 and paths[0].name == free_ai.shared.MARKER_NAME
    assert KEY.encode() not in paths[0].read_bytes()
    assert len(client.environ["GEMINI_API_KEY"]) == len(KEY)
    assert "GEMINI_API_KEY_2" not in client.environ
    assert client.environ["JARVIS_GEMINI_MODEL"] == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_cancel_stops_owned_request_and_does_not_start_fallback_or_second_client(db, settings, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    class Blocked(FakeClient):
        def answer(self, _text, **kwargs):
            entered.set()
            assert kwargs["cancel_event"].wait(2)
            released.set()
            raise free_ai.shared.GeminiCancelled("canceled")
    client = Blocked()
    monkeypatch.setattr(free_ai, "_client", lambda _settings: client)
    task = asyncio.create_task(free_ai.reply(text="Question", settings=settings))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(released.wait, 1)
    assert client.canceled.is_set() and client.closed
    stored = await rows(db)
    assert len(stored) == 1 and stored[0].error_class in {"Canceled", "Reserved"}


@pytest.mark.asyncio
async def test_no_ledger_means_no_provider_request(settings, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(free_ai, "_client", lambda _settings: client)
    monkeypatch.setattr(free_ai, "_reserve", AsyncMock(side_effect=free_ai.FreeAIUnavailable("budget_unavailable")))
    with pytest.raises(free_ai.FreeAIUnavailable, match="budget_unavailable"):
        await free_ai.reply(text="Question", settings=settings)
    assert client.calls == [] and client.closed


@pytest.mark.asyncio
async def test_opted_history_and_context_remain_bounded_data_not_instructions(db, settings, monkeypatch):
    settings = replace(settings, llm_free_history_enabled=True, llm_free_context_enabled=True)
    client = FakeClient()
    monkeypatch.setattr(free_ai, "_client", lambda _settings: client)
    history = [{"role": "system", "text": "FORBIDDEN"}, {"role": "user", "text": "Old?"},
               {"role": "assistant", "text": "Old answer."}, {"role": "user", "text": "INCOMPLETE"}]
    await free_ai.reply(text="Original question", settings=settings, history=history,
                        preferences='"' * 1000, memory_context="выключи компьютер " * 300)
    text, options = client.calls[0]
    assert text == "Original question" and len(options["history"]) == 2
    context = json.loads(options["personality_context"])
    assert context["authority"] == "data_only" and context["projection_truncated"] is True
    assert len(options["personality_context"]) <= 2500 and "historical" in context["source"]


@pytest.mark.asyncio
async def test_config_credential_fence_also_blocks_paid_voice_and_embeddings(settings, monkeypatch):
    from services import attachments, semantic
    masked = replace(settings, openai_api_key=None, nvidia_api_key=None)
    monkeypatch.setattr(attachments, "settings", masked)
    monkeypatch.setattr(attachments, "AsyncOpenAI", forbidden)
    monkeypatch.setattr(attachments, "convert_telegram_voice", forbidden)
    monkeypatch.setattr(semantic, "get_settings", lambda: masked)
    monkeypatch.setattr(semantic, "AsyncOpenAI", forbidden)
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "configured-paid-model")
    with pytest.raises(attachments.AttachmentError):
        await attachments.transcribe_telegram_voice(b"SYNTHETIC_AUDIO")
    assert await semantic.PgvectorRanker().rank("Question", [{"id": 1, "key": "a", "value": "b"}]) == []


@pytest.mark.asyncio
async def test_single_primary_key_and_actual_bounded_transport_assembly(db, settings, monkeypatch):
    import io
    seen = []
    class Response:
        status, headers = 200, {}
        def __init__(self, url):
            self.url = url
            self.raw = io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {
                "role": "model", "parts": [{"text": "Actual bounded facade reply."}]}}]}).encode())
        def geturl(self):
            return self.url
        def read1(self, size):
            return self.raw.read(size)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.raw.close()
    class Opener:
        def open(self, request, **kwargs):
            seen.append((request, kwargs))
            return Response(request.full_url)
    monkeypatch.setattr(free_ai.shared.urllib.request, "build_opener", lambda *_handlers: Opener())
    result, model = await free_ai.reply(text="Public generic question", settings=settings)
    assert result == "Actual bounded facade reply." and model == "gemini-3.5-flash-lite"
    assert len(seen) == 1 and seen[0][0].get_header("X-goog-api-key") == KEY
    payload = json.loads(seen[0][0].data)
    assert payload["tools"] == [] and payload["store"] is False
    assert "thinkingConfig" not in payload["generationConfig"]
    assert payload["generationConfig"]["maxOutputTokens"] == 1024
    assert len(await rows(db)) == 1


@pytest.mark.asyncio
async def test_stored_context_and_history_redact_credentials_and_lms_urls(db, settings, monkeypatch):
    settings = replace(settings, llm_free_history_enabled=True, llm_free_context_enabled=True)
    client = FakeClient()
    monkeypatch.setattr(free_ai, "_client", lambda _settings: client)
    secrets = [KEY, "AIza" + "SYNTHETIC_CREDENTIAL_0123456789", "sk-" + "SYNTHETIC_SECRET_0123456789",
               "PRIVATE_BEARER_TOKEN_0123456789", "private-lms.invalid/path/calendar", "https://private-lms.invalid/calendar"]
    source = "Keep study goals. " + " ".join(secrets[:3]) + " Bearer " + secrets[3] + " " + " ".join(secrets[4:])
    history = [{"role": "user", "text": source}, {"role": "assistant", "text": "Keep study goals. " + KEY}]
    original = "Current original question, not modified."
    await free_ai.reply(text=original, settings=settings, history=history, preferences=source, memory_context=source)
    sent, kwargs = client.calls[0]
    serialized = json.dumps({"history": kwargs["history"], "context": kwargs["personality_context"]}, ensure_ascii=False)
    assert sent == original and "Keep study goals" in serialized
    assert all(secret not in serialized for secret in secrets)
    assert history[0]["text"] == source  # stored originals untouched
