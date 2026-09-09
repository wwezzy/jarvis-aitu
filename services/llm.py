from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import ValidationError
from upstash_redis.asyncio import Redis

from config import get_settings
from services.schemas import JarvisResponse

logger = logging.getLogger(__name__)
settings = get_settings()

RECENT_HISTORY_MESSAGES = 24
HISTORY_TTL_SECONDS = 60 * 60 * 24 * 30
LEGACY_HISTORY_KEY = "jarvis_chat_history"

redis: Redis | None = None
if settings.redis_url and settings.redis_token:
    redis = Redis(url=settings.redis_url, token=settings.redis_token)

_key_cooldown_until: dict[int, float] = {}
_llm_state: dict[str, object | None] = {
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "last_debug_id": None,
    "last_mode": None,
    "last_model": None,
    "last_key_slot": None,
}


def _history_key(user_id: int) -> str:
    return f"jarvis:chat_history:{user_id}"


def _read_prompt_file() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "jarvis_system.md"
    return path.read_text(encoding="utf-8")


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
        if not history_data:
            history_data = await redis.get(LEGACY_HISTORY_KEY)
        parsed = json.loads(history_data) if history_data else []
        return parsed if isinstance(parsed, list) else []
    except Exception:
        logger.exception("Failed to read short-term chat history")
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
        logger.exception("Failed to save short-term chat history")


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
    return "\n".join(pieces)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "429",
            "resource_exhausted",
            "rate limit",
            "quota",
            "500",
            "502",
            "503",
            "504",
            "unavailable",
            "timeout",
            "timed out",
            "connection reset",
            "temporarily unavailable",
        )
    )


def _cooldown_seconds(exc: Exception) -> float:
    text = str(exc).lower()
    if any(token in text for token in ("429", "resource_exhausted", "rate limit", "quota")):
        return 90.0
    if any(token in text for token in ("500", "502", "503", "504", "unavailable")):
        return 20.0
    return 10.0


def _key_order() -> list[tuple[int, str]]:
    now = time.monotonic()
    available = [
        (index, key)
        for index, key in enumerate(settings.gemini_api_keys)
        if _key_cooldown_until.get(index, 0.0) <= now
    ]
    cooling = [
        (index, key)
        for index, key in enumerate(settings.gemini_api_keys)
        if _key_cooldown_until.get(index, 0.0) > now
    ]
    if available:
        return available + cooling
    cooling.sort(key=lambda item: _key_cooldown_until.get(item[0], 0.0))
    return cooling[:1]


def _mark_success(*, mode: str, model: str, key_slot: int) -> None:
    _llm_state.update(
        {
            "last_success_at": datetime.now(settings.timezone).isoformat(),
            "last_error": None,
            "last_debug_id": None,
            "last_mode": mode,
            "last_model": model,
            "last_key_slot": key_slot + 1,
        }
    )
    _key_cooldown_until.pop(key_slot, None)


def _mark_failure(exc: Exception, *, debug_id: str | None = None) -> None:
    _llm_state.update(
        {
            "last_error_at": datetime.now(settings.timezone).isoformat(),
            "last_error": f"{type(exc).__name__}: {exc}"[:1500],
            "last_debug_id": debug_id,
        }
    )


def get_llm_diagnostics() -> dict:
    now = time.monotonic()
    cooling = [
        index + 1
        for index in range(len(settings.gemini_api_keys))
        if _key_cooldown_until.get(index, 0.0) > now
    ]
    return {
        "configured_keys": len(settings.gemini_api_keys),
        "primary_model": settings.gemini_model,
        "fallback_model": settings.gemini_fallback_model,
        "timeout_seconds": settings.llm_timeout_seconds,
        "cooling_key_slots": cooling,
        **_llm_state,
    }


async def _generate(
    *,
    api_key: str,
    model: str,
    contents: list[types.Content],
    system_instruction: str,
    structured: bool,
):
    client = genai.Client(api_key=api_key)
    if structured:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_json_schema=JarvisResponse.model_json_schema(),
        )
    else:
        config = types.GenerateContentConfig(
            system_instruction=(
                system_instruction
                + "\n\nFALLBACK MODE: Return a normal helpful plain-text answer only. "
                "Do not output JSON. Do not claim that structured actions were saved."
            )
        )

    return await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        ),
        timeout=settings.llm_timeout_seconds,
    )


