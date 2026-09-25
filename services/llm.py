from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types as gemini_types
from openai import AsyncOpenAI
from pydantic import ValidationError
from services.redis_backend import create_redis

from config import get_settings
from services.attachments import UnsupportedAttachment, openai_file_supported
from services.schemas import JarvisActions
from services.telemetry import observe, last_call

logger = logging.getLogger(__name__)
settings = get_settings()

RECENT_HISTORY_MESSAGES = 20
HISTORY_TTL_SECONDS = 60 * 60 * 24 * 30

redis = create_redis()

_provider_cooldown_until: dict[str, float] = {}
_llm_state: dict[str, object | None] = {
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "last_debug_id": None,
    "last_provider": None,
    "last_model": None,
    "last_mode": None,
}


def _history_key(user_id: int) -> str:
    return f"jarvis:chat_history:{user_id}"


def _read_prompt_file(name: str = "jarvis_system.md") -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning("Prompt file missing: %s", path)
        return ""


def _read_private_profile() -> str:
    configured = os.getenv("JARVIS_PROFILE_FILE", "profile.local.md").strip()
    if not configured:
        return ""
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError:
        logger.exception("Could not read private Jarvis profile file")
        return ""


async def get_chat_history(user_id: int) -> list[dict]:
    if redis is None:
        return []
    try:
        history_data = await redis.get(_history_key(user_id))
        parsed = json.loads(history_data) if history_data else []
        if not isinstance(parsed, list):
            return []
        return [dict(item, role="assistant" if item.get("role") == "model" else item.get("role"))
                for item in parsed[-RECENT_HISTORY_MESSAGES:] if isinstance(item, dict)]
    except Exception:
        logger.warning("Failed to read short-term chat history")
        return []


async def save_chat_history(user_id: int, history: list[dict]) -> None:
    if redis is None:
        return
    try:
        trimmed = history[-RECENT_HISTORY_MESSAGES:]
        await redis.set(
            _history_key(user_id),
            json.dumps(trimmed, ensure_ascii=False),
            ex=HISTORY_TTL_SECONDS,
        )
    except Exception:
        logger.warning("Failed to save short-term chat history")


def _system_prompt(user_name: str, preferences: str | None, memory_context: str) -> str:
    now = datetime.now(settings.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
    private_profile = _read_private_profile()
    pieces = [
        _read_prompt_file(),
        f"\nCURRENT USER: {user_name}\nCURRENT LOCAL TIME: {now}",
        f"\nLEGACY USER PREFERENCES:\n{preferences or 'None'}",
        f"\nDURABLE DATABASE CONTEXT:\n{memory_context}",
    ]
    if private_profile:
        pieces.append(f"\nPRIVATE LOCAL PROFILE:\n{private_profile}")
    return "\n".join(piece for piece in pieces if piece)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "429",
            "rate limit",
            "quota",
            "500",
            "502",
            "503",
            "504",
            "unavailable",
            "timeout",
            "timed out",
            "connection",
            "overloaded",
        )
    )


def _cooldown_seconds(exc: Exception) -> float:
    text = str(exc).lower()
    if any(token in text for token in ("429", "rate limit", "quota")):
        return 90.0
    if any(token in text for token in ("500", "502", "503", "504", "unavailable", "overloaded")):
        return 25.0
    return 10.0


def _provider_available(provider: str) -> bool:
    return _provider_cooldown_until.get(provider, 0.0) <= time.monotonic()


def _mark_success(provider: str, model: str, mode: str) -> None:
    _provider_cooldown_until.pop(provider, None)
    _llm_state.update(
        {
            "last_success_at": datetime.now(settings.timezone).isoformat(),
            "last_error": None,
            "last_debug_id": None,
            "last_provider": provider,
            "last_model": model,
            "last_mode": mode,
        }
    )


def _mark_failure(provider: str, exc: Exception, *, debug_id: str | None = None) -> None:
    _llm_state.update(
        {
            "last_error_at": datetime.now(settings.timezone).isoformat(),
            "last_error": f"{provider}: {type(exc).__name__}",
            "last_debug_id": debug_id,
        }
    )
    if not isinstance(exc, ValidationError):
        _provider_cooldown_until[provider] = time.monotonic() + _cooldown_seconds(exc)


