"""Authenticated assistant actions. Models and telemetry cannot call these routes."""
from datetime import datetime

from aiohttp import web

from config import get_settings
from services.autonomy import assistant_snapshot, save_goal, check_in, start_focus, finish_focus
from services.knowledge import capture_note, list_notes, update_note
from services.rate_limit import allow


async def api(request):
    from webapp.server import _authorized_user, _json_body
    user = await _authorized_user(request)
    if not allow(user.id, "assistant_api", limit=60):
        raise web.HTTPTooManyRequests(text="Retry in one minute")
    resource = request.match_info["resource"]
    identity = request.match_info.get("identity")
    now = datetime.now(get_settings().timezone)
    try:
        if request.method == "GET":
            if resource == "assistant":
                return web.json_response(await assistant_snapshot(user.id, now))
            if resource == "notes":
                return web.json_response(await list_notes(user.id, request.query.get("q", ""), request.query.get("status")))
            if resource == "pc_status":
                from services.redis_backend import create_redis
                from services.pc_agent import pc_status
                redis = create_redis()
                try:
                    return web.json_response(await pc_status(redis, user.id, get_settings().pc_agent_secret))
                finally:
                    if redis is not None and hasattr(redis, "aclose"):
                        await redis.aclose()
        payload = await _json_body(request) if request.method in {"POST", "PATCH"} else {}
        if resource == "goals" and request.method in {"POST", "PATCH"}:
            return web.json_response(await save_goal(user.id, payload, int(identity) if identity else None))
        if resource == "checkins" and request.method == "POST":
            return web.json_response({"message": await check_in(user.id, int(payload["goal_id"]),
                payload["outcome"], payload.get("minutes", 0), payload.get("obstacle", ""), now)})
        if resource == "focus" and request.method == "POST":
            if payload.get("action") == "start":
                return web.json_response(await start_focus(user.id, payload.get("minutes", 25),
                    payload.get("title", "Один следующий шаг"), payload.get("goal_id"), payload.get("task_id"), now))
            if payload.get("action") in {"stop", "cancel"}:
                if payload["action"] == "stop" and "minutes" not in payload:
                    raise ValueError("Report actual minutes")
                return web.json_response({"message": await finish_focus(user.id,
                    payload["minutes"] if payload["action"] == "stop" else None, now)})
        if resource == "notes":
            if request.method == "POST":
                return web.json_response(await capture_note(user.id, payload["content"], payload.get("title"), payload.get("tags")))
            if identity and request.method in {"PATCH", "DELETE"}:
                await update_note(user.id, int(identity), payload.get("status"), request.method == "DELETE")
                return web.json_response({"ok": True})
    except (ValueError, KeyError, TypeError, AttributeError):
        raise web.HTTPBadRequest(text="Invalid fields, conflict, or record not found") from None
    raise web.HTTPNotFound()


def register(app):
    for resource, methods in {"assistant": ["GET"], "notes": ["GET", "POST"], "goals": ["POST"],
                             "focus": ["POST"], "checkins": ["POST"], "pc_status": ["GET"]}.items():
        for method in methods:
            app.router.add_route(method, "/api/v4/{resource:" + resource + "}", api)
    for resource, methods in {"goals": ["PATCH"], "notes": ["PATCH", "DELETE"]}.items():
        for method in methods:
            app.router.add_route(method, "/api/v4/{resource:" + resource + "}/{identity:\\d+}", api)
