from __future__ import annotations

import asyncio
import json
import logging
import os
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
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "429",
            "resource_exhausted",
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

    client = genai.Client(api_key=settings.gemini_api_key)
    errors: list[str] = []

    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_system_prompt(user_name, preferences, memory_context),
                    response_mime_type="application/json",
                    response_json_schema=JarvisResponse.model_json_schema(),
                ),
            )
            if not response.text:
                raise ValueError("Gemini returned an empty response")

            parsed = JarvisResponse.model_validate_json(response.text)
            history.append({"role": "user", "text": user_text})
            history.append({"role": "model", "text": parsed.reply})
            await save_chat_history(user_id, history)
            return parsed.model_dump()

        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            logger.warning("Structured LLM response invalid on attempt %s: %s", attempt + 1, exc)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            logger.exception("Gemini request failed on attempt %s", attempt + 1)
            if not _is_retryable(exc):
                break

        if attempt < 2:
            await asyncio.sleep(0.6 * (2**attempt))

    debug_id = uuid.uuid4().hex[:8]
    logger.error("Jarvis LLM pipeline failed [%s]: %s", debug_id, " | ".join(errors))
    return JarvisResponse(
        reply=f"LLM-канал временно не ответил корректно. Код сбоя: {debug_id}."
    ).model_dump()