def _choose_openai_model(text: str) -> tuple[str, str]:
    lowered = (text or "").lower()
    premium_terms = (
        "глубокий анализ",
        "проанализируй весь",
        "архитектура проекта",
        "стратегия на семестр",
        "deep analysis",
    )
    planner_terms = (
        "распредели",
        "спланируй",
        "план на неделю",
        "как успеть",
        "приоритет",
        "дедлайн",
        "проанализируй",
        "сравни варианты",
        "планирование",
    )
    if any(term in lowered for term in premium_terms):
        return settings.openai_premium_model, "medium"
    if any(term in lowered for term in planner_terms) or len(text or "") > 700:
        return settings.openai_planner_model, "medium"
    return settings.openai_default_model, settings.openai_reasoning_effort


def _openai_input(
    history: list[dict],
    user_text: str,
    file_bytes: bytes | None,
    mime_type: str | None,
    filename: str | None,
    attachments: list | None = None,
) -> list[dict]:
    if attachments:
        result = _openai_input(history, user_text, None, None, None)
        content = [{"type": "input_text", "text": user_text}]
        for file in attachments:
            content.extend(_openai_input([], "", file.data, file.mime_type, file.filename)[-1]["content"][1:])
        result[-1]["content"] = content
        return result
    items: list[dict] = []
    for item in history[-RECENT_HISTORY_MESSAGES:]:
        role = item.get("role")
        body = item.get("text")
        if role in {"user", "assistant"} and isinstance(body, str) and body:
            items.append({"role": role, "content": body})

    if not file_bytes:
        items.append({"role": "user", "content": user_text})
        return items

    mime = mime_type or "application/octet-stream"
    encoded = base64.b64encode(file_bytes).decode("ascii")
    content: list[dict] = [{"type": "input_text", "text": user_text}]

    if mime.startswith("image/"):
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime};base64,{encoded}",
            }
        )
    elif openai_file_supported(mime, filename):
        file_item: dict = {
            "type": "input_file",
            "filename": filename or "attachment",
            "file_data": f"data:{mime};base64,{encoded}",
        }
        if mime == "application/pdf":
            file_item["detail"] = "low"
        content.append(file_item)
    else:
        raise UnsupportedAttachment(
            f"OpenAI file input does not support {mime or filename or 'this attachment'}"
        )

    items.append({"role": "user", "content": content})
    return items


async def _openai_reply(
    *,
    history: list[dict],
    user_text: str,
    system_instruction: str,
    file_bytes: bytes | None,
    mime_type: str | None,
    filename: str | None = None,
    attachments: list | None = None,
) -> tuple[str, str]:
    if not settings.openai_api_key or not settings.openai_default_model:
        raise RuntimeError("OpenAI is not configured")

    model, effort = _choose_openai_model(user_text)
    input_items = _openai_input(history, user_text, file_bytes, mime_type, filename, attachments)
    async with AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds,
                           max_retries=0) as client:
        response = await observe("openai", model, "reply", asyncio.wait_for(client.responses.create(
            model=model, instructions=system_instruction, input=input_items,
            reasoning={"effort": effort},
        ), timeout=settings.llm_timeout_seconds), input_text=system_instruction + user_text)
    text = (response.output_text or "").strip()
    if not text:
        raise ValueError("OpenAI returned an empty response")
    return text, model


async def _nvidia_reply(
    *,
    history: list[dict],
    user_text: str,
    system_instruction: str,
) -> tuple[str, str]:
    if not settings.nvidia_api_key or not settings.nvidia_model:
        raise RuntimeError("NVIDIA is not configured")

    messages: list[dict] = [{"role": "system", "content": system_instruction}]
    for item in history[-RECENT_HISTORY_MESSAGES:]:
        role = item.get("role")
        body = item.get("text")
        if role in {"user", "assistant"} and isinstance(body, str) and body:
            messages.append({"role": role, "content": body})
    messages.append({"role": "user", "content": user_text})

    async with AsyncOpenAI(api_key=settings.nvidia_api_key, base_url=settings.nvidia_base_url,
                           timeout=settings.llm_timeout_seconds, max_retries=0) as client:
        response = await observe("nvidia", settings.nvidia_model, "reply", asyncio.wait_for(client.chat.completions.create(
            model=settings.nvidia_model, messages=messages, temperature=0.3, max_tokens=2200,
        ), timeout=settings.llm_timeout_seconds), input_text=system_instruction + user_text)
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("NVIDIA returned an empty response")
    return text, settings.nvidia_model


