from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from aiohttp import web

from config import get_settings
from services.gtg import add_gtg_set, gtg_stats
from services.memory import list_memory_facts
from services.reflections import latest_reflection, upsert_reflection
from services.schedule import today_schedule
from services.telegram_auth import TelegramAuthError, TelegramWebAppUser, validate_init_data
from services.users import ensure_user
from services.progression import double_progression_recommendation
from services.workouts import log_workout, recent_workouts

logger = logging.getLogger(__name__)
settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _extract_init_data(request: web.Request) -> str:
    return request.headers.get("X-Telegram-Init-Data", "").strip()


async def _authorized_user(request: web.Request) -> TelegramWebAppUser:
    init_data = _extract_init_data(request)
    if not init_data and settings.miniapp_dev_mode:
        return TelegramWebAppUser(id=settings.admin_id, first_name="Jarvis Dev")

    user = validate_init_data(
        init_data,
        settings.bot_token,
        max_age_seconds=settings.miniapp_auth_max_age_seconds,
    )
    if user.id != settings.admin_id:
        raise web.HTTPForbidden(text="Jarvis is in private mode")
    await ensure_user(user.id, " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "User")
    return user


async def _json_body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="JSON object expected")
    return payload


def _progression_for(workout: dict | None) -> list[dict]:
    if not workout:
        return []
    grouped: dict[str, list[dict]] = {}
    for item in workout.get("sets", []):
        grouped.setdefault(item["exercise_name"], []).append(item)
    return [
        {
            "exercise_name": exercise,
            **double_progression_recommendation(sets),
        }
        for exercise, sets in grouped.items()
    ]


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "jarvis"})


async def miniapp_index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def dashboard(request: web.Request) -> web.Response:
    user = await _authorized_user(request)
    now = datetime.now(settings.timezone)
    workouts = await recent_workouts(user.id, limit=6)
    data = {
        "user": {
            "id": user.id,
            "first_name": user.first_name,
            "username": user.username,
        },
        "server_time": now.isoformat(),
        "schedule": today_schedule(now),
        "gtg": await gtg_stats(user.id, now),
        "workouts": workouts,
        "progression": _progression_for(workouts[0] if workouts else None),
        "reflection": await latest_reflection(user.id),
        "memory": await list_memory_facts(user.id, limit=12),
    }
    return web.json_response(data)


async def create_gtg(request: web.Request) -> web.Response:
    user = await _authorized_user(request)
    payload = await _json_body(request)
    try:
        reps = int(payload.get("reps"))
        await add_gtg_set(user.id, reps, source="miniapp")
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response({"ok": True, "gtg": await gtg_stats(user.id)})


async def create_workout(request: web.Request) -> web.Response:
    user = await _authorized_user(request)
    payload = await _json_body(request)
    title = str(payload.get("title") or "Workout")
    sets = payload.get("sets")
    if not isinstance(sets, list):
        raise web.HTTPBadRequest(text="sets must be an array")
    try:
        row = await log_workout(
            user.id,
            title,
            sets,
            notes=str(payload.get("notes") or "") or None,
            source="miniapp",
            started_at=datetime.now(settings.timezone).replace(tzinfo=None),
        )
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response({"ok": True, "workout_id": row.id})


async def save_reflection(request: web.Request) -> web.Response:
    user = await _authorized_user(request)
    payload = await _json_body(request)
    now = datetime.now(settings.timezone)
    try:
        row = await upsert_reflection(
            user.id,
            now.date(),
            deep_work_hours=float(payload["deep_work_hours"]) if payload.get("deep_work_hours") not in (None, "") else None,
            calories_hit=payload.get("calories_hit"),
            protein_hit=payload.get("protein_hit"),
            rir_respected=payload.get("rir_respected"),
            mood=int(payload["mood"]) if payload.get("mood") not in (None, "") else None,
            energy=int(payload["energy"]) if payload.get("energy") not in (None, "") else None,
            notes=str(payload.get("notes") or "") or None,
        )
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response({"ok": True, "date": row.reflection_date.isoformat()})


async def api_error_middleware_handler(request: web.Request, handler):
    try:
        return await handler(request)
    except TelegramAuthError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from exc


@web.middleware
async def api_error_middleware(request: web.Request, handler):
    try:
        return await api_error_middleware_handler(request, handler)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled Mini App API error")
        raise web.HTTPInternalServerError(text="Internal server error")


def create_web_app() -> web.Application:
    app = web.Application(middlewares=[api_error_middleware, security_headers])
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/app", miniapp_index)
    app.router.add_get("/api/dashboard", dashboard)
    app.router.add_post("/api/gtg", create_gtg)
    app.router.add_post("/api/workouts", create_workout)
    app.router.add_post("/api/reflection", save_reflection)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app
