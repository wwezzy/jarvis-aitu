from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _collect_gemini_api_keys() -> tuple[str, ...]:
    values: list[str] = []
    primary = os.getenv("GEMINI_API_KEY", "").strip()
    if primary:
        values.append(primary)

    packed = os.getenv("GEMINI_API_KEYS", "").strip()
    if packed:
        values.extend(part.strip() for part in packed.split(",") if part.strip())

    for index in range(1, 11):
        value = os.getenv(f"GEMINI_API_KEY_{index}", "").strip()
        if value:
            values.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return tuple(deduped)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_id: int

    openai_api_key: str | None
    openai_default_model: str
    openai_planner_model: str
    openai_premium_model: str
    openai_transcribe_model: str
    openai_reasoning_effort: str

    nvidia_api_key: str | None
    nvidia_base_url: str
    nvidia_model: str

    gemini_api_keys: tuple[str, ...]
    gemini_model: str
    gemini_fallback_model: str | None

    llm_timeout_seconds: float
    redis_url: str | None
    redis_token: str | None
    pc_agent_secret: str | None
    database_url: str
    timezone: ZoneInfo
    enable_master_schedule: bool
    webapp_url: str | None
    webapp_host: str
    webapp_port: int
    miniapp_auth_max_age_seconds: int
    miniapp_dev_mode: bool
    lms_ical_url: str | None
    lms_sync_minutes: int
    log_level: str
    llm_free_only: bool = False
    gemini_api_key: str | None = None
    gemini_free_project_attestation: str | None = None
    gemini_free_state_dir: str | None = None
    llm_free_history_enabled: bool = False
    llm_free_context_enabled: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    admin_id = int(os.getenv("ADMIN_ID", "0"))
    if admin_id <= 0:
        raise RuntimeError("ADMIN_ID is missing or invalid")

    free_only = _env_bool("LLM_FREE_ONLY")
    # Free-only is an application-wide credential fence: attachments and
    # semantic rankers also use Settings, so their paid clients cannot obtain
    # a key even if paid-provider env vars remain configured accidentally.
    openai_api_key = None if free_only else os.getenv("OPENAI_API_KEY", "").strip() or None
    nvidia_api_key = None if free_only else os.getenv("NVIDIA_API_KEY", "").strip() or None
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    gemini_api_keys = ((gemini_api_key,) if gemini_api_key else ()) if free_only else _collect_gemini_api_keys()

    webapp_url = os.getenv("WEBAPP_URL", "").strip() or None
    fallback_model = None if free_only else os.getenv("GEMINI_FALLBACK_MODEL", "").strip() or None

    return Settings(
        bot_token=bot_token,
        admin_id=admin_id,
        openai_api_key=openai_api_key,
        openai_default_model=os.getenv("OPENAI_DEFAULT_MODEL", "").strip(),
        openai_planner_model=os.getenv("OPENAI_PLANNER_MODEL", os.getenv("OPENAI_DEFAULT_MODEL", "")).strip(),
        openai_premium_model=os.getenv("OPENAI_PREMIUM_MODEL", os.getenv("OPENAI_PLANNER_MODEL", os.getenv("OPENAI_DEFAULT_MODEL", ""))).strip(),
        openai_transcribe_model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "").strip(),
        openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low").strip(),
        nvidia_api_key=nvidia_api_key,
        nvidia_base_url=os.getenv(
            "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ).strip().rstrip("/"),
        nvidia_model=os.getenv(
            "NVIDIA_MODEL", ""
        ).strip(),
        gemini_api_keys=gemini_api_keys,
        gemini_model="gemini-3.5-flash-lite" if free_only else os.getenv("GEMINI_MODEL", "").strip(),
        gemini_fallback_model=fallback_model,
        llm_timeout_seconds=max(8.0, float(os.getenv("LLM_TIMEOUT_SECONDS", "35"))),
        redis_url=os.getenv("UPSTASH_REDIS_REST_URL", "").strip() or None,
        redis_token=os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip() or None,
        pc_agent_secret=os.getenv("PC_AGENT_SECRET", "").strip() or None,
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///jarvis.db").strip(),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty").strip()),
        enable_master_schedule=_env_bool("ENABLE_MASTER_SCHEDULE", True),
        webapp_url=webapp_url.rstrip("/") if webapp_url else None,
        webapp_host=os.getenv("HOST", "0.0.0.0").strip(),
        webapp_port=int(os.getenv("PORT", "8080")),
        miniapp_auth_max_age_seconds=int(os.getenv("MINIAPP_AUTH_MAX_AGE", "86400")),
        miniapp_dev_mode=_env_bool("MINIAPP_DEV_MODE", False),
        lms_ical_url=os.getenv("LMS_ICAL_URL", "").strip() or None,
        lms_sync_minutes=max(5, int(os.getenv("LMS_SYNC_MINUTES", "15"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip(),
        llm_free_only=free_only,
        gemini_api_key=gemini_api_key,
        gemini_free_project_attestation=os.getenv("JARVIS_GEMINI_FREE_PROJECT_ATTESTATION", "").strip() or None,
        gemini_free_state_dir=os.getenv("JARVIS_GEMINI_FREE_STATE_DIR", "").strip() or None,
        llm_free_history_enabled=_env_bool("LLM_FREE_HISTORY_ENABLED"),
        llm_free_context_enabled=_env_bool("LLM_FREE_CONTEXT_ENABLED"),
    )