async def _gemini_generate(contents, config, capability, input_text):
    last_exc = None
    models = list(dict.fromkeys(m for m in (settings.gemini_model, settings.gemini_fallback_model) if m))
    for model in models:
        for index, api_key in enumerate(settings.gemini_api_keys):
            circuit = f"gemini:{index}:{model}"
            if not _provider_available(circuit):
                continue
            try:
                client = genai.Client(api_key=api_key)
                async with client.aio as async_client:
                    response = await observe("gemini", model, capability, asyncio.wait_for(
                        async_client.models.generate_content(model=model, contents=contents, config=config),
                        timeout=settings.llm_timeout_seconds), input_text=input_text)
                if not response.text or not response.text.strip():
                    raise ValueError("Gemini returned an empty response")
                _provider_cooldown_until.pop(circuit, None)
                return response, model
            except Exception as exc:
                last_exc = exc
                _provider_cooldown_until[circuit] = time.monotonic() + _cooldown_seconds(exc)
                logger.warning("Gemini request failed error=%s", type(exc).__name__)
    raise last_exc or RuntimeError("No Gemini key/model available")


async def _gemini_reply(*, history, user_text, system_instruction, file_bytes, mime_type, attachments=None):
    contents = []
    for item in history[-RECENT_HISTORY_MESSAGES:]:
        role, body = item.get("role"), item.get("text")
        if role in {"user", "assistant"} and isinstance(body, str) and body:
            contents.append(gemini_types.Content(role="model" if role == "assistant" else "user",
                parts=[gemini_types.Part.from_text(text=body)]))
    parts = []
    for file in attachments or []:
        if file.mime_type != "application/pdf" and not file.mime_type.startswith("image/"):
            raise UnsupportedAttachment("Gemini does not support this native document")
        parts.append(gemini_types.Part.from_bytes(data=file.data, mime_type=file.mime_type))
    if file_bytes and mime_type:
        parts.append(gemini_types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    parts.append(gemini_types.Part.from_text(text=user_text))
    contents.append(gemini_types.Content(role="user", parts=parts))
    response, model = await _gemini_generate(contents,
        gemini_types.GenerateContentConfig(system_instruction=system_instruction), "reply", system_instruction + user_text)
    return response.text.strip(), model


async def generate_reply(
    *,
    text: str,
    user_id: int,
    user_name: str,
    preferences: str | None,
    memory_context: str,
    file_bytes: bytes | None = None,
    mime_type: str | None = None,
    filename: str | None = None,
    attachments: list | None = None,
) -> str:
    user_text = text or "Проанализируй вложение и выполни запрос пользователя."
    history = await get_chat_history(user_id)
    system_instruction = _system_prompt(user_name, preferences, memory_context)
    errors: list[str] = []

    providers = ("openai", "nvidia", "gemini")
    deadline = time.monotonic() + settings.llm_timeout_seconds * 3
    for provider in providers:
        if not _provider_available(provider):
            continue
        if time.monotonic() >= deadline:
            break
        try:
            if provider == "openai" and settings.openai_api_key and settings.openai_default_model and (
                not file_bytes
                or (mime_type or "").startswith("image/")
                or openai_file_supported(mime_type, filename)
            ):
                reply, model = await asyncio.wait_for(_openai_reply(
                    history=history,
                    user_text=user_text,
                    system_instruction=system_instruction,
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    filename=filename,
                    attachments=attachments,
                ), timeout=min(settings.llm_timeout_seconds, max(0, deadline - time.monotonic())))
            elif provider == "nvidia" and settings.nvidia_api_key and settings.nvidia_model and not file_bytes and not attachments:
                reply, model = await asyncio.wait_for(_nvidia_reply(
                    history=history,
                    user_text=user_text,
                    system_instruction=system_instruction,
                ), timeout=min(settings.llm_timeout_seconds, max(0, deadline - time.monotonic())))
            elif provider == "gemini" and settings.gemini_api_keys and settings.gemini_model:
                reply, model = await asyncio.wait_for(_gemini_reply(
                    history=history,
                    user_text=user_text,
                    system_instruction=system_instruction,
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    attachments=attachments,
                ), timeout=min(settings.llm_timeout_seconds, max(0, deadline - time.monotonic())))
            else:
                continue

            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Empty reply")
            _mark_success(provider, model, "reply")
            history.append({"role": "user", "text": user_text})
            history.append({"role": "assistant", "text": reply})
            await save_chat_history(user_id, history)
            return reply
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}")
            logger.warning("Reply provider failed provider=%s error=%s", provider, type(exc).__name__)
            _mark_failure(provider, exc)

    debug_id = uuid.uuid4().hex[:8]
    final_exc = RuntimeError(" | ".join(errors[-4:]) or "no configured AI provider available")
    _mark_failure("all", final_exc, debug_id=debug_id)
    logger.error("Jarvis AI reply pipeline failed [%s]: %s", debug_id, " | ".join(errors))
    return (
        "Сейчас AI-каналы не ответили. Базовые функции Jarvis продолжают работать: "
        f"расписание, дедлайны, тренировки и напоминания. Код: {debug_id}."
    )


