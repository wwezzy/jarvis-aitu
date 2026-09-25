from datetime import date, datetime

from aiohttp import web

from config import get_settings
from services.diagnostics import diagnostics
from services.memory import correct_memory, delete_memory, list_memory_facts
from services.planner import make_plan
from services.preferences import get_preferences, save_preferences
from services.rate_limit import allow
from services.schedule import resolved_week, save_override
from services.tasks import list_tasks, save_task


async def api(request):
    from webapp.server import SCHEDULER_KEY, _authorized_user, _json_body, _resync_assignment_notifications, _resync_schedule_notifications
    user = await _authorized_user(request)
    resource = request.match_info['resource']
    identity = request.match_info.get('identity')
    now = datetime.now(get_settings().timezone)
    try:
        if request.method == 'GET':
            if resource == 'state':
                return web.json_response({'tasks': await list_tasks(user.id),
                    'plan': await make_plan(user.id, now), 'week': await resolved_week(user.id, now.date()),
                    'preferences': (await get_preferences(user.id)).model_dump(),
                    'memory': await list_memory_facts(user.id, limit=100)})
            if resource == 'tasks':
                return web.json_response(await list_tasks(user.id, status=request.query.get('status'),
                    source=request.query.get('source'), course=request.query.get('course')))
            if resource == 'calendar':
                from services.calendar import configured, list_events
                return web.json_response({'configured': configured(), 'events': await list_events(user.id)})
            if resource == 'diagnostics':
                if not allow(user.id, 'diagnostics', limit=10):
                    raise web.HTTPTooManyRequests(text='Retry in one minute')
                return web.json_response(await diagnostics(user.id, request.app.get(SCHEDULER_KEY)))
        payload = await _json_body(request) if request.method in {'POST', 'PATCH'} else {}
        if resource in {'calendar_sync', 'calendar_event'} and request.method == 'POST':
            from services.calendar import sync_calendar, save_event, CalendarUnavailable
            if not allow(user.id, resource, limit=4):
                raise web.HTTPTooManyRequests(text='Retry in one minute')
            try:
                result = await sync_calendar(user.id) if resource == 'calendar_sync' else await save_event(user.id, payload)
            except CalendarUnavailable as exc:
                raise web.HTTPServiceUnavailable(text=str(exc)) from None
            await _resync_schedule_notifications(request, user.id)
            return web.json_response(result)
        if resource == 'tasks' and request.method in {'POST', 'PATCH'}:
            row = await save_task(user.id, payload, int(identity) if identity else None)
            await _resync_assignment_notifications(request, user.id)
            return web.json_response(row)
        if resource == 'preferences' and request.method == 'POST':
            value = await save_preferences(user.id, payload)
            await _resync_schedule_notifications(request, user.id)
            return web.json_response(value.model_dump())
        if resource == 'overrides' and request.method == 'POST':
            await save_override(user.id, date.fromisoformat(payload['date']), payload.get('entry_id'),
                payload.get('changes') or {}, cancelled=bool(payload.get('cancelled', False)))
            await _resync_schedule_notifications(request, user.id)
            return web.json_response({'ok': True})
        if resource == 'memory' and identity:
            if request.method == 'PATCH':
                await correct_memory(user.id, int(identity), payload['value'])
            elif request.method == 'DELETE':
                await delete_memory(user.id, int(identity))
            else:
                raise web.HTTPMethodNotAllowed(request.method, ['PATCH', 'DELETE'])
            return web.json_response({'ok': True})
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text='Invalid fields or record not found') from None
    raise web.HTTPNotFound()


def register(app):
    for resource, methods in {'state': ['GET'], 'tasks': ['GET', 'POST'],
        'preferences': ['POST'], 'overrides': ['POST'], 'diagnostics': ['GET'],
        'calendar': ['GET'], 'calendar_sync': ['POST'], 'calendar_event': ['POST']}.items():
        for method in methods:
            app.router.add_route(method, '/api/v4/{resource:' + resource + '}', api)
    for resource, methods in {'tasks': ['PATCH'], 'memory': ['PATCH', 'DELETE']}.items():
        for method in methods:
            app.router.add_route(method, '/api/v4/{resource:' + resource + '}/{identity:\\d+}', api)
