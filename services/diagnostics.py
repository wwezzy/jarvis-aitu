import asyncio
import os

from sqlalchemy import text, select, func
from database.v4_models import NotificationDelivery

from config import get_settings
from database import engine
from services.assignments import assignment_counts, lms_status
from services.llm import get_llm_diagnostics, redis
from services.pc_agent import pc_status
from services.redis_backend import health
from services.telemetry import daily_usage


async def diagnostics(user_id, scheduler=None, *, factory=None, lms_reader=None, counts_reader=None):
    settings = get_settings()
    data = {"version": "4", "commit": os.getenv("APP_COMMIT") or os.getenv("RENDER_GIT_COMMIT") or "unknown",
            "database": {"type": "PostgreSQL" if engine.DATABASE_URL.startswith("postgresql") else "SQLite"},
            "scheduler": {"running": bool(getattr(scheduler, "running", False)), "jobs": len(scheduler.get_jobs()) if scheduler else 0},
            "providers": get_llm_diagnostics(),
            "transcription": {"configured": bool(settings.openai_api_key and settings.openai_transcribe_model), "model": settings.openai_transcribe_model}}
    try:
        async with asyncio.timeout(5):
            async with (factory or engine.async_session_factory)() as db:
                await db.execute(text("SELECT 1"))
                counts = await db.execute(select(NotificationDelivery.status, func.count()).where(
                    NotificationDelivery.user_id == user_id).group_by(NotificationDelivery.status))
                data["notifications"] = dict(counts.all())
            lms = await (lms_reader or lms_status)(user_id)
            # Legacy rows may contain an exception URL; never expose that text.
            data["lms"] = {k: lms.get(k) for k in ("configured", "last_attempt_at", "last_success_at", "last_created", "last_updated")}
            data["lms"]["last_sync_failed"] = bool(lms.get("last_error"))
            data["assignments"] = await (counts_reader or assignment_counts)(user_id)
            data["database"]["status"] = "reachable"
    except Exception as exc:
        data["database"]["status"] = f"unavailable ({type(exc).__name__})"
    try:
        async with asyncio.timeout(3):
            data["usage"] = await daily_usage(user_id)
    except Exception:
        data["usage"] = {"status": "unavailable"}
    try:
        async with asyncio.timeout(8):
            data["redis"] = await health(redis)
            data["pc"] = await pc_status(redis, user_id, settings.pc_agent_secret)
    except TimeoutError:
        data["redis"] = {"healthy": False, "error": "TimeoutError"}
        data["pc"] = {"online": False, "state": "unavailable"}
    return data