async def _extract_actions_impl(
    *,
    text: str,
    reply: str,
    user_name: str,
    memory_context: str,
) -> dict:
    """Best-effort side effect extraction. Failure must never suppress the user reply."""
    user_text = text or ""
    instruction = (
        _read_prompt_file("action_extractor.md")
        or "Extract only durable actions explicitly supported by the user message. Never invent dates."
    )
    current_time = datetime.now(settings.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
    extraction_input = (
        f"CURRENT USER: {user_name}\nCURRENT LOCAL TIME: {current_time}\n\n"
        f"DURABLE CONTEXT:\n{memory_context}\n\n"
        f"USER MESSAGE:\n{user_text}\n\n"
        f"ASSISTANT REPLY:\n{reply}"
    )

    if settings.openai_api_key and settings.openai_default_model and _provider_available("openai"):
        try:
            async with AsyncOpenAI(api_key=settings.openai_api_key,
                                   timeout=settings.llm_timeout_seconds, max_retries=0) as client:
                response = await observe("openai", settings.openai_default_model, "extraction", asyncio.wait_for(client.responses.parse(
                    model=settings.openai_default_model,
                    input=[{"role": "developer", "content": instruction},
                           {"role": "user", "content": extraction_input}],
                    text_format=JarvisActions,
                ), timeout=settings.llm_timeout_seconds), input_text=extraction_input)
            parsed = response.output_parsed
            if parsed is not None:
                _mark_success("openai", settings.openai_default_model, "action_extraction")
                return parsed.model_dump()
        except Exception as exc:
            logger.warning("OpenAI action extraction failed: %s", type(exc).__name__)
            _mark_failure("openai", exc)

    if settings.gemini_api_keys and settings.gemini_model and _provider_available("gemini"):
        try:
            response, model = await _gemini_generate(extraction_input,
                gemini_types.GenerateContentConfig(system_instruction=instruction,
                    response_mime_type="application/json", response_json_schema=JarvisActions.model_json_schema()),
                "extraction", extraction_input)
            parsed = JarvisActions.model_validate_json(response.text)
            _mark_success("gemini", model, "action_extraction")
            return parsed.model_dump()
        except Exception as exc:
            logger.warning("Gemini action extraction failed error=%s", type(exc).__name__)
            _mark_failure("gemini", exc)

    return JarvisActions().model_dump()


async def extract_actions(**kwargs) -> dict:
    try:
        async with asyncio.timeout(settings.llm_timeout_seconds * 2):
            return await _extract_actions_impl(**kwargs)
    except Exception as exc:
        logger.warning("Action extraction failed error=%s", type(exc).__name__)
        return JarvisActions().model_dump()


def get_llm_diagnostics() -> dict:
    now = time.monotonic()
    cooling = {
        provider: max(0, int(until - now))
        for provider, until in _provider_cooldown_until.items()
        if until > now
    }
    return {
        "providers": {
            "openai": bool(settings.openai_api_key),
            "nvidia": bool(settings.nvidia_api_key),
            "gemini": bool(settings.gemini_api_keys),
        },
        "models": {
            "openai_default": settings.openai_default_model,
            "openai_planner": settings.openai_planner_model,
            "openai_premium": settings.openai_premium_model,
            "nvidia": settings.nvidia_model,
            "gemini": settings.gemini_model,
        },
        "cooldowns_seconds": cooling,
        "timeout_seconds": settings.llm_timeout_seconds,
        "last_call": dict(last_call),
        **_llm_state,
    }