async def _structured_response(
    *,
    contents: list[types.Content],
    system_instruction: str,
) -> tuple[JarvisResponse | None, list[str]]:
    errors: list[str] = []

    for key_slot, api_key in _key_order():
        try:
            response = await _generate(
                api_key=api_key,
                model=settings.gemini_model,
                contents=contents,
                system_instruction=system_instruction,
                structured=True,
            )
            if not response.text:
                raise ValueError("Gemini returned an empty response")

            parsed = JarvisResponse.model_validate_json(response.text)
            _mark_success(mode="structured", model=settings.gemini_model, key_slot=key_slot)
            return parsed, errors

        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"key#{key_slot + 1} structured {type(exc).__name__}: {exc}")
            logger.warning(
                "Structured LLM response invalid on key slot %s: %s",
                key_slot + 1,
                exc,
            )
            break
        except Exception as exc:
            errors.append(f"key#{key_slot + 1} structured {type(exc).__name__}: {exc}")
            logger.exception("Gemini structured request failed on key slot %s", key_slot + 1)
            _mark_failure(exc)
            if _is_retryable(exc):
                _key_cooldown_until[key_slot] = time.monotonic() + _cooldown_seconds(exc)
            continue

    return None, errors


async def _plain_text_fallback(
    *,
    contents: list[types.Content],
    system_instruction: str,
) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    models: list[str] = [settings.gemini_model]
    if settings.gemini_fallback_model and settings.gemini_fallback_model not in models:
        models.append(settings.gemini_fallback_model)

    for model in models:
        for key_slot, api_key in _key_order():
            try:
                response = await _generate(
                    api_key=api_key,
                    model=model,
                    contents=contents,
                    system_instruction=system_instruction,
                    structured=False,
                )
                text = (response.text or "").strip()
                if not text:
                    raise ValueError("Gemini returned an empty plain-text response")
                _mark_success(mode="plain_fallback", model=model, key_slot=key_slot)
                return text, errors
            except Exception as exc:
                errors.append(f"key#{key_slot + 1} {model} plain {type(exc).__name__}: {exc}")
                logger.exception(
                    "Gemini plain fallback failed on key slot %s model %s",
                    key_slot + 1,
                    model,
                )
                _mark_failure(exc)
                if _is_retryable(exc):
                    _key_cooldown_until[key_slot] = time.monotonic() + _cooldown_seconds(exc)
                continue

    return None, errors


async def parse_user_message(
    *,
    text: str,
    user_id: int,
    user_name: str,
    preferences: str | None,
    memory_context: str,
    file_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> dict:
    user_text = text or "Проанализируй вложение и выполни запрос пользователя."
    history = await get_chat_history(user_id)
    contents: list[types.Content] = []

    for item in history:
        role = item.get("role")
        body = item.get("text")
        if role in {"user", "model"} and isinstance(body, str) and body:
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=body)]))

    current_parts: list[types.Part] = []
    if file_bytes and mime_type:
        current_parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    current_parts.append(types.Part.from_text(text=user_text))
    contents.append(types.Content(role="user", parts=current_parts))

    system_instruction = _system_prompt(user_name, preferences, memory_context)

    parsed, structured_errors = await _structured_response(
        contents=contents,
        system_instruction=system_instruction,
    )
    if parsed is not None:
        history.append({"role": "user", "text": user_text})
        history.append({"role": "model", "text": parsed.reply})
        await save_chat_history(user_id, history)
        return parsed.model_dump()

    plain_text, plain_errors = await _plain_text_fallback(
        contents=contents,
        system_instruction=system_instruction,
    )
    if plain_text:
        history.append({"role": "user", "text": user_text})
        history.append({"role": "model", "text": plain_text})
        await save_chat_history(user_id, history)
        return JarvisResponse(reply=plain_text).model_dump()

    debug_id = uuid.uuid4().hex[:8]
    all_errors = structured_errors + plain_errors
    logger.error("Jarvis LLM pipeline failed [%s]: %s", debug_id, " | ".join(all_errors))
    final_exc = RuntimeError(" | ".join(all_errors[-3:]) or "unknown LLM failure")
    _mark_failure(final_exc, debug_id=debug_id)

    return JarvisResponse(
        reply=(
            "Gemini сейчас недоступен даже через резервный канал. "
            f"Код диагностики: {debug_id}. "
            "Расписание, дедлайны, GTG и журнал тренировок продолжают работать без ИИ."
        )
    ).model_dump()
